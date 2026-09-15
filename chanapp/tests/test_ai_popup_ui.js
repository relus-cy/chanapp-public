'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
async function refreshFailure(pending) {
  const nodes = {}, events = [];
  const context = {
    state: {code:'a',ruleProfile:'strict',signalScope:'expanded'}, supplyBusy:false,
    supplyState:{generation:1,epoch:'e'}, analysisIdentity:'same',
    currentAnalysisIdentity:()=>'same', pendingAnalysis:pending,
    ruleSwitchNote:null, AbortController, ANALYSIS_TIMEOUT_MS:150000, AI_SPIN_MIN:0,
    setTimeout:()=>1, clearTimeout(){},
    el:id=>nodes[id] || (nodes[id]={innerHTML:'old',classList:{add(){},remove(){}}}),
    fetch:()=>Promise.reject(new Error('offline')),
    fetchAnalysisSample:()=>Promise.reject(new Error('sample offline')),
    closeAiPopup:()=>events.push('closed'),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var AI_SLOT_MAX ='), source.indexOf('  var ruleSwitchNote =')), context);
  vm.runInContext(source.slice(source.indexOf('  function loadAnalysis('), source.indexOf('  // ---------- 数据口径条')), context);
  context.loadAnalysis({manual:true,refresh:true});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(nodes.aiPanel.innerHTML,/完全分类不可用/);
  assert.ok(events.length, 'failed manual refresh must close any old scenario details');
}
(async()=>{
  await refreshFailure(null);
  await refreshFailure({body:{}});
  console.log('AI popup failure lifecycle checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
