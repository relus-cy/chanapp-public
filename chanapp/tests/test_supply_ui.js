'use strict';
const assert = require('node:assert/strict');
const supply = require('../web/supply.js');
assert.equal(supply.accepts(2, 1, {generation: 2}), false, 'late request never gains new eligibility');
assert.equal(supply.accepts(2, 2, {generation: 1}), false);
assert.equal(supply.accepts(2, 2, {meta: {generation: 2}}), true);
assert.equal(supply.accepts(2, 2, {}), false, 'live server requires identity');
assert.equal(supply.accepts(null, null, {}), true, 'public demo remains usable');
assert.equal(supply.canSwitch('baseline', []), true, 'rollback always available');
assert.equal(supply.canSwitch('primary_candidate', [{id:'primary_candidate', ready:false}]), false);
assert.equal(supply.canSwitch('primary_candidate', [{id:'primary_candidate', ready:true}]), true);
assert.equal(supply.errorText({detail:'内部记录'}), '切换失败，请稍后重试');
assert.equal(supply.errorText({detail:{reasons:['能力待确认']}}), '切换失败，请稍后重试');
console.log('supply UI behavior checks passed');

// Execute the real loaders with deferred network responses, without a browser.
const vm = require('node:vm');
const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const quotesLoader = source.slice(source.indexOf('  function displayDataNote('), source.indexOf('  function addWatch('));
const f10Loader = source.slice(source.indexOf('  function loadF10('), source.indexOf('  // ---------- 数据加载'));
(async function () {
  const pending = [];
  let rendered = 0;
  const context = {
    supplyState: {generation: 0}, state: {code: 'a', quotes: {}},
    fetch: () => new Promise((resolve, reject) => pending.push({resolve, reject})),
    F10_TTL: 300000, f10Last: {code:null, ts:0}, lastF10:null,
    renderWatchlist: () => rendered++, renderF10: () => rendered++, renderFlow: () => rendered++,
    hideF10Cards: () => {}, el: () => ({}), hhmmss: x => x,
  };
  context.eligible = (generation, body) => supply.accepts(context.supplyState.generation, generation, body);
  vm.createContext(context);
  vm.runInContext(quotesLoader + f10Loader, context);
  context.loadQuotes();
  context.loadF10('a');
  context.supplyState.generation = 1;
  context.f10Last = {code:'a', ts:123};
  pending[0].resolve({ok:true, json:async () => ({generation:0, quotes:{a:{price:99}}})});
  pending[1].reject(new Error('late failure'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rendered, 0, 'old quotes cannot render after switch');
  assert.equal(context.f10Last.ts, 123, 'old F10 failure cannot invalidate new cache');
  context.loadQuotes();
  pending[2].resolve({ok:true, json:async () => ({generation:1, quotes:{a:{price:42}}})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.state.quotes.a.price, 42);
  assert.equal(rendered, 1);
  console.log('real loader delayed-response checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

(async function () {
  const pending = [];
  const nodes = {};
  const element = id => nodes[id] || (nodes[id] = {style:{}, options:[{}, {}], classList:{add(){}}});
  let reloads = 0;
  const context = {
    SupplyUI:supply, fetch:() => new Promise(resolve => pending.push(resolve)), el:element,
    state:{code:'a',freq:'day',ruleProfile:'strict',signalScope:'expanded'}, charts:null, chartAbort:null, analysisSlots:{},
    f10Last:{}, lastF10:null, lastChartData:null, lastMeta:null,
    hideF10Cards(){}, renderWatchlist(){}, renderEvidence(){}, load(){reloads++;}, loadQuotes(){},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var supplyState'), source.indexOf('  // ---------- 主题调色板')), context);
  context.supplyState = {generation:1, scheme:'baseline', options:[]};
  assert.equal(context.eligible(1, {generation:2}), false);
  assert.equal(context.eligible(1, {generation:2}), false);
  assert.equal(pending.length, 1, 'higher generations coalesce into one state sync');
  pending[0]({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,scheme:'primary_candidate',options:[],csrf_token:'fresh'})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.supplyState.generation, 2);
  assert.equal(context.supplyState.csrf_token, 'fresh');
  assert.equal(reloads, 1);
  assert.equal(context.eligible(1, {generation:1}), false);
  console.log('external supply switch checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

{
  const nodes = {};
  const context = {el:id => nodes[id] || (nodes[id] = {style:{}}), fmtYi:String, signCls:()=>'flat'};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function renderFlow('), source.indexOf('  // 与 /api/chart 并行调用')), context);
  context.renderFlow({flow:{},meta:{flow_note:'按成交额分档'}});
  assert.equal(nodes.flowNote.textContent, '按成交额分档');
  assert.equal(nodes.flowNote.hidden, false);
  context.renderFlow({flow:{}});
  assert.equal(nodes.flowNote.textContent, '');
  assert.equal(nodes.flowNote.hidden, true);
  context.renderFlow({flow:{},meta:{flow_note:'旧口径'}});
  vm.runInContext(source.slice(source.indexOf('  function hideF10Cards()'), source.indexOf('  // 卡头：')), context);
  context.hideF10Cards();
  assert.equal(nodes.flowNote.textContent, '');
  assert.equal(nodes.flowNote.hidden, true);
}

(async function () {
  const nodes = {};
  const pending = [];
  let reloads = 0;
  const context = {
    SupplyUI:supply, fetch:() => new Promise(resolve => pending.push(resolve)),
    el:id => nodes[id] || (nodes[id] = {style:{},options:[{},{}],classList:{add(){}}}),
    state:{code:'a',freq:'day',ruleProfile:'strict',signalScope:'expanded'}, charts:null, chartAbort:null, analysisSlots:{},
    f10Last:{}, lastF10:null, lastChartData:null, lastMeta:null,
    hideF10Cards(){}, renderWatchlist(){}, renderEvidence(){}, load(){reloads++;}, loadQuotes(){},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var supplyState'), source.indexOf('  // ---------- 主题调色板')), context);
  const init = context.initSupply();
  pending[0]({ok:false,status:503,headers:{get:()=>null}});
  await init;
  assert.equal(nodes.supplyControl.hidden, true);
  context.eligible(null, {generation:0,scheme:'baseline'});
  assert.equal(context.supplyState.generation, 0, 'demo body establishes generation after supply 503');
  assert.equal(context.eligible(0, {generation:0}), true);
  assert.equal(reloads, 1, 'restart initial requests with established identity');
  context.supplySyncAt = 0;
  const recovering = context.syncSupply();
  pending[1]({ok:true,json:async () => ({generation:0,scheme:'baseline',options:[],csrf_token:'recovered'})});
  await recovering;
  assert.equal(nodes.supplyControl.hidden, false, 'temporary failure is recoverable');
  assert.equal(context.supplyState.csrf_token, 'recovered');
  console.log('initial supply failure recovery checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });


(async function () {
  const nodes = {};
  const context = {
    SupplyUI:supply, fetch:async () => ({ok:false,status:503,headers:{get:key => key === 'X-Supply-Generation' ? '0' : key === 'X-Supply-Scheme' ? 'baseline' : null}}),
    el:id => nodes[id] || (nodes[id] = {options:[{},{}]}),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var supplyState'), source.indexOf('  // ---------- 主题调色板')), context);
  await context.initSupply();
  assert.equal(context.supplyState.generation, 0);
  assert.equal(context.eligible(0, {generation:0,status:'unconfigured'}), true);
  assert.equal(context.eligible(0, {meta:{generation:0}}), true);
  console.log('503 response header identity checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

// Run both production fetch paths so response ordering exercises their callbacks.
function makeAnalysisLoaderHarness() {
  const nodes = {}, pending = [], shown = [], chartShown = [];
  const context = {
    state:{code:'a',freq:'day',ruleProfile:'strict',signalScope:'expanded'}, supplyState:{generation:2}, supplyBusy:false, supplyRange:null,
    chartAbort:null, charts:null, loadedTarget:null, AbortController,
    CHART_TIMEOUT_MS:25000, ANALYSIS_TIMEOUT_MS:35000,
    setTimeout:() => 1, clearTimeout(){},
    fetch:(url) => new Promise((resolve, reject) => pending.push({url,resolve,reject})),
    el:id => nodes[id] || (nodes[id] = {classList:{add(){},remove(){}}}),
    renderAnalysis:(body,note) => shown.push({body,note}), renderChart:body => chartShown.push(body),
    ensureCharts(){}, updateAiTitle(){}, setStatus(){}, hideChartError(){}, loadF10(){}, closeAiPopup(){},
    renderEvidence(){}, renderMeta(){}, showChartError:message => { throw new Error(message); },
  };
  context.eligible = (generation, body) => supply.accepts(context.supplyState.generation, generation, body);
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var pendingAnalysis ='),
    source.indexOf('  // ---------- 数据口径条')), context);
  vm.runInContext(source.slice(source.indexOf('  function refreshRangePlan('), source.indexOf('  // 图表渲染主路径')), context);
  vm.runInContext(source.slice(source.indexOf('  function load('),
    source.indexOf('  // ---------- 交易时段自动刷新')), context);
  return {context,pending,shown,chartShown};
}

(async function () {
  const {context,pending,shown,chartShown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const chart = pending.find(request => request.url.startsWith('/api/chart?'));
  const analysis = pending.find(request => request.url.startsWith('/api/analysis?'));
  assert.ok(chart && analysis, 'manual analysis can run alongside chart loading');
  chart.resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,meta:{data_version:'chart-first-v1'}})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(chartShown.length, 1);
  assert.equal(shown.length, 0);
  analysis.resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',data_version:'chart-first-v1'})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shown.length, 1, 'matching AI renders when chart completed first');
  assert.equal(shown[0].body.data_version, 'chart-first-v1');
  assert.equal(shown[0].note, null);
  console.log('chart-first real analysis loader checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

(async function () {
  for (const mode of ['unconfigured', 'fallback']) {
    const {context,pending,shown} = makeAnalysisLoaderHarness();
    context.loadAnalysis({manual:true,refresh:true});
    pending[0].resolve(mode === 'unconfigured'
      ? {ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'unconfigured'})}
      : {ok:false,status:502});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(pending.length, 2, mode + ' starts the real static sample fetch');
    assert.equal(pending[1].url, '/analysis.sample.json');
    const originalController = context.analysisSlots[Object.keys(context.analysisSlots)[0]].abort;
    context.supplyState.generation = 3;
    pending[1].resolve({ok:true,json:async () => ({current_state:'late sample',scenarios:[]})});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(context.analysisSlots[Object.keys(context.analysisSlots)[0]].abort, originalController, 'exercise generation guard independently of abort guard');
    assert.equal(shown.length, 0, mode + ' sample arriving after a generation change is discarded');
  }
  console.log('late static sample real loader checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

assert.equal(supply.accepts({epoch:'new',generation:0}, {epoch:'old',generation:0}, {epoch:'old',generation:0}), false, 'reused generation cannot revive old epoch');
assert.equal(supply.accepts({epoch:'new',generation:0}, {epoch:'new',generation:0}, {meta:{epoch:'new',generation:0}}), true);

function supplyHarness() {
  const nodes = {}, pending = [];
  const context = {
    SupplyUI:supply, fetch:(url, options) => new Promise(resolve => pending.push({url,options,resolve})),
    el:id => nodes[id] || (nodes[id] = {style:{},options:[{},{}],classList:{add(){}}}),
    state:{code:'a',freq:'day',ruleProfile:'strict',signalScope:'expanded'}, charts:null, chartAbort:null, analysisSlots:{},
    f10Last:{},lastF10:null,lastChartData:null,lastMeta:null,
    hideF10Cards(){},renderWatchlist(){},renderEvidence(){},load(){},loadQuotes(){},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var supplyState'), source.indexOf('  // ---------- 主题调色板')), context);
  context.supplyState = {epoch:'old',generation:4,scheme:'primary_candidate',options:[],csrf_token:'initial'};
  return {context,nodes,pending};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
(async function () {
  const {context,pending} = supplyHarness();
  assert.equal(context.eligible(4, {epoch:'new',generation:0}, 'old'), false);
  assert.equal(context.supplyState.epoch, 'old', 'business response cannot change epoch');
  pending[0].resolve({ok:true,json:async () => ({epoch:'new',generation:0,scheme:'baseline',options:[],csrf_token:'new'})});
  await tick();
  assert.equal(context.supplyState.epoch, 'new');
  assert.equal(context.supplyState.generation, 0, 'authority accepts lower generation in new epoch');
  assert.equal(context.eligible(4, {epoch:'old',generation:4}, 'old'), false);
  assert.equal(context.supplyState.epoch, 'new');
  console.log('epoch restore identity checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,nodes,pending} = supplyHarness();
  context.supplySyncAt = Date.now();
  context.el('supplySelect').value = 'baseline';
  context.switchSupply();
  assert.equal(JSON.parse(pending[0].options.body).expected_epoch, 'old');
  pending[0].resolve({ok:false,status:409,json:async () => ({detail:'changed'})});
  await tick();
  assert.equal(pending.length, 2, '409 bypasses throttle with one GET');
  assert.equal(pending[1].options, undefined);
  pending[1].resolve({ok:true,json:async () => ({epoch:'old',generation:5,scheme:'baseline',options:[],csrf_token:'fresh'})});
  await tick();
  assert.equal(context.supplyState.csrf_token, 'fresh');
  assert.equal(pending.length, 2, 'never replay user POST');
  assert.equal(context.supplyState.generation, 5);
  console.log('409 forced authoritative sync checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending} = supplyHarness();
  context.syncSupply({force:true});
  context.el('supplySelect').value = 'baseline';
  context.switchSupply();
  pending[1].resolve({ok:true,json:async () => ({epoch:'old',generation:5,scheme:'baseline',prepared:{}})});
  await tick();
  pending[0].resolve({ok:true,json:async () => ({epoch:'old',generation:4,scheme:'primary_candidate',options:[],csrf_token:'stale'})});
  await tick();
  assert.equal(context.supplyState.generation, 5, 'late GET cannot overwrite accepted POST');
  assert.notEqual(context.supplyState.csrf_token, 'stale');
  assert.equal(pending.length, 3, 'discarded GET causes a fresh authoritative read');
  pending[2].resolve({ok:true,json:async () => ({epoch:'old',generation:5,scheme:'baseline',options:[],csrf_token:'fresh'})});
  await tick();
  assert.equal(context.supplyState.csrf_token, 'fresh');
  console.log('late GET revision fencing checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.supplyState.epoch = 'old';
  context.loadAnalysis({manual:true,refresh:true});
  pending[0].resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',epoch:'old',generation:2,status:'unconfigured'})});
  await tick();
  context.supplyState.epoch = 'new';
  pending[1].resolve({ok:true,json:async () => ({current_state:'late sample',scenarios:[]})});
  await tick();
  assert.equal(shown.length, 0, 'same generation in new epoch revokes pending sample');
  console.log('late sample epoch fencing checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending} = supplyHarness();
  context.supplySyncAt = Date.now();
  context.el('supplySelect').value = 'baseline';
  context.switchSupply();
  pending[0].resolve({ok:false,status:403,json:async () => ({detail:'切换凭据无效，请刷新页面'})});
  await tick();
  assert.equal(pending.length, 2, 'stale CSRF (403) triggers one authoritative sync');
  pending[1].resolve({ok:true,json:async () => ({epoch:'old',generation:4,scheme:'primary_candidate',options:[],csrf_token:'rotated'})});
  await tick();
  assert.equal(context.supplyState.csrf_token, 'rotated');
  assert.equal(pending.length, 2, 'no POST replay after 403');
  console.log('stale CSRF resync checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const secret = '内部验收记录'.repeat(100);
  for (const scheme of ['baseline', 'primary_candidate']) {
    const {context,nodes,pending} = supplyHarness();
    const init = context.initSupply();
    pending[0].resolve({ok:true,json:async () => ({epoch:'old',generation:4,scheme,
      options:[{id:'primary_candidate',ready:false,reasons:[secret]}]})});
    await init;
    assert.equal(nodes.supplySelect.value, scheme, 'initial selection follows current scheme');
    assert.equal(nodes.supplySwitch.disabled, true);
    assert.equal(nodes.supplySelect.options[1].disabled, true, 'unavailable candidate remains gated');
    assert.equal(nodes.supplyReasons.textContent, '候选方案暂不可用');
    assert.ok(!JSON.stringify(nodes).includes(secret));
  }
  const {context,nodes,pending} = supplyHarness();
  context.el('supplySelect').value = 'baseline';
  context.switchSupply();
  pending[0].resolve({ok:true,json:async () => ({epoch:'old',generation:5,scheme:'baseline',prepared:{}})});
  await tick();
  assert.equal(nodes.supplySelect.value, 'baseline', 'successful switch keeps selected scheme');
  assert.equal(nodes.supplySwitch.disabled, true);
  assert.match(nodes.supplyCurrent.textContent, /现有方案/);
  assert.match(nodes.supplyStatus.textContent, /已切换至现有方案/);
  for (const status of [400,403,409,500]) {
    const h = supplyHarness();
    h.context.el('supplySelect').value = 'baseline';
    h.context.switchSupply();
    h.pending[0].resolve({ok:false,status,json:async () => ({detail:{reasons:[secret]}})});
    await tick();
    assert.ok(!JSON.stringify(h.nodes).includes(secret), 'server diagnostics never render');
    assert.ok(h.nodes.supplyStatus.textContent.length < 50);
    if (status === 403 || status === 409) {
      assert.equal(h.pending.length, 2, 'authorization and identity failures still resync');
      h.pending[1].resolve({ok:true,json:async () => ({epoch:'old',generation:4,scheme:'primary_candidate',options:[]})});
      await tick();
    }
  }
  const h = supplyHarness();
  h.context.fetch = () => Promise.reject(new Error(secret));
  h.context.el('supplySelect').value = 'baseline';
  h.context.switchSupply();
  await tick();
  assert.ok(!JSON.stringify(h.nodes).includes(secret), 'network errors never render raw messages');
  console.log('supply selection and diagnostic privacy checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const first = pending.find(r => r.url.startsWith('/api/analysis?'));
  const slotKey = code => Object.keys(context.analysisSlots).find(k => k.startsWith('["' + code + '"'));
  const controller = context.analysisSlots[slotKey('a')].abort;
  context.state.freq = 'm5';
  context.load();
  assert.equal(pending.filter(r => r.url.startsWith('/api/analysis?')).length, 1, 'period change reuses in-flight joint analysis');
  assert.equal(controller.signal.aborted, false);
  first.resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',analysis_scope:'multi_timeframe',data_version:'joint',data_versions:{day:'d',m60:'h',m30:'m'}})});
  await tick();
  assert.equal(shown.length, 1, 'joint result renders without matching single-chart hash');
  const rendersBeforeSwitch = shown.length;
  context.state.freq = 'm15'; context.load();
  assert.equal(shown.length, rendersBeforeSwitch, 'period changes preserve rendered AI DOM');
  assert.equal(pending.filter(r => r.url.startsWith('/api/analysis?')).length, 1, 'completed analysis survives period changes');
  context.load({refresh:true});
  assert.equal(pending.filter(r => r.url.startsWith('/api/analysis?')).length, 1, 'scheduled refresh does not request AI');
  context.loadAnalysis({manual:true,refresh:true});
  const old = pending.filter(r => r.url.startsWith('/api/analysis?'))[1];
  context.state.code = 'b'; context.load();
  old.resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',data_version:'wrong-stock'})});
  await tick();
  assert.equal(shown.length, 1, 'late old-stock result is discarded');
  context.loadAnalysis({manual:true,refresh:true});
  const next = pending.filter(r => r.url.startsWith('/api/analysis?')).at(-1);
  context.supplyState.scheme = 'changed'; context.load();
  next.resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',data_version:'wrong-scheme'})});
  await tick();
  assert.equal(shown.length, 1, 'late old-scheme result is discarded');
  console.log('stock-scoped joint analysis continuity checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.loadAnalysis({manual:true,refresh:true});
  pending[0].resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',data_version:'joint-before'})});
  await tick();
  vm.runInContext(source.match(/el\('aiRefresh'\)\.onclick = function \(\) \{[^\n]+/)[0], context);
  context.el('aiRefresh').onclick();
  assert.equal(pending.length, 2, 'refresh button explicitly checks latest joint data');
  assert.equal(shown.length, 1, 'previous result stays visible while refreshing');
  pending[1].resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',data_version:'joint-after'})});
  await tick();
  assert.equal(shown.at(-1).body.data_version, 'joint-after');
  console.log('manual AI refresh checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown,chartShown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const first = pending.slice();
  assert.ok(first.every(r => r.url.includes('rule_profile=strict')));
  context.state.ruleProfile = 'relaxed'; context.load(); context.loadAnalysis({manual:true,refresh:true});
  const second = pending.slice(2);
  assert.ok(second.every(r => r.url.includes('rule_profile=relaxed')));
  context.state.ruleProfile = 'strict'; context.load(); context.loadAnalysis({manual:true,refresh:true});
  assert.equal(pending.filter(r => r.url.startsWith('/api/analysis?')).length, 2,
    '切回在飞的规则身份复用原分析请求，不重复发');
  const body = {generation:2,rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',status:'ok',meta:{}};
  second.forEach(r => r.resolve({ok:true,json:async()=>({...body,rule_profile:'relaxed'})}));
  await tick();
  assert.equal(shown.length,0); assert.equal(chartShown.length,0);
  first.forEach(r => r.resolve({ok:true,json:async()=>body}));
  await tick();
  assert.equal(shown.length,1, '切回后原身份在飞请求完成即渲染');
  pending.slice(4).forEach(r => r.resolve({ok:true,json:async()=>body}));
  await tick();
  assert.equal(chartShown.length,1);
  context.load({refresh:true});
  const chartErrors = [];
  context.showChartError = m => chartErrors.push(m);
  pending.slice(5).forEach(r => r.resolve({ok:true,json:async()=>({...body,rule_profile:'relaxed'})}));
  await tick();
  assert.equal(shown.length,1); assert.equal(chartShown.length,1);
  assert.equal(chartErrors.length,1, 'live request with mismatched rule identity surfaces an explicit error');
  console.log('strict relaxed strict request sequence and response identity checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown,chartShown} = makeAnalysisLoaderHarness();
  const groups = [];
  for (const profile of ['strict','relaxed']) {
    for (const scope of ['standard','expanded']) {
      const start = pending.length;
      context.state.ruleProfile = profile; context.state.signalScope = scope; context.load(); context.loadAnalysis({manual:true,refresh:true});
      const requests = pending.slice(start);
      assert.equal(requests.length,2);
      assert.ok(requests.every(r => r.url.includes('rule_profile='+profile) && r.url.includes('signal_scope='+scope)));
      groups.push({requests,body:{generation:2,rule_profile:profile,signal_scope:scope,schema_version:'chanpy_v2',calculation_id:profile+'-'+scope,status:'ok',meta:{}}});
    }
  }
  for (const group of groups.slice(0,-1)) group.requests.forEach(r=>r.resolve({ok:true,json:async()=>group.body}));
  await tick();
  assert.equal(shown.length,0); assert.equal(chartShown.length,0);
  const last = groups.at(-1);
  last.requests.forEach(r=>r.resolve({ok:true,json:async()=>last.body}));
  await tick();
  assert.equal(shown.length,1); assert.equal(chartShown.length,1);
  const count = pending.length;
  context.state.freq='m30'; context.load();
  assert.equal(pending.length,count+1,'same stock/profile/scope timeframe switch reuses joint AI');
  pending.at(-1).resolve({ok:true,json:async()=>last.body});
  await tick();
  const errors=[]; context.showChartError=m=>errors.push(m);
  for (const mismatch of [{signal_scope:'standard'},{schema_version:'chanpy_v1'}]) {
    const start=pending.length; context.load({refresh:true});
    pending.slice(start).forEach(r=>r.resolve({ok:true,json:async()=>({...last.body,...mismatch})}));
    await tick();
  }
  assert.equal(shown.length,1,'mismatched scope or legacy schema cannot update AI');
  assert.equal(chartShown.length,2,'mismatched scope or legacy schema cannot update chart');
  assert.equal(errors.length,2);
  console.log('four-combination scope requests, stale responses, schema fencing and timeframe reuse checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  const aiRequests = () => pending.filter(r => r.url.startsWith('/api/analysis?'));
  const slotOf = code => context.analysisSlots[Object.keys(context.analysisSlots).find(k => k.startsWith('["' + code + '"'))];
  context.load();
  context.load({refresh:true});
  context.state.freq = 'm30'; context.load();
  context.state.code = 'b'; context.load();
  context.state.ruleProfile = 'relaxed'; context.load();
  context.state.signalScope = 'standard'; context.load();
  context.supplyState.generation = 3; context.load();
  context.loadAnalysis();
  context.loadAnalysis({refresh:true});
  assert.equal(aiRequests().length, 0, 'initial load, timer, selections and legacy calls cannot trigger AI');
  assert.match(context.el('aiPanel').innerHTML, /点击刷新/);
  assert.equal(context.el('aiRefresh').disabled, false);
  vm.runInContext(source.match(/el\('aiRefresh'\)\.onclick = function \(\) \{[^\n]+/)[0], context);
  context.el('aiRefresh').onclick();
  context.el('aiRefresh').onclick();
  assert.equal(aiRequests().length, 1, 'only the explicit button starts AI and in-flight clicks are deduplicated');
  assert.match(context.el('aiPanel').innerHTML, /加载中/, 'first manual request shows loading feedback');
  const controller = slotOf('b').abort;
  context.state.code = 'c'; context.load();
  assert.equal(controller.signal.aborted, false, '切走不再中止在飞请求');
  assert.equal(context.el('aiRefresh').disabled, false, 'changing stock releases disabled button');
  assert.equal(context.pendingAnalysis, null);
  aiRequests()[0].resolve({ok:true,json:async()=>({rule_profile:'relaxed',signal_scope:'standard',schema_version:'chanpy_v2',calculation_id:'id',generation:3,status:'ok'})});
  await tick();
  assert.equal(shown.length, 0, 'old-stock result lands in its slot without rendering for another stock');
  assert.equal(slotOf('b').body.status, 'ok', '结果按身份落槽保留');
  assert.equal(aiRequests().length, 1, 'stock change does not issue any AI request');
  context.state.code = 'b'; context.load();
  assert.equal(shown.length, 1, '切回直接恢复槽内结果，无需重新点击刷新');
  console.log('manual-only AI trigger and selection invalidation checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const req = pending.find(r => r.url.startsWith('/api/analysis?'));
  context.state.code = 'b'; context.load();
  assert.match(context.el('aiPanel').innerHTML, /点击刷新/);
  context.state.code = 'a'; context.load();
  assert.match(context.el('aiPanel').innerHTML, /加载中/, '切回在飞中的股票恢复加载态');
  assert.equal(context.el('aiRefresh').disabled, true, '在飞身份切回后按钮保持禁用');
  req.resolve({ok:true,json:async () => ({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',generation:2,status:'ok',data_version:'v1'})});
  await tick();
  assert.equal(shown.length, 1, '在飞请求完成后直接渲染到当前面板');
  console.log('in-flight analysis survives stock round-trip checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const firstKey = Object.keys(context.analysisSlots)[0];
  const firstAbort = context.analysisSlots[firstKey].abort;
  for (let i = 1; i <= 10; i++) { context.state.code = 's' + i; context.load(); context.loadAnalysis({manual:true,refresh:true}); }
  assert.equal(Object.keys(context.analysisSlots).length, 10, '槽位上限 10，超出逐出最旧');
  assert.equal(context.analysisSlots[firstKey], undefined, '最旧槽位被逐出');
  assert.equal(firstAbort.signal.aborted, true, '逐出时中止其请求');
  assert.equal(pending.filter(r => r.url.startsWith('/api/analysis?')).length, 11);
  console.log('analysis slot cap eviction checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});
