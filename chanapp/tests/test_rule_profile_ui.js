'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const context = {P:{up:'#ff0000',down:'#0000ff',gold:'#aaaa00'},toTime:x=>x,hexA:(c,a)=>c+':'+a};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function signalLabel('),source.indexOf('  // ---------- 依据卡')),context);
const points = [
 {side:'buy',level:'bi',types:['1p','2'],status:'confirmed',dt:'2026-01-01'},
 {side:'sell',level:'seg',types:['3a','3b'],status:'provisional',dt:'2026-01-01'},
 {side:'buy',level:'bi',types:['2s'],status:'provisional',dt:'2026-01-02'}];
const markers = context.buildMarkers(points);
assert.equal(markers.length,3);
assert.equal(markers[0].text,'B1p/B2');
assert.equal(markers[1].text,'段 ~S3a/S3b');
assert.equal(markers[1].size,2);
assert.equal(markers[1].color,'#aaaa00:0.45');
assert.equal(markers[2].text,'~B2s');
assert.equal(markers[2].position,'belowBar');
assert.equal(context.signalLabel(points[1]),'段 S3a/S3b · 形成中');
assert.match(source,/rule_profile=' \+ encodeURIComponent\(profile\)/);
assert.doesNotMatch(source,/forming_signal/);
assert.match(source,/signal_scope=' \+ encodeURIComponent\(scope\)/);
assert.match(source,/schema_version === 'chanpy_v2'/);
console.log('native multi-type signal and rule profile UI checks passed');

const nodes = {}, saved = {}, range = {from:10,to:20};
const scopeBtns = [{dataset:{scope:'standard'},classList:{toggle(){}}},{dataset:{scope:'expanded'},classList:{toggle(){}}}];
const ruleBtns = [
 {dataset:{profile:'strict'},classList:{toggle(){}}},
 {dataset:{profile:'relaxed'},classList:{toggle(){}}}];
Object.assign(context, {
 state:{code:'a',freq:'m30',ruleProfile:'strict',signalScope:'expanded'},
 el:id=>nodes[id]||(nodes[id]= id==='ruleProfile'
   ? {querySelectorAll:()=>ruleBtns, addEventListener:(ev,fn)=>{nodes.__ruleHandler=fn;}, classList:{add(){}}, innerHTML:''}
   : id==='signalScope' ? {querySelectorAll:()=>scopeBtns,addEventListener:(ev,fn)=>{nodes.__scopeHandler=fn;}} : {classList:{add(){}},innerHTML:''}),
 charts:{main:{timeScale:()=>({getVisibleRange:()=>range})}},
 localStorage:{setItem:(k,v)=>saved[k]=v},
 analysisIdentity:'old',analysisSlots:{old:{abort:new AbortController(),inFlight:true,body:null}},
 pendingAnalysis:{},activeChartVersion:{},lastChartData:{},
 ruleSwitchNote:null,
 renderEvidence(items){context.evidence=items;},load(){context.loads=(context.loads||0)+1;}
});
vm.runInContext(source.slice(source.indexOf('  function syncRuleControl()'),source.indexOf("  el('aiRefresh').onclick")),context);
const oldSlot=context.analysisSlots.old;
nodes.__ruleHandler({target:{closest:()=>ruleBtns[1]}});
assert.equal(saved['chanapp-rule-profile'],'relaxed');
assert.equal(context.state.code,'a'); assert.equal(context.state.freq,'m30');
assert.equal(context.supplyRange.range,range);
assert.equal(context.pendingAnalysis,null); assert.equal(context.analysisIdentity,null,'switch hands the panel to the new rule identity');
assert.equal(oldSlot.abort.signal.aborted,false,'switch keeps the old identity request alive for its slot');
assert.equal(context.evidence.length,0,'switch clears the evidence model as well as the displayed card');
assert.match(nodes.aiPanel.innerHTML,/成笔标准已切换/);
assert.equal(context.loads,1);
nodes.__ruleHandler({target:{closest:()=>ruleBtns[1]}}); assert.equal(context.loads,1);
nodes.__ruleHandler({target:{closest:()=>null}}); assert.equal(context.loads,1);
console.log('rule control persistence, AI panel handoff and visible range checks passed');

const lineContext = {
 c:{biSeries:{setData(v){this.v=v;}},xdSeries:{setData(v){this.v=v;}},formingBiSegs:[],formingXdSegs:[],
    main:{removeSeries(s){(this.removed=this.removed||[]).push(s);},
          addSeries(){const s={setData(v){s.data=v;}};(this.added=this.added||[]).push(s);return s;}}},
 data:{structure:{bi:[{dt0:'a',dt1:'b',y0:1,y1:2},{dt0:'b',dt1:'c',y0:2,y1:3,forming:true}],
                  xd:[{dt0:'a',dt1:'b',y0:1,y1:2},{dt0:'x',dt1:'y',y0:5,y1:9,forming:true},{dt0:'p',dt1:'q',y0:7,y1:4,forming:true}]}},
 segsToPoints:x=>x, toTime:x=>x, P:{goldA:'g',biForming:'bf'},
 LightweightCharts:{LineSeries:function(){},LineStyle:{Dashed:1}}};
vm.createContext(lineContext);
vm.runInContext(source.slice(source.indexOf('    c.biSeries.setData('),source.indexOf('    c.zsOverlay.setBoxes(data.structure.zs')),lineContext);
assert.equal(lineContext.c.biSeries.v.length,1);
assert.equal(lineContext.c.xdSeries.v.length,1);
assert.equal(lineContext.c.formingBiSegs.length,1);
assert.equal(JSON.stringify(lineContext.c.formingBiSegs[0].data),JSON.stringify([{time:'b',value:2},{time:'c',value:3}]));
assert.equal(lineContext.c.formingXdSegs.length,2);  // 不相交 forming 段各自独立 series，不拼线
assert.equal(JSON.stringify(lineContext.c.formingXdSegs[1].data),JSON.stringify([{time:'p',value:7},{time:'q',value:4}]));
console.log('native provisional lines render as independent series');

for (const profile of ['strict','relaxed']) {
  nodes.__ruleHandler({target:{closest:()=>ruleBtns[profile==='strict'?0:1]}});
  for (const scope of ['standard','expanded']) {
    const slot={abort:new AbortController(),inFlight:true,body:null};
    context.analysisSlots={old:slot};
    nodes.__scopeHandler({target:{closest:()=>scopeBtns[scope==='standard'?0:1]}});
    assert.equal(context.state.signalScope,scope); assert.equal(context.state.ruleProfile,profile);
    assert.equal(saved['chanapp-signal-scope'],scope); assert.equal(context.analysisIdentity,null);
    assert.equal(slot.abort.signal.aborted,false,'scope switch keeps the old identity request alive for its slot');
    assert.equal(context.supplyRange.range,range); assert.match(nodes.aiPanel.innerHTML,/提示范围已切换/);
  }
}
console.log('four orthogonal rule and scope selections preserve range and hand the AI panel over');

const evidenceCards=[];
context.document={createElement:()=>({dataset:{},setAttribute(){},focus(){}})};
context.esc=x=>String(x); context.fmtBarTime=x=>x;
context.el('cards').appendChild=card=>evidenceCards.push(card);
nodes.cards.contains=()=>false;
vm.runInContext(source.slice(source.indexOf('  function renderEvidence('),source.indexOf('  // ---------- AI 完全分类面板')),context);
context.renderEvidence([{...points[0],price:10,text:'力度算法：MACD同向柱峰值。原生力度比 1.2（增强）。力度仅作标注，不作硬过滤。'}]);
assert.equal(evidenceCards.length,1,'stronger native point remains visible');
assert.match(evidenceCards[0].innerHTML,/原生力度比 1.2（增强）/);
assert.match(evidenceCards[0].innerHTML,/MACD同向柱峰值/);
assert.equal(context.buildMarkers([{...points[0],strength:{value:1.2,state:'stronger'}}])[0].text,'B1p/B2','strength remains in evidence, not short chart label');
console.log('native strength evidence text renders without filtering or marker label inflation');

const stateInit=source.slice(source.indexOf('  var state ='),source.indexOf('  (function () {\n    var qs'));
for (const stored of [null,'standard','expanded','invalid']) {
  const initial={localStorage:{getItem:key=>key==='chanapp-signal-scope'?stored:null}};
  vm.createContext(initial); vm.runInContext(stateInit,initial);
  assert.equal(initial.state.signalScope,stored==='standard'?'standard':'expanded');
  assert.equal(initial.state.ruleProfile,'strict');
}
console.log('scope defaults to expanded and restores only supported stored values');
