'use strict';
/* 联合 AI 分析：新鲜度提示、真实 loadAnalysis/load 生命周期（手动触发、按身份落槽、规则身份栅栏、
   迟到结果丢弃）、请求携带 adjust 与主图给出的分析令牌、409 整窗重载且不自动重发。
   身份 = 股票 + 成笔标准 + 提示范围 + 复权模式；aiPanel/helpers/etag/load 切片实跑。 */
const assert = require('node:assert/strict');
const { tick, runSlices } = require('./support/dom.js');
const { appContext, okJson, errJson } = require('./support/app.js');

const REANALYZE = '数据已更新，请重新分析';

// ---------- 新鲜度提示（联合版本与当前单图版本不一致） ----------
{
  const note = {};
  const context = {el: () => note, currentAnalysisIdentity: () => 'same',
    pendingAnalysis: {identity:'same',body:{data_versions:{day:'old',m60:'same'}}},
    activeChartVersion:{code:'a',freq:'day',data_version:'new'}};
  runSlices(context, ['analysisFreshness']);
  assert.equal(typeof context.updateAnalysisFreshness,'function');
  context.updateAnalysisFreshness();
  assert.equal(note.hidden,false);
  assert.match(note.textContent,/数据不同步/);
  context.activeChartVersion.freq='m60'; context.activeChartVersion.data_version='same';
  context.updateAnalysisFreshness(); assert.equal(note.hidden,true);
  context.activeChartVersion.freq='m5'; context.updateAnalysisFreshness(); assert.equal(note.hidden,true);
  context.pendingAnalysis=null; context.updateAnalysisFreshness(); assert.equal(note.hidden,true);
  console.log('combined analysis freshness UI checks passed');
}

// Run both production fetch paths so response ordering exercises their callbacks.
function makeAnalysisLoaderHarness() {
  const shown = [], chartShown = [];
  const context = appContext({
    viewState: { adjust: 'qfq', token: null, analysisTokens: null },
    renderAnalysis:(body,note) => shown.push({body,note}), renderChart:body => chartShown.push(body),
  });
  runSlices(context, ['aiPanel', 'helpers', 'etag', 'load']);
  const pending = context._harness.pending;
  return {context, pending, shown, chartShown};
}
const aiOf = pending => pending.filter(r => r.url.startsWith('/api/analysis?'));
const chartsOf = pending => pending.filter(r => r.url.startsWith('/api/chart?'));
const okBody = extra => Object.assign({rule_profile:'strict',calculation_id:'strict-id',schema_version:'chanpy_v2',signal_scope:'expanded',status:'ok'}, extra);

(async function () {
  const {context,pending,shown,chartShown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const chart = chartsOf(pending)[0];
  const analysis = aiOf(pending)[0];
  assert.ok(chart && analysis, 'manual analysis can run alongside chart loading');
  chart.resolve({ok:true,json:async () => okBody({meta:{data_version:'chart-first-v1'}})});
  await tick();
  assert.equal(chartShown.length, 1);
  assert.equal(shown.length, 0);
  analysis.resolve({ok:true,json:async () => okBody({data_version:'chart-first-v1'})});
  await tick();
  assert.equal(shown.length, 1, 'matching AI renders when chart completed first');
  assert.equal(shown[0].body.data_version, 'chart-first-v1');
  assert.equal(shown[0].note, null);
  console.log('chart-first real analysis loader checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

(async function () {
  {
    const {context,pending,shown} = makeAnalysisLoaderHarness();
    context.loadAnalysis({manual:true,refresh:true});
    pending[0].resolve({ok:true,status:200,json:async () => okBody({status:'unconfigured'})});
    await tick();
    assert.equal(pending.length, 2, 'unconfigured starts the real static sample fetch');
    assert.equal(pending[1].url, '/analysis.sample.json');
    const originalController = context.analysisSlots[Object.keys(context.analysisSlots)[0]].abort;
    context.viewState.adjust = 'raw';
    pending[1].resolve({ok:true,json:async () => ({current_state:'late sample',scenarios:[]})});
    await tick();
    assert.equal(context.analysisSlots[Object.keys(context.analysisSlots)[0]].abort, originalController, 'exercise identity guard independently of abort guard');
    assert.equal(shown.length, 0, 'unconfigured sample arriving after an adjust change is discarded');
  }
  // 接口失败（502/503/网络）：不回落静态样例冒充分析，直接提示不可用
  for (const failure of [{ok:false,status:502}, {ok:false,status:503}, null]) {
    const {context,pending,shown} = makeAnalysisLoaderHarness();
    context.loadAnalysis({manual:true,refresh:true});
    if (failure) pending[0].resolve(failure); else pending[0].reject(new Error('offline'));
    await tick(); await tick(); await tick();
    assert.equal(pending.some(r => r.url === '/analysis.sample.json'), false, 'failure does not fetch the static sample');
    assert.equal(shown.length, 0, 'failure renders no analysis body');
    assert.match(context.el('aiPanel').innerHTML, /完全分类不可用/);
  }
  console.log('late static sample real loader checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const first = aiOf(pending)[0];
  const slotKey = code => Object.keys(context.analysisSlots).find(k => k.startsWith('["' + code + '"'));
  const controller = context.analysisSlots[slotKey('a')].abort;
  context.state.freq = 'm5';
  context.load();
  assert.equal(aiOf(pending).length, 1, 'period change reuses in-flight joint analysis');
  assert.equal(controller.signal.aborted, false);
  first.resolve({ok:true,json:async () => okBody({analysis_scope:'multi_timeframe',data_version:'joint',data_versions:{day:'d',m60:'h',m30:'m'}})});
  await tick();
  assert.equal(shown.length, 1, 'joint result renders without matching single-chart hash');
  const rendersBeforeSwitch = shown.length;
  context.state.freq = 'm15'; context.load();
  assert.equal(shown.length, rendersBeforeSwitch, 'period changes preserve rendered AI DOM');
  assert.equal(aiOf(pending).length, 1, 'completed analysis survives period changes');
  context.load({refresh:true});
  assert.equal(aiOf(pending).length, 1, 'scheduled refresh does not request AI');
  context.loadAnalysis({manual:true,refresh:true});
  const old = aiOf(pending)[1];
  context.state.code = 'b'; context.load();
  old.resolve({ok:true,json:async () => okBody({data_version:'wrong-stock'})});
  await tick();
  assert.equal(shown.length, 1, 'late old-stock result is discarded');
  context.loadAnalysis({manual:true,refresh:true});
  const next = aiOf(pending).at(-1);
  context.viewState.adjust = 'raw'; context.load();
  next.resolve({ok:true,json:async () => okBody({data_version:'wrong-adjust'})});
  await tick();
  assert.equal(shown.length, 1, 'late old-adjust result is discarded');
  console.log('stock-scoped joint analysis continuity checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.loadAnalysis({manual:true,refresh:true});
  pending[0].resolve({ok:true,json:async () => okBody({data_version:'joint-before'})});
  await tick();
  runSlices(context, ['aiRefresh']);
  context.el('aiRefresh').onclick();
  assert.equal(pending.length, 2, 'refresh button explicitly checks latest joint data');
  assert.equal(shown.length, 1, 'previous result stays visible while refreshing');
  pending[1].resolve({ok:true,json:async () => okBody({data_version:'joint-after'})});
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
  assert.equal(aiOf(pending).length, 2,
    '切回在飞的规则身份复用原分析请求，不重复发');
  const body = okBody({meta:{}});
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
      groups.push({requests,body:{rule_profile:profile,signal_scope:scope,schema_version:'chanpy_v2',calculation_id:profile+'-'+scope,status:'ok',meta:{}}});
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
  const slotOf = code => context.analysisSlots[Object.keys(context.analysisSlots).find(k => k.startsWith('["' + code + '"'))];
  context.load();
  context.load({refresh:true});
  context.state.freq = 'm30'; context.load();
  context.state.code = 'b'; context.load();
  context.state.ruleProfile = 'relaxed'; context.load();
  context.state.signalScope = 'standard'; context.load();
  context.viewState.adjust = 'raw'; context.load();
  context.loadAnalysis();
  context.loadAnalysis({refresh:true});
  assert.equal(aiOf(pending).length, 0, 'initial load, timer, selections and legacy calls cannot trigger AI');
  assert.match(context.el('aiPanel').innerHTML, /点击刷新/);
  assert.equal(context.el('aiRefresh').disabled, false);
  runSlices(context, ['aiRefresh']);
  context.el('aiRefresh').onclick();
  context.el('aiRefresh').onclick();
  assert.equal(aiOf(pending).length, 1, 'only the explicit button starts AI and in-flight clicks are deduplicated');
  assert.match(context.el('aiPanel').innerHTML, /加载中/, 'first manual request shows loading feedback');
  const controller = slotOf('b').abort;
  context.state.code = 'c'; context.load();
  assert.equal(controller.signal.aborted, false, '切走不再中止在飞请求');
  assert.equal(context.el('aiRefresh').disabled, false, 'changing stock releases disabled button');
  assert.equal(context.pendingAnalysis, null);
  aiOf(pending)[0].resolve({ok:true,json:async()=>({rule_profile:'relaxed',signal_scope:'standard',schema_version:'chanpy_v2',calculation_id:'id',status:'ok'})});
  await tick();
  assert.equal(shown.length, 0, 'old-stock result lands in its slot without rendering for another stock');
  assert.equal(slotOf('b').body.status, 'ok', '结果按身份落槽保留');
  assert.equal(aiOf(pending).length, 1, 'stock change does not issue any AI request');
  context.state.code = 'b'; context.load();
  assert.equal(shown.length, 1, '切回直接恢复槽内结果，无需重新点击刷新');
  console.log('manual-only AI trigger and selection invalidation checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.load();
  context.loadAnalysis({manual:true,refresh:true});
  const req = aiOf(pending)[0];
  context.state.code = 'b'; context.load();
  assert.match(context.el('aiPanel').innerHTML, /点击刷新/);
  context.state.code = 'a'; context.load();
  assert.match(context.el('aiPanel').innerHTML, /加载中/, '切回在飞中的股票恢复加载态');
  assert.equal(context.el('aiRefresh').disabled, true, '在飞身份切回后按钮保持禁用');
  req.resolve({ok:true,json:async () => okBody({data_version:'v1'})});
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
  assert.equal(aiOf(pending).length, 11);
  console.log('analysis slot cap eviction checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

// ---------- 令牌与 409：分析期间数据更新 → 整窗重载、提示、不自动重发 ----------

// 身份含复权模式：切换 adjust 即切到另一槽位
{
  const {context} = makeAnalysisLoaderHarness();
  const qfq = context.currentAnalysisIdentity();
  context.viewState.adjust = 'raw';
  assert.notEqual(context.currentAnalysisIdentity(), qfq, '分析身份包含复权模式');
}

(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  const tokens = {day:'d 1', m60:'h/1', m30:null};
  context.load();
  chartsOf(pending)[0].resolve(okJson(okBody({kline:[], macd:{rows:[]}, meta:{token:'T1', analysis_tokens:tokens}}), '"c1"'));
  await tick(); await tick();
  context.loadAnalysis({manual:true,refresh:true});
  const url = aiOf(pending)[0].url;
  assert.ok(url.includes('&adjust=qfq'), 'AI 请求携带当前复权模式');
  assert.ok(url.includes('&tokens=' + encodeURIComponent(JSON.stringify(tokens))),
    'AI 请求携带主图给出的 analysis_tokens（JSON 后 URL 编码）');

  // 分析期间数据更新：409
  const chartsBefore = chartsOf(pending).length;
  aiOf(pending)[0].resolve(errJson(409, {detail:'数据已更新', tokens:{day:'d2', m60:'h1', m30:'m1'}}));
  await tick(); await tick(); await tick();
  assert.equal(pending.some(r => r.url === '/analysis.sample.json'), false, '409 不回落静态样例');
  assert.equal(shown.length, 0, '409 不渲染任何分析');
  assert.match(context.el('aiPanel').innerHTML, new RegExp(REANALYZE), '提示数据已更新，请重新分析');
  assert.equal(chartsOf(pending).length, chartsBefore + 1, '409 触发一次整窗重载');
  const reload = chartsOf(pending).at(-1);
  assert.equal(reload.options.headers['If-None-Match'], undefined, '整窗重载不带条件请求，保证拿到新令牌');
  assert.equal(context.viewState.token, null, '旧视图令牌作废');
  assert.equal(aiOf(pending).length, 1, '409 后不自动重发分析请求');

  // 重载完成：拿到新令牌，仍不自动重发；提示保留到用户手动刷新
  const fresh = {day:'d2', m60:'h1', m30:'m1'};
  reload.resolve(okJson(okBody({kline:[], macd:{rows:[]}, meta:{token:'T2', analysis_tokens:fresh}}), '"c2"'));
  await tick(); await tick();
  assert.equal(aiOf(pending).length, 1, '重载完成后仍不自动重发（避免重复调用模型）');
  assert.match(context.el('aiPanel').innerHTML, new RegExp(REANALYZE), '重载不冲掉重新分析提示');
  const slot = Object.values(context.analysisSlots)[0];
  assert.equal(slot.inFlight, false, '409 后释放在飞标记，允许手动重发');

  // 手动再点：带新令牌发出
  context.loadAnalysis({manual:true,refresh:true});
  assert.equal(aiOf(pending).length, 2, '用户手动刷新才重发');
  assert.ok(aiOf(pending)[1].url.includes('&tokens=' + encodeURIComponent(JSON.stringify(fresh))), '重发使用重载后的新令牌');
  assert.match(context.el('aiPanel').innerHTML, /加载中/, '手动重发时面板回到加载态');
  console.log('analysis 409 whole-window reload without resend checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

// 缺令牌（主图尚未给出分析令牌）：不带 tokens 参数发出，服务端 409 时同样处理
(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.loadAnalysis({manual:true,refresh:true});
  const url = aiOf(pending)[0].url;
  assert.ok(url.includes('&adjust=qfq'));
  assert.equal(url.includes('tokens='), false, '没有分析令牌时不伪造 tokens 参数');
  aiOf(pending)[0].resolve(errJson(409, {detail:'数据已更新', tokens:{day:'d1', m60:'h1', m30:'m1'}}));
  await tick(); await tick(); await tick();
  assert.equal(pending.some(r => r.url === '/analysis.sample.json'), false);
  assert.equal(shown.length, 0);
  assert.match(context.el('aiPanel').innerHTML, new RegExp(REANALYZE));
  assert.equal(chartsOf(pending).length, 1, '缺令牌的 409 同样整窗重载');
  assert.equal(aiOf(pending).length, 1, '缺令牌的 409 同样不自动重发');
  console.log('analysis missing-token 409 checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

// 409 到达时用户已切走：不改当前面板、不重载当前图表
(async function () {
  const {context,pending} = makeAnalysisLoaderHarness();
  context.loadAnalysis({manual:true,refresh:true});
  context.state.code = 'b'; context.load();
  const panel = context.el('aiPanel').innerHTML;
  const charts = chartsOf(pending).length;
  aiOf(pending)[0].resolve(errJson(409, {detail:'数据已更新', tokens:{}}));
  await tick(); await tick(); await tick();
  assert.equal(context.el('aiPanel').innerHTML, panel, '切走后迟到的 409 不改当前面板');
  assert.equal(chartsOf(pending).length, charts, '切走后迟到的 409 不重载别的标的');
  assert.equal(pending.some(r => r.url === '/analysis.sample.json'), false);
  console.log('late analysis 409 after switching away checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

// demo 即使服务端继承 key，也明确关闭 AI，不伪装未配置或读取旧分析。
(async function () {
  const {context,pending,shown} = makeAnalysisLoaderHarness();
  context.loadAnalysis({manual:true,refresh:true});
  pending[0].resolve(okJson(okBody({status:'disabled'})));
  await tick();
  assert.equal(pending.length, 1, '关闭 AI 不加载分析或静态样例');
  assert.match(context.el('aiPanel').innerHTML, /demo 模式不调用 AI/);
  assert.equal(shown.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });

// 周期组合属于结果身份：切换组合不能恢复另一个组合的 AI 结果。
{
  const {context} = makeAnalysisLoaderHarness();
  context.viewState.analysisTokens = {day: 'd'};
  const day = context.currentAnalysisIdentity();
  context.viewState.analysisTokens = {day: 'd', m60: 'h'};
  assert.notEqual(context.currentAnalysisIdentity(), day, '组合必须区分 AI 槽位');
  const hour = context.currentAnalysisIdentity();
  context.viewState.analysisTokens = {day: 'new-day', m60: 'new-hour'};
  assert.equal(context.currentAnalysisIdentity(), hour, '相同组合的新数据只提示刷新，保留手动结果');
}

// 409 后整窗重载拿到另一组合（期间周期偏好改了）：仍提示重新分析，不被换组合的空槽提示冲掉。
(async function () {
  const {context,pending} = makeAnalysisLoaderHarness();
  context.load();
  chartsOf(pending)[0].resolve(okJson(okBody({kline:[], macd:{rows:[]}, meta:{token:'T1', analysis_tokens:{day:'d1', m60:'h1', m30:'m1'}}}), '"c1"'));
  await tick(); await tick();
  context.loadAnalysis({manual:true,refresh:true});
  aiOf(pending)[0].resolve(errJson(409, {detail:'数据已更新', tokens:{day:'d1'}}));
  await tick(); await tick(); await tick();
  chartsOf(pending).at(-1).resolve(okJson(okBody({kline:[], macd:{rows:[]}, meta:{token:'T2', analysis_tokens:{day:'d1'}}}), '"c2"'));
  await tick(); await tick();
  assert.match(context.el('aiPanel').innerHTML, new RegExp(REANALYZE), '换组合的重载保留重新分析提示');
  // 提示只跟随这一次重载：之后再换组合回到普通空槽提示
  context.viewState.analysisTokens = {day:'d1', m60:'h1'};
  context.syncManualAnalysis();
  assert.match(context.el('aiPanel').innerHTML, /点击刷新生成分析/);
  console.log('analysis 409 note survives combination change on reload checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});
