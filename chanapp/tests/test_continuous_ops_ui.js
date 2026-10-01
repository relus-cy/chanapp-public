'use strict';
/* 批1 连续操作：刷新区间保持 / 搜索 combobox 键盘路径 / 搜索表单焦点保持 /
   旧数据归属标记（classList 断言）与重试 / 自选股行键盘可达 / load() 区间接线 */
const assert = require('node:assert/strict');
const { mkEl, tick, runSlices } = require('./support/dom.js');
const { appContext, loadEnv, searchEnv, chartStub, okJson, errJson } = require('./support/app.js');

// ---------- A. refreshRangePlan：同标的刷新保留可视区间 ----------
{
  const context = appContext();
  runSlices(context, ['helpers']);
  const plan = context.refreshRangePlan;
  const capture = context.captureRefreshRange;
  const j = v => JSON.stringify(v);
  const oldBars = Array.from({ length: 520 }, (_, i) => ({ time: 't' + i }));
  const slidBars = oldBars.slice(1).concat({ time: 't520' });
  assert.equal(plan(null, oldBars), null, 'switch/first load resets');

  // 固定 520 根滑窗移除首根后，历史视口按日期锚点左移一格，并保留跨度与小数偏移
  const history = capture({ from: 140.25, to: 190.75 }, oldBars);
  assert.equal(j(plan(history, slidBars)), j({ from: 139.25, to: 189.75 }), 'history range follows its time anchor');

  // 停在最新端时仅在末根真的变化后跟随；同批数据的轻微滚动不得反复拉回 +3
  const latest = capture({ from: 400.2, to: 522 }, oldBars);
  assert.equal(j(plan(latest, slidBars)), j({ from: 400.2, to: 522 }), 'latest edge follows a new bar in fixed window');
  const nearLatest = capture({ from: 400.2, to: 518.8 }, oldBars);
  assert.equal(j(plan(nearLatest, oldBars)), j({ from: 400.2, to: 518.8 }), 'unchanged data preserves user scroll exactly');
  const appendedBars = oldBars.concat({ time: 't520' });
  assert.equal(j(plan(latest, appendedBars)), j({ from: 401.2, to: 523 }), 'latest edge follows appended bar and preserves span');

  // 锚点已从新窗口淘汰时，明确夹到左边界并保留跨度
  const missing = capture({ from: 0.25, to: 50.75 }, oldBars);
  assert.equal(j(plan(missing, oldBars.slice(100))), j({ from: 0, to: 50.5 }), 'missing anchor clamps to available data');
  console.log('A. refreshRangePlan checks passed');
}

// ---------- B. wireSearch：combobox 键盘路径闭合 ----------
(async function () {
  const cands = [{ code: 'sh600519', name: '贵州茅台' }, { code: 'sz000858', name: '五粮液' }];
  const env = searchEnv({ fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(cands) }) });
  const { input, drop } = env._harness.search;
  const timers = env._harness.timers;
  // 有意改写（2026-09-29 第二阶段）：搜索选中改为「查看」，回车打开候选而不再加入自选
  const opened = env._harness.opened, added = env._harness.added;

  assert.equal(input.attrs.role, 'combobox', 'input exposes combobox role');
  assert.equal(input.attrs['aria-expanded'], 'false', 'dropdown starts collapsed');

  input.value = '茅台';
  input.oninput();
  assert.equal(timers.length, 1, 'debounce scheduled');
  timers[0]();
  await tick();
  assert.equal(drop.hidden, false, 'candidates shown');
  assert.equal(drop.children.length, 2);
  assert.equal(input.attrs['aria-expanded'], 'true');

  const key = k => input.onkeydown({ key: k, preventDefault() {} });
  key('ArrowDown');
  assert.equal(drop.children[0].classList.contains('active'), true, 'first candidate active');
  key('ArrowDown');
  assert.equal(drop.children[1].classList.contains('active'), true, 'second candidate active');
  key('ArrowDown');
  assert.equal(drop.children[1].classList.contains('active'), true, 'clamps at last candidate');
  key('ArrowUp');
  assert.equal(drop.children[0].classList.contains('active'), true, 'arrow up moves back');

  key('Enter');
  assert.deepEqual(opened, [['sh600519', '贵州茅台']], 'enter opens active candidate');
  assert.deepEqual(added, [], 'enter does not join the watchlist');
  assert.equal(drop.hidden, true, 'dropdown closes after open');

  // 无候选激活时 Enter 取首个候选
  input.value = '茅台'; input.oninput(); timers[1]();
  await tick();
  key('Enter');
  assert.deepEqual(opened[1], ['sh600519', '贵州茅台'], 'enter without active picks first candidate');
  console.log('B. search combobox keyboard checks passed');
})().catch(e => { console.error(e); process.exit(1); });

// ---------- C. renderWatchlist：行情刷新不重建搜索表单 ----------
{
  const nodes = { wlItems: mkEl('div'), wlForm: mkEl('div') };
  let wires = 0;
  const context = appContext({
    state: { watchlist: [], quotes: {}, code: null },
    wireSearch: () => { wires++; },
  });
  context.document.getElementById = id => nodes[id] || (nodes[id] = mkEl());
  runSlices(context, ['watchlistUi']);
  context.renderWatchlist();
  assert.equal(wires, 1, 'first render builds the form once');
  const form = nodes.wlForm.firstChild;
  context.renderWatchlist();
  context.renderWatchlist();
  assert.equal(wires, 1, 'quote refresh must not rebuild the search form');
  assert.equal(nodes.wlForm.firstChild, form, 'form instance (input value and focus) survives refresh');
  console.log('C. search form preservation checks passed');
}

// ---------- D. 旧数据归属标记（ctx-old）与就地重试 ----------
{
  const chartError = mkEl('div');
  let loads = 0;
  const context = appContext({
    el: id => id === 'chartError' ? chartError : (context._harness.nodes[id] || (context._harness.nodes[id] = mkEl())),
    load: () => { loads++; },
  });
  runSlices(context, ['status']);
  context.showChartError('HTTP 502');
  assert.equal(chartError.hidden, false);
  const retry = chartError.children.find(ch => ch.tagName === 'button');
  assert.ok(retry, 'chart error offers an in-place retry button');
  retry.click();
  assert.equal(loads, 1, 'retry button triggers load');
  console.log('D. chart error retry checks passed');
}
(async function () {
  // ctx-old 行为断言：切换（非 refresh）打标记，成功后清除；后台 refresh 不打标记；失败保留标记
  {
    const env = loadEnv();
    env.load();
    assert.equal(env.el('center').classList.contains('ctx-old'), true, '切换时旧内容标记为 ctx-old');
    assert.equal(env.el('rail').classList.contains('ctx-old'), true, 'rail 同步标记');
    env._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }));
    await tick(); await tick();
    assert.equal(env.el('center').classList.contains('ctx-old'), false, '成功提交后清除 ctx-old');
    assert.equal(env.el('rail').classList.contains('ctx-old'), false);
  }
  {
    const env = loadEnv();
    env.load({ refresh: true });
    assert.equal(env.el('center').classList.contains('ctx-old'), false, '后台 refresh 不标记旧数据');
    env._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }));
    await tick(); await tick();
  }
  {
    const env = loadEnv();
    env.load();
    assert.equal(env.el('center').classList.contains('ctx-old'), true);
    env._harness.pending[0].resolve(errJson(502, { detail: 'x' }));
    await tick(); await tick();
    assert.equal(env.el('center').classList.contains('ctx-old'), true, '失败时旧数据标记保留');
  }
  console.log('D. stale context class checks passed');
})().catch(e => { console.error(e); process.exit(1); });

// ---------- E. 自选股行键盘可达：可聚焦、Enter/空格激活 ----------
{
  const nodes = { wlItems: mkEl('div'), wlForm: mkEl('div') };
  const calls = [];
  const context = appContext({
    state: { watchlist: [{ code: 'sz001309', name: '德明利', starred: false }], quotes: {}, code: 'sh000001' },
    wireSearch() {},
  });
  // 有意改写（2026-09-29 第二阶段）：行激活统一走 openCode（打开并记一次查看），不再就地 load
  context.openCode = (code, name) => { context.state.code = code; calls.push('open:' + name); };
  context.document.getElementById = id => nodes[id] || (nodes[id] = mkEl());
  runSlices(context, ['watchlistUi']);
  context.renderWatchlist();
  const item = nodes.wlItems.children[0];
  assert.ok(item, 'watchlist row rendered');
  assert.equal(item.tabIndex, 0, 'watchlist row is focusable');
  assert.equal(typeof item.onkeydown, 'function', 'row exposes keyboard activation');
  item.onkeydown({ target: item, key: 'Enter', preventDefault() {} });
  assert.equal(context.state.code, 'sz001309', 'enter selects the row');
  assert.deepEqual(calls, ['open:德明利'], 'enter opens the row');
  item.onkeydown({ target: item, key: ' ', preventDefault() {} });
  assert.deepEqual(calls, ['open:德明利', 'open:德明利'], 'space selects the row');
  console.log('E. watchlist row keyboard checks passed');
}

// ---------- F. load() 接线：同标的后台刷新保留区间、切换不保留 ----------
(async function () {
  function envWith(loadedTarget, lr) {
    const bars = Array.from({ length: 220 }, (_, i) => ({ time: 't' + i }));
    const stub = chartStub(() => lr);
    const env = loadEnv({ loadedTarget, klineData: bars, charts: stub });
    return { env, stub, setRange(r) { lr = r; } };
  }
  const chartBody = { kline: Array.from({ length: 221 }, (_, i) => ({ time: 't' + i })), macd: { rows: [] }, meta: {} };

  // 同标的 refresh：停在历史 → 精确恢复，renderChart 不重置
  {
    const h = envWith({ code: 'a', freq: 'day', adjust: 'qfq' }, { from: 40, to: 90 });
    h.env.load({ refresh: true });
    h.setRange({ from: 140, to: 190 }); // fetch 期间用户继续平移
    h.env._harness.pending[0].resolve(okJson(chartBody));
    await tick(); await tick();
    assert.equal(h.env._harness.chartCalls[0].resetRange, false, 'refresh keeps chart range');
    const applied = h.stub.values.find(v => v.op === 'main.range');
    assert.equal(JSON.stringify(applied.r), JSON.stringify({ from: 140, to: 190 }),
      'response preserves the latest user range');
    assert.equal(JSON.stringify(h.env.loadedTarget), JSON.stringify({ code: 'a', freq: 'day', adjust: 'qfq' }));

    // refreshRangePlan 的第二实参须是 mergedKline（含 prepend 旧段），而非 incomingKline：
    // 同视图令牌 + 视窗外旧段 → mergeWithHistory 产出 224 根（3 旧 + 221 新）。
    // 若误传 incomingKline（221 根），计划长度会以错误总长推算（此处断言实参本身）。
    const hMerge = envWith({ code: 'a', freq: 'day', adjust: 'qfq' }, { from: 40, to: 90 });
    hMerge.env.viewState = { adjust: 'qfq', token: 'T7', analysisTokens: null };
    hMerge.env.historyState = { loading: false, hasMore: true, reqId: 0 };
    hMerge.env.klineData = [
      { time: 'o-2' }, { time: 'o-1' }, { time: 'o-0' },
      ...Array.from({ length: 220 }, (_, i) => ({ time: 't' + i })),
    ];
    const planCalls = [];
    const realPlan = hMerge.env.refreshRangePlan;
    hMerge.env.refreshRangePlan = (keep, bars) => { planCalls.push(bars); return realPlan(keep, bars); };
    hMerge.env.load({ refresh: true });
    hMerge.env._harness.pending[0].resolve(okJson({
      kline: chartBody.kline, macd: { rows: [] },
      meta: { token: 'T7', has_more: true },
    }));
    await tick(); await tick();
    assert.equal(planCalls.length, 1, 'refresh 须调用 refreshRangePlan');
    assert.equal(planCalls[0].length, 224,
      'refreshRangePlan 实参须为 mergeWithHistory 合并结果（旧段+新窗），非裸 incomingKline');
    assert.equal(planCalls[0][0].time, 'o-2', '合并窗首行是 prepend 的旧段');
    assert.equal(hMerge.env._harness.chartCalls[0].mergedKline.length, 224,
      'renderChart 收到的也是同一合并窗');

    // 响应提交时已不再显示请求对应的数据身份，不能套用旧图的区间
    const hStale = envWith({ code: 'a', freq: 'day', adjust: 'qfq' }, { from: 40, to: 90 });
    hStale.env.load({ refresh: true });
    hStale.env.loadedTarget = { code: 'b', freq: 'day', adjust: 'qfq' };
    hStale.env._harness.pending[0].resolve(okJson(chartBody));
    await tick(); await tick();
    assert.equal(hStale.env._harness.chartCalls[0].resetRange, true,
      'changed loaded target resets instead of restoring stale range');
    assert.equal(hStale.stub.values.filter(v => v.op === 'main.range').length, 0,
      'changed loaded target applies no stale logical range');

    // 切周期（无 refresh）：重置到最新，不恢复旧区间
    const h2 = envWith({ code: 'a', freq: 'day', adjust: 'qfq' }, { from: 40, to: 90 });
    h2.env.load();
    h2.env._harness.pending[0].resolve(okJson(chartBody));
    await tick(); await tick();
    assert.equal(h2.env._harness.chartCalls[0].resetRange, true, 'switch resets range');
    assert.equal(h2.stub.values.filter(v => v.op === 'main.range').length, 0, 'switch does not restore old range');
    console.log('F. load refresh/switch range wiring checks passed');
  }
})().catch(e => { console.error(e); process.exit(1); });

// ---------- G. 周线：周期按钮顺序、周期名、「周线不参与共振/AI」标注 ----------
{
  const box = mkEl('div');
  const context = appContext({
    el: id => id === 'resonance' ? box : mkEl(),
    fmtBarTime: t => t, signalLabel: () => '', openDetail() {}, renderTabs() {},
  });
  runSlices(context, ['resonance']);
  assert.equal(context.FREQ_NAME.week, '周线', 'FREQ_NAME 含周线（详情卡标题等处共用）');
  const notes = () => box.children.filter(c => c.classList.contains('res-note')).map(c => c.textContent);
  context.state.freq = 'week';
  context.renderResonance([], ['day', 'm60', 'm30']);
  assert.deepEqual(box.children.filter(c => c.classList.contains('res-chip')).map(c => c.dataset.freq),
    ['day', 'm60', 'm30'], '周线下共振仍只列日/60/30');
  assert.deepEqual(notes(), ['周线不参与共振/AI'], '周线视图在共振条注明不参与共振/AI');
  context.state.freq = 'day';
  context.renderResonance([], ['day', 'm60', 'm30']);
  assert.deepEqual(notes(), [], '其他周期不显示该标注');
  console.log('G. weekly tab and resonance note checks passed');
}

// ---------- H. 复权切换：记忆（localStorage 读写容错）、指数禁用并显示不复权、切换即重载 ----------
{
  const store = v => ({ getItem: k => (k === 'chanapp-adjust' ? v : null), setItem() {} });
  const initial = localStorage => { const c = { localStorage }; runSlices(c, ['viewState']); return c.viewState.adjust; };
  assert.equal(initial(store('raw')), 'raw', '记住的不复权在启动时恢复');
  assert.equal(initial(store('bogus')), 'qfq', '非法记忆值回落前复权');
  assert.equal(initial({ getItem() { throw new Error('blocked'); } }), 'qfq', '存储不可读时回落前复权');
  assert.equal(initial(undefined), 'qfq', '没有存储对象时回落前复权');
}
function tabsEnv(code, extras) {
  const nodes = {};
  const mk = (tag, data) => { const b = mkEl(tag); Object.assign(b.dataset, data); return b; };
  const freqTabs = mkEl('div'), adjustTabs = mkEl('div');
  ['day', 'week', 'm60', 'm30'].forEach(f => freqTabs.appendChild(mk('button', { freq: f })));
  ['qfq', 'raw'].forEach(a => adjustTabs.appendChild(mk('button', { adjust: a })));
  nodes.freqTabs = freqTabs; nodes.adjustTabs = adjustTabs;
  const loads = [], saved = {};
  const context = appContext(Object.assign({
    el: id => nodes[id] || (nodes[id] = mkEl()),
    load() { loads.push(context.state.freq); },
    marketOf: c => (c && c.indexOf('hk') === 0 ? 'hk' : 'cn'),
    localStorage: { getItem: () => null, setItem(k, v) { saved[k] = v; } },
  }, extras || {}));
  context.state.code = code;
  runSlices(context, ['tabs']);
  const btn = (box, key, v) => box.children.find(b => b.dataset[key] === v);
  return { context, loads, saved, freqBtn: f => btn(freqTabs, 'freq', f), adjBtn: a => btn(adjustTabs, 'adjust', a) };
}
{
  const t = tabsEnv('sh600926');
  t.context.renderTabs();
  assert.deepEqual(t.context.el('freqTabs').children.map(b => b.dataset.freq), ['day', 'week', 'm60', 'm30']);
  assert.equal(t.freqBtn('week').title, '周线不参与共振/AI');
  assert.equal(t.adjBtn('qfq').disabled, false, '个股可切换复权');
  assert.equal(t.adjBtn('qfq').classList.contains('active'), true, '默认前复权高亮');
  t.adjBtn('raw').onclick();
  assert.equal(t.context.viewState.adjust, 'raw', '点击不复权切换模式');
  assert.equal(t.saved['chanapp-adjust'], 'raw', '模式写入存储');
  assert.equal(t.adjBtn('raw').classList.contains('active'), true, '按钮高亮跟随');
  assert.deepEqual(t.loads, ['day'], '切换后重载主图');
  t.adjBtn('raw').onclick();
  assert.deepEqual(t.loads, ['day'], '重复点击当前模式不重载');

  const blocked = tabsEnv('sh600926', { localStorage: { getItem: () => null, setItem() { throw new Error('quota'); } } });
  blocked.context.renderTabs();
  blocked.adjBtn('raw').onclick();
  assert.equal(blocked.context.viewState.adjust, 'raw', '存储写入失败不影响切换');
  assert.deepEqual(blocked.loads, ['day']);

  for (const index of ['sh000001', 'sz399006']) {
    const i = tabsEnv(index);
    i.context.renderTabs();
    assert.equal(i.adjBtn('qfq').disabled && i.adjBtn('raw').disabled, true, index + ' 复权切换禁用');
    assert.equal(i.adjBtn('raw').classList.contains('active'), true, index + ' 显示不复权');
    assert.equal(i.adjBtn('qfq').classList.contains('active'), false);
    i.adjBtn('raw').onclick(); i.adjBtn('qfq').onclick();
    assert.equal(i.context.viewState.adjust, 'qfq', '指数下不改记忆的模式');
    assert.deepEqual(i.loads, [], '指数下点击不重载');
  }
  console.log('H. adjust toggle checks passed');
}

// ---------- I. 有意改写（目标 2026-09-29 第三阶段）：两个市场都只有日/周/60/30；旧的 5分/15分在 load() 归到 30 分 ----------
{
  for (const code of ['hk00700', 'sh600926']) {
    const t = tabsEnv(code);
    t.context.renderTabs();
    assert.deepEqual(['day', 'week', 'm60', 'm30'].filter(f => !t.freqBtn(f).hidden),
      ['day', 'week', 'm60', 'm30'], code + ' 四个周期都显示');
  }
  const hk = tabsEnv('hk00700');
  hk.context.renderTabs();
  assert.equal(hk.adjBtn('qfq').disabled, false, '港股个股可切换复权');
}
(async function () {
  const marketOf = c => (c && c.indexOf('hk') === 0 ? 'hk' : 'cn');
  const run = (code, freq) => {
    const env = loadEnv({ marketOf });
    runSlices(env, ['tabs']);
    env.state.code = code; env.state.freq = freq;
    env.load();
    return { env, url: env._harness.pending[0].url };
  };
  for (const code of ['hk00700', 'sh600926']) {
    for (const freq of ['m5', 'm15']) {
      const { env, url } = run(code, freq);
      assert.equal(env.state.freq, 'm30', code + ' ' + freq + ' 归到 30 分');
      assert.match(url, /&freq=m30&/, '请求按 30 分发出');
    }
  }
  assert.match(run('hk00700', 'm30').url, /&freq=m30&/, '港股 30分照常');
  console.log('I. retired short period checks passed');
})().catch(e => { console.error(e); process.exit(1); });
