'use strict';
/* 5 分与 15 分周期下线（目标 2026-09-29 第三阶段）：页面只保留 30 分、60 分、日线、周线。
   失败方式：旧链接（?freq=m5/m15）或残留状态让页面请求已下线周期（服务端回 400，图表报错或空图）；
   周期栏仍有 5分/15分入口。 */
const assert = require('node:assert/strict');
const { loadEnv } = require('./support/app.js');

{
  const env = loadEnv();
  env.state.code = 'sh600036';
  env.state.freq = 'm15';
  env.load();
  assert.match(env._harness.pending[0].url, /[?&]freq=m30(&|$)/, 'm15 归到 30 分后再请求');
  assert.equal(env.state.freq, 'm30', '当前周期同步改为 30 分');
  env.state.freq = 'm5';
  env.load();
  assert.match(env._harness.pending[1].url, /[?&]freq=m30(&|$)/, 'm5 归到 30 分');
  env.state.freq = 'bogus';
  env.load();
  assert.match(env._harness.pending[2].url, /[?&]freq=day(&|$)/, '无法识别的周期回到日线');
}

console.log('retired periods UI checks passed');
