'use strict';
/* 新鲜度徽标（cacheBadge）只看 meta.stale：stale 时抓取时间后显示金色「（缓存·HH:MM:SS）」，
   否则不显示（K 线数据契约「新鲜度」）。from_cache 只表示「本次不是同步首取」，不参与徽标：
   非 stale 一律无徽标，同步首取回来仍 stale 也要显示。覆盖 cacheBadge 本身与状态栏、数据口径条两处落点。 */
const assert = require('node:assert/strict');
const { runSlices } = require('./support/dom.js');
const { appContext } = require('./support/app.js');

function statusEnv() {
  const context = appContext();
  runSlices(context, ['status', 'esc', 'metaBar']);
  return context;
}
const T = '2026-09-28 10:15:30';
const GOLD = '<span class="warn">（缓存·10:15:30）</span>';
const meta = extra => Object.assign({ source: 's', fqf: '前复权', fetch_time: T, bars: 1, first_dt: 'a', last_dt: 'b' }, extra);

// 非 stale：from_cache 真假都没有徽标
{
  const c = statusEnv();
  assert.equal(c.cacheBadge({ from_cache: true, stale: false, fetch_time: T }), '', '非首取且新鲜：不显示「（缓存）」');
  assert.equal(c.cacheBadge({ from_cache: false, stale: false, fetch_time: T }), '', '首取且新鲜：无徽标');
  assert.equal(c.cacheBadge({ from_cache: true, fetch_time: T }), '', '缺 stale 字段按不 stale');
  assert.equal(c.cacheBadge(null), '', 'meta 缺失：无徽标');
}

// stale：金色「（缓存·HH:MM:SS）」，与 from_cache 无关；取不到时刻显示「缓存·旧」
{
  const c = statusEnv();
  assert.equal(c.cacheBadge({ from_cache: true, stale: true, fetch_time: T }), GOLD);
  assert.equal(c.cacheBadge({ from_cache: false, stale: true, fetch_time: T }), GOLD, '同步首取回来仍 stale 也显示');
  assert.equal(c.cacheBadge({ from_cache: true, stale: true, fetch_time: null }), '<span class="warn">（缓存·旧）</span>');
}

// 两处落点：状态栏「抓取 HH:MM:SS」与口径条「抓取：…」
{
  const c = statusEnv();
  c.renderMeta(meta({ from_cache: true, stale: false }));
  assert.match(c.el('status').innerHTML, /抓取 10:15:30/);
  assert.ok(!c.el('status').innerHTML.includes('缓存'), '状态栏：非 stale 不带缓存字样');
  assert.ok(!c.el('metaBar').innerHTML.includes('缓存'), '口径条：非 stale 不带缓存字样');
  c.renderMeta(meta({ from_cache: true, stale: true }));
  assert.ok(c.el('status').innerHTML.includes('抓取 10:15:30' + GOLD), '状态栏：stale 时抓取时间后接金色徽标');
  assert.ok(c.el('metaBar').innerHTML.includes('抓取：' + T + GOLD), '口径条：stale 时抓取时间后接金色徽标');
}
console.log('freshness badge UI checks passed');
