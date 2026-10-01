'use strict';
/* 定时轮询行为断言：真实执行 60s 图表/60s 快照的 setInterval 回调（timers 切片），
   覆盖 document.hidden、交易时段、无选中标的三条分流。开市与否只取后端 /api/session
   （交易日历 + engine/session.py 时段），前端不再按浏览器时钟推算：
   60s 图表回调先取 /api/session 与 /api/periods 再决定是否刷新图表；报价回调用最近一次结果，按自选各标的的市场判断；
   取数失败保留上次结果，从未取到时按未开市处理。 */
const assert = require('node:assert/strict');
const { tick, runSlices } = require('./support/dom.js');
const { timerEnv, appContext, okJson, errJson } = require('./support/app.js');

const marketOf = c => (c && c.indexOf('hk') === 0 ? 'hk' : 'cn');
const markets = (cn, hk) => ({ checked_at: '2026-09-21T10:00:00', markets: { cn: { open: cn }, hk: { open: hk } } });

// 60s 回调并应答 /api/session；body 为 Error 时模拟网络失败，为数字时模拟 HTTP 错误
async function minute(env, body) {
  env._harness.intervals[0]();
  const req = env._harness.pending.shift();
  assert.ok(req, '60s 回调应请求 /api/session');
  assert.equal(req.url, '/api/session');
  if (body instanceof Error) req.reject(body);
  else if (typeof body === 'number') req.resolve(errJson(body));
  else req.resolve(okJson(body));
  await tick(); await tick();
  const prefs = env._harness.pending.shift();
  assert.equal(prefs.url, '/api/periods', '休市也同步服务端偏好');
  prefs.resolve(okJson(env.periodPrefs));
  await tick(); await tick();
}

(async function () {
  // hidden 守卫：两个回调都必须先行返回，renderStatus/任何请求不得发生
  {
    const env = timerEnv({ marketOf });
    env.document.hidden = true;
    assert.equal(env._harness.intervals.length, 2, '两个定时回调都已注册');
    for (const cb of env._harness.intervals) cb();
    assert.equal(env._harness.renders.length, 0, '后台标签下 renderStatus/load/loadQuotes 全部不触发');
    assert.equal(env._harness.pending.length, 0, '后台标签下不请求 /api/session');
  }

  // 从未取到开市状态：报价按未开市不刷新
  {
    const env = timerEnv({ marketOf });
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, [], '未知时按未开市处理');
  }

  // 后端判开市（cn）：60s → 状态保鲜 + load({refresh:true})；之后报价回调 → loadQuotes()
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036' }];
    await minute(env, markets(true, false));
    assert.deepEqual(env._harness.renders, ['status', 'load'], '60s 回调先保鲜状态再刷新图表');
    env._harness.renders.length = 0;
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, ['quotes'], '报价回调只刷快照');
    assert.equal(env._harness.pending.length, 0, '定时回调已完成 session 与周期偏好读取');
  }

  // 图表按当前标的的市场取值：港股开、A 股休
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'hk00700';
    await minute(env, markets(false, true));
    assert.deepEqual(env._harness.renders, ['status', 'load'], '港股按 hk 结果刷新');
    env.state.code = 'sh600036';
    env._harness.renders.length = 0;
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, [], '自选里没有开市市场的标的时不刷报价');
  }

  // 有意改写（目标 2026-09-29 第三阶段）：图表与报价统一 60 秒
  {
    const env = timerEnv({ marketOf });
    assert.deepEqual(env._harness.intervalMs, [60000, 60000], '图表与报价都是 60 秒一轮');
  }

  // 报价按各自选标的的市场判断：所选 A 股休市不停港股自选的报价；没有选中标的也照常
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036' }, { code: 'hk00700' }];
    await minute(env, markets(false, true));
    env._harness.renders.length = 0;
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, ['quotes'], '港股自选开市时照常刷报价');
    env.state.code = null;
    env._harness.renders.length = 0;
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, ['quotes'], '没有选中标的也刷自选报价');
  }

  // 后端判休市（含交易日历判定的节假日）：只保鲜状态，不发刷新
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    await minute(env, markets(false, false));
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, ['status'], '休市时两回调均不发刷新');
  }

  // 取数失败（网络或 HTTP 错误）保留上次结果
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    await minute(env, markets(true, false));
    env._harness.renders.length = 0;
    await minute(env, new Error('offline'));
    assert.deepEqual(env._harness.renders, ['status', 'load'], '网络失败沿用上次「开市」');
    env._harness.renders.length = 0;
    await minute(env, 503);
    assert.deepEqual(env._harness.renders, ['status', 'load'], 'HTTP 错误沿用上次「开市」');
    env._harness.renders.length = 0;
    await minute(env, markets(false, false));
    assert.deepEqual(env._harness.renders, ['status'], '取到新结果后按新结果');
  }

  // 请求未返回时不重复发起，返回后也只刷新一次图表
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    env._harness.intervals[0]();
    env._harness.intervals[0]();
    assert.equal(env._harness.pending.length, 1, '上一轮 /api/session 未返回时不叠加请求');
    env._harness.pending.shift().resolve(okJson(markets(true, false)));
    await tick(); await tick();
    env._harness.pending.shift().resolve(okJson(env.periodPrefs));
    await tick(); await tick();
    assert.deepEqual(env._harness.renders, ['status', 'load'], '重叠的 60s 回调不重复刷新图表');
  }

  // 无选中标的（state.code=null）：取状态但不发刷新
  {
    const env = timerEnv({ marketOf });
    env.state.code = null;
    await minute(env, markets(true, true));
    env._harness.intervals[1]();
    assert.deepEqual(env._harness.renders, ['status'], '无选中标的且自选为空时两回调均不发刷新');
  }

  // 收盘后有限跟进：图表快照还是盘中（phase=live）而市场已收盘时补读一次，之后快照为待定稿/已定稿即停
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    env.lastMeta = { coverage: { data_status: { phase: 'live', day: '2026-09-29', at: '2026-09-29 15:00' } } };
    await minute(env, markets(false, false));
    assert.deepEqual(env._harness.renders, ['status', 'load'], '收盘后补读一次，拿到收盘后的状态');
    env._harness.renders.length = 0;
    env.lastMeta = { coverage: { data_status: { phase: 'awaiting_final', day: '2026-09-29', at: '2026-09-29 15:01' } } };
    await minute(env, markets(false, false));
    assert.deepEqual(env._harness.renders, ['status'], '收盘后的快照到手即停，不常驻轮询');
  }

  // 第三阶段复审应修 6：补读失败（快照仍是 live）时不每分钟重试；每次打开（代码/周期）只跟进一次
  {
    const env = timerEnv({ marketOf });
    env.state.code = 'sh600036';
    env.state.freq = 'm30';
    env.lastMeta = { coverage: { data_status: { phase: 'live', day: '2026-09-29', at: '2026-09-29 15:00' } } };
    await minute(env, markets(false, false));
    assert.deepEqual(env._harness.renders, ['status', 'load'], '收盘后补读一次');
    env._harness.renders.length = 0;
    await minute(env, markets(false, false));          // 补读失败：lastMeta 仍是 live
    assert.deepEqual(env._harness.renders, ['status'], '补读失败不常驻轮询');
    env._harness.renders.length = 0;
    env.state.code = 'sz000002';                        // 打开另一只：它自己的一次跟进
    await minute(env, markets(false, false));
    assert.deepEqual(env._harness.renders, ['status', 'load'], '新打开的代码各有一次跟进');
  }

  // 第三阶段第二轮复审应修 5：跟进按「这次打开」与交易日计——同一代码重新打开（openSeq 变）再跟进一次；
  // A→B→A 各一次；开市回调不重置，新的收盘日是新的一次
  {
    const env = timerEnv({ marketOf });
    const live = day => ({ coverage: { data_status: { phase: 'live', day, at: day + ' 15:00' } } });
    env.state.code = 'sh600036'; env.state.freq = 'm30'; env.state.openSeq = 1;
    env.lastMeta = live('2026-09-29');
    await minute(env, markets(false, false));
    await minute(env, markets(false, false));
    env.state.openSeq = 2;                             // 同一代码重新打开
    await minute(env, markets(false, false));
    env.state.code = 'sz000002'; env.state.openSeq = 3;
    await minute(env, markets(false, false));
    env.state.code = 'sh600036'; env.state.openSeq = 4;
    await minute(env, markets(false, false));
    await minute(env, markets(true, false));           // 开市：正常刷新
    env.lastMeta = live('2026-09-29');                 // 开市回调没拿到新快照
    await minute(env, markets(false, false));
    env.lastMeta = live('2026-09-30');                 // 次日收盘
    await minute(env, markets(false, false));
    assert.equal(env._harness.renders.filter(r => r === 'load').length, 6,
      '打开 1、重开 2、B、再开 A 各一次，开市一次，次日收盘一次：' + env._harness.renders.join(','));
  }

  // 状态栏「交易中 / 已收盘」取同一份后端结果
  {
    const env = appContext({ marketOf });
    runSlices(env, ['status', 'timers']);
    env.state.code = 'hk00700';
    env.renderStatus();
    assert.match(env.el('status').innerHTML, /已收盘/, '未取到前显示已收盘');
    env._harness.intervals[0]();
    env._harness.pending.shift().resolve(okJson(markets(false, true)));
    await tick(); await tick();
    assert.match(env.el('status').innerHTML, /交易中/, '取到港股开市后显示交易中');
  }
  console.log('poll visibility UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

// demo 生命周期不会变成真实模式：首次获取后连 session 也停止轮询。
(async function () {
  const env = timerEnv({ marketOf });
  env.state.code = 'sh600036';
  env.state.watchlist = [{code: 'sh600036'}];
  await minute(env, {...markets(false, false), mode: 'demo'});
  env._harness.renders.length = 0;
  for (let i = 0; i < 3; i++) for (const cb of env._harness.intervals) cb();
  await tick();
  assert.equal(env._harness.pending.length, 0, 'demo 不再轮询 session 或行情');
  assert.deepEqual(env._harness.renders, []);
})().catch(error => { console.error(error); process.exitCode = 1; });

// 打开的 demo 页面期间服务按真实模式重启：回到前台核对一次模式，确认为真实后恢复轮询。
(async function () {
  const listeners = {};
  const env = appContext({ marketOf });
  env.document.addEventListener = (type, fn) => { listeners[type] = fn; };
  runSlices(env, ['timers']);
  env.state.code = 'sh600036';
  env.state.watchlist = [{code: 'sh600036'}];
  await minute(env, {...markets(false, false), mode: 'demo'});
  listeners.visibilitychange();
  const session = env._harness.pending.find(r => r.url === '/api/session');
  assert.ok(session, 'demo 页面回到前台核对一次模式');
  session.resolve(okJson({...markets(true, false), mode: 'real'}));
  env._harness.pending.find(r => r.url === '/api/periods').resolve(okJson(env.periodPrefs));
  await tick(); await tick();
  env._harness.pending.length = 0;
  env._harness.renders.length = 0;
  env._harness.intervals[0]();
  assert.equal(env._harness.pending[0] && env._harness.pending[0].url, '/api/session', '确认真实模式后恢复轮询');
})().catch(error => { console.error(error); process.exitCode = 1; });
