'use strict';
/* 条件请求行为断言：实跑 etag 切片（conditionalHeaders/rememberEtag）+ load() 真身，
   覆盖 200 记录 ETag、304 不读 JSON 不重绘、迟到响应不污染 ETag、
   请求携带 adjust、切换代码/周期/复权后迟到的旧主图响应不渲染。 */
const assert = require('node:assert/strict');
const { tick, runSlices } = require('./support/dom.js');
const { loadEnv, okJson } = require('./support/app.js');

// etag 函数级契约：rememberEtag/conditionalHeaders 的键（code/freq/adjust/profile/scope）与清除语义
{
  const context = {};
  runSlices(context, ['etag']);
  const headers = (code, freq, adjust, profile, scope) =>
    Object.assign({}, context.conditionalHeaders(code, freq, adjust, profile, scope));
  // vm realm 产出的对象原型与本源不同，deepStrictEqual 会因原型不等判失败；浅拷贝回本源再比对。
  assert.deepEqual(headers('sh000001', 'day', 'qfq', 'strict', 'expanded'), {});
  context.rememberEtag('sh000001', 'day', 'qfq', 'strict', 'expanded', '"abc"');
  assert.deepEqual(headers('sh000001', 'day', 'qfq', 'strict', 'expanded'), { 'If-None-Match': '"abc"' });
  assert.deepEqual(headers('sh000001', 'm30', 'qfq', 'strict', 'expanded'), {}, '不同 freq 不带旧 etag');
  assert.deepEqual(headers('sh000001', 'day', 'raw', 'strict', 'expanded'), {}, '不同复权模式不带旧 etag');
  assert.deepEqual(headers('sh000001', 'day', 'qfq', 'relaxed', 'expanded'), {}, '不同 profile 不带旧 etag');
  context.rememberEtag('sh000001', 'day', 'qfq', 'strict', 'expanded', null);
  assert.deepEqual(headers('sh000001', 'day', 'qfq', 'strict', 'expanded'), {}, 'etag 为空时清除');
}

// load() 请求级契约：ETag 只在身份匹配时携带
(async function () {
  const env = loadEnv({ viewState: { adjust: 'qfq', token: null, analysisTokens: null } });
  env.load();
  assert.ok(env._harness.pending[0].url.includes('&adjust=qfq'), '主图请求带当前复权模式');
  assert.equal(env._harness.pending[0].options.headers['If-None-Match'], undefined, '无缓存时不带条件头');
  env._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v1"'));
  await tick(); await tick();

  env.load({ refresh: true });
  assert.equal(env._harness.pending[1].options.headers['If-None-Match'], '"v1"',
    '200 后同身份请求携带记录的 ETag');
  env._harness.pending[1].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v2"'));
  await tick(); await tick();

  env.state.freq = 'm30';
  env.load();
  assert.equal(env._harness.pending[2].options.headers['If-None-Match'], undefined,
    'freq 变化后不带旧 etag');
  env._harness.pending[2].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"m30v"'));
  await tick(); await tick();

  // 304：不读 JSON、不重绘、不带 ETag 更新
  env.state.freq = 'day';
  env.load();
  assert.equal(env._harness.pending[3].options.headers['If-None-Match'], undefined,
    'etag 单槽已被 m30 占用，day 请求回归无缓存');
  env._harness.pending[3].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v4"'));
  await tick(); await tick();
  env.load({ refresh: true });
  assert.equal(env._harness.pending[4].options.headers['If-None-Match'], '"v4"', 'day 重新记录后携带 ETag');
  let jsonRead = false;
  env._harness.pending[4].resolve({
    status: 304, ok: true, headers: { get: () => '"v9"' },
    json() { jsonRead = true; return Promise.resolve({}); },
  });
  await tick(); await tick();
  assert.equal(jsonRead, false, '304 不读响应体');
  assert.equal(env.statusMsg, null, '304 清掉临时状态');
  assert.equal(env.el('center').classList.contains('ctx-old'), false, '304 后清除旧数据标记');
  assert.equal(env._harness.renders.filter(r => r === 'chart').length, 4,
    '304 未重绘（四次 chart 来自四笔 200）');
  env.load({ refresh: true });
  assert.equal(env._harness.pending[5].options.headers['If-None-Match'], '"v4"',
    '304 响应头里的 ETag 不得被采纳');
  env._harness.pending[5].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v5"'));
  await tick(); await tick();

  // 迟到响应不污染 ETag：同标的连发两笔在飞请求（第二笔中止第一笔），
  // 先回新再回旧；旧响应到时应被 chartAbort 守卫整体丢弃（含 rememberEtag）。
  env.load();
  env.load();
  env._harness.pending[7].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"new"'));
  await tick(); await tick();
  env._harness.pending[6].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"stale"'));
  await tick(); await tick();
  env.load({ refresh: true });
  assert.equal(env._harness.pending[8].options.headers['If-None-Match'], '"new"',
    '被新请求中止的旧响应不得覆盖已记录的 ETag');
  env._harness.pending[8].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }));
  await tick(); await tick();

  // 复权模式变化：同 code/freq 也不得带旧模式的 ETag
  env.viewState.adjust = 'raw';
  env.load({ refresh: true });
  assert.ok(env._harness.pending[9].url.includes('&adjust=raw'), '主图请求跟随当前复权模式');
  assert.equal(env._harness.pending[9].options.headers['If-None-Match'], undefined, '换复权模式不带旧 ETag');
  env._harness.pending[9].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }));
  await tick(); await tick();
  console.log('chart conditional request UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

// 切换代码/周期/复权后，旧请求的主图响应才到达 → 不渲染、不记 ETag、不改令牌。
// 选择变化不一定伴随新的 load()（例如删除当前自选后无后继、或改选择的路径早退），
// 守卫必须按「请求发出时的 code/freq/adjust 仍是当前值」判定，而不只靠中止控制器。
(async function () {
  for (const drift of ['code', 'freq', 'adjust']) {
    const env = loadEnv({ viewState: { adjust: 'qfq', token: 'T-old', analysisTokens: { day: 'd0' } } });
    env.load();
    if (drift === 'code') env.state.code = 'b';
    else if (drift === 'freq') env.state.freq = 'm30';
    else env.viewState.adjust = 'raw';
    env._harness.pending[0].resolve(okJson({
      kline: [{ time: 't' }], macd: { rows: [] },
      meta: { token: 'T-late', analysis_tokens: { day: 'late' } },
    }, '"late"'));
    await tick(); await tick();
    assert.equal(env._harness.renders.filter(r => r === 'chart').length, 0, drift + ' 变化后迟到的旧主图响应不得渲染');
    assert.equal(env.viewState.token, 'T-old', drift + ' 变化后迟到响应不得改写视图令牌');
    assert.deepEqual({ ...env.viewState.analysisTokens }, { day: 'd0' }, drift + ' 变化后迟到响应不得改写分析令牌');
    assert.equal(env.loadedTarget, null, drift + ' 变化后迟到响应不得登记图表归属');
    env.load();
    assert.equal(env._harness.pending[1].options.headers['If-None-Match'], undefined,
      drift + ' 变化后迟到响应的 ETag 不得被记下');
  }

  // 正常提交：记下视图令牌与分析令牌
  const env = loadEnv({ viewState: { adjust: 'qfq', token: null, analysisTokens: null } });
  env.load();
  env._harness.pending[0].resolve(okJson({
    kline: [], macd: { rows: [] },
    meta: { token: 'T1', has_more: true, analysis_tokens: { day: 'd1', m60: 'h1', m30: null } },
  }));
  await tick(); await tick();
  assert.equal(env._harness.renders.filter(r => r === 'chart').length, 1);
  assert.deepEqual({ ...env.viewState.analysisTokens }, { day: 'd1', m60: 'h1', m30: null },
    '主图响应的 analysis_tokens 原样保存，供 AI 请求使用');
  console.log('late chart response view gate checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
