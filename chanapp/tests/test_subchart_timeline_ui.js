'use strict';
/* 副图时间轴铺满主图：主副图按逻辑索引同步，副图每条 series 的时间点必须与主图 K 线逐点相同
   （无值处为只有 time 的 whitespace 点），否则同一逻辑索引落在不同日期。
   覆盖两条路径：指标预热期（RSI/BOLL 前若干根为 null）与左拉分页（历史页不带 MACD 行）。
   helpers（prependHistory）与副图指标切片实跑；图表经 chartStub 记录各 series 收到的数据。 */
const assert = require('node:assert/strict');
const { runSlices } = require('./support/dom.js');
const { appContext, chartStub } = require('./support/app.js');

// 2026-07-01 起连续 50 天：前 20 根是历史页，后 30 根是首屏窗口
const TIMES = Array.from({ length: 50 }, (_, i) => new Date(Date.UTC(2026, 6, 1 + i)).toISOString().slice(0, 10));
assert.equal(TIMES[0], '2026-07-01');
assert.equal(TIMES[49], '2026-08-19');
const bar = (t, i) => ({ time: t, open: 10 + i % 7, high: 12 + i % 7, low: 9 + i % 5, close: 10 + i % 9, volume: 100 + i });
const OLDER = TIMES.slice(0, 20).map(bar);
const WINDOW = TIMES.slice(20).map((t, i) => bar(t, i + 20));
const MACD_ROWS = TIMES.slice(20).map((t, i) => ({ time: t, hist: i - 15, dif: i / 10, dea: i / 20 }));

function harness(ind) {
  const stub = chartStub({ from: 0, to: 29 });
  const context = appContext({
    charts: stub,
    viewState: { adjust: 'qfq', token: 'T1', analysisTokens: null },
    historyState: { loading: true, hasMore: true, reqId: 1 },
    lastChartData: { kline: WINDOW, signals: [], meta: { token: 'T1', has_more: true } },
    P: new Proxy({}, { get: () => '#000' }),
    LightweightCharts: { HistogramSeries: 'h', LineSeries: 'l', LineStyle: { Dashed: 1 } },
    computeMAs() {}, refreshMaSeries() {}, buildMarkers: () => [],
  });
  runSlices(context, ['helpers', 'subChart']);
  context.klineData = WINDOW.slice();
  context.macdRowsRaw = MACD_ROWS;
  context.showInd(ind);
  return { context, stub };
}
const subRows = (stub, from) => stub.values.slice(from).filter(v => v.op === 'sub.setData').map(v => v.rows);
const times = rows => rows.map(r => r.time);
const SERIES_COUNT = { macd: 3, kdj: 3, rsi: 1, boll: 4 };

// 首屏：含 null 预热期的指标也与主图等长，预热段是 whitespace
for (const ind of ['macd', 'kdj', 'rsi', 'boll']) {
  const { stub } = harness(ind);
  const rows = subRows(stub, 0);
  assert.equal(rows.length, SERIES_COUNT[ind], ind + ' series 数');
  for (const r of rows) assert.deepEqual(times(r), TIMES.slice(20), ind + ' 首屏副图时间点与主图 30 根逐点相同');
}
{
  const rsi = subRows(harness('rsi').stub, 0)[0];
  assert.deepEqual(Object.keys(rsi[14]), ['time'], 'RSI(14) 第 15 根之前是 whitespace');
  assert.equal(typeof rsi[15].value, 'number', 'RSI(14) 自第 16 根起有值');
  const bollUp = subRows(harness('boll').stub, 0)[0];
  assert.deepEqual(Object.keys(bollUp[18]), ['time'], 'BOLL(20) 前 19 根是 whitespace');
  assert.equal(typeof bollUp[19].value, 'number', 'BOLL(20) 自第 20 根起有值');
}

// 左拉分页：前置 20 根后，副图每条 series 与主图 K 线逐点相同；历史段 MACD 无值，用 whitespace 占位
for (const ind of ['macd', 'kdj', 'rsi', 'boll']) {
  const { context, stub } = harness(ind);
  const mark = stub.values.length;
  context.prependHistory({ kline: OLDER.concat(WINDOW.slice(0, 1)), meta: { history: true, token: 'T1', has_more: true } });
  const candle = stub.values.slice(mark).find(v => v.op === 'candle.setData').rows;
  assert.deepEqual(times(candle), TIMES, ind + ' 主图前置后为 50 根');
  const rows = subRows(stub, mark);
  assert.equal(rows.length, SERIES_COUNT[ind], ind + ' 分页后副图 series 全部重建');
  for (const r of rows) assert.deepEqual(times(r), TIMES, ind + ' 分页后副图时间点与主图逐点相同');
  if (ind === 'macd') {
    const [hist, dif] = rows;
    assert.deepEqual({ ...hist[19] }, { time: '2026-07-20' }, '历史段 MACD 柱是只有 time 的 whitespace');
    assert.deepEqual({ ...dif[0] }, { time: '2026-07-01' }, '历史段 DIF 是 whitespace');
    assert.deepEqual({ ...hist[20] }, { time: '2026-07-21', value: -15, color: '#000' }, '原窗口 MACD 柱值保持不变');
    assert.deepEqual({ ...dif[49] }, { time: '2026-08-19', value: 2.9 }, '原窗口 DIF 值保持不变');
  }
  assert.deepEqual({ ...stub.values.filter(v => v.op === 'sub.range').at(-1).r },
    { from: 20, to: 49 }, ind + ' 副图最终与主图同步平移 20 根');
}
console.log('subchart timeline alignment checks passed');
