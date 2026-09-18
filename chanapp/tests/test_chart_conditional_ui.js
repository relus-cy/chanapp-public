'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

const start = source.indexOf('  var chartEtag = null;');
const end = source.indexOf('  // ---------- UI 骨架 ----------');
assert.ok(start > 0 && end > start, 'chartEtag 声明须位于状态变量区、UI 骨架之前');
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

assert.equal(typeof context.conditionalHeaders, 'function');
assert.equal(typeof context.rememberEtag, 'function');
// vm realm 产出的对象原型与本源不同，deepStrictEqual 会因原型不等判失败；浅拷贝回本源再比对。
function headers(code, freq, profile, scope) {
  return Object.assign({}, context.conditionalHeaders(code, freq, profile, scope));
}
assert.deepEqual(headers('sh000001', 'day', 'strict', 'expanded'), {});
context.rememberEtag('sh000001', 'day', 'strict', 'expanded', '"abc"');
assert.deepEqual(headers('sh000001', 'day', 'strict', 'expanded'), {'If-None-Match': '"abc"'});
assert.deepEqual(headers('sh000001', 'm30', 'strict', 'expanded'), {}, '不同 freq 不带旧 etag');
assert.deepEqual(headers('sh000001', 'day', 'relaxed', 'expanded'), {}, '不同 profile 不带旧 etag');
context.rememberEtag('sh000001', 'day', 'strict', 'expanded', null);
assert.deepEqual(headers('sh000001', 'day', 'strict', 'expanded'), {}, 'etag 为空时清除');

const loadSrc = source.slice(source.indexOf('  function load(options) {'), source.indexOf('  function clearSelection() {'));
assert.match(loadSrc, /headers:\s*conditionalHeaders\(state\.code, state\.freq, profile, scope\)/, 'load() 的 fetch 须带条件头');
assert.match(loadSrc, /r\.status === 304/, 'load() 须处理 304');
assert.match(loadSrc, /rememberEtag\(state\.code, state\.freq, profile, scope, r\.headers\.get\('ETag'\)\)/, '200 时须记录 ETag');
assert.match(loadSrc, /if \(ctl === chartAbort\) rememberEtag\(/, 'rememberEtag 须仅对未中止请求记录');
console.log('chart conditional request UI checks passed');
