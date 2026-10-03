'use strict';
// web/indicators.js 的纯函数单测：node 直接 require 同一文件（不经 app.js 文本切片）。
// 期望值是手算字面量：修改实现后若与这里不符，先确认是行为改动而不是失误。
const assert = require('node:assert');
const ind = require('../web/indicators.js');

// BOLL(3,2) on [1,2,3,4]：mid = [null,null,2,3]；sd = sqrt(2/3) → up = mid + 2·sd
{
  const r = ind.boll([1, 2, 3, 4], 3, 2);
  assert.deepStrictEqual(r.mid, [null, null, 2, 3]);
  assert.deepStrictEqual(r.lo, [null, null, 2 - 2 * Math.sqrt(2 / 3), 3 - 2 * Math.sqrt(2 / 3)]);
  assert.ok(Math.abs(r.up[2] - (2 + 2 * Math.sqrt(2 / 3))) < 1e-12);
  assert.ok(Math.abs(r.up[3] - (3 + 2 * Math.sqrt(2 / 3))) < 1e-12);
}

// RSI(2) on [1,2,1,2]：i=1,2 预热（ag=.5,al=0 → ag=.5,al=.5），i=3 起平滑：ag=.75, al=.25 → 75
{
  const r = ind.rsi([1, 2, 1, 2], 2);
  assert.deepStrictEqual(r, [null, null, null, 75]);
  // 全程上涨：al 恒 0 → 100
  assert.deepStrictEqual(ind.rsi([1, 2, 3, 4], 2), [null, null, null, 100]);
}

// KDJ 单根 {h:10,l:8,c:10}：rsv=100，k=2/3·50+1/3·100=200/3，d=2/3·50+1/3·k=500/9，j=3k-2d
{
  const r = ind.kdj([{ high: 10, low: 8, close: 10 }]);
  const k = 2 / 3 * 50 + 1 / 3 * 100;
  assert.deepStrictEqual(r.k, [k]);
  assert.deepStrictEqual(r.d, [2 / 3 * 50 + 1 / 3 * k]);
  assert.deepStrictEqual(r.j, [3 * k - 2 * (2 / 3 * 50 + 1 / 3 * k)]);
  // hh === ll 时 rsv=50（不涨不跌的一根）；k/d/j 都收敛到 50（浮点非精确，容差断言）
  const flat = ind.kdj([{ high: 5, low: 5, close: 5 }]);
  for (const key of ['k', 'd', 'j']) assert.ok(Math.abs(flat[key][0] - 50) < 1e-12, flat[key][0]);
}

console.log('indicators ok');
