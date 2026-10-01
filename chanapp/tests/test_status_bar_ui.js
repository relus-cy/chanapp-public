'use strict';
/* 状态栏文案（目标 2026-09-29 第三阶段「报价、F10 与状态栏」）：只按本次图表响应的 meta.coverage.data_status
   渲染，严格三种文案加「暂无数据」。失败方式：沿用「交易中/已收盘 · 抓取 HH:MM:SS」而不说是否定稿；
   从未成功却显示时间；按浏览器时段而非本次快照判断。 */
const assert = require('node:assert/strict');
const { runSlices } = require('./support/dom.js');
const { appContext } = require('./support/app.js');

function statusEnv(open) {
  const context = appContext({ isSessionOpen: () => !!open });
  runSlices(context, ['status', 'esc', 'metaBar']);
  context.state.code = 'sh600036';
  return context;
}
const meta = status => ({ source: 's', fqf: '前复权', fetch_time: '2026-09-29 17:32:05', stale: true, bars: 1,
                          coverage: { qfq_from: null, data_status: status } });
const text = c => c.el('status').innerHTML.replace(/<[^>]+>/g, '');

{
  const c = statusEnv(true);
  c.renderMeta(meta({ phase: 'live', day: '2026-09-29', at: '2026-09-29 14:50' }));
  assert.equal(text(c), '● 交易中 · 实时抓取 09-29 14:50');
}
{
  const c = statusEnv(false);
  c.renderMeta(meta({ phase: 'awaiting_final', day: '2026-09-29', at: '2026-09-29 15:01' }));
  assert.equal(text(c), '● 已收盘 · 待定稿 09-29 15:01');
  c.renderMeta(meta({ phase: 'final', day: '2026-09-29', at: '2026-09-29 17:32' }));
  assert.equal(text(c), '● 已收盘 · 历史抓取 09-29 17:32');
}
{
  const c = statusEnv(false);
  c.renderMeta(meta({ phase: 'none', day: '2026-09-29', at: null }));
  assert.equal(text(c), '● 已收盘 · 暂无数据', '从未成功：不编造时间');
  const live = statusEnv(true);
  live.renderMeta(meta({ phase: 'none', day: '2026-09-30', at: null }));
  assert.equal(text(live), '● 交易中 · 暂无数据');
}
{
  const c = statusEnv(true);                          // 浏览器侧会话状态与快照不一致时以快照为准
  c.renderMeta(meta({ phase: 'awaiting_final', day: '2026-09-29', at: '2026-09-29 15:01' }));
  assert.equal(text(c), '● 已收盘 · 待定稿 09-29 15:01');
}
console.log('status bar UI checks passed');

// demo 不参与实时新鲜度；历史日期不是抓取时间，也不显示交易中。
{
  const c = statusEnv(true);
  c.renderMeta(meta({ phase: 'historical', day: '2024-12-31', at: null }));
  assert.equal(text(c), '● 历史截止于 2024-12-31');
}
