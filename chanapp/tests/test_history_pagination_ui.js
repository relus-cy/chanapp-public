'use strict';
/* 历史分页：左拉门（onMainRangeChanged 真实守卫）、loadHistoryPage 请求身份（before/limit/token/adjust）、
   409 与令牌不符时整窗重载（不混接旧段）、prependHistory 合并/平移/markers 有序断言、
   renderChart 按视图令牌决定保留或丢弃旧段。helpers/renderChart/load 切片实跑；图表操作经 chartStub 事件流断言顺序。 */
const assert = require('node:assert/strict');
const { appSource, tick, runSlices } = require('./support/dom.js');
const { appContext, loadEnv, chartStub, okJson, errJson } = require('./support/app.js');

const RELOADED = '数据已更新，已重新加载';

// 左拉分页订阅接线（ensureCharts 依赖完整图表栈，无等价行为替身：保留声明断言）
assert.ok(appSource.includes('subscribeVisibleLogicalRangeChange(onMainRangeChanged)'),
  '主图须注册可视区间订阅 onMainRangeChanged');

// 视图身份默认值与分页周期门：取自 app.js 真实声明
{
  const context = {};
  runSlices(context, ['viewState']);
  assert.equal(context.viewState.adjust, 'qfq', '复权模式默认前复权');
  assert.equal(context.viewState.token, null, '首屏前没有视图令牌');
  assert.equal(context.viewState.analysisTokens, null, '首屏前没有分析令牌');
}

// onMainRangeChanged 行为门：hasMore/loading/可用周期/令牌/首行存在/阈值 8
{
  const env = appContext({ viewState: { adjust: 'qfq', token: null, analysisTokens: null } });
  runSlices(env, ['helpers']);
  let calls = 0;
  env.loadHistoryPage = () => calls++;
  env.state.freq = 'day';
  env.klineData = [{ time: 't0' }];
  const h = env.historyState;
  const fire = range => env.onMainRangeChanged(range);

  h.hasMore = false; h.loading = false; env.viewState.token = 'T1';
  fire({ from: 0, to: 1 });
  assert.equal(calls, 0, 'hasMore=false 不取历史');

  h.hasMore = true; h.loading = true;
  fire({ from: 0, to: 1 });
  assert.equal(calls, 0, 'loading 中不重复取');

  h.loading = false; env.state.freq = 'm5';
  fire({ from: 0, to: 1 });
  assert.equal(calls, 0, '非 HISTORY_FREQS（m5）不取');

  env.state.freq = 'day'; env.viewState.token = null;
  fire({ from: 0, to: 1 });
  assert.equal(calls, 0, '没有视图令牌（整窗重载进行中或首屏未到）不取');

  env.viewState.token = 'T1'; env.klineData = [];
  fire({ from: 0, to: 1 });
  assert.equal(calls, 0, '空 K 线不取');

  env.klineData = [{ time: 't0' }];
  fire({ from: 9, to: 20 });
  assert.equal(calls, 0, '视口左沿超过阈值 8 不取');
  fire({ from: 8, to: 20 });
  assert.equal(calls, 1, '视口左沿到达阈值即取一页');
  console.log('onMainRangeChanged guard checks passed');
}

// renderChart：同令牌保留严格早于新窗首行的旧段；不同令牌丢弃旧段整窗替换；
// 所有路径递增 reqId；markers 在所有 series setData/区间操作之后（有序事件断言）。
function renderHarness(token, opts, freq, meta) {
  const stub = chartStub();
  const context = appContext({
    charts: stub,
    ensureCharts: () => stub,
    state: { freq: freq || 'day' },
    viewState: { adjust: 'qfq', token: 'T7', analysisTokens: null },
    historyState: { loading: false, hasMore: true, reqId: 4 },
    klineData: [
      { time: '2026-09-01' }, { time: '2026-09-02' },
      { time: '2026-09-03' }, { time: '2026-09-04' },
    ],
    P: { upA: 'up', downA: 'down', goldA: 'g', biForming: 'bf' },
    LightweightCharts: { LineSeries: function () {}, LineStyle: { Dashed: 1 } },
    DISPLAY_BARS: 120, curInd: 'macd',
    computeMAs() {}, refreshMaSeries() {}, showInd() {},
    legendLatest() {}, renderResonance() {}, updateAxisAnchor() {},
    buildMarkers: signals => (signals || []).map(s => s.time),
  });
  runSlices(context, ['helpers', 'renderChart']);
  context.renderChart({
    kline: [{ time: '2026-09-03' }, { time: '2026-09-04' }, { time: '2026-09-05' }],
    macd: { rows: [] },
    structure: { bi: [], xd: [], zs: [] },
    meta: meta || { token, has_more: true },
  }, opts === undefined ? { resetRange: false } : opts);
  return { context, stub };
}
{
  const same = renderHarness('T7');
  assert.deepEqual(Array.from(same.context.klineData, b => b.time),
    ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-05'],
    '同令牌刷新保留严格早于新窗首行的历史段');
  assert.equal(same.context.viewState.token, 'T7');
  assert.equal(same.context.historyState.hasMore, true);
  assert.equal(same.context.historyState.reqId, 5, '刷新重建状态使旧历史请求失效');
  assert.equal(same.stub.events.at(-1), 'markers', 'renderChart 最后操作须是 setMarkers');
  assert.ok(same.stub.events.indexOf('candle.setData') < same.stub.events.indexOf('markers'),
    'markers 须在 K 线 setData 之后设置');

  const changed = renderHarness('T8');
  assert.deepEqual(Array.from(changed.context.klineData, b => b.time),
    ['2026-09-03', '2026-09-04', '2026-09-05'], '令牌变化时丢弃旧段，整窗替换');
  assert.equal(changed.context.viewState.token, 'T8', '记下新视图令牌');

  // resetRange 默认路径：同样登记令牌并使旧 reqId 失效
  const reset = renderHarness('T7', {});
  assert.equal(reset.context.historyState.reqId, 5, 'resetRange 路径同样使旧历史请求失效');
  assert.equal(reset.context.viewState.token, 'T7');
  assert.ok(reset.stub.values.some(v => v.op === 'main.range'), 'resetRange 路径主动回最新窗口');

  // 非分页周期（m5）：不登记 hasMore，但 reqId 仍须递增以使在飞旧页失效
  const m5 = renderHarness('T8', undefined, 'm5');
  assert.equal(m5.context.historyState.hasMore, false, 'm5 不登记 hasMore');
  assert.equal(m5.context.historyState.reqId, 5, 'm5 路径仍递增 reqId 使旧历史请求失效');

  // week 属于分页周期
  const week = renderHarness('W1', undefined, 'week');
  assert.equal(week.context.historyState.hasMore, true, 'week 登记 hasMore');

  // 响应没有令牌：不可分页，也不与旧段拼接
  const tokenless = renderHarness(null, undefined, 'day', { has_more: true });
  assert.equal(tokenless.context.viewState.token, null);
  assert.equal(tokenless.context.historyState.hasMore, false, '无令牌不开放分页');
  assert.equal(tokenless.context.klineData.length, 3, '无令牌不拼接旧段');
}

// prepend：排除边界重复行，按实际新增根数平移双图，并在 series 更新后重设 markers。
{
  const stub = chartStub({ from: 0, to: 1 });
  const context = appContext({
    charts: stub,
    viewState: { adjust: 'qfq', token: 'T7', analysisTokens: null },
    historyState: { loading: true, hasMore: true, reqId: 1 },
    klineData: [
      { time: '2026-09-03', open: 3, high: 3, low: 3, close: 3, volume: 30 },
      { time: '2026-09-04', open: 4, high: 4, low: 4, close: 4, volume: 40 },
    ],
    lastChartData: { kline: [], signals: [{ time: '2026-09-04', type: 'buy' }], meta: { token: 'T7', has_more: true } },
    P: { upA: 'up', downA: 'down' }, curInd: 'macd',
    computeMAs() {}, refreshMaSeries() {}, showInd() {},
    buildMarkers: signals => signals.map(s => s.time),
  });
  runSlices(context, ['helpers']);
  context.prependHistory({
    kline: [
      { time: '2026-09-01', open: 1, high: 1, low: 1, close: 1, volume: 10 },
      { time: '2026-09-02', open: 2, high: 2, low: 2, close: 2, volume: 20 },
      { time: '2026-09-03', open: 3, high: 3, low: 3, close: 3, volume: 30 },
    ],
    meta: { history: true, token: 'T7', has_more: false },
  });
  assert.deepEqual(Array.from(context.klineData, b => b.time),
    ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04'], 'prepend 合并且去掉边界重复行');
  assert.deepEqual({ ...stub.values.find(v => v.op === 'main.range').r }, { from: 2, to: 3 },
    '主图按两根新增历史平移');
  assert.deepEqual({ ...stub.values.find(v => v.op === 'sub.range').r }, { from: 2, to: 3 },
    '副图按两根新增历史平移');
  const markersIdx = stub.events.indexOf('markers');
  assert.ok(markersIdx > stub.events.indexOf('candle.setData') && markersIdx > stub.events.indexOf('volume.setData'),
    'markers 在 series setData 后重设');
  assert.deepEqual(stub.values.find(v => v.op === 'markers').rows, ['2026-09-04'], 'series 更新后重设近期信号 markers');
  assert.equal(context.lastChartData.kline, context.klineData, '主题重绘使用扩展后的 K 线');
  assert.equal(context.lastChartData.meta.has_more, false, 'has_more 同步进主题数据');
  assert.equal(context.historyState.hasMore, false, '分页 has_more 同步');
}

// 扩展窗刷新链路：capture 与 plan 都使用同一合并窗口，时间锚点不左跳
{
  const context = appContext({
    klineData: [{ time: 't0' }, { time: 't1' }, { time: 't2' }, { time: 't3' }],
  });
  runSlices(context, ['helpers']);
  const keep = context.captureRefreshRange({ from: 1.25, to: 2.25 }, context.klineData);
  const merged = context.mergeWithHistory(
    [{ time: 't2' }, { time: 't3' }, { time: 't4' }], 'T7', 'T7', context.klineData);
  const plan = context.refreshRangePlan(keep, merged);
  assert.deepEqual({ ...plan }, { from: 1.25, to: 2.25 }, '刷新合并后保持原时间锚点');
  assert.equal(context.mergeWithHistory([{ time: 't2' }], 'T8', 'T7', context.klineData).length, 1,
    '令牌不同不拼接旧段');
  assert.equal(context.mergeWithHistory([{ time: 't2' }], 'T7', null, context.klineData).length, 1,
    '旧令牌已作废（整窗重载）时不拼接旧段');
}

// 分页请求完整 Promise 生命周期与请求身份守卫。
// 桩 prependHistory/load 一律在 runSlices 之后赋值（helpers 切片会定义真身）。
// fetch 包一层记录请求 URL：before/limit/token/adjust 缺失时分页静默失效或被服务端判为旧页面，
// 必须直查 URL；firstTime 用含空格的分钟级时间戳顺带证明 encodeURIComponent（%20）。
const mkHistoryContext = fetchImpl => {
  const context = appContext({
    state: { code: 'a', freq: 'day' },
    viewState: { adjust: 'qfq', token: 'T 7', analysisTokens: null },
    historyState: { loading: false, hasMore: true, reqId: 0 },
    klineData: [{ time: '2026-09-03 09:30' }],
    fetch: fetchImpl,
  });
  runSlices(context, ['helpers']);
  context.fetch = url => {
    (context._harness.urls || (context._harness.urls = [])).push(url);
    return fetchImpl(url);
  };
  context._harness.loads = [];
  context.load = options => context._harness.loads.push(options || {});
  return context;
};
const pageOf = (kline, meta) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({
  code: 'a', freq: 'day', adjust: 'qfq', kline, meta: Object.assign({ history: true, token: 'T 7' }, meta),
}) });

(async () => {
  // 正常空页：静默回落常驻状态并标记耗尽；URL 带 before/limit/token/adjust
  {
    const context = mkHistoryContext(() => pageOf([], { has_more: false }));
    context.prependHistory = page => { context.historyState.hasMore = !!page.meta.has_more; };
    context.loadHistoryPage();
    await tick(); await tick();
    const url = context._harness.urls[0];
    assert.equal(context._harness.urls.length, 1, '分页须发出一笔请求');
    assert.ok(url.includes('&before=2026-09-03%2009%3A30'),
      '分页 URL 须带 before=首行时间且经 encodeURIComponent（空格→%20、冒号→%3A）');
    assert.ok(url.includes('&limit=520'), '分页请求须固定 limit=520');
    assert.ok(url.includes('&token=T%207'), '分页请求须带当前视图令牌（经 URL 编码）');
    assert.ok(url.includes('&adjust=qfq'), '分页请求须带当前复权模式');
    assert.equal(context._harness.loads.length, 0, '同令牌空页不重载');
    assert.equal(context._harness.statuses.at(-1), '', '正常空页静默回落常驻状态');
    assert.equal(context.historyState.hasMore, false, '正常空页标记耗尽');
    assert.equal(context._harness.statuses.includes('加载历史…'), true, '分页期间显示加载态');
  }

  // HTTP 失败：独立临时失败态，不误标耗尽，不重载
  {
    const context = mkHistoryContext(() => Promise.resolve({ ok: false, status: 503 }));
    context.prependHistory = () => { throw new Error('failed response must not prepend'); };
    context.loadHistoryPage();
    await tick(); await tick();
    assert.equal(context._harness.statuses.at(-1), '历史加载失败', 'HTTP 失败须显示独立临时失败态');
    assert.equal(context.historyState.hasMore, true, '加载失败不得误标记历史耗尽');
    assert.equal(context._harness.loads.length, 0, '503 不是令牌冲突，不整窗重载');
  }

  // 成功 prepend：finally 仍释放 loading 并清状态
  {
    const context = mkHistoryContext(() => pageOf(
      [{ time: '2026-09-01', open: 1, high: 1, low: 1, close: 1, volume: 10 }], { has_more: false }));
    let prepended = 0;
    context.prependHistory = () => { prepended++; context.historyState.hasMore = false; };
    context.loadHistoryPage();
    await tick(); await tick();
    assert.equal(prepended, 1, '同令牌分页页拼接');
    assert.equal(context.historyState.loading, false, 'prepend 成功后 finally 释放 loading');
    assert.equal(context._harness.statuses.at(-1), '', 'prepend 成功后 finally 清除加载状态');
  }

  // 分页中途令牌变化 → 409：整窗重载并提示，不拼接旧段，作废旧令牌与条件请求
  {
    const context = mkHistoryContext(() => Promise.resolve(errJson(409, { detail: '数据已更新', token: 'T8' })));
    context.chartEtag = { code: 'a', freq: 'day', etag: '"old"' };
    context.prependHistory = () => { throw new Error('409 must not prepend'); };
    context.loadHistoryPage();
    await tick(); await tick();
    assert.equal(context._harness.loads.length, 1, '409 须整窗重载一次');
    assert.equal(context._harness.loads[0].refresh, true, '整窗重载走刷新路径（保留可视区间锚点）');
    assert.equal(context._harness.loads[0].notice, RELOADED, '重载完成后提示数据已更新');
    assert.equal(context.viewState.token, null, '旧令牌作废：重载结果不与旧段拼接');
    assert.equal(context.chartEtag, null, '整窗重载不带条件请求，保证拿到新令牌');
    assert.equal(context.historyState.loading, false, '重载前释放分页 loading');
    assert.notEqual(context._harness.statuses.at(-1), '历史加载失败', '409 不显示为失败');
  }

  // 200 但令牌与当前不同：丢弃该页，整窗重载
  {
    const context = mkHistoryContext(() => pageOf([{ time: '2026-09-01' }], { token: 'T8', has_more: true }));
    context.prependHistory = () => { throw new Error('token mismatch must not prepend'); };
    context.loadHistoryPage();
    await tick(); await tick();
    assert.equal(context._harness.loads.length, 1, '令牌不同须整窗重载');
    assert.equal(context._harness.loads[0].notice, RELOADED);
    assert.equal(context.viewState.token, null);
  }

  // 整窗重载落在左沿会立刻续拉下一页：提示期内分页不得覆盖「数据已更新」，提示消退后恢复常规状态
  {
    const context = mkHistoryContext(() => pageOf([], { has_more: false }));
    context.prependHistory = page => { context.historyState.hasMore = !!page.meta.has_more; };
    context.showNotice(RELOADED);
    context.loadHistoryPage();
    await tick(); await tick();
    assert.equal(context._harness.statuses.includes('加载历史…'), false, '提示期内分页不显示加载态');
    assert.equal(context._harness.statuses.at(-1), RELOADED, '提示期内分页结束后提示仍在');
    context._harness.timers.at(-1)();  // 提示计时到期
    context.loadHistoryPage();
    await tick(); await tick();
    assert.equal(context._harness.statuses.includes('加载历史…'), true, '提示消退后分页恢复加载态');
    assert.equal(context._harness.statuses.at(-1), '', '提示消退后分页结束回落常驻状态');
  }

  // 响应到齐时视图已漂移：code/freq/adjust/首行时间 任一变化 → 不提交、不重载
  for (const drift of ['code', 'freq', 'adjust', 'firstTime']) {
    for (const status of [200, 409]) {
      const context = mkHistoryContext(() => status === 200
        ? pageOf([{ time: '2026-09-01' }], { has_more: true })
        : Promise.resolve(errJson(409, { detail: '数据已更新', token: 'T8' })));
      let prependCalls = 0;
      context.prependHistory = () => prependCalls++;
      context.loadHistoryPage();
      if (drift === 'code') context.state.code = 'b';                // 响应到达前用户已切换标的
      else if (drift === 'freq') context.state.freq = 'm30';         // 响应到达前已切周期
      else if (drift === 'adjust') context.viewState.adjust = 'raw';  // 响应到达前已切复权模式
      else context.klineData = [{ time: '2026-09-02' }];              // 另一页已先行提交，首行变了
      await tick(); await tick();
      assert.equal(prependCalls, 0, drift + ' 漂移后迟到的历史页不得提交');
      assert.equal(context._harness.loads.length, 0, drift + ' 漂移后迟到的 ' + status + ' 不得触发重载');
    }
  }

  // reqId 守卫：同标的两页在飞，旧页后到不得提交（renderChart 重建或二次左拉都会递增 reqId）
  {
    const context = mkHistoryContext(() => pageOf([{ time: '2026-08-31' }], { has_more: true }));
    let prependCalls = 0;
    context.prependHistory = () => prependCalls++;
    context.loadHistoryPage();      // reqId 1
    context.historyState.reqId++;   // 等价于第二次左拉/renderChart 重建使 reqId 递增
    await tick(); await tick();
    assert.equal(prependCalls, 0, 'reqId 失效的旧历史页不得提交');
  }

  // 已扩展 A/day 后普通切换 B/day：即使令牌字面相同，也只能整窗提交 B。
  {
    const stub = chartStub({ from: 0, to: 2 });
    const context = loadEnv({
      state: { code: 'B', freq: 'day' },
      charts: stub,
      viewState: { adjust: 'qfq', token: 'T7', analysisTokens: null },
      loadedTarget: { code: 'A', freq: 'day', adjust: 'qfq' },
      historyState: { loading: false, hasMore: true, reqId: 4 },
      klineData: [{ time: 'A-old-1' }, { time: 'A-old-2' }, { time: 'B-new-1' }],
    });
    context.renderChart = (data, opts) => { context.klineData = opts.mergedKline; };
    context.load();
    context._harness.pending[0].resolve(okJson({
      kline: [{ time: 'B-new-1' }, { time: 'B-new-2' }],
      meta: { token: 'T7', has_more: true },
    }));
    await tick(); await tick();
    assert.deepEqual(Array.from(context.klineData, b => b.time), ['B-new-1', 'B-new-2'],
      '普通切换 B/day 不得混入 A/day 历史行');
  }

  // 整窗重载提示：load({notice}) 在新窗口提交后显示，8s 自动消退；失败不显示
  {
    const context = loadEnv({ viewState: { adjust: 'qfq', token: null, analysisTokens: null } });
    context.load({ refresh: true, notice: RELOADED });
    context._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: { token: 'T8' } }));
    await tick(); await tick();
    assert.equal(context.statusMsg, RELOADED, '新窗口提交后显示数据已更新');
    context._harness.timers.at(-1)();
    assert.equal(context.statusMsg, null, '提示按计时器自动消退');

    const failed = loadEnv({ viewState: { adjust: 'qfq', token: null, analysisTokens: null } });
    failed.showChartError = () => {};
    failed.load({ refresh: true, notice: RELOADED });
    failed._harness.pending[0].resolve(errJson(502, { detail: 'x' }));
    await tick(); await tick();
    assert.notEqual(failed.statusMsg, RELOADED, '重载失败不得谎报已重新加载');
  }
  console.log('history pagination UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
