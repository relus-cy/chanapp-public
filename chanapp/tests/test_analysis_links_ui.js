'use strict';
/* 批3 分析效率：依据卡→图表定位、摘要 chip→切周期、AI 证据中性色、静态样例身份 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');

function mkEl(tag) {
  const el = {
    tagName: tag || 'div', children: [], style: {}, dataset: {}, hidden: false,
    attrs: {}, tabIndex: -1, _cls: new Set(), _html: '',
    classList: {
      add: c => el._cls.add(c), remove: c => el._cls.delete(c),
      toggle: (c, on) => { (on === undefined ? !el._cls.has(c) : on) ? el._cls.add(c) : el._cls.delete(c); },
      contains: c => el._cls.has(c),
    },
    appendChild(ch) { el.children.push(ch); return ch; },
    contains(target) { return el.children.includes(target); },
    focus() {},
    addEventListener() {}, setAttribute(k, v) { el.attrs[k] = String(v); }, removeAttribute(k) { delete el.attrs[k]; },
  };
  Object.defineProperty(el, 'className', {
    set(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get() { return [...el._cls].join(' '); },
  });
  Object.defineProperty(el, 'innerHTML', {
    set(v) { el._html = String(v); if (v === '') el.children = []; },
    get() { return el._html; },
  });
  return el;
}

// ---------- A. locateEvidenceRange：bar 居中定位且边界收敛 ----------
{
  const context = {};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function locateEvidenceRange('), source.indexOf('  function locateBar(')), context);
  assert.equal(typeof context.locateEvidenceRange, 'function', 'locateEvidenceRange should exist');
  const f = context.locateEvidenceRange, j = JSON.stringify;
  assert.equal(j(f(100, 500, 60)), j({ from: 70, to: 130 }), 'centers the bar');
  assert.equal(j(f(5, 500, 60)), j({ from: 0, to: 60 }), 'clamps at the left edge');
  assert.equal(j(f(498, 500, 60)), j({ from: 442, to: 502 }), 'clamps at the right edge incl whitespace');
  console.log('A. locateEvidenceRange checks passed');
}

// ---------- B. 依据卡点击定位图表 + 浮层左右切换 ----------
{
  const cards = mkEl('div'), dock = mkEl('div'), count = mkEl('span'), prev = mkEl('button'), next = mkEl('button');
  dock.hidden = true;
  const nodes = { cards, cardsDock: dock, cardsCount: count, cardsPrev: prev, cardsNext: next };
  const located = [];
  const context = {
    el: id => nodes[id], document: { createElement: () => mkEl('div') },
    esc: x => String(x), fmtBarTime: x => x, signalLabel: () => '笔 B2',
    locateBar: dt => located.push(dt),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function renderEvidence('), source.indexOf('  // ---------- AI 完全分类面板')), context);
  context.renderEvidence([{ side: 'buy', level: 'bi', types: ['2'], status: 'confirmed', dt: '2026-06-23', price: 3850.86, text: 'x' }]);
  const card = cards.children[0];
  assert.ok(card, 'card rendered');
  assert.equal(card.dataset.dt, '2026-06-23', 'card carries raw signal dt');
  assert.equal(card.tabIndex, 0, 'card is focusable');
  card.onclick();
  assert.deepEqual(located, ['2026-06-23'], 'click locates the signal bar on chart');
  card.onkeydown({ key: 'Enter', preventDefault() {} });
  assert.deepEqual(located, ['2026-06-23', '2026-06-23'], 'enter locates too');
  assert.equal(dock.hidden, false, 'dock unhidden when evidence exists');
  assert.equal(count.textContent, '1/1', 'counter shows position');
  assert.equal(prev.disabled, true, 'prev disabled on the newest card');
  assert.equal(next.disabled, true, 'next disabled on the only card');

  // dt 降序排列，左右切换逐条翻看且越界忽略
  context.renderEvidence([
    { side: 'sell', level: 'bi', types: ['1'], status: 'confirmed', dt: '2026-06-20', price: 10, text: 'older' },
    { side: 'buy', level: 'bi', types: ['2'], status: 'confirmed', dt: '2026-06-23', price: 11, text: 'newer' },
  ]);
  assert.equal(cards.children.length, 1, 'dock renders exactly the current card');
  assert.equal(cards.children[0].dataset.dt, '2026-06-23', 'newest signal first (dt desc)');
  assert.equal(count.textContent, '1/2', 'counter resets to first of two');
  assert.equal(prev.disabled, true, 'prev disabled at index 0');
  assert.equal(next.disabled, false, 'next enabled when older cards remain');
  context.showEvidence(1);
  assert.equal(cards.children[0].dataset.dt, '2026-06-20', 'next shows the older card');
  assert.equal(count.textContent, '2/2', 'counter follows the current index');
  assert.equal(prev.disabled, false, 'prev enabled away from the head');
  assert.equal(next.disabled, true, 'next disabled at the tail');
  context.showEvidence(2);
  assert.equal(cards.children[0].dataset.dt, '2026-06-20', 'out-of-range index is ignored (high)');
  context.showEvidence(-1);
  assert.equal(cards.children[0].dataset.dt, '2026-06-20', 'out-of-range index is ignored (low)');

  // 空列表隐藏浮层并清空卡片
  context.renderEvidence([]);
  assert.equal(dock.hidden, true, 'empty evidence hides the dock');
  assert.equal(cards.children.length, 0, 'empty evidence clears #cards');
  console.log('B. evidence card chart-link and dock-nav checks passed');
}

// ---------- C. 摘要 chip 点击切周期 ----------
{
  const box = mkEl('div');
  const calls = [];
  const context = {
    el: () => box, document: { createElement: () => mkEl('span') },
    esc: x => String(x), fmtBarTime: x => x, signalLabel: s => 'S2', FREQ_NAME: { day: '日线', m60: '60分', m30: '30分' },
    state: { freq: 'day' }, renderTabs() {}, load() { calls.push('load'); },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function renderResonance('), source.indexOf('  // ---------- 原生信号标注')), context);
  context.renderResonance([{ freq: 'm60', signals: [{ side: 'sell', level: 'bi', types: ['2'], status: 'confirmed', dt: '2026-09-01 14:00' }] }]);
  assert.equal(box.children.length, 3, 'three freq slots');
  const m60 = box.children[1];
  assert.equal(m60.dataset.freq, 'm60', 'chip carries freq');
  m60.onclick();
  assert.equal(context.state.freq, 'm60', 'chip click switches freq');
  assert.deepEqual(calls, ['load'], 'chip click reloads');
  box.children[2].onclick();
  assert.equal(context.state.freq, 'm30');
  box.children[0].onclick();
  assert.equal(calls.length, 3, 'different freq reloads each time');
  // 当前周期 chip 不重复加载
  const before = calls.length;
  box.children[0].onclick();
  assert.equal(calls.length, before, 'current freq chip does not reload');
  console.log('C. resonance chip freq-switch checks passed');
}

// ---------- D. 静态样例身份常驻徽章 ----------
{
  const panel = mkEl('div'), dataStatus = mkEl('div'), badge = mkEl('span');
  const nodes = { aiPanel: panel, aiDataStatus: dataStatus, aiSampleBadge: badge };
  const context = {
    el: id => nodes[id], esc: x => String(x), fmtRefDt: x => x,
    pendingAnalysis: { body: {} },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function renderAnalysis('), source.indexOf('  // 后验置信徽章')), context);
  context.renderAnalysis({ current_state: 'x', scenarios: [] }, 'unconfigured');
  assert.equal(badge.hidden, false, 'sample badge shows for unconfigured fallback');
  context.renderAnalysis({ current_state: 'x', scenarios: [] }, 'fallback');
  assert.equal(badge.hidden, false, 'sample badge shows for error fallback');
  context.renderAnalysis({ current_state: 'x', scenarios: [] }, null);
  assert.equal(badge.hidden, true, 'badge hidden for real analysis');
  assert.match(html, /id="aiSampleBadge"/, 'badge lives in the AI panel header');
  // 情景折叠为 .ai-row 标题行（徽章沿用 posteriorBadge，点击弹层在初始化区委托）
  context.posteriorBadge = p => '<span class="badge hi">后验·高</span>';
  context.renderAnalysis({ scenarios: [{ name: '强势延续', posterior: '高 0.72' }] }, null);
  assert.match(panel.innerHTML, /<div class="ai-row" data-i="0" role="button" tabindex="0"><span>强势延续<\/span><span class="badge hi">后验·高<\/span><\/div>/, 'scenarios render as .ai-row title rows');
  console.log('D. sample identity badge checks passed');
}

// ---------- E. AI 证据中性色：红青只表达价格方向 ----------
{
  assert.doesNotMatch(html, /\.kv\.ev-for \{ color: var\(--up\)/, 'evidence-for no longer red');
  assert.doesNotMatch(html, /\.kv\.ev-against \{ color: var\(--down\)/, 'evidence-against no longer teal');
  assert.match(html, /\.kv\.ev-for b::before/, 'evidence-for carries a neutral plus mark');
  assert.match(html, /\.kv\.ev-against b::before/, 'evidence-against carries a neutral minus mark');
  console.log('E. evidence neutral-color checks passed');
}
