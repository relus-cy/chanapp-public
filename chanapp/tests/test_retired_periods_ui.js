'use strict';
/* 5 分周期下线（目标 2026-09-29 第三阶段）；15 分同期下线，2026-10-29 恢复，只在市场能力允许时可用（A 股）。
   失败方式：旧链接（?freq=m5）或残留状态让页面请求已下线周期（服务端回 400，图表报错或空图）；
   恢复后的 15 分仍被改写成 30 分；港股残留的 15 分照样请求（服务端回 400）。 */
const assert = require('node:assert/strict');
const { loadEnv } = require('./support/app.js');

const all = ['day', 'week', 'm60', 'm30', 'm15'];
const prefs = () => ({
  catalog: [{freq: 'day', label: '日线'}, {freq: 'week', label: '周线'}, {freq: 'm60', label: '60分'},
    {freq: 'm30', label: '30分'}, {freq: 'm15', label: '15分'}],
  selected: all.slice(), notice: null,
  markets: {cn: {available: all.slice()}, hk: {available: ['day', 'week', 'm60', 'm30']}},
});

{
  const env = loadEnv({ periodPrefs: prefs() });
  env.state.code = 'sh600036';
  env.state.freq = 'm15';
  env.load();
  assert.match(env._harness.pending[0].url, /[?&]freq=m15(&|$)/, 'A 股 15 分照常请求');
  assert.equal(env.state.freq, 'm15');
  env.state.freq = 'm5';
  env.load();
  assert.match(env._harness.pending[1].url, /[?&]freq=m30(&|$)/, 'm5 归到 30 分');
  assert.equal(env.state.freq, 'm30', '当前周期同步改为 30 分');
  env.state.freq = 'bogus';
  env.load();
  assert.match(env._harness.pending[2].url, /[?&]freq=day(&|$)/, '无法识别的周期回到日线');
}

{
  const env = loadEnv({ periodPrefs: prefs() });
  env.state.code = 'hk00700';
  env.state.freq = 'm15';
  env.load();
  assert.match(env._harness.pending[0].url, /[?&]freq=day(&|$)/, '港股不能合成 15 分：回到第一个已勾选的可用周期');
  assert.equal(env.state.freq, 'day');
}

console.log('retired periods UI checks passed');
