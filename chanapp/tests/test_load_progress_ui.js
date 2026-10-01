'use strict';
/* 顶部加载细线：用户发起的图表请求超过 150ms 仍未完成才出现（快响应不闪），完成/失败后收起；
   被新请求取代时由新请求接力，期间不出现「隐藏再显示」；后台轮询刷新与历史分页不触发。
   load 切片实跑（loadEnv），定时器换成可推进的假时钟，#loadBar 记录每次显隐切换。 */
const assert = require('node:assert/strict');
const { mkEl, tick } = require('./support/dom.js');
const { loadEnv, okJson, errJson } = require('./support/app.js');

function progressEnv() {
  const context = loadEnv();
  context.showChartError = () => {};
  // 假时钟：setTimeout 按到期时间排队，advance 推进并依次点火
  let now = 0, seq = 0;
  const queue = [];
  context.setTimeout = (fn, ms) => { const id = ++seq; queue.push({ id, fn, at: now + (ms || 0) }); return id; };
  context.clearTimeout = id => { const k = queue.findIndex(t => t.id === id); if (k >= 0) queue.splice(k, 1); };
  const advance = ms => {
    const until = now + ms;
    for (;;) {
      queue.sort((a, b) => a.at - b.at);
      if (!queue.length || queue[0].at > until) break;
      const t = queue.shift();
      now = t.at;
      t.fn();
    }
    now = until;
  };
  // 显隐切换历史：只记变化
  const bar = mkEl('div'), history = [];
  const toggle = bar.classList.toggle;
  bar.classList.toggle = (c, on) => {
    const before = bar.classList.contains(c);
    toggle(c, on);
    const after = bar.classList.contains(c);
    if (c === 'on' && before !== after) history.push(after ? 'show' : 'hide');
  };
  context._harness.nodes.loadBar = bar;
  const center = context._harness.nodes.center = mkEl('div');
  return { context, advance, history, shown: () => bar.classList.contains('on'), busy: () => center.getAttribute('aria-busy') };
}
const ok = () => okJson({ kline: [], macd: { rows: [] }, meta: { token: 'T1' } });

(async () => {
  // 150ms 内完成：从未显示
  {
    const e = progressEnv();
    e.context.load();
    assert.equal(e.busy(), 'true', '请求期间图表区 aria-busy');
    e.advance(100);
    e.context._harness.pending[0].resolve(ok());
    await tick(); await tick();
    e.advance(1000);
    assert.deepEqual(e.history, [], '快响应不显示进度条');
    assert.equal(e.busy(), 'false', '完成后 aria-busy 复位');
  }

  // 超过门槛未完成：显示，完成后隐藏
  {
    const e = progressEnv();
    e.context.load();
    e.advance(149);
    assert.equal(e.shown(), false, '门槛内不显示');
    e.advance(2);
    assert.equal(e.shown(), true, '超过 150ms 显示');
    e.context._harness.pending[0].resolve(ok());
    await tick(); await tick();
    assert.equal(e.shown(), false, '完成后隐藏');
    assert.deepEqual(e.history, ['show', 'hide']);
    assert.equal(e.busy(), 'false');
  }

  // 连续两次请求（前一次被新请求取消）：接力显示，第二次完成后隐藏，期间不抖动
  {
    const e = progressEnv();
    e.context.load();
    e.advance(200);
    e.context.state.freq = 'm60';
    e.context.load();
    await tick(); await tick();
    e.advance(100);
    assert.equal(e.shown(), true, '接力期间保持显示');
    e.context._harness.pending[1].resolve(ok());
    await tick(); await tick();
    e.advance(1000);
    assert.deepEqual(e.history, ['show', 'hide'], '只出现一次显示和一次隐藏');
    // 被取代的旧请求迟到也不得把进度条重新拉起或收起后再显示
    e.context._harness.pending[0].resolve(ok());
    await tick(); await tick();
    e.advance(1000);
    assert.deepEqual(e.history, ['show', 'hide']);

    // 新请求在门槛前接替：从第一次发起起算，不重新计时、不闪
    const f = progressEnv();
    f.context.load();
    f.advance(100);
    f.context.state.freq = 'm60';
    f.context.load();
    f.advance(60);
    assert.equal(f.shown(), true, '自第一次发起超过 150ms 即显示');
    f.context._harness.pending[1].resolve(ok());
    await tick(); await tick();
    assert.deepEqual(f.history, ['show', 'hide']);
  }

  // 请求失败：收起
  {
    const e = progressEnv();
    e.context.load();
    e.advance(300);
    e.context._harness.pending[0].resolve(errJson(502, { detail: '行情暂不可用' }));
    await tick(); await tick(); await tick();
    assert.equal(e.shown(), false, '失败后收起');
    assert.deepEqual(e.history, ['show', 'hide']);
    assert.equal(e.busy(), 'false');
  }

  // 60s 轮询刷新（load({refresh:true})）：无论快慢都不显示，也不标 aria-busy
  {
    const e = progressEnv();
    e.context.load({ refresh: true });
    assert.notEqual(e.busy(), 'true', '后台刷新不标忙');
    e.advance(5000);
    e.context._harness.pending[0].resolve({ ok: true, status: 304, headers: { get: () => null }, json: async () => null });
    await tick(); await tick();
    assert.deepEqual(e.history, [], '轮询刷新不显示进度条');
  }

  // 手动重拉是用户操作：慢时显示
  {
    const e = progressEnv();
    e.context.load({ refresh: true, refetch: true });
    e.advance(500);
    assert.equal(e.shown(), true, '重拉超过门槛显示');
    e.context._harness.pending[0].resolve(ok());
    await tick(); await tick();
    assert.equal(e.shown(), false);
  }
  console.log('load progress bar checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
