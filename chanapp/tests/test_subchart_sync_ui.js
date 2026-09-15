'use strict';
/* 副图指标切换不得变动主图时间轴：showInd 重建副图 series 时，真实 lightweight-charts
   会在清空后触发 时间轴 null→自动适配 瞬变（axis_probe 实测事件序列），双向同步必须有
   subRebuild 抑制，且重建后恢复原可视区间。假 chart 按实测行为建模这两个瞬变。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');

function mkEl() {
  return {
    children: [], style: {}, textContent: '', clientWidth: 800, clientHeight: 400,
    appendChild(ch) { this.children.push(ch); return ch; },
  };
}

// 实测瞬变建模：最后一个 series 被移除 → 时间轴 fire null；此后首个 setData → fire 自动适配区间
function fakeChart() {
  const subs = [];
  const calls = { setRange: [] };
  const chart = {
    _range: null, _series: 0, _justCleared: false,
    addSeries() {
      chart._series++;
      return {
        setData() {
          if (chart._justCleared) { chart._justCleared = false; chart._fire({ from: 398.81, to: 522 }); }
        },
        applyOptions() {},
      };
    },
    removeSeries() {
      if (--chart._series <= 0) { chart._series = 0; chart._justCleared = true; chart._fire(null); }
    },
    priceScale: () => ({ applyOptions() {} }),
    subscribeCrosshairMove() {},
    applyOptions() {},
    timeScale() {
      return {
        subscribeVisibleLogicalRangeChange(fn) { subs.push(fn); },
        getVisibleLogicalRange: () => chart._range,
        setVisibleLogicalRange(r) { calls.setRange.push(r); chart._fire(r); },
        getVisibleRange: () => null,
      };
    },
    _fire(r) { chart._range = r; subs.slice().forEach(fn => fn(r)); },
    _calls: calls,
  };
  return chart;
}

const chartEl = mkEl(), subEl = mkEl(), ohlcEl = mkEl(), subNoteEl = mkEl();
const made = [];
const LW = {
  CandlestickSeries: 'c', HistogramSeries: 'h', LineSeries: 'l', LineStyle: { Dashed: 1 },
  createChart(el) { const c = fakeChart(); c._el = el; made.push(c); return c; },
  createSeriesMarkers: () => ({ setMarkers() {} }),
};
const segBtns = ['macd', 'kdj', 'rsi', 'boll'].map(ind => ({
  dataset: { ind }, classList: { toggle() {} },
}));
const context = {
  LightweightCharts: LW,
  charts: null,
  el: id => ({ chart: chartEl, sub: subEl, ohlc: ohlcEl, subNote: subNoteEl })[id],
  document: { querySelectorAll: sel => (sel === '#subSeg [data-ind]' ? segBtns : []) },
  makeZsOverlay: () => ({ setBoxes() {}, redraw() {} }),
  ResizeObserver: function () { this.observe = () => {}; },
  requestAnimationFrame: fn => fn(),
  P: new Proxy({}, { get: () => '#000' }),
  axisTick: () => '', axisLabel: () => '', updateAxisAnchor() {},
  klineData: [
    { time: '2026-09-01', open: 1, high: 1, low: 1, close: 1, volume: 1 },
    { time: '2026-09-02', open: 1, high: 1, low: 1, close: 1, volume: 1 },
  ],
  macdRowsRaw: [{ time: '2026-09-01', hist: 0, dif: 0, dea: 0 }],
  toTime: t => t,
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function chartOpts()'), source.indexOf('  // ---------- 日间/夜间主题')), context);
vm.runInContext(source.slice(source.indexOf('  // ---------- 副图指标'), source.indexOf('  // ---------- 多级别共振角标')), context);

const charts = context.ensureCharts();
const [main, macd] = made;
assert.equal(made.length, 2, 'two charts created');

// 初始渲染（renderChart → showInd('macd')），主副图落在同一可视区间
context.showInd('macd');
main._range = macd._range = { from: 400, to: 522 };
main._calls.setRange.length = 0;
macd._calls.setRange.length = 0;

// A. 正常状态下副图区间变化会同步到主图（同步链路本身未被破坏）
macd._fire({ from: 401, to: 522 });
assert.deepEqual(main._calls.setRange.at(-1), { from: 401, to: 522 }, 'normal sync main follows macd');
main._range = macd._range = { from: 400, to: 522 };
main._calls.setRange.length = 0;

// B. 切换副图指标：清空→自动适配的时间轴瞬变不得传导到主图
context.showInd('kdj');
assert.deepEqual(main._calls.setRange, [], 'main chart time scale untouched by sub rebuild');
// C. 重建后副图恢复切换前的可视区间（而不是自动适配到 [398.81, 522]）
assert.deepEqual(macd._calls.setRange.at(-1), { from: 400, to: 522 }, 'sub chart restores its previous visible range');
assert.deepEqual(macd._range, { from: 400, to: 522 });
// D. 切换结束后同步链路恢复
macd._fire({ from: 402, to: 522 });
assert.deepEqual(main._calls.setRange.at(-1), { from: 402, to: 522 }, 'sync works again after rebuild');

// E. BOLL 样本不足时也必须正常完成重建，显示缺值并恢复时间轴/同步锁
main._range = macd._range = { from: 0, to: 1 };
main._calls.setRange.length = 0;
macd._calls.setRange.length = 0;
assert.doesNotThrow(() => context.showInd('boll'), 'short BOLL data must not throw');
assert.match(subNoteEl.innerHTML, /样本不足/, 'short BOLL explains the minimum-sample state');
assert.match(subNoteEl.innerHTML, /MID[^]*--/, 'short BOLL renders missing MID as --');
assert.equal(context.subRebuild, false, 'short BOLL always releases rebuild lock');
assert.deepEqual(main._calls.setRange, [], 'short BOLL rebuild does not move main chart');
assert.deepEqual(macd._calls.setRange.at(-1), { from: 0, to: 1 }, 'short BOLL restores subchart range');

// F. 空数据走同一 finally：清空 series 触发的自动适配不能遗留锁或时间轴漂移
context.klineData = [];
main._range = macd._range = { from: 0, to: 1 };
main._calls.setRange.length = 0;
macd._calls.setRange.length = 0;
assert.doesNotThrow(() => context.showInd('boll'), 'empty indicator data must not throw');
assert.equal(context.subRebuild, false, 'empty data always releases rebuild lock');
assert.deepEqual(main._calls.setRange, [], 'empty rebuild does not move main chart');
assert.deepEqual(macd._calls.setRange.at(-1), { from: 0, to: 1 }, 'empty rebuild restores subchart range');

console.log('subchart sync isolation checks passed');
