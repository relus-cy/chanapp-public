'use strict';
/* 搜索查看与自选跟踪分开（目标 2026-09-29 第二阶段，页面侧）。
   实现前列出的失败方式：
   - 搜索选中（点击/回车）仍加入自选，而不是打开图表并记一次查看；
   - 搜索结果没有显式「加入自选」控件，或该控件同时打开图表/记查看；已在自选的结果仍能重复加入；
   - 打开时不记查看，或记下的周期/复权/名称与当前视图不符（接口按 ViewItem 校验，错了静默 422）；
   - 记录失败打断图表或弹错误；自动刷新、整窗重载也记成「查看」；
   - 侧栏「最近查看」不按 /api/views 渲染、不标已自选、点击不打开、切换不持久；计数仍显示自选数；
   - 当前看的是非自选代码时，页面没有「加入自选」入口；左拉到本地最早数据时不提示加入自选补完整历史；
   - 手动重拉没有带 refetch=1，或仍带条件请求头被 304 吞掉。
   阶段评审（astra）补充：
   - 重拉无论服务端结果都提示「已重拉」（空返回、部分失败也报成功）；
   - 无当前代码时加入首个自选会自动打开，但不记查看（URL 预选见 test_bootstrap_order_ui.js）；
   - 反向：启动时自动落到自选首项不是用户选择，记成查看会让每次打开页面都把它顶到最近查看最前。
   不在本文件：预选非自选代码不被纠正（test_bootstrap_order_ui.js）、移出当前代码保持查看
   （test_watchlist_interactions_ui.js）、搜索键盘路径（test_continuous_ops_ui.js B 段）。 */
const assert = require('node:assert/strict');
const { mkEl, deferred, tick, runSlices } = require('./support/dom.js');
const { appContext, loadEnv, searchEnv, okJson } = require('./support/app.js');

function flush() {
  return Promise.resolve().then(() => Promise.resolve()).then(() => tick());
}

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

const CANDS = [{ code: 'sh600519', name: '贵州茅台' }, { code: 'sz000858', name: '五粮液' }];

async function searched(extras) {
  const env = searchEnv(Object.assign({
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(CANDS) }),
  }, extras));
  const { input } = env._harness.search;
  input.value = '酒'; input.oninput(); env._harness.timers[0]();
  await flush();
  return env;
}

check('search Enter and click open the chart without joining the watchlist', async () => {
  const env = await searched();
  const { input, drop } = env._harness.search;
  input.onkeydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
  assert.deepEqual(env._harness.opened, [['sh600519', '贵州茅台']], 'Enter opens the first candidate');
  assert.deepEqual(env._harness.added, [], 'Enter does not join the watchlist');
  assert.equal(drop.hidden, true);

  input.value = '酒'; input.oninput(); env._harness.timers[1]();
  await flush();
  drop.children[1].onmousedown({ preventDefault() {} });
  assert.deepEqual(env._harness.opened[1], ['sz000858', '五粮液'], 'clicking a candidate opens it');
  assert.deepEqual(env._harness.added, []);
});

check('each candidate has an explicit add control that joins without opening', async () => {
  const env = await searched({ state: { code: null, freq: 'day', watchlist: [{ code: 'sz000858', name: '五粮液' }], quotes: {} } });
  const { drop } = env._harness.search;
  const add = drop.children[0].children.find(n => n.classList.contains('c-add'));
  assert.ok(add, 'candidate carries an add control');
  assert.equal(add.tagName, 'button');
  assert.match(add.attrs['aria-label'] || '', /加入自选/);
  let stopped = false;
  add.onmousedown({ preventDefault() {}, stopPropagation() { stopped = true; } });
  assert.ok(stopped, 'add does not bubble into the open action');
  assert.deepEqual(env._harness.added, [['sh600519', '贵州茅台']]);
  assert.deepEqual(env._harness.opened, [], 'add does not open the chart');
  assert.equal(drop.hidden, true);

  const env2 = await searched({ state: { code: null, freq: 'day', watchlist: [{ code: 'sz000858', name: '五粮液' }], quotes: {} } });
  const watched = env2._harness.search.drop.children[1].children.find(n => n.classList.contains('c-add'));
  assert.equal(watched.disabled, true, 'already watched candidate cannot be added again');
  assert.match(watched.textContent, /已自选/);
  watched.onmousedown({ preventDefault() {}, stopPropagation() {} });
  assert.deepEqual(env2._harness.added, []);
});

function opsEnv(extras) {
  const requests = [];
  const context = appContext(Object.assign({
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
  }, extras));
  runSlices(context, ['watchlistOps']);
  context._harness.requests = requests;
  return context;
}

check('opening records one view with the current period and adjust, then refreshes recent', async () => {
  const context = opsEnv({ state: { code: null, freq: 'm60', watchlist: [], quotes: {}, recent: [] } });
  context.viewState.adjust = 'raw';
  context.openCode('sz000002', '万科A');
  const { requests, renders } = context._harness;
  assert.equal(context.state.code, 'sz000002');
  assert.ok(renders.includes('load'), 'chart loads');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/views');
  assert.equal(requests[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(requests[0].options.body),
    { code: 'sz000002', name: '万科A', freq: 'm60', adjust: 'raw' });
  requests[0].resolve({ ok: true, json: async () => ({ ok: true }) });
  await flush();
  assert.equal(requests[1].url, '/api/views', 'recent list refreshed after recording');
  assert.ok(!requests[1].options || !requests[1].options.method || requests[1].options.method === 'GET');
  requests[1].resolve({ ok: true, json: async () => ({ recent: [{ code: 'sz000002', name: '万科A', watched: false }] }) });
  await flush();
  assert.deepEqual(context.state.recent.map(r => r.code), ['sz000002']);
  assert.ok(!requests.some(r => /\/api\/watchlist/.test(r.url)), 'viewing never touches the watchlist');
});

check('a failed view record is silent and leaves the chart alone', async () => {
  const context = opsEnv({ state: { code: null, freq: 'day', watchlist: [], quotes: {}, recent: [] } });
  context.openCode('sz000002', '万科A');
  context._harness.requests[0].reject(new Error('offline'));
  await flush();
  assert.equal(context.state.code, 'sz000002');
  assert.deepEqual(context._harness.statuses.filter(Boolean), [], 'no error status for a log write');
});

check('chart loads and refreshes never record views', async () => {
  const env = loadEnv();
  env.load();
  env.load({ refresh: true });
  env.load({ refresh: true, refetch: true });
  assert.ok(env._harness.pending.length >= 3);
  assert.ok(!env._harness.pending.some(p => /\/api\/views/.test(p.url)));
});

check('manual refetch asks the server to refetch and bypasses the conditional cache', async () => {
  const env = loadEnv({ state: { code: 'sz000002', freq: 'day', ruleProfile: 'strict', signalScope: 'expanded', watchlist: [], quotes: {} } });
  env.rememberEtag('sz000002', 'day', 'qfq', 'strict', 'expanded', 'W/"x"');
  env.load({ refresh: true });
  assert.doesNotMatch(env._harness.pending[0].url, /refetch/);
  assert.equal(env._harness.pending[0].options.headers['If-None-Match'], 'W/"x"');
  env.refetchCurrent();
  const last = env._harness.pending[env._harness.pending.length - 1];
  assert.match(last.url, /[?&]refetch=1(&|$)/);
  assert.equal(last.options.headers['If-None-Match'], undefined, 'refetch must not be answered by 304');
  const before = env._harness.pending.length;
  env.state.code = null;
  env.refetchCurrent();
  assert.equal(env._harness.pending.length, before, 'nothing to refetch without a code');
});

function chartReply(status) {
  return { ok: true, status: 200, headers: { get: k => (k === 'X-Refetch-Status' ? status : null) },
    json: async () => ({ kline: [], meta: {} }) };
}

check('refetch notice follows the server-reported result', async () => {
  const shown = {};
  for (const status of ['ok', 'partial', 'failed', 'busy', 'disabled', null]) {
    const env = loadEnv({ state: { code: 'sz000002', freq: 'day', ruleProfile: 'strict', signalScope: 'expanded', watchlist: [], quotes: {} } });
    const notices = [];
    env.showNotice = msg => notices.push(msg);
    env.refetchCurrent();
    env._harness.pending[env._harness.pending.length - 1].resolve(chartReply(status));
    await flush();
    shown[status] = notices.join('|');
  }
  assert.match(shown.ok, /已重拉/);
  for (const status of ['partial', 'failed', 'busy', 'disabled', 'null']) {
    assert.doesNotMatch(shown[status], /^已重拉当前窗口$/, status + ' must not claim success');
    assert.ok(shown[status], status + ' still tells the user something');
  }
  assert.match(shown.partial, /部分/);
  assert.match(shown.failed, /失败/);
  assert.match(shown.busy, /稍后/, 'busy says the refetch did not run and can be retried');
});

check('adding the first watch item with nothing open records it as a view', async () => {
  const context = opsEnv({ state: { code: null, freq: 'day', watchlist: [], quotes: {}, recent: [] } });
  context.addWatch('sz000002', '万科A', null);
  const { requests } = context._harness;
  await flush();
  requests[0].resolve({ ok: true, status: 200, json: async () => [{ code: 'sz000002', name: '万科A' }] });
  await flush();
  assert.equal(context.state.code, 'sz000002');
  const view = requests.find(r => r.url === '/api/views' && r.options && r.options.method === 'POST');
  assert.ok(view, 'auto-opened first watch item is recorded as a view');
  assert.equal(JSON.parse(view.options.body).code, 'sz000002');
});

check('watchlist boot fallback to the first item is not recorded as a view', async () => {
  const context = opsEnv({ state: { code: null, freq: 'day', watchlist: [], quotes: {}, recent: [] } });
  context.loadWatchlist();
  const { requests } = context._harness;
  await flush();
  requests[0].resolve({ ok: true, status: 200, json: async () => [{ code: 'sh600036', name: '招商银行' }] });
  await flush();
  assert.equal(context.state.code, 'sh600036', 'boot still shows the first watched code');
  assert.ok(!requests.some(r => r.url === '/api/views'), 'automatic fallback is not a user view');
});

function sidebarEnv(state) {
  const nodes = { wlItems: mkEl('div'), wlForm: mkEl('div') };
  const context = opsEnv({ state: Object.assign({ freq: 'day', quotes: {} }, state), wireSearch() {} });
  context.document.getElementById = id => nodes[id] || (nodes[id] = mkEl());
  context.el = context.document.getElementById;
  runSlices(context, ['watchlistUi', 'watchlistOps']);
  context._harness.nodes = nodes;
  return context;
}

const RECENT = [
  { code: 'sz000002', name: '万科A', freq: 'm60', adjust: 'qfq', first_viewed_at: '2026-09-28T10:00:00',
    last_viewed_at: '2026-09-29T14:05:00', views: 2, watched: false },
  { code: 'sh600000', name: '浦发银行', freq: 'day', adjust: 'qfq', first_viewed_at: '2026-09-27T09:40:00',
    last_viewed_at: '2026-09-28T09:40:00', views: 1, watched: true },
];

check('recent tab lists viewed codes, marks watched ones and opens on click', async () => {
  const context = sidebarEnv({ code: 'sh600000', watchlist: [{ code: 'sh600000', name: '浦发银行' }],
    recent: RECENT, sideTab: 'recent' });
  const { nodes, requests } = context._harness;
  context.renderWatchlist();
  const rows = nodes.wlItems.children;
  assert.deepEqual(rows.map(r => r.dataset.code), ['sz000002', 'sh600000']);
  assert.match(rows[0].innerHTML, /万科A/);
  assert.match(rows[0].innerHTML, /09-29 14:05/, 'row shows when it was last viewed');
  assert.match(rows[0].innerHTML, /class="add"/, 'unwatched row offers add');
  assert.doesNotMatch(rows[1].innerHTML, /class="add"/, 'watched row has no add control');
  assert.match(rows[1].innerHTML, /已自选/);
  assert.ok(rows[1].classList.contains('active'), 'current code highlighted');
  assert.equal(nodes.wlCount.textContent, '02', 'count follows the visible list');

  rows[0].querySelector('.add').onclick({ stopPropagation() {} });
  await flush();
  assert.equal(requests[0].url, '/api/watchlist');
  assert.deepEqual(JSON.parse(requests[0].options.body), { code: 'sz000002', name: '万科A' });
  assert.equal(context.state.code, 'sh600000', 'adding from recent does not switch the chart');
  requests[0].resolve({ ok: true, json: async () => [{ code: 'sh600000', name: '浦发银行' }, { code: 'sz000002', name: '万科A' }] });
  await flush();
  assert.doesNotMatch(nodes.wlItems.children[0].innerHTML, /class="add"/,
    'watched mark follows the watchlist, not the stale server flag');

  nodes.wlItems.children[0].onclick();
  assert.equal(context.state.code, 'sz000002');
  assert.ok(requests.some(r => r.url === '/api/views' && r.options && r.options.method === 'POST'),
    'opening from recent records a view');
});

check('side tab switch persists, refreshes recent and restores the watchlist', async () => {
  const context = sidebarEnv({ code: null, watchlist: [{ code: 'sh600000', name: '浦发银行' }],
    recent: RECENT, sideTab: 'watch' });
  const { nodes, requests } = context._harness;
  context.renderWatchlist();
  assert.deepEqual(nodes.wlItems.children.map(r => r.dataset.code), ['sh600000']);
  assert.equal(nodes.wlCount.textContent, '01');
  assert.equal(nodes.wlTabWatch.attrs['aria-selected'], 'true');
  assert.equal(nodes.wlTabRecent.attrs['aria-selected'], 'false');

  nodes.wlTabRecent.onclick();
  assert.equal(context.state.sideTab, 'recent');
  assert.equal(context.localStorage.getItem('chanapp-side-tab'), 'recent');
  assert.deepEqual(nodes.wlItems.children.map(r => r.dataset.code), ['sz000002', 'sh600000']);
  assert.equal(nodes.wlTabRecent.attrs['aria-selected'], 'true');
  assert.equal(requests.length, 1, 'switching to recent refreshes it');
  assert.equal(requests[0].url, '/api/views');

  nodes.wlTabWatch.onclick();
  assert.equal(context.localStorage.getItem('chanapp-side-tab'), 'watch');
  assert.deepEqual(nodes.wlItems.children.map(r => r.dataset.code), ['sh600000']);
});

check('viewing an unwatched code offers an explicit add in the header', async () => {
  const context = sidebarEnv({ code: 'sz000002', watchlist: [{ code: 'sh600000', name: '浦发银行' }],
    recent: RECENT, sideTab: 'watch' });
  const { nodes, requests } = context._harness;
  context.renderWatchlist();
  assert.equal(nodes.trackBtn.hidden, false, 'unwatched code shows the add entry');
  assert.match(nodes.trackBtn.title, /加入自选以补完整历史/);
  nodes.trackBtn.onclick();
  await flush();
  assert.deepEqual(JSON.parse(requests[0].options.body), { code: 'sz000002', name: '万科A' });
  requests[0].resolve({ ok: true, json: async () => [{ code: 'sh600000', name: '浦发银行' }, { code: 'sz000002', name: '万科A' }] });
  await flush();
  assert.equal(nodes.trackBtn.hidden, true, 'watched code hides the add entry');
  context.state.code = null;
  context.renderWatchlist();
  assert.equal(nodes.trackBtn.hidden, true, 'no code, no add entry');
});

check('left edge of an unwatched code hints that joining fills full history, once per view', async () => {
  function edgeEnv(watched) {
    const env = loadEnv({ klineData: [{ time: '2025-01-02' }] });
    env.state.code = 'sz000002';
    env.state.watchlist = watched ? [{ code: 'sz000002', name: '万科A' }] : [];
    env.viewState.token = 't1';
    env.historyState = { loading: false, hasMore: false, reqId: 0 };
    const real = env.setStatus;                       // status 切片的真 setStatus 不记录：包一层收集
    env.setStatus = msg => { env._harness.statuses.push(msg); real(msg); };
    return env;
  }
  const env = edgeEnv(false);
  env.onMainRangeChanged({ from: 2, to: 60 });
  env.onMainRangeChanged({ from: 1, to: 59 });
  const hints = env._harness.statuses.filter(s => /加入自选以补完整历史/.test(s || ''));
  assert.equal(hints.length, 1, 'hint shown once for this view');
  assert.equal(env._harness.pending.length, 0, 'no network request to widen history');
  env.viewState.token = 't2';
  env.onMainRangeChanged({ from: 0, to: 58 });
  assert.equal(env._harness.statuses.filter(s => /加入自选以补完整历史/.test(s || '')).length, 1,
    'a refreshed token of the same view does not repeat the hint');
  env.state.freq = 'm60';
  env.onMainRangeChanged({ from: 0, to: 58 });
  assert.equal(env._harness.statuses.filter(s => /加入自选以补完整历史/.test(s || '')).length, 2,
    'another period is a new view and may hint again');

  const watchedEnv = edgeEnv(true);
  watchedEnv.onMainRangeChanged({ from: 2, to: 60 });
  assert.ok(!watchedEnv._harness.statuses.some(s => /加入自选/.test(s || '')), 'watched code gets no hint');
});

(async () => {
  let failures = 0;
  for (const item of checks) {
    try {
      await item.fn();
      console.log('ok - ' + item.name);
    } catch (error) {
      failures += 1;
      console.error('not ok - ' + item.name);
      console.error(error.stack || error);
    }
  }
  if (failures) process.exitCode = 1;
})();
