'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const note = {};
const context = {el: () => note, currentAnalysisIdentity: () => 'same',
  pendingAnalysis: {identity:'same',body:{data_versions:{day:'old',m60:'same'}}},
  activeChartVersion:{code:'a',freq:'day',data_version:'new'}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function updateAnalysisFreshness('),source.indexOf('  function queueAnalysis(')),context);
assert.equal(typeof context.updateAnalysisFreshness,'function');
context.updateAnalysisFreshness();
assert.equal(note.hidden,false);
assert.match(note.textContent,/数据不同步/);
context.activeChartVersion.freq='m60'; context.activeChartVersion.data_version='same';
context.updateAnalysisFreshness(); assert.equal(note.hidden,true);
context.activeChartVersion.freq='m5'; context.updateAnalysisFreshness(); assert.equal(note.hidden,true);
context.pendingAnalysis=null; context.updateAnalysisFreshness(); assert.equal(note.hidden,true);
console.log('combined analysis freshness UI checks passed');
