'use strict';
/* 数据口径条（renderMeta）的覆盖提示：复权口径 fqf、前复权覆盖起点「自 {qfq_from}」、视图提示 notices
   （前复权显示至/起可用、分钟历史加载中、结构输入尚未补齐、该市场暂不提供）逐条显示；
   口径条不再出现复权基准与重建文案；视图令牌变化本身不产生临时提示。
   另含图例成交量单位（fmtVol 输入恒为股）。 */
const assert = require('node:assert/strict');
const { tick, runSlices } = require('./support/dom.js');
const { appContext, loadEnv, okJson } = require('./support/app.js');

function metaEnv() {
  const context = appContext();
  runSlices(context, ['status', 'esc', 'metaBar']);
  return { context, bar: context.el('metaBar') };
}
const base = { source: 's', fqf: '前复权', fetch_time: 't', bars: 2, first_dt: 'a', last_dt: 'b' };

// 基础四段照旧；没有覆盖与提示时不多出任何段
{
  const { context, bar } = metaEnv();
  context.renderMeta(Object.assign({}, base));
  assert.match(bar.innerHTML, /数据源：s/);
  assert.match(bar.innerHTML, /复权：前复权</, '无覆盖起点时复权段只显示口径本身');
  assert.match(bar.innerHTML, /抓取：t/);
  assert.match(bar.innerHTML, /K线：2 根/);
  assert.ok(!bar.innerHTML.includes('基准'), '口径条不再显示复权基准');
  assert.ok(!bar.innerHTML.includes('重建'), '口径条不再显示重建徽标');
  assert.ok(!bar.innerHTML.includes('自 '), '没有 coverage 时不显示覆盖起点');
}

// 前复权覆盖起点：coverage.qfq_from 非空时复权段带「自 X」；为空（原始价、指数、港股供应商口径）不显示
{
  const { context, bar } = metaEnv();
  context.renderMeta(Object.assign({}, base, {
    coverage: { qfq_from: '2016-01-04', qfq_through: '2026-09-25', stop_reason: null } }));
  assert.match(bar.innerHTML, /复权：前复权（自 2016-01-04）/);
  context.renderMeta(Object.assign({}, base, { fqf: '不复权',
    coverage: { qfq_from: null, qfq_through: null, stop_reason: null } }));
  assert.match(bar.innerHTML, /复权：不复权</);
  assert.ok(!bar.innerHTML.includes('自 '), 'qfq_from 为空时不显示覆盖起点');
}

// notices 逐条显示（文本经 esc 转义）；数组缺失或条目无文本不报错
{
  const { context, bar } = metaEnv();
  context.esc = s => String(s).replace(/</g, '&lt;');
  context.renderMeta(Object.assign({}, base, { notices: [
    { code: 'today_unconfirmed', text: '今日除权信息待确认，前复权显示至 2026-09-25' },
    { code: 'qfq_from', text: '前复权自 2020-01-02 起可用' },
    { code: 'backfill_pending', text: '更早分钟历史加载中' },
    { code: 'structure_short', text: '结构输入尚未补齐' },
    { code: 'x', text: '<b>' },
    { code: 'empty' },
  ] }));
  for (const text of ['今日除权信息待确认，前复权显示至 2026-09-25', '前复权自 2020-01-02 起可用',
    '更早分钟历史加载中', '结构输入尚未补齐']) {
    assert.ok(bar.innerHTML.includes(text), 'notice 显示：' + text);
  }
  assert.ok(bar.innerHTML.includes('&lt;b>') && !bar.innerHTML.includes('<b>'), 'notice 文本经转义');
  assert.ok(!bar.innerHTML.includes('undefined'), '无文本的条目不渲染');
  assert.match(bar.innerHTML, /class="warn"/, '提示以醒目样式显示');
  context.renderMeta(Object.assign({}, base, { notices: null, coverage: null }));
  assert.match(bar.innerHTML, /K线：2 根/, 'notices/coverage 为 null 时照常渲染');
}

// 该市场暂不提供（港股 5分/15分直达）：提示可见
{
  const { context, bar } = metaEnv();
  context.renderMeta(Object.assign({}, base, { bars: 0, notices: [{ code: 'unsupported', text: '该市场暂不提供' }] }));
  assert.ok(bar.innerHTML.includes('该市场暂不提供'));
}

// 成交量：输入恒为股，≥1e8 亿股、≥1e4 万股，其余直接标股
{
  const context = {};
  runSlices(context, ['fmtVol']);
  assert.equal(context.fmtVol(123456789), '1.23亿股');
  assert.equal(context.fmtVol(1e8), '1.00亿股');
  assert.equal(context.fmtVol(99999999), '10000.00万股');
  assert.equal(context.fmtVol(12345), '1.23万股');
  assert.equal(context.fmtVol(1e4), '1.00万股');
  assert.equal(context.fmtVol(3500), '3500股');
  assert.equal(context.fmtVol(0), '0股');
}

// 主图提交不再弹出复权重建提示：连续两次不同视图令牌的提交后，状态行回到常驻态
(async function () {
  const env = loadEnv();
  env.load();
  env._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: { token: 'T1' }, calculation_id: 'c1' }));
  await tick(); await tick();
  env.load({ refresh: true });
  env._harness.pending[1].resolve(okJson({ kline: [], macd: { rows: [] }, meta: { token: 'T2' }, calculation_id: 'c1' }));
  await tick(); await tick();
  assert.equal(env.statusMsg, null, '视图令牌变化本身不产生临时提示');
  console.log('coverage notice UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
