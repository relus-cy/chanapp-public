'use strict';
// 失败方式：偏好未确认先请求、URL/刷新加载取消项、全不勾仍请求、灰色项仍被加载。
// 浏览器 E2E 覆盖服务端持久化与实际对话框；这里锁住既有 load 入口的请求守卫。
const assert = require('node:assert/strict');
const { loadEnv } = require('./support/app.js');
const prefs = selected => ({ selected, markets: {
  cn: { available: ['day', 'week', 'm60'], reasons: { m30: '当前粒度无法显示30分' } },
  hk: { available: ['day', 'week', 'm60', 'm30'], reasons: {} },
}, notice: null, revision: 'test' });
{
  const env = loadEnv({ periodPrefs: null });
  env.load();
  assert.equal(env._harness.pending.length, 0, '周期偏好尚未返回：不发图表请求');
}
{
  const env = loadEnv({ periodPrefs: prefs(['week']), state: {code: 'sh600036', freq: 'm30'} });
  env.load();
  assert.match(env._harness.pending[0].url, /freq=week/, 'URL指向未勾选周期时切换到已勾选项');
}
{
  const env = loadEnv({ periodPrefs: prefs([]) });
  env.load();
  assert.equal(env._harness.pending.length, 0, '全不勾时没有图表请求');
}
{
  const env = loadEnv({ periodPrefs: prefs(['day', 'm30']), state: {code: 'sh600036', freq: 'm30'} });
  env.load();
  assert.match(env._harness.pending[0].url, /freq=day/, '保留勾选但不能合成的周期不会请求');
}
console.log('period selection UI checks passed');
