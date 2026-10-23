'use strict';
// web/donchian.js 的纯函数单测：设置读取、绘制数据、标记样式、图例文字。数值来自后端，这里只验证呈现规则。
// 可能出错的方式：坏的本地存储值把开关弄成未知状态；null 线值画成 0；止损线从窗口起点而不是入场根开始画；
// 假突破画成实心、形成中或盘中触价的信号画成收盘确认的实心；只画了当前判定方式的标记（规格要求两种都画）；
// 两种方式在同一根同一线位的同类信号重复画；图例在空仓时仍显示止损。
// 止损线只画当前持有那笔、已平仓的止损消失（用户看到跌破标记却找不到被跌破的线）；相邻两笔之间、离场之后没断开；
// 卖出说明不写判定价，也不分止损还是离场线。
// 规则状态摘要（右栏状态卡）：持有时没盯离场价而盯了入场线；空仓时用了 next.entry 而不是后端给的接下来要判定的入场线；
// 与收盘的距离符号反了；最近信号取了另一种判定方式的；空仓时还显示入场价与止损。
const assert = require('node:assert');
const dc = require('../web/donchian.js');

const id = (t) => t;

// 设置：默认关闭 + 收盘确认；无法识别的值回到默认；读取抛错也回到默认
{
  const store = (o) => (k) => (k in o ? o[k] : null);
  assert.deepStrictEqual(dc.readSettings(store({})), { key: 'off', mode: 'close' });
  assert.deepStrictEqual(dc.readSettings(store({ 'chanapp-donchian': '55/20', 'chanapp-donchian-mode': 'touch' })),
    { key: '55/20', mode: 'touch' });
  assert.deepStrictEqual(dc.readSettings(store({ 'chanapp-donchian': '30/7', 'chanapp-donchian-mode': 'x' })),
    { key: 'off', mode: 'close' });
  assert.deepStrictEqual(dc.readSettings(() => { throw new Error('blocked'); }), { key: 'off', mode: 'close' });
}

const rows = [
  { time: '2026-01-01', entry: null, exit: null, n: null },
  { time: '2026-01-02', entry: 11, exit: 9, n: 2 },
  { time: '2026-01-03', entry: 11, exit: 9.5, n: 2.1 },
  { time: '2026-01-04', entry: 12, exit: 10, n: 2.2 },
];

// 线值 null 不出点
assert.deepStrictEqual(dc.lineData(rows, 'entry', id),
  [{ time: '2026-01-02', value: 11 }, { time: '2026-01-03', value: 11 }, { time: '2026-01-04', value: 12 }]);

// 止损线逐笔画：图表库的线段取起点的颜色，空白点也不会断线，所以笔外的点和离场根都给透明色——线从入场根画到离场根为止。
// 笔外的点取下一笔（之后没有就取上一笔）的止损作值，不把价格轴拉偏；一笔都没有时不出点
const G = 'transparent';
assert.deepStrictEqual(dc.stopData(rows, [{ entry_time: '2026-01-02', stop: 8, exit_time: '2026-01-03' }], id),
  [{ time: '2026-01-01', value: 8, color: G }, { time: '2026-01-02', value: 8 }, { time: '2026-01-03', value: 8, color: G },
   { time: '2026-01-04', value: 8, color: G }]);
// 前后两笔紧挨着（离场根的下一根就入场）也断开：离场根透明，下一根从新止损起画；持有中的那笔画到末根
assert.deepStrictEqual(dc.stopData(rows, [{ entry_time: '2026-01-01', stop: 8, exit_time: '2026-01-02' },
                                          { entry_time: '2026-01-03', stop: 7.5, exit_time: null }], id),
  [{ time: '2026-01-01', value: 8 }, { time: '2026-01-02', value: 8, color: G }, { time: '2026-01-03', value: 7.5 },
   { time: '2026-01-04', value: 7.5 }]);
assert.deepStrictEqual(dc.stopData(rows, [], id), []);
assert.deepStrictEqual(dc.stopData(rows, undefined, id), []);

// 标记样式
assert.deepStrictEqual(dc.markerStyle({ side: 'buy', kind: 'close', status: 'confirmed' }), { up: true, fill: 'solid', tilde: false });
assert.deepStrictEqual(dc.markerStyle({ side: 'buy', kind: 'touch', status: 'provisional' }), { up: true, fill: 'faint', tilde: true });
assert.deepStrictEqual(dc.markerStyle({ side: 'sell', kind: 'false_break', status: 'confirmed' }), { up: false, fill: 'hollow', tilde: false });
// 盘中触价即使已收盘也用形成中样式（与收盘确认的实心区分）；收盘确认落在形成中的 bar 上同样淡色加 ~
assert.deepStrictEqual(dc.markerStyle({ side: 'sell', kind: 'touch', status: 'confirmed' }), { up: false, fill: 'faint', tilde: true });
assert.deepStrictEqual(dc.markerStyle({ side: 'buy', kind: 'close', status: 'provisional' }), { up: true, fill: 'faint', tilde: true });

// 两种判定方式的标记合并：同根同方向同类同线位只留一个；淡色先画、空心其次、实心最后（实心压在上面）
{
  const s = (time, side, kind, level, status) => ({ time, side, kind, status: status || 'confirmed', price: level, level });
  const merged = dc.allSignals({ modes: {
    close: { signals: [s('2026-01-03', 'buy', 'close', 11), s('2026-01-04', 'buy', 'false_break', 12),
                       s('2026-01-05', 'buy', 'touch', 12, 'provisional')] },
    touch: { signals: [s('2026-01-03', 'buy', 'touch', 11), s('2026-01-05', 'buy', 'touch', 12, 'provisional')] },
  } });
  assert.deepStrictEqual(merged.map((x) => [x.time, x.kind]),
    [['2026-01-03', 'touch'], ['2026-01-05', 'touch'], ['2026-01-04', 'false_break'], ['2026-01-03', 'close']]);
  assert.deepStrictEqual(dc.allSignals(null), []);
}
assert.strictEqual(dc.signalText({ side: 'buy', kind: 'close', status: 'provisional' }), '收盘突破入场线（形成中）');
// 卖出说明分止损与离场线；带线位时数值放在「形成中」之前；旧数据没有 basis 时退回「离场价」
assert.strictEqual(dc.signalText({ side: 'sell', kind: 'close', basis: 'stop', level: 32.8979, status: 'confirmed' }, true),
  '收盘跌破止损 32.90');
assert.strictEqual(dc.signalText({ side: 'sell', kind: 'false_break', basis: 'exit', level: 29.5, status: 'confirmed' }, true),
  '假跌破：盘中跌破离场线 29.50，收盘收回');
assert.strictEqual(dc.signalText({ side: 'sell', kind: 'touch', basis: 'stop', level: 32.64, status: 'provisional' }, true),
  '盘中触及止损 32.64（形成中）');
assert.strictEqual(dc.signalText({ side: 'sell', kind: 'touch', basis: 'stop', status: 'confirmed' }), '盘中触及止损');
assert.strictEqual(dc.signalText({ side: 'sell', kind: 'close', status: 'confirmed' }), '收盘跌破离场价');

// 图例
{
  const payload = {
    rows,
    next: { entry: 12.5, exit: 10.2 },
    suggestion: { weight: 0.0789, stop_distance: 0.1267, risk_budget: 0.01 },
    modes: {
      close: { signals: [{ time: '2026-01-03', side: 'buy', kind: 'close', status: 'confirmed', price: 11.6, level: 11 }],
               trades: [{ entry_time: '2026-01-03', entry_price: 11.6, stop: 7.4, exit_time: null, exit_price: null }],
               state: { holding: true, entry_time: '2026-01-03', entry_price: 11.6, stop: 7.4, effective_exit: 10.2 } },
      touch: { signals: [{ time: '2026-01-02', side: 'buy', kind: 'touch', status: 'confirmed', price: 11, level: 11 },
                         { time: '2026-01-03', side: 'sell', kind: 'touch', basis: 'stop', status: 'confirmed', price: 9.1, level: 9.1 }],
               trades: [{ entry_time: '2026-01-02', entry_price: 11, stop: 9.1, exit_time: '2026-01-03', exit_price: 9.1 }],
               state: { holding: false } },
    },
  };
  // 收盘确认那笔从第 3 根才入场：第 2 根不显示止损，只列另一种方式在这根的入场
  assert.deepStrictEqual(dc.legendParts(payload, 1, 'close'),
    [{ label: '入', value: '11.00' }, { label: '出', value: '9.00' }, { label: 'N', value: '2.00' },
     { label: '', value: '盘中触及入场线 11.00' }]);
  assert.deepStrictEqual(dc.legendParts(payload, 2, 'close'),
    [{ label: '入', value: '11.00' }, { label: '出', value: '9.50' }, { label: 'N', value: '2.10' },
     { label: '止损', value: '7.40' }, { label: '', value: '收盘突破入场线 11.00' }, { label: '', value: '盘中触及止损 9.10' }]);
  // 已平仓那笔的止损在它的持有期内照样显示（含离场那根），所选方式的信号在前
  assert.deepStrictEqual(dc.legendParts(payload, 2, 'touch'),
    [{ label: '入', value: '11.00' }, { label: '出', value: '9.50' }, { label: 'N', value: '2.10' },
     { label: '止损', value: '9.10' }, { label: '', value: '盘中触及止损 9.10' }, { label: '', value: '收盘突破入场线 11.00' }]);
  // 最后一根也只给逐根读数；规则状态、接下来判定的线位与建议仓位在右栏状态卡（statusSummary）
  assert.deepStrictEqual(dc.legendParts(payload, 3, 'close'),
    [{ label: '入', value: '12.00' }, { label: '出', value: '10.00' }, { label: 'N', value: '2.20' },
     { label: '止损', value: '7.40' }]);
  // 盘中触价那笔已在第 3 根离场：之后不显示止损
  assert.deepStrictEqual(dc.legendParts(payload, 3, 'touch'),
    [{ label: '入', value: '12.00' }, { label: '出', value: '10.00' }, { label: 'N', value: '2.20' }]);
  assert.deepStrictEqual(dc.legendParts(null, 0, 'close'), []);
  assert.deepStrictEqual(dc.legendParts(payload, 9, 'close'), []);
}

// 规则状态摘要（右栏状态卡）：按所选判定方式，盯接下来要判定的那条线，并给出它与最新收盘的距离
{
  const payload = {
    rows,
    next: { entry: 12.5, exit: 10.2 },
    suggestion: { weight: 0.0789, stop_distance: 0.1267, risk_budget: 0.01 },
    modes: {
      close: { signals: [{ time: '2026-01-03', side: 'buy', kind: 'close', status: 'confirmed', price: 11.6, level: 11 }],
               state: { holding: true, entry_time: '2026-01-03', entry_price: 11.6, stop: 7.4, effective_exit: 10.2,
                        effective_entry: null, provisional: false } },
      touch: { signals: [{ time: '2026-01-02', side: 'buy', kind: 'touch', status: 'confirmed', price: 11, level: 11 },
                         { time: '2026-01-04', side: 'sell', kind: 'touch', status: 'provisional', price: 10, level: 10 }],
               state: { holding: false, entry_time: null, entry_price: null, stop: null, effective_exit: null,
                        effective_entry: 12, provisional: true } },
    },
  };
  assert.deepStrictEqual(dc.statusSummary(payload, 'close', 12), {
    holding: true, provisional: false,
    watch: { side: 'exit', price: '10.20', dist: '-15.0%' },
    entry: { time: '2026-01-03', price: '11.60' }, stop: '7.40', n: '2.20',
    suggestion: { weight: '7.9%', risk: '1%' },
    last: { time: '2026-01-03', text: '收盘突破入场线', price: '11.60' },
  });
  // 空仓：盯后端给的 effective_entry（12），不是 next.entry（12.5）；最近信号取所选方式的最后一条
  assert.deepStrictEqual(dc.statusSummary(payload, 'touch', 10), {
    holding: false, provisional: true,
    watch: { side: 'entry', price: '12.00', dist: '+20.0%' },
    entry: null, stop: null, n: '2.20',
    suggestion: { weight: '7.9%', risk: '1%' },
    last: { time: '2026-01-04', text: '盘中触及离场价（形成中）', price: '10.00' },
  });
  // 没有收盘价时不给距离；没有信号、没有建议仓位时为 null
  const bare = { rows, next: {}, suggestion: null, modes: { close: { signals: [], state: payload.modes.touch.state } } };
  assert.deepStrictEqual(dc.statusSummary(bare, 'close', null).watch, { side: 'entry', price: '12.00', dist: null });
  assert.strictEqual(dc.statusSummary(bare, 'close', 10).last, null);
  assert.strictEqual(dc.statusSummary(bare, 'close', 10).suggestion, null);
  assert.strictEqual(dc.statusSummary(null, 'close', 10), null);
  assert.strictEqual(dc.statusSummary(bare, 'touch', 10), null);
}

console.log('donchian ui ok');
