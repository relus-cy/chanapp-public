'use strict';
/* 首开遇到 503（同一代码的历史规划、缓存刷新或重拉在途，服务端等锁超过预算且没有可服务快照）：
   页面受控自动重试——隔几秒重发同一视图的请求，次数有上限，用尽才报错；视图已切走不重试；
   502 等其他失败照常报错、不重试；手动重拉不走自动重试（目标 2026-09-29 第四阶段复核）。 */
const assert = require('node:assert/strict');
const { tick } = require('./support/dom.js');
const { loadEnv, okJson, errJson } = require('./support/app.js');

const BUSY = { detail: '正在更新这只标的，请稍后再试' };
const chartReqs = env => env._harness.pending.filter(r => r.url.startsWith('/api/chart?'));

function env() {
  const e = loadEnv({ state: { code: 'sz000004', freq: 'm30', ruleProfile: 'strict', signalScope: 'expanded',
                               watchlist: [], quotes: {} } });
  e._errors = [];
  e.showChartError = m => { e._errors.push(m); };   // 切片之后替换（loadEnv 注释的陷阱）
  e._delays = [];
  const timers = e._harness.timers;
  e.setTimeout = (fn, ms) => { timers.push(fn); e._delays.push(ms); return timers.length; };   // 记下延迟
  return e;
}

async function answer(req, body) {
  req.resolve(body);
  await tick(); await tick(); await tick();
}

// 最近一次登记的定时器（load 自己的超时定时器也在 timers 里，重试定时器登记在它之后）
const lastTimer = e => e._harness.timers[e._harness.timers.length - 1];

(async function () {
  // 503 → 不报错、提示稍后自动重试 → 定时器点火重发同一视图 → 200 正常渲染
  {
    const e = env();
    e.load();
    await answer(chartReqs(e)[0], errJson(503, BUSY));
    assert.deepEqual(e._errors, [], '503 首次不报错');
    assert.match(e.statusMsg || '', /自动重试/, '状态提示将自动重试');
    assert.equal(e._delays[e._delays.length - 1], 5000, '5 秒后重试');
    lastTimer(e)();
    const reqs = chartReqs(e);
    assert.equal(reqs.length, 2, '定时器点火后重发主图请求');
    assert.ok(reqs[1].url.includes('code=sz000004') && reqs[1].url.includes('freq=m30'), '重发同一视图');
    await answer(reqs[1], okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v1"'));
    assert.ok(e._harness.renders.includes('chart'), '重试成功后渲染');
    assert.deepEqual(e._errors, []);
  }

  // 一直 503：重试有上限，用尽后报错，不再登记重试
  {
    const e = env();
    e.load();
    let n = 0;
    while (chartReqs(e).length > n && n < 20) {
      await answer(chartReqs(e)[n], errJson(503, BUSY));
      n += 1;
      if (e._errors.length) break;
      lastTimer(e)();
    }
    assert.equal(n, 7, '首次加 6 次重试，实际请求 ' + n + ' 次');
    assert.equal(e._errors.length, 1, '用尽后报错一次');
    assert.match(e._errors[0], /正在更新/);
    const before = chartReqs(e).length;
    lastTimer(e)();
    assert.equal(chartReqs(e).length, before, '用尽后没有待发的重试');
  }

  // 等待重试期间切到别的代码：旧视图的重试不再发出
  {
    const e = env();
    e.load();
    await answer(chartReqs(e)[0], errJson(503, BUSY));
    const retry = lastTimer(e);
    e.state.code = 'sh600036';
    e.load();
    const before = chartReqs(e).length;
    retry();
    assert.equal(chartReqs(e).length, before, '已切走的视图不重试');
  }

  // 502 照常报错，不自动重试
  {
    const e = env();
    e.load();
    const timersBefore = e._harness.timers.length;
    await answer(chartReqs(e)[0], errJson(502, { detail: '行情暂不可用' }));
    assert.deepEqual(e._errors, ['行情暂不可用']);
    assert.equal(e._harness.timers.length, timersBefore, '502 不登记重试');
  }

  // 手动重拉遇到 503 照常报错（重拉结果另由 X-Refetch-Status 说明），不自动重试
  {
    const e = env();
    e.load({ refresh: true, refetch: true });
    const timersBefore = e._harness.timers.length;
    await answer(chartReqs(e)[0], errJson(503, BUSY));
    assert.equal(e._errors.length, 1);
    assert.equal(e._harness.timers.length, timersBefore, '重拉不登记自动重试');
  }
  console.log('ok - busy chart response is retried a bounded number of times');
})().catch(err => { console.error(err); process.exit(1); });
