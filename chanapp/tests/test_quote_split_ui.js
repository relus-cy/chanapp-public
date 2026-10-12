'use strict';
/* 报价与 F10 分离（目标 2026-09-29 第三阶段「报价、F10 与状态栏」）与报价同快照（2026-10-09 起当前 A 股、
   2026-10-12 起自选各行与两个市场）：价格卡与自选各行只取报价表 state.quotes，报价表整表来自 /api/chart 应答
   内嵌的 quotes（与图中末根 bar 同一次服务端读取）或图表不刷新时的 /api/quotes（同样一次读取）。
   失败方式：F10 慢或失败时价格卡不显示；图表应答只刷新当前行、其他自选行另走报价链（左栏混着两个时刻，
   点开某只时它那行跳价）；较早发出的报价晚到覆盖较新的；304 刷新丢报价；无事实时编造或残留旧价；
   港股走另一条通道；移出当前代码后价格卡变空；图表失败时报价表停住；页面仍发单代码报价请求。 */
const assert = require('node:assert/strict');
const { tick, runSlices } = require('./support/dom.js');
const { appContext, loadEnv, okJson, errJson } = require('./support/app.js');

function railEnv(extras) {
  const context = appContext(Object.assign({
    watchName: code => code, signCls: v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'), F10_TTL: 300000,
    renderF10() { context._harness.renders.push('f10'); }, renderFlow() {},
  }, extras || {}));
  runSlices(context, ['quoteView', 'f10Header', 'hideF10', 'quotesF10', 'f10']);
  return context;
}

const byUrl = (env, url) => env._harness.pending.find(p => p.url === url);

// 价格卡是独立区块：不在 F10 区块里（F10 区块整体隐藏时不连带隐藏价格）
{
  const fs = require('node:fs'), path = require('node:path');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const block = id => { const i = html.indexOf('id="' + id + '"'); return html.slice(i, html.indexOf('<div class="sec"', i + 1)); };
  assert.match(block('quoteSec'), /id="f10Px"/, '价格在价格卡里');
  assert.doesNotMatch(block('f10Sec'), /id="f10Px"/, 'F10 区块不含价格');
}

// 页面不再有单代码报价链
{
  const { appSource } = require('./support/dom.js');
  assert.doesNotMatch(appSource, /\/api\/quote\?/, '页面不发 /api/quote');
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

  // 第三阶段复审阻断 2：打开时还没有报价，F10 挂起，报价表随后到达——价格卡立即显示，不等 F10
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
    byUrl(env, '/api/quotes?code=sh600036').resolve(okJson({ quotes: { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.00', '报价后到：F10 仍挂起时价格卡也更新');
  }

  // 港股与 A 股同一口径：价格时刻取 price_time
  {
    const env = railEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.state.watchlist = [{ code: 'hk00700', name: '腾讯控股' }];
    env.state.quotes = { hk00700: { price: 512.5, pct: -1.2, price_label: '最新', price_time: '2026-09-30 15:30' } };
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '512.50');
    assert.match(nodes.f10Px.title, /价格时刻 2026-09-30 15:30/, nodes.f10Px.title);
  }

  // 自选增删：报价表恒含当前代码，移出当前代码后价格卡不变空；加入后整表重取一次
  const opsEnv = () => {
    const env = railEnv({ renderWatchlist() {}, load() {}, recordView() {}, setStatus() {} });
    runSlices(env, ['watchlistOps']);
    return env;
  };
  {
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'hk00700';
    env.state.watchlist = [{ code: 'hk00700', name: '腾讯控股' }];
    env.state.quotes = { hk00700: { price: 512.5, pct: -1.2, price_label: '最新' } };
    env.removeWatch('hk00700');
    await tick();
    byUrl(env, '/api/watchlist/hk00700').resolve(okJson([]));
    await tick(); await tick();
    env.renderF10Header();
    assert.equal(nodes.f10Px.textContent, '512.50', '移出后价格卡照常（报价表恒含当前代码）');
    assert.ok(!env._harness.pending.some(p => p.url.startsWith('/api/quote')), '移出不另发报价请求');
  }
  {
    const env = opsEnv();
    env.state.code = 'sz000002';
    env.state.watchlist = [];
    env.addWatch('hk00700', '腾讯控股');
    await tick();
    byUrl(env, '/api/watchlist').resolve(okJson([{ code: 'hk00700', name: '腾讯控股' }]));
    await tick(); await tick();
    assert.ok(byUrl(env, '/api/quotes?code=sz000002'), '加入后立即整表重取（带上当前代码）');
  }
  {
    // 启动：报价表先到、自选列表后到，F10 挂起——自选列表一到价格卡就显示
    const env = opsEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.watchlist = [];
    env.state.quotes = { sh600036: { price: 45.0, pct: 2.0 } };
    env.loadWatchlist({ skipLoad: true });
    await tick();
    byUrl(env, '/api/watchlist').resolve(okJson([{ code: 'sh600036', name: '招商银行' }]));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '45.00');
  }
  {
    // 整表报价逆序返回：较早发出的请求晚到不覆盖
    const env = railEnv({ renderWatchlist() {} });
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.loadQuotes();
    env.loadQuotes();
    const [first, second] = env._harness.pending.filter(p => p.url.startsWith('/api/quotes'));
    second.resolve(okJson({ quotes: { sh600036: { price: 45.0, pct: 2.0 } } }));
    await tick(); await tick();
    first.resolve(okJson({ quotes: { sh600036: { price: 40.0, pct: 1.0 } } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '45.00');
  }

  /* 图表应答内嵌报价表：与末根 bar 同一次服务端读取，价格卡与自选各行一并跟进。 */
  function chartQuoteEnv() {
    const env = loadEnv({
      watchName: code => code, signCls: v => (v > 0 ? 'up' : v < 0 ? 'down' : 'flat'),
      wireSearch() {}, renderF10() {}, renderFlow() {},
    });
    runSlices(env, ['quoteView', 'watchlistUi', 'f10Header', 'quotesF10']);   // 真身覆盖桩（含 applyQuotes）
    return env;
  }
  const CHART_BODY = quotes => ({
    kline: [{ time: '2026-10-09', open: 40, high: 41, low: 39.5, close: 40.12, volume: 1 }],
    macd: { rows: [] }, meta: {}, quotes,
  });
  const rows = env => env._harness.nodes.wlItems.children.map(c => c.innerHTML);

  // chart 200 的报价表同时刷新价格卡、当前行与其他自选行；打开不另发报价请求
  {
    const env = chartQuoteEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.freq = 'day';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }, { code: 'hk00700', name: '腾讯控股' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' },
                         hk00700: { price: 500.0, pct: 0.1, price_label: '最新' } };
    env.load();
    assert.deepEqual(env._harness.pending.map(p => p.url.split('?')[0]), ['/api/chart'], '打开只发图表请求');
    env._harness.pending[0].resolve(okJson(CHART_BODY({
      sh600036: { price: 40.12, pct: 1.36, price_label: '最新', price_time: '2026-10-09 10:31' },
      hk00700: { price: 512.5, pct: -1.2, price_label: '最新', price_time: '2026-10-09 10:30' } })));
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.12', '卡头取应答报价');
    assert.equal(nodes.f10Chg.textContent, '+1.36%');
    assert.ok(rows(env)[0].includes('40.12') && rows(env)[0].includes('+1.36%'), '当前行取应答报价');
    assert.ok(rows(env)[1].includes('512.50') && rows(env)[1].includes('-1.20%'), '其他自选行同一次应答一并更新');

    // 304 刷新：报价表保留
    env.load({ refresh: true });
    env._harness.pending[env._harness.pending.length - 1].resolve(
      { status: 304, ok: true, headers: { get: () => null }, json: async () => ({}) });
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.12', '304 后报价保留');
    assert.ok(rows(env)[1].includes('512.50'));
  }

  // 较早发出的 /api/quotes 在较新的图表应答之后到达：不覆盖；之后发出的报价表照常接纳
  {
    const env = chartQuoteEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.freq = 'day';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.loadQuotes();
    env.load();
    const chart = env._harness.pending.find(p => p.url.startsWith('/api/chart'));
    chart.resolve(okJson(CHART_BODY({ sh600036: { price: 40.12, pct: 1.36, price_label: '最新' } })));
    await tick(); await tick(); await tick();
    byUrl(env, '/api/quotes?code=sh600036').resolve(okJson({ quotes: { sh600036: { price: 39.0, pct: 0.1 } } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.12', '较早发出的报价表晚到不覆盖');
    env.loadQuotes();
    env._harness.pending.filter(p => p.url.startsWith('/api/quotes')).pop()
      .resolve(okJson({ quotes: { sh600036: { price: 40.2, pct: 1.5 } } }));
    await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '40.20', '之后发出的报价表照常接纳');
  }

  // 无事实（price_unavailable）：卡头与自选行显示暂无可信价格，不留旧价
  {
    const env = chartQuoteEnv();
    const nodes = env._harness.nodes;
    env.state.code = 'sh600036';
    env.state.freq = 'day';
    env.state.watchlist = [{ code: 'sh600036', name: '招商银行' }];
    env.state.quotes = { sh600036: { price: 40.0, pct: 1.0, price_label: '最新' } };
    env.load();
    env._harness.pending[0].resolve(okJson(CHART_BODY({
      sh600036: { price: null, pct: null, limit_up: false, price_label: null, price_unavailable: true } })));
    await tick(); await tick(); await tick();
    assert.equal(nodes.f10Px.textContent, '--');
    assert.equal(nodes.f10Chg.textContent, '暂无可信价格', '无事实不显示残留价');
    assert.ok(rows(env)[0].includes('暂无可信价格'), '自选行同样置空');
  }

  // 图表加载失败：报价表单独取一次，不随失败的图表停住
  {
    const env = chartQuoteEnv();
    env.showChartError = () => {};
    env.state.code = 'hk00700';
    env.state.freq = 'day';
    env.load();
    env._harness.pending[0].resolve(errJson(502, { detail: '行情暂不可用' }));
    await tick(); await tick(); await tick();
    assert.ok(byUrl(env, '/api/quotes?code=hk00700'), '图表失败后补取报价表');
  }
  console.log('quote and F10 split UI checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
