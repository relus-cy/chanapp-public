'use strict';
/* 报价与 F10 分离（目标 2026-09-29 第三阶段「报价、F10 与状态栏」）。
   失败方式：F10 慢或失败时价格卡不显示；非自选代码没有价格；快速切换代码时旧代码的价格串到新代码；
   正在看的非自选代码不随报价轮询刷新。 */
const assert = require('node:assert/strict');
const { tick, runSlices, mkEl } = require('./support/dom.js');
const { appContext, loadEnv, okJson, timerEnv } = require('./support/app.js');

function railEnv(extras) {
  const context = appContext(Object.assign({
    watchName: code => code, signCls: v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'), F10_TTL: 300000,
    renderF10() { context._harness.renders.push('f10'); }, renderFlow() {},
  }, extras || {}));
  runSlices(context, ['quoteView', 'f10Header', 'hideF10', 'quotesF10', 'f10']);
  return context;
}

// 价格卡是独立区块：不在 F10 区块里（F10 区块整体隐藏时不连带隐藏价格）
{
  const fs = require('node:fs'), path = require('node:path');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const block = id => { const i = html.indexOf('id="' + id + '"'); return html.slice(i, html.indexOf('<div class="sec"', i + 1)); };
  assert.match(block('quoteSec'), /id="f10Px"/, '价格在价格卡里');
  assert.doesNotMatch(block('f10Sec'), /id="f10Px"/, 'F10 区块不含价格');
}

(async function () {
  // F10 阻塞或失败都不挡已有报价：卡头独立显示，F10 失败只收起 F10 与资金流
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新', price_time: '2026-09-30 10:05' } };
    env.loadF10('sh600036');                          // F10 在途（阻塞）
    env.el('quoteSec').style.display = 'none';        // 页面初始隐藏
    env.renderF10Header();
    assert.equal(env.el('quoteSec').style.display, '', 'F10 在途时价格卡已显示');
    assert.equal(nodes.f10Px.textContent, '40.00');
    env._harness.pending[0].reject(new Error('f10 timeout'));
    await tick(); await tick();
    assert.equal(env.el('f10Sec').style.display, 'none', 'F10 失败收起 F10');
    assert.equal(env.el('quoteSec').style.display, '', 'F10 失败不收起价格卡');
    assert.equal(nodes.f10Px.textContent, '40.00', '价格仍在');
  }

  // 第三阶段复审阻断 2：自选代码打开时还没有报价，F10 挂起，报价表随后到达——价格卡立即显示，不等 F10
  // （自选列表渲染只在 F10 已到时顺带刷新卡头，报价到达须自己刷新价格卡）
  {
    const env = railEnv({ renderWatchlist() {} });
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = {};
    env.loadF10('sh600036');                          // F10 在途（阻塞）
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '--');
    env.loadQuotes();
    const quotes = env._harness.pending.find(p => p.url === '/api/quotes');
    quotes.resolve(okJson({ quotes: { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.00', '报价后到：F10 仍挂起时价格卡也更新');
  }

  // 第三阶段复审应修 6：港股报价只带源时间戳（秒），价格卡也显示价格时刻（与 A 股 price_time 同为北京时间）
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.state.watchlist = [{ code: 'hk00700', name: '腾讯控股' }];
    env.state.quotes = { hk00700: { price: 512.5, pct: -1.2, source_ts: 1790755680 } };
    env.renderF10Header();
    assert.match(nodes.f10Px.title, /价格时刻 2026-09-30 16:08/, nodes.f10Px.title);
  }

  // 非自选代码：单代码报价入口；自选代码不重复请求（由自选报价表覆盖）
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.code = 'sh600036';
    env.loadViewQuote('sh600036');
    assert.equal(env._harness.pending.length, 0, '自选代码不走单代码报价');
    env.state.code = 'sz000002';
    env.loadViewQuote('sz000002');
    assert.equal(env._harness.pending[0].url, '/api/quote?code=sz000002');
    env._harness.pending[0].resolve(okJson({ code: 'sz000002', quote: { price: 7.12, pct: 0.85, price_label: '最新' } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '7.12', '非自选代码显示单代码报价');
    assert.notEqual(env.el('quoteSec').style.display, 'none');
  }

  // 快速切换：旧代码的迟到报价不串到新代码
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sz000002';
    env.loadViewQuote('sz000002');
    env.state.code = 'sz000004';
    env.loadViewQuote('sz000004');
    env._harness.pending[0].resolve(okJson({ code: 'sz000002', quote: { price: 7.12, pct: 0.85, price_label: '最新' } }));
    await tick(); await tick();
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '--', '旧代码的迟到报价不显示在新代码上');
    env._harness.pending[1].resolve(okJson({ code: 'sz000004', quote: { price: 3.3, pct: -0.3, price_label: '最新' } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '3.30');
  }

  // 报价轮询也刷新正在看的非自选代码（按它的市场判断）
  {
    const seen = [];
    const env = timerEnv({ marketOf: c => (c && c.indexOf('hk') === 0 ? 'hk' : 'cn'),
                           loadViewQuote: code => seen.push(code) });
    env.state.watchlist = [{ code: 'sh600036' }];
    env.state.code = 'sz000002';
    env._harness.intervals[0]();                      // 60s 回调取 /api/session：A 股开市
    env._harness.pending.shift().resolve(okJson({ markets: { cn: { open: true }, hk: { open: false } } }));
    await tick(); await tick();
    env._harness.intervals[1]();
    assert.deepEqual(seen, ['sz000002'], '非自选代码随报价轮询刷新');
  }
  // 第三阶段第二轮复审应修 4：同一代码的两次单代码报价逆序返回（首开的空报价晚到）——只接纳最新一次请求
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sz000002';
    env.loadViewQuote('sz000002');                    // 首开：事实还没到
    env.loadViewQuote('sz000002');                    // 首取完成后补取
    env._harness.pending[1].resolve(okJson({ code: 'sz000002', quote: { price: 7.12, pct: 0.85, price_label: '最新' } }));
    await tick(); await tick();
    env._harness.pending[0].resolve(okJson({ code: 'sz000002', quote: { price: null, price_unavailable: true } }));
    await tick(); await tick();
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '7.12', '较早请求的空报价晚到不覆盖');
  }

  // 第三阶段第七轮复审应修：移出最后一只自选后，报价表里的残留旧价不能遮住单代码新报价
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '40.00');
    env.state.watchlist = [];                          // 移出自选：报价表不再刷新这只
    env.loadViewQuote('sh600036');
    env._harness.pending[0].resolve(okJson({ code: 'sh600036', quote: { price: 45.0, pct: 2.0, price_label: '最新' } }));
    await tick(); await tick();
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '45.00', '非自选按单代码报价显示');
  }

  // 第三阶段第八轮复审应修：自选成员关系变化立即切换报价通道并重绘价格卡（不等 F10、不等下一轮报价轮询）；
  // 整表报价只接纳最新一次请求
  const opsEnv = () => {
    const env = railEnv({ renderWatchlist() {}, load() {}, recordView() {}, setStatus() {} });
    runSlices(env, ['watchlistOps']);
    return env;
  };
  const byUrl = (env, url) => env._harness.pending.find(p => p.url === url);
  {
    // 移出（没有单代码报价）：立即请求单代码报价，价格卡不再显示报价表里的旧价
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.removeWatch('sh600036');
    await tick();
    byUrl(env, '/api/watchlist/sh600036').resolve(okJson([]));
    await tick(); await tick();
    assert.ok(byUrl(env, '/api/quote?code=sh600036'), '移出后立即取单代码报价');
    assert.equal(nodes.f10Px.textContent, '--', '移出后不显示报价表旧价');
    byUrl(env, '/api/quote?code=sh600036').resolve(okJson({ code: 'sh600036', quote: { price: 45.0, pct: 2.0 } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '45.00');
  }
  {
    // 重新加入：报价表里上次成员期间的残留不可信，立即刷新报价表；表到之前沿用单代码报价
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sz000002';
    env.state.watchlist = [];
    env.state.quotes = { sz000002: { price: 40.0, pct: 1.0 } };
    env.state.viewQuote = { code: 'sz000002', quote: { price: 45.0, pct: 2.0 } };
    env.addWatch('sz000002', '万科A');
    await tick();
    byUrl(env, '/api/watchlist').resolve(okJson([{ code: 'sz000002', name: '万科A' }]));
    await tick(); await tick();
    assert.ok(byUrl(env, '/api/quotes'), '加入后立即刷新报价表');
    assert.equal(nodes.f10Px.textContent, '45.00', '不回到残留旧价');
  }
  {
    // 启动：报价表先到、自选列表后到，F10 挂起——自选列表一到价格卡就显示
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [];
    env.state.quotes = { sh600036: { price: 45.0, pct: 2.0 } };
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '--');
    env.loadWatchlist({ skipLoad: true });
    await tick();
    byUrl(env, '/api/watchlist').resolve(okJson([{ code: 'sh600036', name: '招商银行' }]));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '45.00');
  }
  {
    // 整表报价逆序返回：较早请求晚到不覆盖
    const env = railEnv({ renderWatchlist() {} });
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.loadQuotes();
    env.loadQuotes();
    const [first, second] = env._harness.pending.filter(p => p.url === '/api/quotes');
    second.resolve(okJson({ quotes: { sh600036: { price: 45.0, pct: 2.0 } } }));
    await tick(); await tick();
    first.resolve(okJson({ quotes: { sh600036: { price: 40.0, pct: 1.0 } } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '45.00');
  }

  // 第三阶段复审应修 6：收盘后首开非自选代码，单代码报价与图表首取并行、事实还没到时为空；首取完成后补取一次，
  // 已有价格或只是刷新时不补
  {
    const seen = [];
    const env = loadEnv({ loadViewQuote: code => seen.push(code) });
    env.state.code = 'sz000002';
    env.state.freq = 'day';
    env.load();
    assert.deepEqual(seen, ['sz000002'], '打开时取一次');
    const opened = env.state.openSeq;
    assert.ok(opened >= 1, '打开（非刷新）推进打开序号：收盘后跟进按这次打开计');
    env._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v1"'));
    await tick(); await tick(); await tick();
    assert.deepEqual(seen, ['sz000002', 'sz000002'], '首取完成后报价仍空：补取一次');
    env.state.viewQuote = { code: 'sz000002', quote: { price: 7.12 } };
    env.load({ refresh: true });
    env._harness.pending[1].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v2"'));
    await tick(); await tick(); await tick();
    assert.equal(seen.length, 2, '刷新或已有价格时不再补');
    assert.equal(env.state.openSeq, opened, '刷新不推进打开序号');
  }
  console.log('quote and F10 split UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
