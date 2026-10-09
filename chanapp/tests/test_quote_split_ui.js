'use strict';
/* 报价与 F10 分离（目标 2026-09-29 第三阶段「报价、F10 与状态栏」；2026-10-09 修订：A 股当前代码的价格卡与
   自选行改由 /api/chart 应答内嵌 quote 驱动，与图中末根 bar 同一次服务端读取，不再经独立报价轮询链；
   港股报价通道不变）。
   失败方式：F10 慢或失败时价格卡不显示；内嵌报价与报价表轮流覆盖同一行（盘中读数跳变）；
   A 股仍走 /api/quote 独立链（与图不同快照）；港股单代码报价被一并砍掉；quote 为 null 时编造价格；
   快速切换代码时旧代码的价格串到新代码；正在看的非自选港股不随报价轮询刷新。 */
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

  // 非自选代码：单代码报价入口只服务港股；A 股由 /api/chart 内嵌报价覆盖，不发 /api/quote；自选代码不重复请求
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.code = 'sh600036';
    env.loadViewQuote('sh600036');
    assert.equal(env._harness.pending.length, 0, '自选代码不走单代码报价');
    env.state.code = 'sz000002';
    env.loadViewQuote('sz000002');
    assert.equal(env._harness.pending.length, 0, 'A 股不走单代码报价（内嵌报价覆盖）');
    env.state.code = 'hk00700';
    env.loadViewQuote('hk00700');
    assert.equal(env._harness.pending[0].url, '/api/quote?code=hk00700');
    env._harness.pending[0].resolve(okJson({ code: 'hk00700', quote: { price: 512.5, pct: -1.2, price_label: '最新' } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '512.50', '非自选港股显示单代码报价');
    assert.notEqual(env.el('quoteSec').style.display, 'none');
  }

  // 快速切换：旧代码的迟到报价不串到新代码
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.loadViewQuote('hk00700');
    env.state.code = 'hk00001';
    env.loadViewQuote('hk00001');
    env._harness.pending[0].resolve(okJson({ code: 'hk00700', quote: { price: 512.5, pct: -1.2, price_label: '最新' } }));
    await tick(); await tick();
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '--', '旧代码的迟到报价不显示在新代码上');
    env._harness.pending[1].resolve(okJson({ code: 'hk00001', quote: { price: 3.3, pct: -0.3, price_label: '最新' } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '3.30');
  }

  // 报价轮询也刷新正在看的非自选港股（按它的市场判断）；A 股不随报价轮询发单代码请求（内嵌报价覆盖）
  {
    const seen = [];
    const env = timerEnv({ marketOf: c => (c && c.indexOf('hk') === 0 ? 'hk' : 'cn'),
                           loadViewQuote: code => seen.push(code) });
    env.state.watchlist = [{ code: 'sh600036' }];
    env.state.code = 'hk00700';
    env._harness.intervals[0]();                      // 60s 回调取 /api/session：港股开市
    env._harness.pending.shift().resolve(okJson({ markets: { cn: { open: false }, hk: { open: true } } }));
    await tick(); await tick();
    env._harness.intervals[1]();
    assert.deepEqual(seen, ['hk00700'], '非自选港股随报价轮询刷新');
  }
  // 第三阶段第二轮复审应修 4：同一代码的两次单代码报价逆序返回（首开的空报价晚到）——只接纳最新一次请求
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.loadViewQuote('hk00700');                     // 首开：快照还没到
    env.loadViewQuote('hk00700');                     // 补取
    env._harness.pending[1].resolve(okJson({ code: 'hk00700', quote: { price: 512.5, pct: -1.2, price_label: '最新' } }));
    await tick(); await tick();
    env._harness.pending[0].resolve(okJson({ code: 'hk00700', quote: { price: null, price_unavailable: true } }));
    await tick(); await tick();
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '512.50', '较早请求的空报价晚到不覆盖');
  }

  // 第三阶段第七轮复审应修：移出最后一只自选后，报价表里的残留旧价不能遮住当前报价——
  // A 股当前报价即 chart 内嵌报价（与图同快照，不按是否自选切换通道），不取单代码报价
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.chartQuote = { code: 'sh600036', quote: { price: 40.5, pct: 1.2, price_label: '最新' } };
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '40.50', '内嵌报价优先于报价表');
    env.state.watchlist = [];                          // 移出自选：报价表不再刷新这只
    env.loadViewQuote('sh600036');
    assert.equal(env._harness.pending.length, 0, 'A 股移出后不取单代码报价');
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '40.50', '移出后内嵌报价继续覆盖，残留旧价不遮挡');
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
    // 移出（A 股）：不取单代码报价；价格卡继续显示 chart 内嵌报价（与图同快照），不显示报价表残留旧价
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.chartQuote = { code: 'sh600036', quote: { price: 40.5, pct: 1.2, price_label: '最新' } };
    env.removeWatch('sh600036');
    await tick();
    byUrl(env, '/api/watchlist/sh600036').resolve(okJson([]));
    await tick(); await tick();
    assert.ok(!byUrl(env, '/api/quote?code=sh600036'), 'A 股移出后不取单代码报价');
    assert.equal(nodes.f10Px.textContent, '40.50', '移出后显示内嵌报价而非报价表旧价');
  }
  {
    // 移出（港股，没有单代码报价）：立即请求单代码报价，价格卡不再显示报价表里的旧价
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.state.watchlist = [{ code: 'hk00700', name: '腾讯控股' }];
    env.state.quotes = { hk00700: { price: 512.5, pct: -1.2, price_label: '最新' } };
    env.removeWatch('hk00700');
    await tick();
    byUrl(env, '/api/watchlist/hk00700').resolve(okJson([]));
    await tick(); await tick();
    assert.ok(byUrl(env, '/api/quote?code=hk00700'), '港股移出后立即取单代码报价');
    assert.equal(nodes.f10Px.textContent, '--', '移出后不显示报价表旧价');
    byUrl(env, '/api/quote?code=hk00700').resolve(okJson({ code: 'hk00700', quote: { price: 513.0, pct: -1.1 } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '513.00');
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

  // 第三阶段复审应修 6：收盘后首开非自选港股，单代码报价与图表首取并行、快照还没到时为空；首取完成后补取一次，
  // 已有价格或只是刷新时不补（A 股无此路径：首取应答即内嵌报价）
  {
    const seen = [];
    const env = loadEnv({ loadViewQuote: code => seen.push(code) });
    env.state.code = 'hk00700';
    env.state.freq = 'day';
    env.load();
    assert.deepEqual(seen, ['hk00700'], '打开时取一次');
    const opened = env.state.openSeq;
    assert.ok(opened >= 1, '打开（非刷新）推进打开序号：收盘后跟进按这次打开计');
    env._harness.pending[0].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v1"'));
    await tick(); await tick(); await tick();
    assert.deepEqual(seen, ['hk00700', 'hk00700'], '首取完成后报价仍空：补取一次');
    env.state.viewQuote = { code: 'hk00700', quote: { price: 512.5 } };
    env.load({ refresh: true });
    env._harness.pending[1].resolve(okJson({ kline: [], macd: { rows: [] }, meta: {} }, '"v2"'));
    await tick(); await tick(); await tick();
    assert.equal(seen.length, 2, '刷新或已有价格时不再补');
    assert.equal(env.state.openSeq, opened, '刷新不推进打开序号');
  }

  /* 2026-10-09 内嵌报价：A 股价格卡与自选当前行取 /api/chart 应答的 quote（与末根 bar 同一次服务端读取）。
     失败方式：内嵌报价不生效（卡头/行仍走报价表，盘中与图各读各的）；报价表轮询覆盖当前行；
     304 刷新丢报价；quote 为 null 时显示旧价或编造价格；港股应答（无 quote 键）误进内嵌通道。 */
  function chartQuoteEnv() {
    const env = loadEnv({
      watchName: code => code, signCls: v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'),
      wireSearch() {}, renderF10() {}, renderFlow() {},
    });
    runSlices(env, ['watchlistUi', 'f10Header', 'quotesF10']);   // 真身覆盖桩（含 chartQuote/chartQuoteFor）
    return env;
  }
  const CHART_BODY = quote => ({
    kline: [{ time: '2026-10-09', open: 40, high: 41, low: 39.5, close: 40.12, volume: 1 }],
    macd: { rows: [] }, meta: {}, quote,
  });

  // chart 200 内嵌 quote 驱动价格卡与自选当前行；报价表轮询不覆盖当前行、其他行照常
  {
    const env = chartQuoteEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.freq = 'day';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }, { code: 'sz000002', name: '万科A' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.load();
    assert.equal(env._harness.pending.filter(p => p.url.startsWith('/api/quote?')).length, 0,
      '自选 A 股打开不发单代码报价');
    env._harness.pending[0].resolve(okJson(CHART_BODY(
      { price: 40.12, pct: 1.36, price_label: '最新', price_time: '2026-10-09 10:31' })));
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.12', '卡头取内嵌报价而非报价表');
    assert.equal(nodes.f10Chg.textContent, '+1.36%');
    const rows = () => nodes.wlItems.children.map(c => c.innerHTML);
    assert.ok(rows()[0].includes('40.12') && rows()[0].includes('+1.36%'), '自选当前行取内嵌报价');
    env.loadQuotes();
    byUrl(env, '/api/quotes').resolve(okJson({ quotes: {
      sh600036: { price: 41.5, pct: 2.0, price_label: '最新' },
      sz000002: { price: 9.9, pct: -0.5, price_label: '最新' } } }));
    await tick(); await tick();
    assert.ok(rows()[0].includes('40.12'), '报价表轮询不覆盖当前 A 股行');
    assert.ok(rows()[1].includes('9.90') && rows()[1].includes('-0.50%'), '其他自选行照常随报价表更新');
    assert.equal(nodes.f10Px.textContent, '40.12', '卡头也不被报价表覆盖');

    // 304 刷新：内嵌报价保留
    env.load({ refresh: true });
    env._harness.pending[env._harness.pending.length - 1].resolve(
      { status: 304, ok: true, headers: { get: () => null }, json: async () => ({}) });
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.12', '304 后内嵌报价保留');
  }

  // quote 为 null（标的无任何事实）：卡头显示暂无可信价格，不留旧价
  {
    const env = chartQuoteEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.freq = 'day';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.load();
    env._harness.pending[0].resolve(okJson(CHART_BODY(null)));
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '--');
    assert.equal(nodes.f10Chg.textContent, '暂无可信价格', '无事实不显示报价表残留价');
    assert.ok(nodes.wlItems.children[0].innerHTML.includes('暂无可信价格'), '自选行同样置空');
  }

  // 港股应答不含 quote 键：不进内嵌通道，报价表/单代码报价照旧
  {
    const env = chartQuoteEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.state.freq = 'day';
    env.state.watchlist = [{ code: 'hk00700', name: '腾讯控股' }];
    env.state.quotes = { hk00700: { price: 512.5, pct: -1.2, price_label: '最新' } };
    env.load();
    const body = CHART_BODY(undefined);
    delete body.quote;
    env._harness.pending[0].resolve(okJson(body));
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '512.50', '港股仍取报价表');
  }
  console.log('quote and F10 split UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
