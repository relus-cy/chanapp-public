'use strict';
const assert = require('node:assert/strict');
const { mkEl, deferred, tick, runSlices } = require('./support/dom.js');
const { appContext, searchEnv } = require('./support/app.js');

function flush() {
  return Promise.resolve().then(() => Promise.resolve()).then(() => tick());
}

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

check('search clears stale candidates and ignores late results', async () => {
  const requests = [];
  const env = searchEnv({
    fetch(url) { const d = deferred(); requests.push({ url, ...d }); return d.promise; },
  });
  const { input, drop } = env._harness.search;
  const timers = env._harness.timers;
  const opened = env._harness.opened;

  assert.equal(drop.attrs.role, 'listbox');
  assert.ok(drop.id, 'listbox has an id');
  assert.equal(input.attrs['aria-controls'], drop.id);

  input.value = 'A'; input.oninput(); timers.shift()();
  requests[0].resolve({ ok: true, json: async () => [{ code: 'sh600000', name: 'A股' }] });
  await flush();
  assert.equal(drop.hidden, false);

  input.value = 'B'; input.oninput();
  assert.equal(drop.hidden, true, 'editing the query immediately invalidates old candidates');
  input.onkeydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
  // 有意改写（2026-09-29 第二阶段）：回车改为打开候选，旧查询的候选同样不能被打开
  assert.deepEqual(opened, [], 'Enter cannot open a candidate from the previous query');

  timers.shift()();
  input.onblur();
  requests[1].resolve({ ok: true, json: async () => [{ code: 'sz000001', name: 'B股' }] });
  await flush();
  assert.equal(drop.hidden, true, 'a result arriving after blur stays closed');
});

check('search Enter is IME-safe', async () => {
  const env = searchEnv({
    fetch: async () => ({ ok: true, json: async () => [{ code: 'sh600519', name: '贵州茅台' }] }),
  });
  const { input } = env._harness.search;
  const timers = env._harness.timers;
  input.value = '茅台'; input.oninput(); timers[0](); await flush();
  input.onkeydown({ key: 'Enter', isComposing: true, keyCode: 13, preventDefault() {} });
  input.onkeydown({ key: 'Enter', isComposing: false, keyCode: 229, preventDefault() {} });
  assert.deepEqual(env._harness.opened, []);
});

check('watchlist operations are ordered and continue after failure', async () => {
  const requests = [], statuses = [];
  const context = appContext({
    state: { watchlist: [{ code: 'sh600000' }], code: 'sh600000' },
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
  });
  runSlices(context, ['watchlistOps']);

  const first = context.removeWatch('sh600000');
  const second = context.addWatch('sh600519', '贵州茅台', null);
  await flush();
  assert.equal(requests.length, 1, 'only the first operation starts');
  requests[0].resolve({ ok: false, status: 500, json: async () => ({}) });
  await first;
  await flush();
  assert.equal(requests.length, 2, 'a failed operation does not block the queue');
  requests[1].resolve({ ok: true, status: 200, json: async () => [{ code: 'sh600000' }, { code: 'sh600519' }] });
  await second;
  assert.deepEqual(context.state.watchlist.map(w => w.code), ['sh600000', 'sh600519']);
  assert.match(context._harness.statuses[0], /删除失败 500/);
});

// 有意改写（2026-09-29 第二阶段）：移出自选退回搜索查看——当前图表保持不动，不清空也不切到别的自选；
// 「无当前代码时添加首个自选即选中」保持原行为。
check('removing the current item keeps viewing it and adding the first selects it when nothing is open', async () => {
  const requests = [];
  const context = appContext({
    state: { watchlist: [{ code: 'sh600000' }, { code: 'sh600519' }], code: 'sh600000' },
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
  });
  runSlices(context, ['watchlistOps']);

  const removing = context.removeWatch('sh600000');
  await flush();
  requests[0].resolve({ ok: true, json: async () => [{ code: 'sh600519' }] });
  await removing;
  assert.equal(context.state.code, 'sh600000', 'removed code stays open as a viewed code');
  assert.deepEqual(context.state.watchlist.map(w => w.code), ['sh600519']);
  assert.equal(context._harness.renders.filter(r => r === 'load').length, 0, 'no reload for the same chart');

  context.state.code = null;
  const adding = context.addWatch('sh600036', '招商银行', null);
  await flush();
  requests[1].resolve({ ok: true, json: async () => [{ code: 'sh600036', name: '招商银行' }] });
  await adding;
  assert.equal(context.state.code, 'sh600036');
  assert.equal(context._harness.renders.filter(r => r === 'load').length, 1);
});

check('tag editor keeps its code and does not replace a newer editor', async () => {
  const box = mkEl('div'), requests = [];
  const nodes = { f10Tags: box };
  function findById(node, id) {
    if (node.id === id) return node;
    for (const child of node.children) { const hit = findById(child, id); if (hit) return hit; }
    return null;
  }
  const context = appContext({
    state: { code: 'sh600000', watchlist: [
      { code: 'sh600000', tags: ['银行'] }, { code: 'sh600519', tags: ['白酒'] },
    ] },
    el(id) { return nodes[id] || findById(box, id); },
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
    queueWatchlist(run) { return run(); },
  });
  context.document.getElementById = id => nodes[id] || findById(box, id);
  runSlices(context, ['tags']);

  context.renderTags();
  const editA = box.children.find(node => node.id === 'f10TagEdit');
  assert.equal(editA.tagName, 'button');
  assert.equal(editA.type, 'button');
  editA.onclick();
  const labelA = box.children.find(node => node.tagName === 'label');
  const inputA = box.children.find(node => node.tagName === 'input');
  assert.equal(labelA.attrs.for, inputA.id);

  inputA.value = '新标签';
  context.state.code = 'sh600519';
  inputA.onkeydown({ key: 'Enter', isComposing: false, keyCode: 13, preventDefault() {} });
  assert.match(requests[0].url, /sh600000\/tags$/, 'save uses the code captured when editing started');
  assert.equal(inputA.readOnly, true, 'a submitted value cannot keep changing while its request is pending');
  assert.equal(inputA.attrs['aria-busy'], 'true');
  inputA.onkeydown({ key: 'Escape', preventDefault() {} });
  assert.equal(inputA.parentNode, box, 'Escape does not pretend to cancel an in-flight save');

  context.renderTags();
  box.children.find(node => node.id === 'f10TagEdit').onclick();
  const inputB = box.children.find(node => node.tagName === 'input');
  requests[0].resolve({ ok: true, json: async () => context.state.watchlist });
  await flush();
  assert.equal(inputB.parentNode, box, 'an older response does not replace the current editor');

  const before = requests.length;
  inputB.onkeydown({ key: 'Enter', isComposing: true, keyCode: 13, preventDefault() {} });
  inputB.onkeydown({ key: 'Enter', isComposing: false, keyCode: 229, preventDefault() {} });
  assert.equal(requests.length, before, 'IME confirmation does not save');
  inputB.onkeydown({ key: 'Escape', preventDefault() {} });
  assert.ok(box.children.find(node => node.id === 'f10TagEdit'), 'Escape cancels editing');
});

(async () => {
  let failures = 0;
  for (const item of checks) {
    try {
      await item.fn();
      console.log('ok - ' + item.name);
    } catch (error) {
      failures += 1;
      console.error('not ok - ' + item.name);
      console.error(error.stack || error);
    }
  }
  if (failures) process.exitCode = 1;
})();
