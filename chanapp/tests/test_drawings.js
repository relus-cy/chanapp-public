'use strict';
// web/drawings.js 的纯函数单测：node 直接 require 同一文件（不经 app.js 文本切片）。
// 这些情形 e2e 够不着（demo 样本没有港股、停牌和除权）：跨周期映射、未来时段槽位、前复权比例。
// 实现前列出的失败方式：
// - 日线锚点在分钟图上落到当天收盘或前一天，而不是当天第一根；周线锚点落到周末而不是该周第一个交易日；
// - 分钟锚点跨分钟周期落错桶（港股 12:00 短桶、午后第一桶）；
// - 停牌日锚点找不到 bar 而丢失，没有落到复牌后第一根；
// - 未来锚点按日历秒数外推，跨午休、隔夜、周末后位置错开；放置后存回的槽位与显示的槽位不一致；
// - 前复权比例在分钟图上拿未收完的当天算，或跨周期拿不到同一天的收盘。
const assert = require('node:assert');
const d = require('../web/drawings.js');

const bars = labels => labels.map((time, i) => ({ time, close: 100 + i }));
const labels = list => list.map(b => b.time);

// 1. 日线锚点 → 30 分：当天第一根（区间末端 10:00）
{
  const m30 = ['2026-10-09 15:00', '2026-10-12 10:00', '2026-10-12 10:30', '2026-10-12 15:00'];
  assert.deepStrictEqual(d.locate({ freq: 'day', dt: '2026-10-12' }, m30, 'm30', 'cn'), { index: 1 });
}

// 2. 周线锚点 → 日线：该周第一个交易日；周一停牌则落到周二
{
  const day = ['2026-10-09', '2026-10-13', '2026-10-14', '2026-10-16', '2026-10-19'];
  assert.deepStrictEqual(d.locate({ freq: 'week', dt: '2026-10-16' }, day, 'day', 'cn'), { index: 1 });
  // 形成中的周线标签是当天（周三）：同一周稍后的日线、分钟锚点仍落在这根，不算下一周
  const week = ['2026-10-09', '2026-10-14'];
  assert.deepStrictEqual(d.locate({ freq: 'day', dt: '2026-10-16' }, week, 'week', 'cn'), { index: 1 });
  assert.deepStrictEqual(d.locate({ freq: 'm30', dt: '2026-10-15 10:00' }, week, 'week', 'cn'), { index: 1 });
  assert.deepStrictEqual(d.locate({ freq: 'day', dt: '2026-10-19' }, week, 'week', 'cn'), { future: 1 });
}

// 3. 分钟跨周期：A 股 30 分 → 60 分；港股 30 分 12:00 → 60 分 12:00 短桶；60 分 14:00 → 30 分 13:30
{
  const cn60 = ['2026-10-12 10:30', '2026-10-12 11:30', '2026-10-12 14:00', '2026-10-12 15:00'];
  const at = (dt, list, freq, market) => d.locate({ freq: dt[1], dt: dt[0] }, list, freq, market).index;
  assert.strictEqual(at(['2026-10-12 10:00', 'm30'], cn60, 'm60', 'cn'), 0);
  assert.strictEqual(at(['2026-10-12 11:30', 'm30'], cn60, 'm60', 'cn'), 1);
  assert.strictEqual(at(['2026-10-12 13:30', 'm30'], cn60, 'm60', 'cn'), 2);
  const hk60 = ['2026-10-12 10:30', '2026-10-12 11:30', '2026-10-12 12:00', '2026-10-12 14:00'];
  assert.strictEqual(at(['2026-10-12 12:00', 'm30'], hk60, 'm60', 'hk'), 2);
  assert.strictEqual(at(['2026-10-12 11:30', 'm30'], hk60, 'm60', 'hk'), 1);
  const cn30 = ['2026-10-12 11:30', '2026-10-12 13:30', '2026-10-12 14:00'];
  assert.strictEqual(at(['2026-10-12 14:00', 'm60'], cn30, 'm30', 'cn'), 1);
  // 分钟锚点 → 日线：所在交易日
  assert.strictEqual(at(['2026-10-12 10:30', 'm60'], ['2026-10-09', '2026-10-12', '2026-10-13'], 'day', 'cn'), 1);
}

// 4. 停牌日（没有 bar）：复牌后第一根；同周期但该 bar 已不在数据里时同样处理
{
  const day = ['2026-10-09', '2026-10-14'];
  assert.deepStrictEqual(d.locate({ freq: 'day', dt: '2026-10-12' }, day, 'day', 'cn'), { index: 1 });
  assert.deepStrictEqual(d.locate({ freq: 'm30', dt: '2026-10-12 10:00' }, day, 'day', 'cn'), { index: 1 });
}

// 5. 早于已加载数据：past；晚于最后一根：按交易时段数未来槽位
{
  const m30 = ['2026-10-16 14:30', '2026-10-16 15:00'];
  assert.strictEqual(d.locate({ freq: 'day', dt: '2026-10-15' }, m30, 'm30', 'cn').past, true);
  // 周五 15:00 之后：下周一 10:00 是第 1 根，13:30 是第 5 根
  assert.deepStrictEqual(d.locate({ freq: 'm30', dt: '2026-10-19 10:00' }, m30, 'm30', 'cn'), { future: 1 });
  assert.deepStrictEqual(d.locate({ freq: 'm30', dt: '2026-10-19 13:30' }, m30, 'm30', 'cn'), { future: 5 });
  // 日线锚点画在未来的下周一，分钟图上是该日第一根
  assert.deepStrictEqual(d.locate({ freq: 'day', dt: '2026-10-19' }, m30, 'm30', 'cn'), { future: 1 });
  assert.strictEqual(d.futureLabel('2026-10-16 15:00', 'm30', 'cn', 5), '2026-10-19 13:30');
  assert.strictEqual(d.futureLabel('2026-10-16 15:00', 'm60', 'hk', 4), '2026-10-19 12:00');
  assert.strictEqual(d.futureLabel('2026-10-16', 'day', 'cn', 1), '2026-10-19');
  assert.strictEqual(d.futureLabel('2026-10-16', 'week', 'cn', 2), '2026-10-30');
  assert.deepStrictEqual(d.locate({ freq: 'day', dt: '2026-10-21' }, ['2026-10-15', '2026-10-16'], 'day', 'cn'), { future: 3 });
}

// 6. 图表时间 ↔ 锚点：未来槽位按「最后一根 + k × 中位间隔」与图表互转，放置与回显一致
{
  const list = bars(['2026-10-16 10:00', '2026-10-16 10:30', '2026-10-16 11:00', '2026-10-16 11:30',
    '2026-10-16 13:30', '2026-10-16 14:00', '2026-10-16 14:30', '2026-10-16 15:00']);
  const toTime = t => Date.parse(t.replace(' ', 'T') + 'Z') / 1000;
  const view = d.buildView({ code: 'sz300308', freq: 'm30', market: 'cn', adjust: 'qfq', bars: list, toTime });
  const last = toTime('2026-10-16 15:00');
  assert.strictEqual(d.chartTime({ freq: 'm30', dt: '2026-10-19 13:30' }, view), last + 5 * 1800);
  assert.strictEqual(d.anchorDt(last + 5 * 1800, view), '2026-10-19 13:30');
  assert.strictEqual(d.anchorDt(last + 5 * 1800 + 500, view), '2026-10-19 13:30');
  assert.strictEqual(d.chartTime({ freq: 'day', dt: '2026-10-16' }, view), toTime('2026-10-16 10:00'));
  assert.strictEqual(d.anchorDt(toTime('2026-10-16 14:00'), view), '2026-10-16 14:00');
}

// 7. 前复权参照：分钟图取最后一根所在日之前的完整交易日收盘；跨周期可得同一天收盘
{
  const m30 = [{ time: '2026-10-15 14:30', close: 9 }, { time: '2026-10-15 15:00', close: 10 },
    { time: '2026-10-16 10:00', close: 11 }];
  assert.deepStrictEqual(d.refFor(m30, 'm30'), { day: '2026-10-15', close: 10 });
  assert.strictEqual(d.dayClose(m30, 'm30', '2026-10-16'), null);            // 当天未收完
  const day = [{ time: '2026-10-14', close: 7 }, { time: '2026-10-15', close: 20 }, { time: '2026-10-16', close: 22 }];
  assert.strictEqual(d.dayClose(day, 'day', '2026-10-15'), 20);
  assert.deepStrictEqual(d.refFor(day, 'day'), { day: '2026-10-15', close: 20 });
  const week = [{ time: '2026-10-09', close: 5 }, { time: '2026-10-16', close: 6 }];
  assert.strictEqual(d.dayClose(week, 'week', '2026-10-15'), null);          // 不是周末交易日，周线上得不到
}

// 8. 前复权换算：新除权后同一天收盘变了，所有锚点按同一比例缩放并换新参照；不复权、比例为 1 或拿不到参照时不动
{
  const body = { v: 1, kind: 'trend-line', adjust: 'qfq', style: {},
    anchors: [{ freq: 'day', dt: '2026-09-01', price: 50 }, { freq: 'day', dt: '2026-09-10', price: 60 }],
    ref: { day: '2026-10-15', close: 20 } };
  const day = [{ time: '2026-10-14', close: 7 }, { time: '2026-10-15', close: 10 }, { time: '2026-10-16', close: 11 }];
  const scaled = d.rescale(body, day, 'day');
  assert.deepStrictEqual(scaled.anchors.map(a => a.price), [25, 30]);
  assert.deepStrictEqual(scaled.ref, { day: '2026-10-15', close: 10 });
  assert.deepStrictEqual(body.anchors.map(a => a.price), [50, 60]);           // 原对象不被改
  assert.strictEqual(d.rescale(Object.assign({}, body, { ref: { day: '2026-10-15', close: 10 } }), day, 'day'), null);
  assert.strictEqual(d.rescale(Object.assign({}, body, { ref: { day: '2026-01-05', close: 10 } }), day, 'day'), null);
  assert.strictEqual(d.rescale(Object.assign({}, body, { adjust: 'raw', ref: null }), day, 'day'), null);
}

console.log('drawings mapping checks passed');
