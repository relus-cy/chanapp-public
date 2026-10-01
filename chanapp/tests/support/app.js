'use strict';
/* web/app.js 闭包的共享测试环境：只集中各用例重复搭建的最小桩
   （state/视图身份/延迟 fetch/定时器收集/元素注册表），业务函数一律来自切片实跑。
   工厂返回 {context, nodes, pending, timers, intervals, statuses}，桩行为均留记录可断言。 */
const { mkEl, deferred, runSlices } = require('./dom.js');

// 基础上下文：DOM 元素注册表 + 挂起 fetch 队列 + 不点火的 setTimeout/setInterval。
// extras 覆盖默认桩（默认桩只含变量槽位与空操作，不含业务逻辑）。
function appContext(extras) {
  const nodes = {}, pending = [], timers = [], intervals = [], intervalMs = [], statuses = [], renders = [];
  const context = {
    sessionDemo: false,
    state: { code: 'a', freq: 'day', ruleProfile: 'strict', signalScope: 'expanded', watchlist: [], quotes: {} },
    periodLoad: null, periodSaving: false,
    periodPrefs: { catalog: [{freq: 'day', label: '日线'}, {freq: 'week', label: '周线'}, {freq: 'm60', label: '60分'}, {freq: 'm30', label: '30分'}], selected: ['day', 'week', 'm60', 'm30'], notice: null, markets: {cn: {available: ['day', 'week', 'm60', 'm30']}, hk: {available: ['day', 'week', 'm60', 'm30']}} },
    viewState: { adjust: 'qfq', token: null, analysisTokens: null },
    restoreRange: null,
    chartAbort: null, charts: null, loadedTarget: null, detailKind: null, activeChartVersion: null,
    analysisSlots: {}, analysisIdentity: null, pendingAnalysis: null, ruleSwitchNote: null,
    f10Last: { code: null, ts: 0 }, lastF10: null, lastChartData: null, lastMeta: null,
    klineData: [], klineByTime: {}, macdRowsRaw: [],
    historyState: { loading: false, hasMore: false, reqId: 0 },
    CHART_TIMEOUT_MS: 25000, REFETCH_TIMEOUT_MS: 120000, ANALYSIS_TIMEOUT_MS: 150000,
    AbortController,
    setTimeout: fn => { timers.push(fn); return timers.length; },
    clearTimeout() {}, setInterval: (fn, ms) => { intervals.push(fn); intervalMs.push(ms); return intervals.length; },
    fetch: (url, options) => new Promise((resolve, reject) => pending.push({
      url, options,
      resolve: body => resolve(Object.assign({ headers: { get: () => null } }, body)),
      reject,
    })),
    document: {
      hidden: false,
      createElement: tag => mkEl(tag),
      createTextNode: text => ({ tagName: '#text', textContent: text }),
      getElementById: id => nodes[id] || (nodes[id] = mkEl()),
      querySelectorAll: () => [],
      addEventListener() {},
    },
    el(id) { return nodes[id] || (nodes[id] = mkEl()); },
    localStorage: { _s: {}, getItem(k) { return k in this._s ? this._s[k] : null; }, setItem(k, v) { this._s[k] = String(v); } },
    setStatus(msg) { context.statusMsg = msg || null; statuses.push(msg); },
    renderStatus() { renders.push('status'); }, ensureCharts() { return context.charts; }, updateAiTitle() {},
    hideChartError() {}, showChartError(m) { throw new Error(m); },
    loadF10() {}, loadViewQuote() {}, renderF10Header() {}, isWatched: () => false,
    syncManualAnalysis() {}, updateAnalysisFreshness() {},
    renderEvidence() {}, renderMeta() {},
    renderChart(data, opts) { renders.push('chart'); (context._harness.chartCalls || (context._harness.chartCalls = [])).push(opts); },
    renderAnalysis(body, note) { renders.push('analysis'); (context._harness.analysisCalls || (context._harness.analysisCalls = [])).push({ body, note }); },
    toggleStar() {}, removeWatch() {},
    renderWatchlist() { renders.push('watchlist'); }, renderF10() {}, renderFlow() {},
    hideF10Cards() {}, closeAiPopup() {}, setSidebar() {}, sbMode: () => 'pinned',
    esc: s => String(s), hhmmss: x => x, toTime: t => t,
    isSessionOpen: () => false, marketOf: () => 'cn',
    acceptsRule: () => true,
    load() { renders.push('load'); }, loadQuotes() { renders.push('quotes'); },
    renderTabs() {},
  };
  runSlices(context, ['periods', 'analysisIdentity']);
  Object.assign(context, extras || {});
  context._harness = { nodes, pending, timers, intervals, intervalMs, statuses, renders };
  return context;
}

// 真实 fetch 响应便捷构造（loadEnv.pending[i].resolve 的参数）
const okJson = (body, etag) => ({
  ok: true, status: 200,
  headers: { get: k => (etag != null && k === 'ETag' ? etag : null) },
  json: async () => body,
});
const errJson = (status, body) => ({ ok: false, status, headers: { get: () => null }, json: async () => body || {} });

// load() 主路径环境：etag + status + helpers（mergeWithHistory 等）+ load 切片实跑
// （renderChart 保持记录桩；需真身时后续运行 'renderChart' 切片覆盖）。
// 陷阱：extras 在切片之前挂到 context，与切片内函数声明同名的 extra（如 showChartError）
// 会被真身静默覆盖——要替换切片定义的函数，必须在 runSlices/loadEnv 返回之后再赋值。
function loadEnv(extras) {
  const context = appContext(extras);
  runSlices(context, ['etag', 'status', 'helpers', 'load']);
  return context;
}

// 60s 图表/报价定时回调环境：timers 切片注册真实回调到 intervals[]，document.hidden 可切换。
function timerEnv(extras) {
  const context = appContext(extras);
  runSlices(context, ['timers']);
  return context;
}

// wireSearch 环境：form/input/drop 均为 mkEl，防抖计时器挂起待手动点火；
// 选中（查看）与显式加入自选分别记入 opened / added。
function searchEnv(extras) {
  const context = appContext(extras);
  const added = context._harness.added = [], opened = context._harness.opened = [];
  context.addWatch = (code, name) => added.push([code, name]);
  context.openCode = (code, name) => opened.push([code, name]);
  const input = mkEl('input'), drop = mkEl('div'), form = mkEl('form');
  form.q = input;
  form.querySelector = () => drop;
  runSlices(context, ['search']);
  context.wireSearch(form);
  context._harness.search = { input, drop, form };
  return context;
}

// renderChart/prependHistory 用的最小图表桩：所有 series/timeScale/markers 操作
// 按发生顺序记入 events（'candle.setData'/'main.range'/'markers'…），供有序断言；
// 带参数的操作（range/setData 行数）同时记 values 供取值断言。
// range 可为对象或 () => 对象（load() 提交前实时取可视区间的场景用函数）。
function chartStub(range) {
  const stub = { events: [], values: [] };
  const get = typeof range === 'function' ? range : () => range || null;
  const series = name => ({ setData(rows) { stub.events.push(name); stub.values.push({ op: name, rows }); } });
  const scale = name => () => ({
    getVisibleLogicalRange: get,
    getVisibleRange: get,
    setVisibleLogicalRange(r) { stub.events.push(name + '.range'); stub.values.push({ op: name + '.range', r }); },
    setVisibleRange(r) { stub.events.push(name + '.rangeAbs'); stub.values.push({ op: name + '.rangeAbs', r }); },
  });
  Object.assign(stub, {
    main: {
      timeScale: scale('main'),
      addSeries() { return series('addSeries'); },
      removeSeries() { stub.events.push('removeSeries'); },
    },
    // 副图 series 的 setData 记为 'sub.setData'（showInd 重建时每条 series 一次）
    macdChart: {
      timeScale: scale('sub'),
      addSeries() { return series('sub.setData'); },
      removeSeries() { stub.events.push('sub.removeSeries'); },
    },
    candleSeries: series('candle.setData'),
    volumeSeries: series('volume.setData'),
    biSeries: series('bi.setData'),
    xdSeries: series('xd.setData'),
    zsOverlay: { setBoxes(b) { stub.events.push('zs'); stub.values.push({ op: 'zs', boxes: b }); } },
    markers: { setMarkers(rows) { stub.events.push('markers'); stub.values.push({ op: 'markers', rows }); } },
    channelSeries: [],
    formingBiSegs: [], formingXdSegs: [],
  });
  return stub;
}

module.exports = { appContext, loadEnv, timerEnv, searchEnv, chartStub, okJson, errJson };
