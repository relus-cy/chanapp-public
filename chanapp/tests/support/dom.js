'use strict';
/* 测试共享 DOM/微任务最小桩（不是浏览器实现）：
   - mkEl：行为断言需要的可记录元素（classList 语义集、appendChild 维护 parentNode、
     class 选择器先深搜子树，未命中退回 _qs 懒建按钮桩——renderWatchlist 的 innerHTML 假节点
     靠懒建拿到 .star/.del，renderTags 靠深搜找到真实 .tag-input）。
   - runSlices：把 web/app.js IIFE 内的真实函数切片放进 node:vm 沙盒执行（只含声明/赋值，
     不产生网络或定时器副作用的切片才安全；timers 切片执行会注册 setInterval，先塞桩）。
   - srcOverride：供定点 mutation 探针传改写后的源码副本，生产文件保持只读。 */
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const appSource = fs.readFileSync(path.join(__dirname, '../../web/app.js'), 'utf8');

function mkEl(tag) {
  const el = {
    tagName: String(tag || 'div').toLowerCase(), children: [], style: {}, dataset: {},
    hidden: false, tabIndex: -1, value: '', textContent: '', parentNode: null,
    attrs: {}, options: [{}, {}], _qs: {}, _cls: new Set(), _html: '',
    classList: {
      add: c => el._cls.add(c), remove: c => el._cls.delete(c),
      toggle: (c, on) => { (on === undefined ? !el._cls.has(c) : on) ? el._cls.add(c) : el._cls.delete(c); },
      contains: c => el._cls.has(c),
    },
    appendChild(ch) { ch.parentNode = el; el.children.push(ch); return ch; },
    querySelector(sel) {
      if (el._qs[sel]) return el._qs[sel];
      if (sel.startsWith('.')) {
        for (const child of el.children) {
          if (child.classList && child.classList.contains(sel.slice(1))) return child;
          const nested = child.querySelector && child.querySelector(sel);
          if (nested) return nested;
        }
      }
      return sel.startsWith('.') ? (el._qs[sel] = mkEl('button')) : null;
    },
    querySelectorAll() { return el.children; },
    addEventListener() {},
    setAttribute(k, v) { el.attrs[k] = String(v); },
    getAttribute(k) { return el.attrs[k]; },
    removeAttribute(k) { delete el.attrs[k]; },
    focus() { el.focused = true; }, blur() { if (el.onblur) el.onblur(); },
    click() { el.onclick && el.onclick({ stopPropagation() {}, preventDefault() {} }); },
  };
  Object.defineProperty(el, 'className', {
    set(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get() { return [...el._cls].join(' '); },
  });
  Object.defineProperty(el, 'innerHTML', {
    set(v) { el._html = String(v); if (v === '') { el.children.forEach(c => { c.parentNode = null; }); el.children = []; } },
    get() { return el._html; },
  });
  Object.defineProperty(el, 'firstChild', { get() { return el.children[0] || null; } });
  return el;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

// name → [起始标记, 结束标记)，闭包内函数声明经 vm.runInContext 挂到 context 全局位
const slices = {
  stateInit:   ['  var state =', '  (function () {\n    var qs'],
  viewState:   ['  var viewState', '  var lastChartData = null;'],
  etag:        ['  var chartEtag = null;', '  // ---------- UI 骨架'],
  watchlistUi: ['  var SVG_STAR', '  // ---------- 搜索（'],
  search:      ['  var searchTimer', '  // 点搜索区外部收起下拉'],
  watchlistOps:['  var watchlistQueue', '  function renderTabs'],
  periods:     ['  // ---------- 展示周期偏好', '  function renderTabs'],
  tabs:        ['  function renderTabs', '  // ---------- header 状态'],
  status:      ['  // ---------- header 状态', '  // ---------- 中枢矩形'],
  markers:     ['  function signalLabel(', '  // ---------- 依据卡'],
  resonance:   ['  // ---------- 多级别共振角标', '  // ---------- 原生信号标注'],
  evidence:    ['  function renderEvidence(', '  // ---------- AI 完全分类面板'],
  esc:         ['  function esc(', '  function updateAiTitle('],
  analysisIdentity: ['  function currentAnalysisIdentity(', '  function updateAnalysisFreshness('],
  aiPanel:     ['  var pendingAnalysis', '  // ---------- 数据口径条'],
  metaBar:     ['  // ---------- 数据口径条', '  // ---------- F10'],
  tags:        ['  function renderTags', '  function renderFlow('],
  flow:        ['  function renderFlow(', '  // 与 /api/chart 并行调用'],
  hideF10:     ['  function hideF10Cards()', '  // 卡头：'],
  quoteView:   ['  // 行情行的展示口径', '  function renderWatchlist()'],
  f10Header:   ['  // 卡头：', '  function renderF10('],
  quotesF10:   ['  function displayDataNote(', '  function addWatch('],
  f10:         ['  function loadF10(', '  // ---------- 数据加载'],
  helpers:     ['  // ---------- 数据加载', '  // 图表渲染主路径'],
  renderChart: ['  function renderChart(', '  function load(options)'],
  load:        ['  function load(options)', '  // ---------- 交易时段自动刷新'],
  timers:      ['  // ---------- 交易时段自动刷新', '  // ---------- 侧边栏'],
  sidebar:     ['  // ---------- 侧边栏', '  // ---------- 初始化'],
  indicators:  ['  function computeMAs()', '  function wireMaSeg()'],
  subInd:      ['  function rsiArr(', '  function lastVal('],
  subChart:    ['  // ---------- 副图指标', '  // ---------- 多级别共振角标'],
  ruleControl: ['  function syncRuleControl()', '  // 依据卡浮层左右切换'],
  aiRefresh:   ["  el('aiRefresh').onclick", '  // 依据卡浮层左右切换'],
  formingLines:['    c.biSeries.setData(', '    c.zsOverlay.setBoxes('],
  fmtVol:      ['  // 成交量单位是股', '  // 图例/依据卡/AI 缓存行日期口径'],
};

function sliceSource(name, srcOverride) {
  const src = srcOverride || appSource;
  const marks = slices[name];
  const start = src.indexOf(marks[0]);
  const end = src.indexOf(marks[1], start + marks[0].length);
  if (start < 0 || end <= start) throw new Error('slice ' + name + ' 标记未命中：' + marks.join(' → '));
  return src.slice(start, end);
}

// context 先 vm.createContext（重复调用幂等），再依次执行具名切片；返回同一 context。
function runSlices(context, names, srcOverride) {
  vm.createContext(context);
  for (const name of names) vm.runInContext(sliceSource(name, srcOverride), context);
  return context;
}

module.exports = { appSource, mkEl, deferred, tick, slices, sliceSource, runSlices };
