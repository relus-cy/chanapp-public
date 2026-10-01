'use strict';
/* 侧栏跨断点：窄窗（≤1100px）侧栏是抽屉，没有固定展开态。宽窗存下的 pinned 偏好在窄窗不生效、
   也不被窄窗里的开合改写；窗口回到宽屏时恢复钉住。matchMedia 桩按查询串返回结果并记下 change
   监听，模拟拖动窗口跨断点；侧边栏切片实跑。 */
const assert = require('node:assert/strict');
const { mkEl, runSlices } = require('./support/dom.js');

function sidebarEnv(saved, width) {
  const body = mkEl('body');
  const store = saved == null ? {} : { 'chanapp-sidebar': saved };
  const listeners = {}, mediaListeners = [];
  const media = { width };
  const query = q => {
    const max = /max-width:\s*(\d+)px/.exec(q), min = /min-width:\s*(\d+)px/.exec(q);
    if (max) return media.width <= +max[1];
    if (min) return media.width >= +min[1];
    return false;  // prefers-reduced-motion 等：不匹配
  };
  const nodes = {};
  const node = id => nodes[id] || (nodes[id] = Object.assign(mkEl(), {
    addEventListener(ev, fn) { listeners[id + ':' + ev] = fn; },
    matches: () => false, contains: () => false,
  }));
  const context = {
    document: { body, activeElement: null, hidden: false, addEventListener(ev, fn) { listeners['doc:' + ev] = fn; } },
    el: node,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    window: {
      matchMedia(q) {
        return {
          get matches() { return query(q); },
          addEventListener(ev, fn) { if (ev === 'change') mediaListeners.push(() => fn({ matches: query(q) })); },
        };
      },
      addEventListener(ev, fn) { listeners['win:' + ev] = fn; },
    },
    setTimeout() { return 0; }, clearTimeout() {},
  };
  runSlices(context, ['sidebar']);
  context.initSidebar();
  return {
    context, store, listeners,
    classes: () => ['sb-rail', 'sb-open'].filter(c => body.classList.contains(c)),
    resize(w) { media.width = w; mediaListeners.forEach(fn => fn()); },
  };
}

// 宽屏钉住的偏好带进窄窗：抽屉收起，偏好原样保留
{
  const env = sidebarEnv('pinned', 820);
  assert.deepEqual(env.classes(), ['sb-rail'], '窄窗初始化：保存的 pinned 不展开抽屉');
  assert.equal(env.store['chanapp-sidebar'], 'pinned', '窄窗初始化不改写宽屏偏好');

  // 窄窗里手动开合抽屉：可打开，Esc 关闭，都不写偏好
  env.listeners['sidebarExpand:click']();
  assert.deepEqual(env.classes(), ['sb-open'], '窄窗展开钮打开抽屉');
  env.listeners['doc:keydown']({ key: 'Escape', target: {}, stopImmediatePropagation() {} });
  assert.deepEqual(env.classes(), ['sb-rail'], 'Esc 关闭抽屉');
  env.listeners['sidebarExpand:click']();
  env.listeners['doc:mousedown']({ target: { closest: () => null } });
  assert.deepEqual(env.classes(), ['sb-rail'], '点抽屉外关闭抽屉');
  assert.equal(env.store['chanapp-sidebar'], 'pinned', '窄窗开合不改写宽屏偏好');

  env.resize(1440);
  assert.deepEqual(env.classes(), [], '回到宽屏恢复钉住');
  assert.equal(env.store['chanapp-sidebar'], 'pinned');

  env.resize(390);
  assert.deepEqual(env.classes(), ['sb-rail'], '宽屏钉住时缩到窄窗：抽屉收起');
  assert.equal(env.store['chanapp-sidebar'], 'pinned', '缩窄不改写偏好');
  env.resize(1440);
  assert.deepEqual(env.classes(), [], '再回宽屏仍钉住');
}

// 宽屏偏好为窄栏：跨断点来回都保持窄栏，不被窄窗默认值改写
{
  const env = sidebarEnv('rail', 1440);
  assert.deepEqual(env.classes(), ['sb-rail']);
  env.resize(820);
  assert.deepEqual(env.classes(), ['sb-rail']);
  env.resize(1440);
  assert.deepEqual(env.classes(), ['sb-rail'], '宽屏偏好为窄栏时回宽屏仍是窄栏');
  assert.equal(env.store['chanapp-sidebar'], 'rail');
}

// 宽屏浮动展开（悬停）时缩到窄窗：临时浮层收起
{
  const env = sidebarEnv('rail', 1440);
  env.listeners['sidebar:mousemove']();
  assert.deepEqual(env.classes(), ['sb-open']);
  env.resize(820);
  assert.deepEqual(env.classes(), ['sb-rail'], '跨断点收起临时浮层');
  assert.equal(env.store['chanapp-sidebar'], 'rail');
}
console.log('sidebar breakpoint checks passed');
