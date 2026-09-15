'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function mkEl(tag) {
  const node = {
    tagName: String(tag || 'div').toLowerCase(), children: [], style: {}, attrs: {},
    hidden: false, value: '', textContent: '', parentNode: null, _html: '', _cls: new Set(),
    classList: {
      toggle(name, on) { (on === undefined ? !node._cls.has(name) : on) ? node._cls.add(name) : node._cls.delete(name); },
      contains(name) { return node._cls.has(name); },
    },
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    querySelector(selector) {
      if (!selector.startsWith('.')) return null;
      const name = selector.slice(1);
      for (const child of node.children) {
        if (child.classList.contains(name)) return child;
        const nested = child.querySelector(selector);
        if (nested) return nested;
      }
      return null;
    },
    querySelectorAll() { return node.children; },
    setAttribute(name, value) { node.attrs[name] = String(value); },
    getAttribute(name) { return node.attrs[name]; }, removeAttribute(name) { delete node.attrs[name]; },
    focus() { node.focused = true; }, blur() { if (node.onblur) node.onblur(); },
  };
  Object.defineProperty(node, 'className', {
    get() { return [...node._cls].join(' '); },
    set(value) { node._cls = new Set(String(value).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(node, 'innerHTML', {
    get() { return node._html; },
    set(value) {
      node.children.forEach(child => { child.parentNode = null; });
      node.children = [];
      node._html = String(value);
    },
  });
  Object.defineProperty(node, 'firstChild', { get() { return node.children[0] || null; } });
  return node;
}

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

function flush() {
  return Promise.resolve().then(() => Promise.resolve()).then(() => new Promise(resolve => setImmediate(resolve)));
}

const checks = [];
function check(name, fn) { checks.push({ name, fn }); }

check('search clears stale candidates and ignores late results', async () => {
  const input = mkEl('input'), drop = mkEl('div'), form = mkEl('form');
  form.q = input;
  form.querySelector = () => drop;
  const timers = [], requests = [], added = [];
  const context = {
    esc: String,
    addWatch(code, name) { added.push([code, name]); },
    setStatus() {},
    fetch(url) { const d = deferred(); requests.push({ url, ...d }); return d.promise; },
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
    document: { createElement: tag => mkEl(tag) },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var searchTimer'), source.indexOf('  // 点搜索区外部收起下拉')), context);
  context.wireSearch(form);

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
  assert.deepEqual(added, [], 'Enter cannot add a candidate from the previous query');

  timers.shift()();
  input.onblur();
  requests[1].resolve({ ok: true, json: async () => [{ code: 'sz000001', name: 'B股' }] });
  await flush();
  assert.equal(drop.hidden, true, 'a result arriving after blur stays closed');
});

check('search Enter is IME-safe', async () => {
  const input = mkEl('input'), drop = mkEl('div'), form = mkEl('form');
  form.q = input; form.querySelector = () => drop;
  const timers = [], added = [];
  const context = {
    esc: String, addWatch(code) { added.push(code); }, setStatus() {},
    fetch: async () => ({ ok: true, json: async () => [{ code: 'sh600519', name: '贵州茅台' }] }),
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
    document: { createElement: tag => mkEl(tag) },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var searchTimer'), source.indexOf('  // 点搜索区外部收起下拉')), context);
  context.wireSearch(form);
  input.value = '茅台'; input.oninput(); timers[0](); await flush();
  input.onkeydown({ key: 'Enter', isComposing: true, keyCode: 13, preventDefault() {} });
  input.onkeydown({ key: 'Enter', isComposing: false, keyCode: 229, preventDefault() {} });
  assert.deepEqual(added, []);
});

check('watchlist operations are ordered and continue after failure', async () => {
  const requests = [], statuses = [];
  const context = {
    state: { watchlist: [{ code: 'sh600000' }], code: 'sh600000' },
    renderWatchlist() {}, load() {}, setStatus(message) { statuses.push(message); },
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var watchlistQueue'), source.indexOf('  function renderTabs')), context);

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
  assert.match(statuses[0], /删除失败 500/);
});

check('removing the last item clears content and adding the first selects it', async () => {
  const requests = [];
  let clears = 0, loads = 0;
  const context = {
    state: { watchlist: [{ code: 'sh600000' }], code: 'sh600000' },
    renderWatchlist() {}, setStatus() {}, clearSelection() { clears += 1; }, load() { loads += 1; },
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  var watchlistQueue'), source.indexOf('  function renderTabs')), context);

  const removing = context.removeWatch('sh600000');
  await flush();
  requests[0].resolve({ ok: true, json: async () => [] });
  await removing;
  assert.equal(context.state.code, null);
  assert.equal(clears, 1);

  const adding = context.addWatch('sh600519', '贵州茅台', null);
  await flush();
  requests[1].resolve({ ok: true, json: async () => [{ code: 'sh600519', name: '贵州茅台' }] });
  await adding;
  assert.equal(context.state.code, 'sh600519');
  assert.equal(loads, 1);
});

check('tag editor keeps its code and does not replace a newer editor', async () => {
  const box = mkEl('div'), requests = [];
  const nodes = { f10Tags: box };
  function findById(node, id) {
    if (node.id === id) return node;
    for (const child of node.children) { const hit = findById(child, id); if (hit) return hit; }
    return null;
  }
  const context = {
    state: { code: 'sh600000', watchlist: [
      { code: 'sh600000', tags: ['银行'] }, { code: 'sh600519', tags: ['白酒'] },
    ] },
    el(id) { return nodes[id] || findById(box, id); }, esc: String, setStatus() {},
    document: { createElement: tag => mkEl(tag), getElementById(id) { return nodes[id] || findById(box, id); } },
    fetch(url, options) { const d = deferred(); requests.push({ url, options, ...d }); return d.promise; },
    queueWatchlist(run) { return run(); },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function renderTags'), source.indexOf('  function renderFlow(')), context);

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
