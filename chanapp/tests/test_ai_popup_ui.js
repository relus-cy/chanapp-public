'use strict';
const assert = require('node:assert/strict');
const { runSlices } = require('./support/dom.js');
async function refreshFailure(pending) {
  const nodes = {}, events = [];
  const context = {
    state: {code:'a',ruleProfile:'strict',signalScope:'expanded'},
    viewState:{adjust:'qfq',token:null,analysisTokens:null}, loadedTarget:null, analysisIdentity:'same',
    currentAnalysisIdentity:()=>'same', pendingAnalysis:pending,
    ruleSwitchNote:null, AbortController, ANALYSIS_TIMEOUT_MS:150000, AI_SPIN_MIN:0,
    setTimeout:()=>1, clearTimeout(){},
    el:id=>nodes[id] || (nodes[id]={innerHTML:'old',classList:{add(){},remove(){}}}),
    fetch:()=>Promise.reject(new Error('offline')),
    fetchAnalysisSample:()=>Promise.reject(new Error('sample offline')),
    closeAiPopup:()=>events.push('closed'),
  };
  runSlices(context, ['aiSlots', 'loadAnalysis']);
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
