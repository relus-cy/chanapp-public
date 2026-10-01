'use strict';
/* 侧边栏三态：pinned 固定展开 / open 悬停浮出 / rail 窄栏；持久化与窄窗默认。
   CSS 断言只查「规则-属性」语义关系（选择器在某 @media 下声明了什么属性），
   装饰数值（padding/投影色值/缓动曲线/图标尺寸）不在此逐字锁定——
   布局与可用性契约由下方关系断言 + browser_ui_regression.js 的计算样式/几何检查接管。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { mkEl, runSlices } = require('./support/dom.js');
const { appContext } = require('./support/app.js');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');

/* <style> 逐字符扫描出的规则表：{sel, media, decls}。只针对本文件现有 CSS 语法，
   不是通用 parser：media 取最内层 @media 文本（@supports/@layer 等其他嵌套条件不感知，
   包进这类块会被当成无条件规则——现行文件没有，属已知窄道），声明值含 ;/{}/引号内
   花括号会错位（现行文件亦无）。@keyframes 只登记名字，内部百分比帧不产生规则。 */
const cssText = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n');
const cssRules = (() => {
  const src = cssText.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [], stack = [], names = new Set();
  let prelude = '';
  for (let i = 0; i <= src.length; i++) {
    const ch = src[i];
    if (ch === '{') { stack.push(prelude.trim()); prelude = ''; continue; }
    if (ch === '}') {
      const sel = stack.pop();
      const media = [...stack].reverse().find(s => s.startsWith('@media')) || '';
      if (sel && sel.startsWith('@keyframes')) names.add(sel.split(/\s+/)[1]);
      else if (sel && !sel.startsWith('@') && !stack.some(s => s.startsWith('@keyframes')) && prelude.trim()) {
        const decls = {};
        for (const d of prelude.split(';')) {
          const k = d.indexOf(':');
          if (k > 0) decls[d.slice(0, k).trim()] = d.slice(k + 1).trim();
        }
        for (const s of sel.split(',')) rules.push({ sel: s.trim(), media, decls });
      }
      prelude = '';
      continue;
    }
    if (i < src.length) prelude += ch;
  }
  rules.keyframes = names;
  return rules;
})();
/* 同一选择器可有分散的多个规则块：后者覆盖前者；media 默认 ''（只算无条件规则），
   查 @media 内声明时显式传 media 文本；返回最后定义该属性的值 */
const cssDecl = (sel, prop, media = '') => {
  let val;
  for (const r of cssRules) {
    if (r.sel === sel && r.media === media && prop in r.decls) val = r.decls[prop];
  }
  return val;
};

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
    window: { matchMedia: () => ({ matches: !!narrow, addEventListener() {} }), addEventListener(ev, fn) { listeners['win:' + ev] = fn; } },
    setTimeout, clearTimeout,
    _listeners: listeners, _saved: saved, _items: items,
  };
  runSlices(context, ['sidebar']);
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

// ---------- C. 结构契约：JS 依赖的 DOM 挂钩与交互入口 ----------
// 这些 id/标记是 vm 行为用例与 app.js el() 接线的前提；钩子消失时行为桩不会变红。
{
  for (const id of ['sidebar', 'sidebarPin', 'sidebarExpand', 'wlRailBtn', 'wlForm', 'wlCount', 'wlCountRail', 'wlItems'])
    assert.match(html, new RegExp('id="' + id + '"'), 'JS/皮肤接线依赖 #' + id);
  assert.match(html, /<aside id="sidebar">/, 'watchlist 是通高侧栏容器');
  /* 侧栏底部不再有方案切换区：自选列表之后直接闭合侧栏 */
  assert.match(html, /<div id="wlItems"><\/div>\s*<\/div>\s*<\/aside>/, '自选列表是侧栏最后一块');
  /* 窄窗展开钮默认收起，仅在「窄窗 + 窄栏」组合下出现（两条规则缺一不可） */
  assert.equal(cssDecl('#sidebarExpand', 'display'), 'none', '展开钮默认不显示');
  assert.equal(cssDecl('body.sb-rail #sidebarExpand', 'display', '@media (max-width: 1100px)'), 'flex',
    '展开钮仅窄窗窄栏显示');
  /* wlRailBtn 是 wlForm 常驻首子（按类名找已建表单，不能拿 firstChild 判空）且位于标签行之上 */
  assert.match(html, /id="wlForm"><button id="wlRailBtn"[\s\S]*?<\/button><\/div>\s*<div class="wl-head">/,
    'rail 版按钮是 wlForm 常驻首子且在标签行之前');
  console.log('C. sidebar structure checks passed');
}
// 星/删钮走 currentColor 内联 SVG（跟随主题/颜色态）：真实 renderWatchlist 渲染出的自选行里，
// 按钮内容只有 SVG（置顶态也不夹文本字形）
{
  const nodes = { wlItems: mkEl('div'), wlForm: mkEl('div') };
  const context = appContext({ wireSearch() {} });
  context.document.getElementById = id => nodes[id] || (nodes[id] = mkEl());
  runSlices(context, ['watchlistUi']);
  context.state.watchlist = [{ code: 'sh600000', name: 'A', starred: true }, { code: 'sz000001', name: 'B', starred: false }];
  context.renderWatchlist();
  const [starredRow, plainRow] = nodes.wlItems.children.map(c => c.innerHTML);
  assert.match(starredRow, /<button class="star on"[^>]*><svg class="ic-star"[^>]*stroke="currentColor"[^>]*>[\s\S]*?<\/svg><\/button>/,
    'starred row: star button holds only a currentColor SVG');
  assert.match(plainRow, /<button class="star"[^>]*><svg class="ic-star"[^>]*stroke="currentColor"[^>]*>[\s\S]*?<\/svg><\/button>/,
    'unstarred row: star button holds only a currentColor SVG');
  for (const row of [starredRow, plainRow])
    assert.match(row, /<button class="del"[^>]*><svg class="ic-x"[^>]*stroke="currentColor"[^>]*>[\s\S]*?<\/svg><\/button>/,
      'delete button holds only a currentColor SVG');
  console.log('C2. watchlist row icon rendering checks passed');
}

// ---------- E. 几何等高 + 展开文字编排（悬停补偿机制已随几何统一删除） ----------
// CSS 不变量：两态等高由「同名属性在两态规则中各自声明」表达（真值对齐由浏览器实测接管，
// 见 browser_ui_regression.js 逐行 railHeight/openHeight ≤1px 对比），此处只锁关系与机制。
{
  /* 占位而非移除：rail 态这些区块保留盒高（visibility 隐藏），总高两态一致的前提 */
  assert.equal(cssDecl('body.sb-rail .wl-head', 'visibility'), 'hidden', 'wl-head 占位隐藏而非 display:none');
  assert.equal(cssDecl('body.sb-rail #wlForm', 'visibility'), 'hidden', 'wlForm 占位隐藏');
  /* 隐藏块必须不可点（pointer-events 随占位一并关闭），否则浮层控件盖住窄栏行 */
  assert.equal(cssDecl('body.sb-rail #wlForm', 'pointer-events'), 'none', '隐藏的 wlForm 不接收指针');
  /* 名称列：rail 态居中 + 硬截断（clip 而非省略号，CJK 等宽截整字） */
  const railName = cssDecl('body.sb-rail #sidebar .item .row > span:first-child', 'text-overflow');
  assert.equal(railName, 'clip', '窄栏名称硬截断不带省略号');
  assert.equal(cssDecl('body.sb-rail #sidebar .item .row > span:first-child', 'text-align'), 'center', '窄栏名称居中');
  /* 涨幅行在 rail 态顶替 row2：必须有显式 height（空报价时行高不塌，守 rail/open 逐行等高）
     且与 row2 同口径（margin-top 对齐 row2 间距）。数值与展开态行高的相等关系在浏览器实测。 */
  const chgH = cssDecl('body.sb-rail #sidebar .item .chg', 'height');
  const row2H = cssDecl('#sidebar .item .row2', 'height');
  assert.ok(chgH, '窄栏涨幅行有显式高度（空报价不塌行）');
  assert.ok(row2H, '展开 row2 有显式行高');
  assert.equal(chgH, row2H, '窄栏涨幅行高与展开 row2 行高同口径');
  assert.equal(cssDecl('body.sb-rail #sidebar .item .chg', 'margin-top'),
    cssDecl('#sidebar .item .row2', 'margin-top'), '窄栏涨幅间距顶替 row2 间距');
  assert.ok(cssDecl('#sidebar .item .row', 'height'), '展开 row1 固定行高（基线/按钮不再撑行）');
  assert.equal(cssDecl('body.sb-rail #sidebar .item .row', 'height'), 'auto', '窄栏行高由两行堆叠行高之和决定');
  /* rail 态收窄行距但纵向 padding 不得变（变则破等高）：水平 padding 收窄、纵向沿用展开值 */
  const railPad = cssDecl('body.sb-rail #sidebar .item', 'padding');
  const openPad = cssDecl('#sidebar .item', 'padding');
  assert.ok(railPad && openPad, '两态各自声明 item padding');
  assert.equal(railPad.split(/\s+/)[0], openPad.split(/\s+/)[0], '窄栏与展开态纵向 padding 一致（逐行等高前提）');
  /* 文字编排：rail→展开挂 sb-anim-in；row2 在同一动画之上再多一个延迟项（asymmetric insertion，
     与 name/chg 规则的动画声明对比得出），时长/曲线/具体延迟值为皮肤参数不锁定 */
  assert.ok(cssRules.keyframes.has('sbTextIn'), '文字淡入关键帧存在');
  const nameAnim = cssDecl('body.sb-anim-in #sidebar .item .row > span:first-child', 'animation');
  const row2Anim = cssDecl('body.sb-anim-in #sidebar .item .row2', 'animation');
  assert.ok(nameAnim && nameAnim.includes('sbTextIn'), 'name/chg 参与 sb-anim-in 文字编排');
  assert.ok(row2Anim && row2Anim.includes('sbTextIn'), 'row2 参与同一文字编排');
  const timeTokens = a => (a.match(/(\d*\.?\d+)s\b/g) || []).length;
  assert.equal(timeTokens(row2Anim), timeTokens(nameAnim) + 1, 'row2 比 name/chg 多一个延迟项（错峰插入）');
  console.log('E2. geometry invariants and animation CSS checks passed');
}
// 可用性契约：选中可辨识、星/删钮「视觉隐身但可聚焦」、触屏常显、rail 版槽位互斥显隐
{
  /* 选中态必须有底/影任一可辨识标记（皮肤值不锁）；media=='' 限定无条件规则，挪进 @media 会红 */
  const active = cssRules.find(r => r.sel === '#sidebar .item.active' && r.media === '');
  assert.ok(active && (active.decls.background || active.decls['box-shadow']), '选中态有可辨识样式');
  /* 星/删钮默认透明度隐身且不接收指针，但保持 display:flex + 无 visibility（仍可 Tab 聚焦）；
     hover / focus-within / active 三路显现对两个钮各断言一遍（同组要求过脆：拆成两条等价规则会误红）；
     .star.on 常显仅 star 独有（del 无对应常驻态），不套到 .del 上 */
  for (const btn of ['.star', '.del']) {
    const sel = '#sidebar .item ' + btn;
    assert.equal(cssDecl(sel, 'opacity'), '0', btn + ' 默认透明度隐身');
    assert.equal(cssDecl(sel, 'pointer-events'), 'none', btn + ' 隐身态不接收指针');
    assert.equal(cssDecl(sel, 'display'), 'flex', btn + ' 隐身保持可聚焦（display 非 none）');
    assert.equal(cssDecl(sel, 'visibility'), undefined, btn + ' 不得回退 visibility 隐藏方案');
    for (const st of ['#sidebar .item:hover ' + btn, '#sidebar .item:focus-within ' + btn,
      '#sidebar .item.active ' + btn])
      assert.equal(cssDecl(st, 'opacity'), '1', st + ' 显现');
    /* 触屏（hover:none）常显：自动守卫，不得删除 */
    assert.equal(cssDecl(sel, 'opacity', '@media (hover: none)'), '1', '触屏 ' + btn + ' 常显');
    assert.equal(cssDecl(sel, 'pointer-events', '@media (hover: none)'), 'auto');
  }
  assert.equal(cssDecl('#sidebar .item .star.on', 'opacity'), '1', '已置顶星钮常显（star 独有）');
  /* rail 版槽位内容 absolute 出流 + 默认隐藏、仅窄栏可见；按钮与窄栏同宽且可点 */
  for (const sel of ['.wl-count-rail', '#wlRailBtn']) {
    assert.equal(cssDecl(sel, 'position'), 'absolute', sel + ' 出流不占槽位高度');
    assert.equal(cssDecl(sel, 'visibility'), 'hidden', sel + ' 默认隐藏');
    assert.equal(cssDecl('body.sb-rail ' + sel, 'visibility'), 'visible', sel + ' 仅窄栏可见');
  }
  assert.equal(cssDecl('body.sb-rail #wlRailBtn', 'pointer-events'), 'auto', '窄栏按钮可点');
  assert.match(cssDecl('#wlRailBtn', 'width') || '', /--rail-width/, 'rail 版按钮贴窄栏宽度');
  /* 搜索框是候选浮层锚点：wl-add relative + wl-drop absolute 是下拉定位契约 */
  assert.equal(cssDecl('#sidebar .wl-add', 'position'), 'relative', '搜索框作为候选浮层锚点');
  assert.equal(cssDecl('#sidebar .wl-drop', 'position'), 'absolute', '候选浮层绝对定位于搜索框');
  /* 窄窗窄栏整条隐藏（translateX + visibility），展开钮接管入口 */
  assert.equal(cssDecl('body.sb-rail #sidebar', 'visibility', '@media (max-width: 1100px)'), 'hidden',
    '窄窗窄栏侧栏整条隐藏');
  console.log('F. sidebar usability contract checks passed');
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
