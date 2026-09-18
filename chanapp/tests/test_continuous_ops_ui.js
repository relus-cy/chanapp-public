'use strict';
/* 批1 连续操作：刷新区间保持 / 搜索 combobox 键盘路径 / 搜索表单焦点保持 / 旧数据归属标记与重试 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');

function mkEl(tag) {
  const el = {
    tagName: tag || 'div', children: [], style: {}, dataset: {}, hidden: false,
    attrs: {}, tabIndex: -1, value: '', textContent: '',
    _cls: new Set(), _html: '',
    classList: {
      add: c => el._cls.add(c), remove: c => el._cls.delete(c),
      toggle: (c, on) => { (on === undefined ? !el._cls.has(c) : on) ? el._cls.add(c) : el._cls.delete(c); },
      contains: c => el._cls.has(c),
    },
    appendChild(ch) { el.children.push(ch); return ch; },
    _qs: {},
    querySelector(sel) { return el._qs[sel] || (el._qs[sel] = mkEl('button')); },
    querySelectorAll() { return []; },
    addEventListener() {}, setAttribute(k, v) { el.attrs[k] = String(v); },
    getAttribute(k) { return el.attrs[k]; }, removeAttribute(k) { delete el.attrs[k]; },
    blur() {}, focus() { el.focused = true; }, click() { el.onclick && el.onclick({ stopPropagation() {}, preventDefault() {} }); },
  };
  Object.defineProperty(el, 'className', {
    set(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get() { return [...el._cls].join(' '); },
  });
  Object.defineProperty(el, 'innerHTML', {
    set(v) { el._html = String(v); if (v === '') el.children = []; },
    get() { return el._html; },
  });
  Object.defineProperty(el, 'firstChild', { get() { return el.children[0] || null; } });
  return el;
}

// ---------- A. refreshRangePlan：同标的刷新保留可视区间 ----------
{
  const context = { toTime: t => t };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function refreshRangePlan('), source.indexOf('  // 图表渲染主路径')), context);
  assert.equal(typeof context.refreshRangePlan, 'function', 'refreshRangePlan should exist');

  const capture = context.captureRefreshRange;
  assert.equal(typeof capture, 'function', 'refresh range capture should exist');
  const plan = context.refreshRangePlan;
  const j = v => JSON.stringify(v);
  const oldBars = Array.from({ length: 520 }, (_, i) => ({ time: 't' + i }));
  const slidBars = oldBars.slice(1).concat({ time: 't520' });
  assert.equal(plan(null, oldBars), null, 'switch/first load resets');

  // 固定 520 根滑窗移除首根后，历史视口按日期锚点左移一格，并保留跨度与小数偏移
  const history = capture({ from: 140.25, to: 190.75 }, oldBars);
  assert.equal(j(plan(history, slidBars)), j({ from: 139.25, to: 189.75 }), 'history range follows its time anchor');

  // 停在最新端时仅在末根真的变化后跟随；同批数据的轻微滚动不得反复拉回 +3
  const latest = capture({ from: 400.2, to: 522 }, oldBars);
  assert.equal(j(plan(latest, slidBars)), j({ from: 400.2, to: 522 }), 'latest edge follows a new bar in fixed window');
  const nearLatest = capture({ from: 400.2, to: 518.8 }, oldBars);
  assert.equal(j(plan(nearLatest, oldBars)), j({ from: 400.2, to: 518.8 }), 'unchanged data preserves user scroll exactly');
  const appendedBars = oldBars.concat({ time: 't520' });
  assert.equal(j(plan(latest, appendedBars)), j({ from: 401.2, to: 523 }), 'latest edge follows appended bar and preserves span');

  // 锚点已从新窗口淘汰时，明确夹到左边界并保留跨度
  const missing = capture({ from: 0.25, to: 50.75 }, oldBars);
  assert.equal(j(plan(missing, oldBars.slice(100))), j({ from: 0, to: 50.5 }), 'missing anchor clamps to available data');
  console.log('A. refreshRangePlan checks passed');
}

// ---------- B. wireSearch：combobox 键盘路径闭合 ----------
{
  const input = mkEl('input'), drop = mkEl('div'), form = mkEl('form');
  form.q = input; form.querySelector = () => drop;
  const added = [], timers = [];
  const cands = [{ code: 'sh600519', name: '贵州茅台' }, { code: 'sz000858', name: '五粮液' }];
  const context = {
    esc: x => String(x), addWatch: (code, name) => added.push([code, name]),
    setStatus() {}, fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(cands) }),
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    document: { createElement: () => mkEl('div') },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var searchTimer'), source.indexOf('  // 点搜索区外部收起下拉')), context);
  context.wireSearch(form);

  assert.equal(input.attrs.role, 'combobox', 'input exposes combobox role');
  assert.equal(input.attrs['aria-expanded'], 'false', 'dropdown starts collapsed');

  input.value = '茅台';
  input.oninput();
  assert.equal(timers.length, 1, 'debounce scheduled');
  timers[0]();
  const key = k => input.onkeydown({ key: k, preventDefault() {} });
  Promise.resolve().then(() => Promise.resolve()).then(() => {
    assert.equal(drop.hidden, false, 'candidates shown');
    assert.equal(drop.children.length, 2);
    assert.equal(input.attrs['aria-expanded'], 'true');

    key('ArrowDown');
    assert.equal(drop.children[0].classList.contains('active'), true, 'first candidate active');
    key('ArrowDown');
    assert.equal(drop.children[1].classList.contains('active'), true, 'second candidate active');
    key('ArrowDown');
    assert.equal(drop.children[1].classList.contains('active'), true, 'clamps at last candidate');
    key('ArrowUp');
    assert.equal(drop.children[0].classList.contains('active'), true, 'arrow up moves back');

    key('Enter');
    assert.deepEqual(added, [['sh600519', '贵州茅台']], 'enter adds active candidate');
    assert.equal(drop.hidden, true, 'dropdown closes after add');

    // 无候选激活时 Enter 取首个候选
    input.value = '茅台'; input.oninput(); timers[1]();
    return Promise.resolve().then(() => Promise.resolve()).then(() => {
      key('Enter');
      assert.deepEqual(added[1], ['sh600519', '贵州茅台'], 'enter without active picks first candidate');
      console.log('B. search combobox keyboard checks passed');
    });
  }).catch(e => { console.error(e); process.exit(1); });
}

// ---------- C. renderWatchlist：行情刷新不重建搜索表单 ----------
{
  const wlItems = mkEl('div'), wlForm = mkEl('div');
  const nodes = { wlItems, wlForm };
  let wires = 0;
  const context = {
    state: { watchlist: [], quotes: {}, code: null },
    document: { createElement: t => mkEl(t), getElementById: id => nodes[id] },
    esc: x => String(x), wireSearch: () => { wires++; },
    updateAiTitle() {}, renderF10Header() {}, lastF10: null,
    toggleStar() {}, removeWatch() {}, load() {},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var SVG_STAR'), source.indexOf('  // ---------- 搜索式添加')), context);
  context.renderWatchlist();
  assert.equal(wires, 1, 'first render builds the form once');
  const form = wlForm.firstChild;
  context.renderWatchlist();
  context.renderWatchlist();
  assert.equal(wires, 1, 'quote refresh must not rebuild the search form');
  assert.equal(wlForm.firstChild, form, 'form instance (input value and focus) survives refresh');
  console.log('C. search form preservation checks passed');
}

// ---------- D. 旧数据归属标记与就地重试 ----------
{
  const chartError = mkEl('div');
  let loads = 0;
  const context = { el: () => chartError, load: () => { loads++; },
    document: { createElement: t => mkEl(t), createTextNode: t => ({ tagName: '#text', textContent: t }) } };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function showChartError('), source.indexOf('  function renderStatus(')), context);
  context.showChartError('HTTP 502');
  assert.equal(chartError.hidden, false);
  const retry = chartError.children.find(ch => ch.tagName === 'button');
  assert.ok(retry, 'chart error offers an in-place retry button');
  retry.click();
  assert.equal(loads, 1, 'retry button triggers load');
  console.log('D. chart error retry checks passed');
}
{
  // 切换（非 refresh）标记旧内容，成功后清除；后台 refresh 不标记
  assert.match(source, /classList\.add\('ctx-old'\)/, 'load marks stale context on switch');
  assert.match(source, /classList\.remove\('ctx-old'\)/, 'successful load clears stale mark');
  assert.match(source, /if \(!options \|\| !options\.refresh\)/, 'background refresh does not mark displayed data old');
  console.log('D. stale context wiring checks passed');
}

// ---------- E. 自选股行键盘可达：可聚焦、Enter/空格激活 ----------
{
  const wlItems = mkEl('div'), wlForm = mkEl('div');
  const nodes = { wlItems, wlForm };
  const calls = [];
  const context = {
    state: { watchlist: [{ code: 'sz001309', name: '德明利', starred: false }], quotes: {}, code: 'sh000001' },
    document: { createElement: t => mkEl(t), getElementById: id => nodes[id] },
    esc: x => String(x), wireSearch() {}, updateAiTitle() {}, renderF10Header() {}, lastF10: null,
    toggleStar() {}, removeWatch() {}, load() { calls.push('load'); },
    sbMode: () => 'pinned', setSidebar() {},  // 行点击会查侧栏模式：固定展开时不收回
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var SVG_STAR'), source.indexOf('  // ---------- 搜索式添加')), context);
  context.renderWatchlist();
  const item = wlItems.children[0];
  assert.ok(item, 'watchlist row rendered');
  assert.equal(item.tabIndex, 0, 'watchlist row is focusable');
  assert.equal(typeof item.onkeydown, 'function', 'row exposes keyboard activation');
  item.onkeydown({ target: item, key: 'Enter', preventDefault() {} });
  assert.equal(context.state.code, 'sz001309', 'enter selects the row');
  assert.deepEqual(calls, ['load'], 'enter triggers load');
  item.onkeydown({ target: item, key: ' ', preventDefault() {} });
  assert.deepEqual(calls, ['load', 'load'], 'space selects the row');
  console.log('E. watchlist row keyboard checks passed');
}

// ---------- F. load() 接线：同标的后台刷新保留区间、切换不保留 ----------
{
  function loadHarness(loadedTarget) {
    const nodes = {}, pending = [];
    const calls = { ranges: [], renderOpts: [] };
    let lr = { from: 40, to: 90 };
    const bars = Array.from({ length: 220 }, (_, i) => ({ time: 't' + i }));
    const context = {
      state: { code: 'a', freq: 'day', ruleProfile: 'strict', signalScope: 'expanded' },
      supplyState: { generation: 1, epoch: 'e' }, supplyBusy: false, supplyRange: null,
      chartAbort: null, AbortController, activeChartVersion: null, detailKind: null,
      CHART_TIMEOUT_MS: 25000, setTimeout: () => 1, clearTimeout() {},
      fetch: url => new Promise(resolve => pending.push({ url, resolve: body => resolve({ headers: { get: () => null }, ...body }) })),
      el: id => nodes[id] || (nodes[id] = { classList: { add() {}, remove() {} } }),
      charts: {
        main: { timeScale: () => ({ getVisibleLogicalRange: () => lr, setVisibleLogicalRange: r => calls.ranges.push(r) }) },
        macdChart: { timeScale: () => ({ setVisibleLogicalRange: () => {} }) },
      },
      loadedTarget, klineData: bars, toTime: t => t,
      ensureCharts() {}, updateAiTitle() {}, setStatus() {}, hideChartError() {}, loadF10() {}, syncManualAnalysis() {},
      renderChart: (data, opts) => calls.renderOpts.push(opts),
      renderEvidence() {}, renderMeta() {}, updateAnalysisFreshness() {},
      eligible: () => true, acceptsRule: () => true,
    };
    vm.createContext(context);
    vm.runInContext(source.slice(source.indexOf('  function refreshRangePlan('), source.indexOf('  // 图表渲染主路径')), context);
    vm.runInContext(source.slice(source.indexOf('  var chartEtag = null;'), source.indexOf('  // ---------- UI 骨架 ----------')), context);
    vm.runInContext(source.slice(source.indexOf('  function load('), source.indexOf('  // ---------- 交易时段自动刷新')), context);
    return { context, pending, calls, nodes, setRange(range) { lr = range; }, bars };
  }
  const chartBody = { kline: Array.from({ length: 221 }, (_, i) => ({ time: 't' + i })), meta: {} };

  // 同标的 refresh：停在历史 → 精确恢复，renderChart 不重置
  {
    const h = loadHarness({ code: 'a', freq: 'day' });
    h.context.load({ refresh: true });
    h.setRange({ from: 140, to: 190 }); // fetch 期间用户继续平移
    h.pending[0].resolve({ ok: true, json: async () => chartBody });
    const wait = () => new Promise(r => setImmediate(r));
    wait().then(() => wait()).then(() => {
      assert.equal(h.calls.renderOpts[0].resetRange, false, 'refresh keeps chart range');
      assert.equal(JSON.stringify(h.calls.ranges[0]), JSON.stringify({ from: 140, to: 190 }), 'response preserves the latest user range');
      assert.equal(JSON.stringify(h.context.loadedTarget), JSON.stringify({ code: 'a', freq: 'day' }));

      // 响应提交时已不再显示请求对应的数据身份，不能套用旧图的区间
      const hStale = loadHarness({ code: 'a', freq: 'day' });
      hStale.context.load({ refresh: true });
      hStale.context.loadedTarget = { code: 'b', freq: 'day' };
      hStale.pending[0].resolve({ ok: true, json: async () => chartBody });
      return wait().then(() => wait()).then(() => {
        assert.equal(hStale.calls.renderOpts[0].resetRange, true, 'changed loaded target resets instead of restoring stale range');
        assert.equal(hStale.calls.ranges.length, 0, 'changed loaded target applies no stale logical range');
      // 切周期（无 refresh）：重置到最新，不恢复旧区间
      const h2 = loadHarness({ code: 'a', freq: 'day' });
      h2.context.load();
      h2.pending[0].resolve({ ok: true, json: async () => chartBody });
      return wait().then(() => wait()).then(() => {
        assert.equal(h2.calls.renderOpts[0].resetRange, true, 'switch resets range');
        assert.equal(h2.calls.ranges.length, 0, 'switch does not restore old range');
        console.log('F. load refresh/switch range wiring checks passed');
      });
      });
    }).catch(e => { console.error(e); process.exit(1); });
  }
}
