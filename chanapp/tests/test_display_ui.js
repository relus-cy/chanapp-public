'use strict';
/* 显示层：行情/F10 状态注记、资金流口径注记，以及真实 loadQuotes/loadF10 的接纳规则
   （行情是自选级数据，不随当前 code/freq/adjust 门控；F10 只接纳请求发出时仍是当前代码的响应）。 */
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { appSource: source, mkEl, tick, runSlices } = require('./support/dom.js');
const { appContext } = require('./support/app.js');

{
  const context = {hhmmss: x => x};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function displayDataNote('), source.indexOf('  function loadQuotes()')), context);
  assert.equal(typeof context.displayDataNote, 'function');
  assert.equal(context.displayDataNote({}), '');
  assert.match(context.displayDataNote({degraded:true,fetch_time:'10:00'}), /10:00.*缓存/);
  assert.match(context.displayDataNote({meta:{sources:['baseline_backup']}}), /备用/);
  assert.match(context.displayDataNote({missing_codes:['sh600000']}), /部分数据缺失/);
  assert.match(context.displayDataNote({meta:{source_stale:true, source_ts:1788912000}}), /源时间较早/);
  assert.match(context.displayDataNote({meta:{source_stale:true}}), /源时间未确认/);
  assert.match(context.displayDataNote({meta:{source_time_unknown:true,source_stale:false}}), /源时间未确认/);
  console.log('display status UI checks passed');
}

// 资金流口径注记：有则显示，无则隐藏，隐藏卡片时一并清空
{
  const context = appContext({ fmtYi:String, signCls:()=>'flat' });
  const nodes = context._harness.nodes;
  runSlices(context, ['flow']);
  context.renderFlow({flow:{},meta:{flow_note:'按成交额分档'}});
  assert.equal(nodes.flowNote.textContent, '按成交额分档');
  assert.equal(nodes.flowNote.hidden, false);
  context.renderFlow({flow:{}});
  assert.equal(nodes.flowNote.textContent, '');
  assert.equal(nodes.flowNote.hidden, true);
  context.renderFlow({flow:{},meta:{flow_note:'旧口径'}});
  runSlices(context, ['hideF10']);
  context.hideF10Cards();
  assert.equal(nodes.flowNote.textContent, '');
  assert.equal(nodes.flowNote.hidden, true);
  console.log('flow note UI checks passed');
}

// 真实 loadQuotes/loadF10：延迟响应与接纳规则
(async function () {
  const context = appContext({
    state: {code: 'a', freq: 'day', quotes: {}},
    viewState: { adjust: 'qfq', token: null, analysisTokens: null },
    F10_TTL: 300000,
    renderWatchlist() { context._harness.renders.push('watchlist'); },
    renderF10() { context._harness.renders.push('f10'); },
    renderFlow() { context._harness.renders.push('flow'); },
  });
  runSlices(context, ['quotesF10', 'f10']);
  const pending = context._harness.pending;
  const renders = context._harness.renders;

  // 行情：切换代码/周期/复权期间到达的响应照常接纳（行情按自选代码成表，与当前视图无关）
  context.loadQuotes();
  context.state.code = 'b'; context.state.freq = 'm30'; context.viewState.adjust = 'raw';
  pending[0].resolve({ok:true, json:async () => ({quotes:{a:{price:42}}})});
  await tick();
  assert.equal(context.state.quotes.a.price, 42, '行情响应不带任何身份字段也被接纳');
  assert.deepEqual(renders, ['watchlist']);

  // F10：请求发出时的代码已不是当前代码 → 丢弃，且旧代码的失败不得清掉新代码的 TTL
  context.state.code = 'a';
  renders.length = 0;
  context.loadF10('a');
  context.state.code = 'b';
  pending[1].resolve({ok:true, json:async () => ({code:'a', f10:{}})});
  await tick();
  assert.equal(renders.length, 0, '切走后迟到的旧代码 F10 不渲染');
  context.loadF10('b');
  context.f10Last = {code:'b', ts:123};
  context.state.code = 'b';
  context.loadF10('a');            // 旧代码请求（例如切回又切走）
  context.f10Last = {code:'b', ts:123};
  pending[3].reject(new Error('late failure'));
  await tick();
  assert.equal(context.f10Last.ts, 123, '旧代码 F10 失败不得作废新代码的缓存计时');

  // F10：当前代码的响应在切周期/复权后照常接纳（F10 与周期、复权无关）
  context.state.code = 'c';
  context.f10Last = {code:null, ts:0};
  context.loadF10('c');
  context.state.freq = 'm60'; context.viewState.adjust = 'qfq';
  pending[4].resolve({ok:true, json:async () => ({code:'c', f10:{}})});
  await tick();
  assert.deepEqual(renders, ['f10', 'flow'], '当前代码的 F10 响应渲染');

  assert.equal(pending.length, 5, '显示层加载只发出行情与 F10 请求，没有额外的状态同步');
  console.log('real quote and F10 loader acceptance checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

// 右栏卡头与自选行价格口径：A 股价格只来自事实行情（/api/quotes），不回退 F10 价格；
// price_unavailable → 价格为空并标「暂无可信价格」；price_label=昨收 → 涨跌为空；两处取同一行情行
{
  const nodes = {};
  const context = appContext({
    el: id => nodes[id] || (nodes[id] = mkEl()),
    watchName: code => code, signCls: v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'),
  });
  runSlices(context, ['quoteView', 'f10Header']);
  const header = (q, f) => {
    context.state.code = 'sh600926';
    context.state.watchlist = [{ code: 'sh600926' }];   // 自选行口径（价格卡按是否自选选报价通道）
    context.state.quotes = q === undefined ? {} : { sh600926: q };
    context.renderF10Header(f || {});
    return { px: nodes.f10Px.textContent, chg: nodes.f10Chg.textContent, title: nodes.f10Px.title };
  };
  let h = header({ price: 17.0, pct: 1.37, price_label: '最新', price_time: '2026-09-28 10:05' }, { price: 99 });
  assert.deepEqual([h.px, h.chg], ['17.00', '+1.37%'], '最新价与涨跌来自行情行');
  assert.match(h.title, /10:05/, '价格时刻可见');
  h = header({ price: null, pct: null, limit_up: false, price_label: null, price_unavailable: true }, { price: 99 });
  assert.deepEqual([h.px, h.chg], ['--', '暂无可信价格'], '事实缺失：不回退 F10 价格，标暂无可信价格');
  h = header(undefined, { price: 99 });
  assert.deepEqual([h.px, h.chg], ['--', ''], '没有行情行（非自选或尚未加载）：不回退 F10 价格');
  h = header({ price: 20.0, pct: 0.5, price_label: '昨收', price_time: null });
  assert.deepEqual([h.px, h.chg], ['20.00', '昨收'], '盘前显示昨收，涨跌为空');
  assert.ok(!/(^| )(up|down)( |$)/.test(nodes.f10Px.className), '昨收不着涨跌色');
  h = header({ price: 17.0, pct: 1.37, price_label: '最新', price_time: '2026-09-28 10:05', stale: true });
  assert.deepEqual([h.px, h.chg], ['17.00', '+1.37%'], '过期行情照常显示数值');
  assert.match(h.title, /延迟/, '过期行情在价格提示里标延迟');
  assert.match(nodes.f10Px.className, /(^| )stale( |$)/, '过期行情价格弱化');
  h = header({ price: 500.0, pct: -1.2, name: 'x', limit_up: false });
  assert.deepEqual([h.px, h.chg], ['500.00', '-1.20%'], '港股显示层行照常显示');
}
{
  const nodes = { wlItems: mkEl('div'), wlForm: mkEl('div') };
  const context = appContext({ wireSearch() {} });
  context.document.getElementById = id => nodes[id] || (nodes[id] = mkEl());
  runSlices(context, ['watchlistUi']);
  context.state.watchlist = [{ code: 'sh600926', name: 'A' }, { code: 'sz000001', name: 'B' },
    { code: 'sh600000', name: 'C' }, { code: 'sh600001', name: 'D' }, { code: 'sh600002', name: 'E' }];
  context.state.quotes = {
    sh600926: { price: 17.0, pct: 1.37, price_label: '最新', limit_up: true },
    sz000001: { price: null, pct: null, limit_up: false, price_label: null, price_unavailable: true },
    sh600000: { price: 20.0, pct: 0.5, price_label: '昨收' },
    sh600001: { price: 9.0, pct: 1.0, price_label: '最新', stale: true },
  };
  context.renderWatchlist();
  const html = nodes.wlItems.children.map(c => c.innerHTML);
  assert.match(html[0], /\+1\.37%/); assert.match(html[0], />17\.00</); assert.match(html[0], /涨停/);
  assert.match(html[1], /暂无可信价格/, '事实缺失的自选行标暂无可信价格');
  assert.ok(!/%/.test(html[1]), '事实缺失的自选行没有涨跌');
  assert.match(html[2], /20\.00/); assert.match(html[2], /昨收/);
  assert.ok(!/%/.test(html[2]), '昨收时自选行涨跌为空');
  assert.match(html[3], /延迟/, '过期行情的自选行标延迟');
  assert.ok(!/暂无可信价格/.test(html[4]), '行情尚未加载的行不提前标暂无可信价格');
  console.log('rail header and watchlist quote checks passed');
}
