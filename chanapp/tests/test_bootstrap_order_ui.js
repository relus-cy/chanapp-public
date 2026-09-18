'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

const src = source.slice(source.indexOf('  function loadWatchlist('), source.indexOf('  // 左栏行情快照'));
function run(items, code, opts) {
  const calls = [];
  const context = {
    state: { code: code, watchlist: [] },
    queueWatchlist: function (fn) { return fn(); },
    fetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve(items); } }); },
    renderWatchlist: function () { calls.push('render'); },
    load: function () { calls.push('load'); },
    setStatus: function () {},
  };
  vm.createContext(context);
  vm.runInContext(src, context);
  return context.loadWatchlist(opts).then(function () { return { calls: calls, code: context.state.code }; });
}
const items = [{ code: 'sh000001' }, { code: 'sz000001' }];
Promise.all([
  run(items, null).then(function (r) { assert.deepEqual(r.calls, ['render', 'load']); assert.equal(r.code, 'sh000001'); }),
  run(items, 'sz000001', { skipLoad: true }).then(function (r) { assert.deepEqual(r.calls, ['render'], '预选 code 在列表中：不再重复 load'); assert.equal(r.code, 'sz000001'); }),
  run(items, 'hk00700', { skipLoad: true }).then(function (r) { assert.deepEqual(r.calls, ['render', 'load'], '预选 code 不在列表：纠正后仍须 load'); assert.equal(r.code, 'sh000001'); }),
  run(items, 'sz000001').then(function (r) { assert.deepEqual(r.calls, ['render', 'load'], '无 skipLoad 保持旧行为'); }),
]).then(function () {
  const boot = source.slice(source.lastIndexOf('  initSupply().then('));
  assert.match(boot, /var preselected = !!state\.code;/);
  assert.match(boot, /if \(preselected\) load\(\);/);
  assert.match(boot, /loadWatchlist\(\{ skipLoad: preselected \}\)/);
  console.log('bootstrap order UI checks passed');
}).catch(function (e) { console.error(e); process.exit(1); });
