'use strict';
/* 主图均线设置（web/ma.js）：周期可改、逐条开关、颜色按色位自动分配、至多 7 条。
   实现前列出的失败方式：
   - 本地保存缺失、损坏、条数超限、周期重复或越界时页面报错，或画出超过 7 条、重复的均线；
   - 改周期或开关一条线时，其他线换了颜色（色位跟着排序位置走）；新加的线与已有线撞色；
   - 输入非整数、过小过大或重复的周期仍被接受，或拒绝后原设置被改坏；
   - 满 7 条仍能添加；删光后无法再添加，或重载后删光的设置又变回默认；
   - 均线数值错位（前 n-1 根应无值，第 n 根起为 n 根收盘均值）。 */
const assert = require('node:assert/strict');
const ma = require('../web/ma.js');

const store = value => key => (key === ma.STORE_KEY ? value : null);
const periods = cfg => cfg.map(l => l.n);

// 默认：5、13、20、55、144、250，前三条打开；色位 0..5
{
  const cfg = ma.readConfig(store(null));
  assert.deepEqual(periods(cfg), [5, 13, 20, 55, 144, 250]);
  assert.deepEqual(cfg.map(l => l.on), [true, true, true, false, false, false]);
  assert.deepEqual(cfg.map(l => l.slot), [0, 1, 2, 3, 4, 5]);
  assert.equal(ma.MAX, 7);
}

// 损坏或类型不对的保存值、全部无效的条目回到默认；读取抛错也回到默认；删光后保存的空数组照样空着
{
  assert.deepEqual(ma.readConfig(store('[]')), []);
  for (const bad of ['{', '"x"', '{"a":1}', '[{"n":1}]', 'null']) {
    assert.deepEqual(periods(ma.readConfig(store(bad))), [5, 13, 20, 55, 144, 250], bad);
  }
  assert.deepEqual(periods(ma.readConfig(() => { throw new Error('blocked'); })), [5, 13, 20, 55, 144, 250]);
}

// 逐条清洗：越界、非整数、重复周期丢弃；色位越界或重复的重新分配最小空位；超过 7 条截断；结果按周期升序
{
  const saved = JSON.stringify([
    { n: 30, on: true, slot: 2 }, { n: 10, on: false, slot: 2 }, { n: 1, on: true, slot: 0 },
    { n: 1000, on: true, slot: 1 }, { n: 7.5, on: true, slot: 1 }, { n: 30, on: false, slot: 4 },
    { n: 60, on: 'yes', slot: 9 }, { n: 99, slot: 0 }, { n: 120, on: true, slot: 1 },
    { n: 200, on: true, slot: 3 }, { n: 250, on: true, slot: 5 }, { n: 300, on: true, slot: 6 },
    { n: 400, on: true, slot: 0 },
  ]);
  const cfg = ma.readConfig(store(saved));
  assert.deepEqual(periods(cfg), [10, 30, 60, 99, 120, 200, 250]);
  assert.deepEqual(cfg.map(l => l.on), [false, true, false, false, true, true, true]);
  const slots = cfg.map(l => l.slot);
  assert.equal(new Set(slots).size, 7, '色位互不相同');
  assert.ok(slots.every(s => s >= 0 && s < 7));
  assert.equal(cfg.find(l => l.n === 30).slot, 2, '先到的有效色位保留');
}

// 开关与改周期不改变任何一条线的色位；改周期后重新按周期排序
{
  let cfg = ma.readConfig(store(null));
  const before = Object.fromEntries(cfg.map(l => [l.n, l.slot]));
  cfg = ma.toggle(cfg, 144);
  assert.equal(cfg.find(l => l.n === 144).on, true);
  const r = ma.setPeriod(cfg, 13, 60);
  assert.equal(r.error, null);
  assert.deepEqual(periods(r.config), [5, 20, 55, 60, 144, 250]);
  assert.equal(r.config.find(l => l.n === 60).slot, before[13], '改周期保留原色位');
  assert.equal(r.config.find(l => l.n === 60).on, true, '改周期保留开关');
  assert.equal(r.config.find(l => l.n === 144).slot, before[144]);
}

// 非法周期拒绝并说明原因，原设置不变
{
  const cfg = ma.readConfig(store(null));
  const snapshot = JSON.stringify(cfg);
  for (const [value, pattern] of [['abc', /整数/], ['7.5', /整数/], ['1', /2/], ['1000', /999/], ['20', /已有/], ['', /整数/]]) {
    const r = ma.setPeriod(cfg, 13, value);
    assert.match(r.error, pattern, value);
    assert.equal(r.config, cfg, value);
  }
  assert.equal(JSON.stringify(cfg), snapshot);
  assert.equal(ma.setPeriod(cfg, 13, ' 21 ').error, null, '两端空白可接受');
  assert.equal(ma.setPeriod(cfg, 13, '13').error, null, '改成自己不算重复');
}

// 添加：取最小空色位、选一个未用的周期、默认打开；满 7 条不再添加；删除释放色位
{
  let cfg = ma.readConfig(store(null));
  cfg = ma.add(cfg);
  assert.equal(cfg.length, 7);
  const added = cfg.find(l => [5, 13, 20, 55, 144, 250].indexOf(l.n) < 0);
  assert.equal(added.slot, 6);
  assert.equal(added.on, true);
  assert.equal(ma.add(cfg), cfg, '满 7 条不再添加');
  assert.equal(ma.canAdd(cfg), false);
  cfg = ma.remove(cfg, 13);
  assert.equal(cfg.length, 6);
  assert.equal(ma.canAdd(cfg), true);
  cfg = ma.add(cfg);
  assert.equal(cfg.find(l => [5, 20, 55, 144, 250, added.n].indexOf(l.n) < 0).slot, 1, '复用删掉的色位');
  for (const n of periods(cfg)) cfg = ma.remove(cfg, n);
  assert.deepEqual(cfg, []);
  cfg = ma.add(cfg);
  assert.equal(cfg.length, 1, '删光后仍能添加');
  assert.equal(cfg[0].slot, 0);
}

// 保存再读回得到同一设置
{
  let saved = null;
  const cfg = ma.setPeriod(ma.toggle(ma.readConfig(store(null)), 5), 250, 233).config;
  ma.writeConfig((k, v) => { assert.equal(k, ma.STORE_KEY); saved = v; }, cfg);
  assert.deepEqual(ma.readConfig(store(saved)), cfg);
  ma.writeConfig(() => { throw new Error('quota'); }, cfg);   // 写失败不抛出
}

// 简单移动平均：前 n-1 根为 null，之后为最近 n 根收盘均值
{
  assert.deepEqual(ma.sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  assert.deepEqual(ma.sma([10, 20], 3), [null, null]);
  assert.deepEqual(ma.sma([], 5), []);
}

console.log('ma settings checks passed');
