'use strict';
const assert = require('node:assert/strict');
const { runSlices } = require('./support/dom.js');

function run(items, code, opts) {
  const calls = [];
  const context = {
    state: { code: code, watchlist: [] },
    queueWatchlist: function (fn) { return fn(); },
    fetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve(items); } }); },
    renderWatchlist: function () { calls.push('render'); },
    renderF10Header: function () {},   // 自选列表到达即重绘价格卡（第三阶段第八轮复审）；这里只看加载顺序
    load: function () { calls.push('load'); },
    setStatus: function () {},
  };
  runSlices(context, ['loadWatchlist']);
  return context.loadWatchlist(opts).then(function () { return { calls: calls, code: context.state.code }; });
}
const items = [{ code: 'sh000001' }, { code: 'sz000001' }];
Promise.all([
  run(items, null).then(function (r) { assert.deepEqual(r.calls, ['render', 'load']); assert.equal(r.code, 'sh000001'); }),
  run(items, 'sz000001', { skipLoad: true }).then(function (r) { assert.deepEqual(r.calls, ['render'], '预选 code 在列表中：不再重复 load'); assert.equal(r.code, 'sz000001'); }),
  // 有意改写（2026-09-29 第二阶段）：非自选代码是合法的「查看」，预选后不再被纠正到自选首项
  run(items, 'hk00700', { skipLoad: true }).then(function (r) { assert.deepEqual(r.calls, ['render'], '预选 code 不在列表：保持查看，不重复 load'); assert.equal(r.code, 'hk00700'); }),
  run([], null).then(function (r) { assert.deepEqual(r.calls, ['render'], '空自选且无预选：不 load'); assert.equal(r.code, null); }),
  run(items, 'sz000001').then(function (r) { assert.deepEqual(r.calls, ['render', 'load'], '无 skipLoad 保持旧行为'); }),
]).then(function () {
  // 启动序：交易时段、周期偏好与 watchlist 并发；load 自身在偏好确认前不发 chart，随后拉行情。
  for (const code of ['sz000001', null]) {
    const calls = [];
    const context = {
      state: { code },
      renderTabs() { calls.push('tabs'); }, renderStatus() { calls.push('status'); },
      load() { calls.push('load'); }, loadWatchlist(o) { calls.push('watchlist:' + !!(o && o.skipLoad)); },
      loadQuotes() { calls.push('quotes'); }, refreshSession() { calls.push('session'); },
      loadRecent() { calls.push('recent'); },
      loadPeriodPrefs() { calls.push('periods'); },
      recordView(c) { calls.push('view:' + c); },
      fetch(url) { calls.push('fetch:' + url); return Promise.resolve({ ok: false }); },
    };
    runSlices(context, ['boot']);
    assert.deepEqual(calls, code
      ? ['tabs', 'status', 'session', 'periods', 'load', 'view:sz000001', 'watchlist:true', 'quotes', 'recent']
      : ['tabs', 'status', 'session', 'periods', 'watchlist:false', 'quotes', 'recent'], '启动并发读取周期偏好与其它状态；最近查看随启动取一次；URL 预选的代码是一次显式打开，记查看');
  }
  console.log('bootstrap order UI checks passed');
}).catch(function (e) { console.error(e); process.exit(1); });
