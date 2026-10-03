'use strict';
/* 主副图十字线联动与副图读数：主图十字线停在某根 bar → 副图在同一时间画竖线、#subNote 显示该 bar 的指标值；
   移出 → 副图清除十字线、读数回到最新一根；副图上移动同理联动主图（图例跟随）。
   该 bar 无指标值（预热期、翻页进来的历史段 MACD）时读数显示 —，副图仍有竖线。
   ensureCharts 与副图指标切片实跑。假图表按 lightweight-charts 5.0.9 实测建模：程序化 setCrosshairPosition /
   clearCrosshairPosition / applyOptions 都会异步回发本图的 crosshairMove（回声）；指针进出走容器的 pointerenter/leave。
   回声必须被忽略，否则两图互相设置无限往返（浏览器实测踩过）。 */
const assert = require('node:assert/strict');
const { mkEl, runSlices } = require('./support/dom.js');
const chanIndicators = require('../web/indicators.js');

const echoes = [];  // 待回发的程序化事件；drain() 模拟库在下一帧回发
function drain() {
  for (let round = 0; echoes.length; round++) {
    assert.ok(round < 50, '十字线回声没有收敛：两图在互相设置');
    echoes.splice(0).forEach(fire => fire());
  }
}
function fakeChart(name) {
  const calls = { cross: [], clear: 0, options: [] };
  let crossFn = null, at = null;
  const echo = () => echoes.push(() => crossFn && crossFn(at ? { time: at, point: { x: 1, y: 1 } } : { time: undefined, point: undefined }));
  const chart = {
    name, calls,
    addSeries(type) { return { type, setData() {}, applyOptions() {} }; },
    removeSeries() {},
    priceScale: () => ({ applyOptions() {} }),
    applyOptions(o) { calls.options.push(o); echo(); },
    subscribeCrosshairMove(fn) { crossFn = fn; },
    setCrosshairPosition(price, time, series) { calls.cross.push({ price, time, series }); at = time; echo(); },
    clearCrosshairPosition() { calls.clear++; at = null; echo(); },
    timeScale: () => ({
      subscribeVisibleLogicalRangeChange() {}, width: () => 800,
      getVisibleLogicalRange: () => null, setVisibleLogicalRange() {},
    }),
    // 指针在图上移动：库按真实鼠标事件同步回调，sourceEvent 存在
    move(param) { at = param.time || null; crossFn({ ...param, sourceEvent: {} }); drain(); },
  };
  return chart;
}

const TIMES = Array.from({ length: 30 }, (_, i) => new Date(Date.UTC(2026, 6, 1 + i)).toISOString().slice(0, 10));
const BARS = TIMES.map((t, i) => ({ time: t, open: 10, high: 11 + i % 3, low: 9, close: 10 + i / 10, volume: 100 }));
// MACD 只覆盖后 10 根（前 20 根相当于翻页进来的历史段）
const MACD = TIMES.slice(20).map((t, k) => ({ time: t, dif: k / 100 + 0.1, dea: 0.1, hist: k === 5 ? 0.1 : 2 * (k / 100) }));

function env() {
  const made = [], legends = [];
  const nodes = { subNote: mkEl('span') };
  const pointer = {};
  for (const id of ['chart', 'sub']) {
    const node = nodes[id] = mkEl('div');
    node.addEventListener = (type, fn) => { pointer[id + ':' + type] = fn; };
  }
  const context = {
    LightweightCharts: {
      CandlestickSeries: 'candle', HistogramSeries: 'hist', LineSeries: 'line', LineStyle: { Dashed: 1 },
      createChart(container) { const c = fakeChart(made.length ? 'sub' : 'main'); made.push(c); return c; },
      createSeriesMarkers: () => ({ setMarkers() {} }),
    },
    charts: null,
    el: id => nodes[id] || (nodes[id] = mkEl()),
    document: { querySelectorAll: () => [] },
    makeZsOverlay: () => ({ setBoxes() {} }),
    P: new Proxy({}, { get: (_, k) => String(k) }),
    chanIndicators,
    axisTick() {}, axisLabel() {}, updateAxisAnchor() {},
    renderLegend(bar, prev, i) { legends.push(i); },
    legendLatest() { legends.push('latest'); },
    toTime: t => t,
  };
  runSlices(context, ['chartSetup', 'subChart']);
  context.klineData = BARS;
  context.klineByTime = Object.fromEntries(BARS.map((b, i) => [b.time, { bar: b, prev: BARS[i - 1] || null, i }]));
  context.macdRowsRaw = MACD;
  context.ensureCharts();
  context.wireChartPointer();
  const [main, sub] = made;
  const fire = key => { assert.equal(typeof pointer[key], 'function', '图容器监听 ' + key); pointer[key](); drain(); };
  const enter = id => fire(id + ':pointerenter');
  const leave = id => fire(id + ':pointerleave');
  return { context, main, sub, legends, enter, leave, note: () => nodes.subNote.innerHTML.replace(/<[^>]+>/g, '') };
}
const at = i => ({ time: TIMES[i], point: { x: 100, y: 50 } });
const LATEST_MACD = 'MACD · DIF 0.190 · DEA 0.100 · MACD 0.180 · hist=2×(DIF−DEA)';

{
  const { context, main, sub, legends, enter, leave, note } = env();
  context.showInd('macd');
  assert.equal(note(), LATEST_MACD, '未悬停时读数是最新一根');

  enter('chart');
  main.move(at(25));
  assert.equal(sub.calls.cross.at(-1).time, '2026-07-26', '主图十字线 → 副图同一时间竖线');
  assert.equal(sub.calls.cross.length, 1, '副图只被设置一次（回声不再触发联动）');
  assert.equal(main.calls.cross.length, 0, '副图回声不得反过来设置主图');
  assert.equal(note(), 'MACD · DIF 0.150 · DEA 0.100 · MACD 0.100 · hist=2×(DIF−DEA)', '读数是该 bar 的指标值');
  assert.equal(legends.at(-1), 25, '主图图例跟随');
  assert.ok(sub.calls.options.some(o => o.crosshair && o.crosshair.horzLine && o.crosshair.horzLine.visible === false &&
    o.crosshair.horzLine.labelVisible === false), '跟随一侧关掉横线与价格标签，不显示误导价格');

  main.move(at(5));
  assert.equal(sub.calls.cross.at(-1).time, '2026-07-06', '无 MACD 的历史段 bar 副图仍有竖线');
  assert.equal(note(), 'MACD · DIF — · DEA — · MACD — · hist=2×(DIF−DEA)', '无值显示 —');

  const clears = sub.calls.clear;
  leave('chart');
  assert.equal(sub.calls.clear, clears + 1, '移出主图 → 副图清除十字线');
  assert.equal(note(), LATEST_MACD, '移出后读数回到最新一根');
  assert.equal(legends.at(-1), 'latest');

  const crosses = main.calls.cross.length;
  enter('sub');
  sub.move(at(25));
  assert.equal(main.calls.cross.length, crosses + 1, '副图十字线 → 主图联动');
  assert.deepEqual({ ...main.calls.cross.at(-1), series: main.calls.cross.at(-1).series === context.charts.candleSeries },
    { price: 12.5, time: '2026-07-26', series: true }, '主图十字线落在该 bar 收盘价');
  assert.equal(legends.at(-1), 25, '副图悬停时主图图例显示该 bar');
  assert.equal(note(), 'MACD · DIF 0.150 · DEA 0.100 · MACD 0.100 · hist=2×(DIF−DEA)');
  assert.ok(main.calls.options.some(o => o.crosshair && o.crosshair.horzLine && o.crosshair.horzLine.visible === false),
    '副图为源时主图关掉横线');
  assert.equal(main.calls.cross.length, crosses + 1, '主图回声不得反过来再设置副图后又回到主图');
  leave('sub');
  assert.ok(main.calls.clear >= 1, '移出副图 → 主图清除十字线');
  assert.equal(note(), LATEST_MACD);

  // 指针不在任何图上时，程序化十字线（依据卡定位 locateBar）的回声不得驱动联动
  const subCrosses = sub.calls.cross.length;
  main.setCrosshairPosition(12.5, TIMES[25], context.charts.candleSeries);
  drain();
  assert.equal(sub.calls.cross.length, subCrosses, '无指针时主图回声不联动副图');
}

// 预热期：RSI(14) 前 15 根无值显示 —；BOLL/KDJ 读数格式
{
  const { context, main, enter, note } = env();
  enter('chart');
  context.showInd('rsi');
  main.move(at(3));
  assert.equal(note(), 'RSI(14) · RSI — · 显示用');
  context.showInd('kdj');
  main.move(at(0));
  assert.equal(note(), 'KDJ(9,3,3) · K 50.00 · D 50.00 · J 50.00 · 显示用', 'KDJ 首根 RSV=50');
  context.showInd('boll');
  main.move(at(10));
  assert.equal(note(), 'BOLL(20,2) · MID — · UP — · LOW — · C 11.00 · 显示用', 'BOLL 预热期无值');
}
console.log('crosshair sync and subchart readout checks passed');
