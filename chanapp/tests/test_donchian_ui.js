'use strict';
// web/donchian.js 的纯函数单测：设置读取、绘制数据、标记样式、图例文字。数值来自后端，这里只验证呈现规则。
// 可能出错的方式：坏的本地存储值把开关弄成未知状态；null 线值画成 0；止损线从窗口起点而不是入场根开始画；
// 假突破画成实心、形成中或盘中触价的信号画成收盘确认的实心；只画了当前判定方式的标记（规格要求两种都画）；
// 两种方式在同一根同一线位的同类信号重复画；图例在空仓时仍显示止损、在非最后一根显示建议仓位。
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

// 止损线从入场根画到末根；空仓不画
assert.deepStrictEqual(dc.stopData(rows, { holding: true, entry_time: '2026-01-03', stop: 7.5 }, id),
  [{ time: '2026-01-03', value: 7.5 }, { time: '2026-01-04', value: 7.5 }]);
assert.deepStrictEqual(dc.stopData(rows, { holding: false, entry_time: null, stop: null }, id), []);

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

// 图例
{
  const payload = {
    rows,
    next: { entry: 12.5, exit: 10.2 },
    suggestion: { weight: 0.0789, stop_distance: 0.1267, risk_budget: 0.01 },
    modes: {
      close: { signals: [{ time: '2026-01-03', side: 'buy', kind: 'close', status: 'confirmed', price: 11.6, level: 11 }],
               state: { holding: true, entry_time: '2026-01-03', entry_price: 11.6, stop: 7.4, effective_exit: 10.2 } },
      touch: { signals: [{ time: '2026-01-03', side: 'buy', kind: 'touch', status: 'confirmed', price: 11, level: 11 }],
               state: { holding: false } },
    },
  };
  assert.deepStrictEqual(dc.legendParts(payload, 1, 'close'),
    [{ label: '入', value: '11.00' }, { label: '出', value: '9.00' }, { label: 'N', value: '2.00' }]);
  assert.deepStrictEqual(dc.legendParts(payload, 2, 'close'),
    [{ label: '入', value: '11.00' }, { label: '出', value: '9.50' }, { label: 'N', value: '2.10' },
     { label: '止损', value: '7.40' }, { label: '', value: '收盘突破入场线' }, { label: '', value: '盘中触及入场线' }]);
  assert.deepStrictEqual(dc.legendParts(payload, 3, 'close'),
    [{ label: '入', value: '12.00' }, { label: '出', value: '10.00' }, { label: 'N', value: '2.20' },
     { label: '规则状态', value: '持有' }, { label: '止损', value: '7.40' },
     { label: '下一根', value: '入 12.50 出 10.20' }, { label: '建议仓位', value: '7.9%（风险1%）' }]);
  // 盘中触价模式下空仓：最新一根写明规则状态空仓，不显示止损
  assert.deepStrictEqual(dc.legendParts(payload, 3, 'touch').slice(3, 5),
    [{ label: '规则状态', value: '空仓' }, { label: '下一根', value: '入 12.50 出 10.20' }]);
  assert.deepStrictEqual(dc.legendParts(null, 0, 'close'), []);
  assert.deepStrictEqual(dc.legendParts(payload, 9, 'close'), []);
}

console.log('donchian ui ok');
