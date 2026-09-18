'use strict';
/* 侧边栏三态：pinned 固定展开 / open 悬停浮出 / rail 窄栏；持久化与窄窗默认 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');

function mkClassList() {
  const s = new Set();
  return { add: c => s.add(c), remove: c => s.delete(c), toggle: (c, on) => { (on === undefined ? !s.has(c) : on) ? s.add(c) : s.delete(c); }, contains: c => s.has(c) };
}
function mkItem() {
  const spans = { name: {style: {}}, chg: {style: {}}, row2: {style: {}} };
  return {
    querySelector(sel) { return sel.includes('first-child') ? spans.name : (sel.includes('.chg') ? spans.chg : spans.row2); },
    querySelectorAll() { return [spans.name, spans.chg, spans.row2]; },
    _spans: spans,
  };
}
function mkContext(savedVal, narrow) {
  const body = { classList: mkClassList() };
  const saved = savedVal ? { 'chanapp-sidebar': savedVal } : {};
  const listeners = {};
  const items = [mkItem(), mkItem(), mkItem()];
  /* pin 在 sidebar 内：focus(pin) 会像真实浏览器一样向 sidebar 冒泡 focusin */
  const pin = { classList: mkClassList(), setAttribute() {}, addEventListener(ev, fn) { listeners['pin:' + ev] = fn; },
    focus() { if (listeners['sidebar:focusin']) listeners['sidebar:focusin']({}); } };
  const sidebar = { addEventListener(ev, fn) { listeners['sidebar:' + ev] = fn; },
    contains(node) { return !!(node && node.inSidebar); }, matches() { return !!sidebar._hover; }, _hover: false,
    querySelectorAll(sel) { return sel === '.item' ? items : []; } };
  const nodes = { sidebarPin: pin, sidebar, sidebarExpand: { addEventListener() {}, focus() {} },
    wlRailBtn: { addEventListener(ev, fn) { listeners['railbtn:' + ev] = fn; } },
    wlForm: { querySelector() { return { focus() {} }; } } };
  const context = {
    document: { body, activeElement: null, hidden: false, addEventListener(ev, fn) { listeners['doc:' + ev] = fn; } },
    el: id => nodes[id],
    localStorage: { getItem: k => saved[k] || null, setItem: (k, v) => { saved[k] = v; } },
    window: { matchMedia: () => ({ matches: !!narrow }), addEventListener(ev, fn) { listeners['win:' + ev] = fn; } },
    setTimeout, clearTimeout,
    _listeners: listeners, _saved: saved, _items: items,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  // ---------- 侧边栏'), source.indexOf('  // ---------- 初始化')), context);
  return context;
}

// ---------- A. 三态切换与持久化 ----------
{
  const ctx = mkContext(null, false);
  assert.equal(typeof ctx.toggleSidebar, 'function', 'toggleSidebar should exist');
  ctx.initSidebar();
  const b = ctx.document.body.classList, saved = ctx._saved, L = ctx._listeners;
  assert.equal(b.contains('sb-rail') || b.contains('sb-open'), false, '宽窗默认固定展开');
  assert.equal(saved['chanapp-sidebar'], 'pinned', '初始持久化 pinned');
  assert.equal(ctx.document.body ? true : true, true);
  ctx.toggleSidebar();
  assert.equal(b.contains('sb-rail'), true, '固定展开 → 窄栏');
  assert.equal(saved['chanapp-sidebar'], 'rail');
  ctx.toggleSidebar();
  assert.equal(b.contains('sb-open'), true, '窄栏 → 浮动展开');
  assert.equal(b.contains('sb-rail'), false);
  assert.equal(saved['chanapp-sidebar'], 'rail', 'open 是临时态，不持久化');
  ctx.toggleSidebar();
  assert.equal(b.contains('sb-rail'), true, '浮动展开 → 窄栏');
  assert.equal(typeof L['pin:click'], 'function', '图钉按钮已接线');
  L['pin:click']();
  assert.equal(!b.contains('sb-rail') && !b.contains('sb-open'), true, '窄栏点图钉 → 固定展开');
  assert.equal(saved['chanapp-sidebar'], 'pinned');
  console.log('A. sidebar three-mode checks passed');
}

// ---------- B. 状态恢复与窄窗默认 ----------
{
  const ctx = mkContext('collapsed', false);
  ctx.initSidebar();
  assert.equal(ctx.document.body.classList.contains('sb-rail'), true, '旧键值 collapsed 映射到窄栏');
}
{
  const ctx = mkContext('expanded', false);
  ctx.initSidebar();
  const b = ctx.document.body.classList;
  assert.equal(!b.contains('sb-rail') && !b.contains('sb-open'), true, '旧键值 expanded 映射到固定展开');
}
{
  const ctx = mkContext(null, true);
  ctx.initSidebar();
  assert.equal(ctx.document.body.classList.contains('sb-rail'), true, '窄窗默认窄栏');
}
console.log('B. sidebar restore checks passed');

// ---------- D. 回归：浮出选中自选股收回后，焦点迁移不得借 focusin 重开 ----------
{
  const ctx = mkContext('rail', false);
  ctx.initSidebar();
  const b = ctx.document.body.classList, L = ctx._listeners;
  L['sidebar:mousemove']();
  assert.equal(b.contains('sb-open'), true, '窄栏悬停 → 浮动展开');
  /* 点击自选行：行有 tabIndex=0，点击即获得焦点；onclick 调 setSidebar('rail') 收回 */
  ctx.document.activeElement = { inSidebar: true };
  ctx.setSidebar('rail');
  assert.equal(b.contains('sb-rail'), true, '选中收回后不得被焦点迁移重开');
  assert.equal(b.contains('sb-open'), false);
  L['sidebar:mouseleave']();
  assert.equal(b.contains('sb-rail'), true, '鼠标离开后保持窄栏，不错失自动收回');
  /* Escape / 点外部走同一条 setSidebar('rail') + 焦点迁移路径，同样不得重开 */
  L['sidebar:mousemove']();
  assert.equal(b.contains('sb-open'), true, '再次悬停可正常浮出');
  ctx.setSidebar('rail');
  assert.equal(b.contains('sb-rail'), true, '再次收回仍稳定');
  console.log('D. sidebar focus-migration regression checks passed');
}

// ---------- G. 回归：标签页切换/窗口失焦收回悬停展开态，且不卡死再次悬停 ----------
{
  const ctx = mkContext('rail', false);
  ctx.initSidebar();
  const b = ctx.document.body.classList, L = ctx._listeners;
  L['sidebar:mousemove']();
  assert.equal(b.contains('sb-open'), true, '窄栏悬停 → 浮动展开');
  ctx.el('sidebar')._hover = true;  /* 指针仍停在栏上：切标签页没有 mouseleave，收回会读到 :hover 并置 suppressHover */
  ctx.document.hidden = true;
  L['doc:visibilitychange']();
  assert.equal(b.contains('sb-rail'), true, '标签页切走（document.hidden）悬停展开收回窄栏');
  assert.equal(b.contains('sb-open'), false);
  assert.equal(ctx.suppressHover, false, '收回后 suppressHover 显式复位（新交互回合）');
  L['sidebar:mousemove']();
  assert.equal(b.contains('sb-open'), true, '回来后指针移动即可再次悬停展开，未被卡死');
  /* window blur 同路收回 */
  L['win:blur']();
  assert.equal(b.contains('sb-rail'), true, '窗口失焦同样收回悬停展开');
  assert.equal(ctx.suppressHover, false, 'blur 收回同样复位 suppressHover');
  /* pinned 态不受 visibilitychange / blur 影响 */
  ctx.setSidebar('pinned');
  L['doc:visibilitychange']();
  L['win:blur']();
  assert.equal(!b.contains('sb-rail') && !b.contains('sb-open'), true, '固定展开不受切标签页/失焦影响');
  /* visible 回来（hidden=false）不触发收回 */
  ctx.setSidebar('rail');
  ctx.suppressHover = false;
  L['sidebar:mousemove']();
  assert.equal(b.contains('sb-open'), true);
  ctx.document.hidden = false;
  L['doc:visibilitychange']();
  assert.equal(b.contains('sb-open'), true, '回前台（visible）不收回');
  console.log('G. visibilitychange/blur reclaim checks passed');
}

// ---------- C. 结构：侧栏通高分组、供数收底、单图钉钮、行内 SVG 图标 ----------
{
  assert.match(html, /<aside id="sidebar">/, 'watchlist becomes a full-height sidebar');
  assert.match(html, /id="sidebarPin"/, 'sidebar hosts the pin button');
  assert.doesNotMatch(html, /id="sidebarToggle"|id="sidebarRailExpand"/, '旧的收起/窄栏展开按钮已移除');
  assert.match(html, /id="sidebarExpand"/, '窄窗顶栏保留展开钮');
  assert.match(html, /body\.sb-rail #sidebarExpand \{ display: flex; \}/, '展开钮仅窄窗窄栏显示');
  assert.match(html, /<details class="sb-supply" id="supplyWrap">/, 'supply control collapses into a bottom group');
  assert.doesNotMatch(source, /w\.starred \? '★ ' : ''/, 'star glyph replaced by inline SVG');
  assert.match(source, /class="ic-star"/, 'star renders as currentColor SVG');
  assert.match(source, /class="ic-x"/, 'delete renders as currentColor SVG');
  console.log('C. sidebar structure checks passed');
}

// ---------- E. 几何等高 + 展开文字编排（悬停补偿机制已随几何统一删除） ----------
// 源码层：不得再出现补偿/滚动校正
{
  assert.doesNotMatch(source, /hoverCompensation|alignHoveredRow|keepHoveredRowSteady|clearHoverShift/,
    '悬停补偿已移除：窄栏与展开逐行等高，展开零纵向位移');
  assert.doesNotMatch(source, /paddingBottom/, '不再用底部 padding 制造滚动空间');
  console.log('E1. hover compensation removal checks passed');
}
// CSS 不变量：两态等高、文字交错淡入、图钉窄栏大/展开小并随形态过渡
{
  assert.match(html, /body\.sb-rail \.wl-head \{ visibility: hidden; \}/, 'wl-head 占位隐藏而非 display:none');
  assert.match(html, /sb-rail #sidebar \.sb-group \{ padding: 0 5px; \}/, '窄栏分组左右 5px、上下 0（沿用皮肤 watchlist 配平）');
  assert.match(html, /sb-rail #sidebar \.item \.row > span:first-child \{[^}]*text-align: center; text-overflow: clip/,
    '窄栏名称居中硬截断（13px/1.55 与展开态同一口径）');
  assert.match(html, /sb-rail #sidebar \.item \.chg \{[^}]*height: 13px; line-height: 13px; text-align: center; margin-top: 6px/,
    '窄栏涨幅行盒顶替被隐藏的 row2（height 固定：空报价时行高不塌，守 rail/open 逐行等高）');
  assert.match(html, /#sidebar \.item \.row \{[^}]*height: calc\(1\.55 \* 13px\)/, '展开 row1 固定行高（基线/按钮不再撑行）');
  assert.match(html, /#sidebar \.item \.row2 \{[^}]*font: 10px\/13px var\(--mono\)[^}]*height: 13px/, '展开 row2 固定行高');
  assert.match(html, /body\.sb-rail #sidebar \.item \.row \{[^}]*height: auto/, '窄栏行高由两行堆叠行高之和决定');
  assert.match(html, /@keyframes sbTextIn/, '文字淡入关键帧存在');
  assert.match(html, /body\.sb-anim-in #sidebar \.item \.row2 \{ animation: sbTextIn \.26s cubic-bezier\(\.16, 1, \.3, 1\) \.09s both; \}/,
    'row2 延迟 90ms 插入（asymmetric insertion）');
  assert.match(html, /#sidebarPin svg \{ width: 17px; height: 17px;/, '展开态图钉 17px');
  assert.match(html, /body\.sb-rail #sidebarPin svg \{ width: 22px; height: 22px; \}/, '窄栏图钉 22px');
  console.log('E2. geometry invariants and animation CSS checks passed');
}
// 皮肤卡片化（Phase B）：卡片参数、选中态投影（金条已删）、钮透明度方案、计数/搜索框/品牌块
{
  assert.match(html, /#sidebar \.item \{\s*padding: 12px 11px; margin-bottom: 3px; border-radius: var\(--radius-m\)/,
    '卡片化参数（12px 纵向 padding + 3px 底距 + 8px 圆角）');
  assert.match(html, /#sidebar \.item\.active \{ background: var\(--panel\); box-shadow: 0 1px 3px rgba\(0,0,0,\.035\); \}/,
    '选中态 panel 底 + 轻投影（无金条）');
  assert.doesNotMatch(html, /inset 3px 0 0 var\(--gold\)/, '选中金条已全部移除');
  assert.match(html, /:root\[data-theme="dark"\] #sidebar \.item\.active \{ box-shadow: 0 1px 3px rgba\(0,0,0,\.32\); \}/,
    '暗色主题选中态换暗色投影');
  assert.match(html, /#sidebar \.item \.del, #sidebar \.item \.star \{[^}]*opacity: 0; pointer-events: none/,
    '星/删钮默认透明度隐身且不接收指针');
  assert.match(html, /#sidebar \.item\.active \.del, #sidebar \.item\.active \.star \{ opacity: 1; pointer-events: auto; \}/,
    '选中态星/删钮显现');
  assert.match(html, /@media \(hover: none\) \{ #sidebar \.item \.del, #sidebar \.item \.star \{ opacity: 1; pointer-events: auto; \} \}/,
    '触屏星/删钮常显');
  assert.match(html, /class="wl-count" id="wlCount"/, '「自选股 NN」计数元素存在');
  assert.match(html, /#sidebar \.wl-add \{\s*margin: 10px 12px 18px; display: flex; align-items: center; gap: 8px/,
    '搜索/添加框皮肤化（margin/flex/gap）');
  assert.match(html, /id="wlForm"><button id="wlRailBtn"[\s\S]*?<\/button><\/div>\s*<div class="wl-head">/, '搜索框槽位含 rail 版按钮，位于标签行与列表之上（皮肤顺序）');
  assert.match(html, /class="brand-mark">/, '品牌块金色软底 mark 存在');
  assert.match(html, /class="brand-caption">缠论结构 · 投研工作台</, '品牌副题文案');
  assert.doesNotMatch(html, /padding: 4px; visibility: hidden/, '星/删钮不再用 visibility 方案');
  /* rail 空档填充：占位槽内 rail 版内容 absolute 出流、互斥显隐，总高两态不变 */
  assert.match(html, /\.wl-count-rail, #wlRailBtn \{ position: absolute; visibility: hidden; \}/, 'rail 版内容默认隐藏且出流');
  assert.match(html, /body\.sb-rail \.wl-count-rail, body\.sb-rail #wlRailBtn \{ visibility: visible; \}/, 'rail 版内容仅窄栏可见');
  assert.match(html, /#wlRailBtn \{[^}]*width: calc\(var\(--rail-width\) - 1px\)/, 'rail 版按钮与窄栏同宽');
  assert.match(html, /class="wl-count-rail" id="wlCountRail"/, '窄栏自选计数元素存在');
  assert.match(html, /body\.sb-rail #sidebarPin \.sb-chev \{ transform: rotate\(180deg\); \}/, '收起/展开图标 chevron 两态翻转');
  console.log('F. sidebar skin CSS checks passed');
}
// 行为：rail→open / rail→pinned 触发交错淡入并按时清理；reduced-motion 整体跳过
(async () => {
  const ctx = mkContext('rail', false);
  ctx.initSidebar();
  const b = ctx.document.body.classList;
  ctx._listeners['sidebar:mousemove']();
  assert.equal(b.contains('sb-open'), true, '窄栏悬停 → 浮动展开');
  assert.equal(b.contains('sb-anim-in'), true, '进入展开态挂上文字编排类');
  assert.equal(ctx._items[0]._spans.name.style.animationDelay, '0ms', '首行无级联延迟');
  assert.equal(ctx._items[1]._spans.name.style.animationDelay, '12ms', '逐行 12ms 级联');
  assert.equal(ctx._items[1]._spans.row2.style.animationDelay, 'calc(.09s + 12ms)', 'row2 在级联之上再延迟 90ms');
  await new Promise(r => setTimeout(r, 700));
  assert.equal(b.contains('sb-anim-in'), false, '编排类按时清理');
  assert.equal(ctx._items[1]._spans.name.style.animationDelay, '', '级联内联样式清理干净');
  /* 图钉从窄栏直接固定展开走同一编排 */
  ctx.setSidebar('rail');
  assert.equal(b.contains('sb-anim-in'), false, '收回窄栏不触发编排');
  ctx.setSidebar('pinned');
  assert.equal(b.contains('sb-anim-in'), true, 'rail→pinned 同样触发文字编排');
  await new Promise(r => setTimeout(r, 700));
  /* reduced-motion（mock 以 narrow 复用 matchMedia=true）整体跳过编排 */
  const calm = mkContext('rail', true);
  calm.initSidebar();
  calm._listeners['sidebar:mousemove']();
  assert.equal(calm.document.body.classList.contains('sb-open'), true);
  assert.equal(calm.document.body.classList.contains('sb-anim-in'), false, 'reduced-motion 不挂编排类');
  console.log('E3. enter-animation orchestration checks passed');
})().catch(e => { console.error(e); process.exit(1); });
