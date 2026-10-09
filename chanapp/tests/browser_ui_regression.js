// Run with playwright-cli run-code using this file's contents on a local app page.
// All business endpoints are intercepted; no watchlist writes or model calls leave the browser.
async page => {
  await page.bringToFront();
  const origin = await page.evaluate(() => location.origin);
  await page.unroute('**/app.js');
  await page.route('**/app.js', async route => {
    const response = await route.fetch();
    const source = await response.text();
    if (!source.trimEnd().endsWith('})();')) throw new Error('app entry point changed');
    // Test-only access to the real closures; calculations and rendering run unchanged.
    const probe = 'window.__chartTest = {refresh: function(){load({refresh:true});}, setToken: function(t){viewState.token=t;}, setAnalysisTokens: function(t){viewState.analysisTokens=t;}, view: function(){return {token:viewState.token,analysisTokens:viewState.analysisTokens,hasMore:historyState.hasMore};}, setRange: function(r){charts.main.timeScale().setVisibleLogicalRange(r);}, read: function(){return {ready:!!activeChartVersion && activeChartVersion.code===state.code && activeChartVersion.freq===state.freq,main:charts.main.timeScale().getVisibleLogicalRange(),sub:charts.macdChart.timeScale().getVisibleLogicalRange(),times:klineData.map(function(b){return b.time;})};}};';
    await route.fulfill({response, body: source.replace(/\}\)\(\);\s*$/, probe + '})();')});
  });
  const failures = [], measurements = [], mutations = [];
  const check = (condition, message) => { if (!condition) failures.push(message); };
  const items = [
    {code: 'sh000001', name: '示例指数', starred: false, tags: []},
    {code: 'sz001309', name: '示例股票', starred: false, tags: ['观察']},
  ];
  const kline = Array.from({length: 80}, (_, i) => ({
    time: new Date(Date.UTC(2026, 0, 1 + i)).toISOString().slice(0, 10),
    open: 100 + i / 10, high: 102 + i / 10, low: 99 + i / 10,
    close: 101 + i / 10, volume: 10000 + i * 100,
  }));
  const evidence = [70, 60, 50].map((i, n) => ({
    dt: kline[i].time, price: kline[i].close, side: 'buy', level: 'bi',
    types: ['2'], status: 'confirmed', text: '测试信号 ' + n + '。依据和失效边界保留完整，点击定位到对应 K 线。',
  }));
  let analyses = 0, failChart = false, busyChart = false, pauseChart = false, releaseChart = null, refetchStatus = 'ok';
  // 视图令牌 fixture：分页带错令牌、分析带错或缺令牌时按契约返回扁平 409
  const fixtureToken = 'fixture-token';
  const fixtureAnalysisTokens = {day: 'fixture-day', m60: 'fixture-m60', m30: 'fixture-m30'};
  let historyMore = false, historyConflicts = 0, staleAnalyses = 0;
  // 查看记录 fixture：POST 记一次打开，GET 返回按代码去重、最近在前的列表（watched 故意恒为 false，页面须以自选列表为准）
  const viewPosts = [], recentViews = [], chartRequests = [];
  // 第三阶段：F10 可被挂起（验证报价卡不等 F10）；单代码报价只给非自选
  let holdF10 = false, releaseF10 = null;
  const quoteRequests = [];
  await page.unroute('**/api/**');
  await page.route('**/api/**', async route => {
    const request = route.request();
    const parts = request.url().slice(origin.length).split('?');
    const params = Object.fromEntries((parts[1] || '').split('&').map(s => s.split('=').map(decodeURIComponent)));
    const url = {pathname: parts[0], searchParams: {get: key => params[key]}};
    if (url.pathname === '/api/chart' && url.searchParams.get('before')) {
      if (url.searchParams.get('token') !== fixtureToken) {
        historyConflicts++;
        await route.fulfill({status: 409, json: {detail: '数据已更新', token: fixtureToken}});
        return;
      }
      await route.fulfill({json: {code: url.searchParams.get('code'), freq: url.searchParams.get('freq'), adjust: url.searchParams.get('adjust'),
        kline: [], meta: {history: true, token: fixtureToken, has_more: false, oldest_dt: kline[0].time}}});
      return;
    }
    if (url.pathname === '/api/chart') chartRequests.push({code: url.searchParams.get('code'), freq: url.searchParams.get('freq'), refetch: url.searchParams.get('refetch') || null,
      conditional: !!request.headers()['if-none-match']});
    if (url.pathname === '/api/chart' && pauseChart) await new Promise(resolve => { releaseChart = resolve; });
    if (url.pathname === '/api/chart' && failChart) {
      // 一般性数据失败用 502（服务端「行情暂不可用」）；503 专指同一代码取数在途，页面会自动重试
      await route.fulfill({status: 502, json: {detail: 'fixture unavailable'}});
      return;
    }
    if (url.pathname === '/api/chart' && busyChart) {     // 冷窗口且同一标的重拉在途：服务端不等锁，直接 503
      await route.fulfill({status: 503, json: {detail: '正在更新这只标的，请稍后再试'}, headers: {'X-Refetch-Status': 'busy'}});
      return;
    }
    if (url.pathname === '/api/f10' && holdF10) await new Promise(resolve => { releaseF10 = resolve; });
    let json = {};
    // 未列出的业务接口一律抛错：页面若再请求已删除的方案状态接口，本脚本即失败。
    if (url.pathname.startsWith('/api/watchlist')) {
      if (request.method() !== 'GET') mutations.push({path: url.pathname, method: request.method()});
      const code = url.pathname.split('/')[3];
      const index = items.findIndex(item => item.code === code);
      if (request.method() === 'DELETE' && index >= 0) items.splice(index, 1);
      else if (url.pathname.endsWith('/star') && index >= 0) items[index].starred = !items[index].starred;
      else if (request.method() === 'POST' && url.pathname === '/api/watchlist') items.push({...request.postDataJSON(), starred:false, tags:[]});
      json = items;
    } else if (url.pathname === '/api/periods') json = {catalog: [{freq: 'day', label: '日线'}, {freq: 'week', label: '周线'}, {freq: 'm60', label: '60分'}, {freq: 'm30', label: '30分'}], selected: ['day', 'week', 'm60', 'm30'], notice: null, revision: 'fixture', markets: {cn: {available: ['day', 'week', 'm60', 'm30'], reasons: {}}, hk: {available: ['day', 'week', 'm60', 'm30'], reasons: {}}}};
    else if (url.pathname === '/api/quotes') json = {quotes: {}};
    else if (url.pathname === '/api/quote') {
      quoteRequests.push(url.searchParams.get('code'));
      json = {code: url.searchParams.get('code'), quote: {price: 9.87, pct: 0.5, name: '查看股票'}, degraded: false};
    }
    else if (url.pathname === '/api/session') json = {checked_at: '2026-01-05T20:00:00', markets: {cn: {open: false}, hk: {open: false}}};
    else if (url.pathname === '/api/f10') json = {code: url.searchParams.get('code'), f10: {name: '示例指数', price: 108, pct: 1}, meta: {}};
    else if (url.pathname === '/api/chart') json = {
      code: url.searchParams.get('code'), freq: url.searchParams.get('freq'), adjust: url.searchParams.get('adjust'),
      schema_version: 'chanpy_v2', calculation_id: 'fixture-v1', data_version: 'fixture-v1',
      // A 股应答内嵌同快照报价（488ca50）：价格卡与自选当前行由此驱动，不再请求 /api/quote；港股不含此键
      ...(/^(sh|sz)/.test(url.searchParams.get('code') || '') ? {quote: {price: 9.87, pct: 0.5}} : {}),
      rule_profile: url.searchParams.get('rule_profile'), signal_scope: url.searchParams.get('signal_scope'),
      kline, macd: {rows: kline.map(b => ({time: b.time, dif: 1, dea: .5, hist: 1}))},
      structure: {bi: [], xd: [], zs: []}, channels: [], signals: [],
      resonance: [{freq: 'm60', signals: [{...evidence[0], detail: {last_sure_pos: 1}}]}],
      evidence: evidence.map(e => ({...e, text: e.text + ' [' + url.searchParams.get('code') + '/' + url.searchParams.get('freq') + ']'})),
      meta: {source: 'fixture', fqf: '前复权', fetch_time: '2026-03-21T12:00:00+08:00', bars: 80, first_dt: kline[0].time, last_dt: kline[79].time,
        adjust: url.searchParams.get('adjust'), token: fixtureToken, has_more: historyMore, oldest_dt: kline[0].time,
        analysis_tokens: fixtureAnalysisTokens, analysis_freqs: ['day', 'm60', 'm30'],
        coverage: {qfq_from: null, qfq_through: null, stop_reason: null,
          data_status: {phase: 'awaiting_final', day: '2026-01-05', at: '2026-01-05 15:01'}}},
    };
    else if (url.pathname === '/api/analysis') {
      analyses++;
      let sent = null;
      try { sent = JSON.parse(url.searchParams.get('tokens')); } catch (_) { sent = null; }
      if (!sent || Object.keys(fixtureAnalysisTokens).some(k => sent[k] !== fixtureAnalysisTokens[k])) {
        staleAnalyses++;
        await route.fulfill({status: 409, json: {detail: '数据已更新', tokens: fixtureAnalysisTokens}});
        return;
      }
      json = {status: 'unconfigured', adjust: url.searchParams.get('adjust'), schema_version: 'chanpy_v2', calculation_id: 'fixture-v1', rule_profile: url.searchParams.get('rule_profile'), signal_scope: url.searchParams.get('signal_scope')};
    } else if (url.pathname === '/api/search') json = [{code:'sh000001',name:'新增自选'}];
    else if (url.pathname === '/api/views') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON();
        viewPosts.push(body);
        const at = recentViews.findIndex(v => v.code === body.code);
        if (at >= 0) recentViews.splice(at, 1);
        recentViews.unshift({...body, first_viewed_at: '2026-01-05T20:00:00', last_viewed_at: '2026-01-05T20:0' + viewPosts.length + ':00',
          views: 1, watched: false});
        json = {ok: true};
      } else json = {recent: recentViews};
    }
    else throw new Error('Unexpected business endpoint: ' + url.pathname);
    const refetchHeader = url.pathname === '/api/chart' && url.searchParams.get('refetch') ? {'X-Refetch-Status': refetchStatus} : undefined;
    await route.fulfill({json, headers: refetchHeader});
  });
  await page.setViewportSize({width: 1440, height: 900});
  await page.goto(origin);
  await page.evaluate(() => { localStorage.removeItem('chanapp-sidebar'); localStorage.setItem('chanapp-theme', 'light'); });
  await page.reload();
  await page.locator('#cardsCount').filter({hasText: '1/3'}).waitFor();
  check(analyses === 0, 'chart load must not request AI');

  // Details belong to the successfully loaded chart, never the pending selection.
  const detailOpen = () => page.locator('#aiPop').evaluate(e => e.classList.contains('open'));
  const closeDetailIfOpen = async () => { if (await detailOpen()) await page.locator('#aiPopBack').click(); };
  const waitForChart = () => page.waitForFunction(() => window.__chartTest.read().ready && !document.getElementById('center').classList.contains('ctx-old'));
  const waitForPausedChart = async () => {
    const deadline = Date.now() + 5000;
    while (!releaseChart && Date.now() < deadline) await page.waitForTimeout(20);
    if (!releaseChart) throw new Error('detail regression request did not reach the fixture');
  };
  const resumeChart = async () => { pauseChart = false; releaseChart(); releaseChart = null; await waitForChart(); };
  await page.locator('#evidenceBtn').click();
  check(await detailOpen(), 'loaded evidence must open a nonempty detail');
  check((await page.locator('#aiPopBody').innerText()).includes('[sh000001/day]'), 'initial detail must carry the day fixture');
  pauseChart = true;
  await page.locator('.freq [data-freq="m60"]').click();
  await waitForPausedChart();
  check(!(await detailOpen()), 'frequency switch must close the previous chart detail immediately');
  await closeDetailIfOpen();
  await page.locator('.res-chip[data-freq="m60"] .res-detail-btn').click();
  check(!(await detailOpen()), 'pending minute chart must not interpret signal positions using daily bars');
  await closeDetailIfOpen();
  await resumeChart();
  await page.locator('.res-chip[data-freq="m60"] .res-detail-btn').click();
  check(await detailOpen(), 'successfully loaded minute chart must allow signal details');
  check((await page.locator('#aiPopBody').innerText()).includes('最近确认 K 线：01-02'), 'loaded matching bars provide the confirmation time');
  await closeDetailIfOpen();
  await page.locator('#evidenceBtn').click();
  check((await page.locator('#aiPopBody').innerText()).includes('[sh000001/m60]'), 'new detail must use the newly loaded frequency');
  pauseChart = true;
  await page.locator('#wlItems .item[data-code="sz001309"]').click();
  await waitForPausedChart();
  check(!(await detailOpen()), 'stock switch must close the previous detail');
  await closeDetailIfOpen();
  await page.locator('#evidenceBtn').click();
  check(!(await detailOpen()), 'pending stock must not label old evidence with the new stock name');
  await closeDetailIfOpen();
  failChart = true;
  pauseChart = false; releaseChart(); releaseChart = null;
  await page.locator('#chartError').waitFor({state: 'visible'});
  await page.locator('#evidenceBtn').click();
  check(!(await detailOpen()), 'failed stock load must keep old evidence details unavailable');
  await closeDetailIfOpen();
  await page.locator('.res-chip[data-freq="m60"] .res-detail-btn').click();
  check(!(await detailOpen()), 'failed stock load must keep old signal details unavailable');
  await closeDetailIfOpen();
  failChart = false;
  await page.locator('#chartError button').click();
  await waitForChart();
  await page.locator('#evidenceBtn').click();
  check(await detailOpen(), 'retry success must restore evidence details');
  check((await page.locator('#aiPopBody').innerText()).includes('[sz001309/m60]'), 'retry detail must use the new stock data');
  await page.locator('.freq [data-freq="day"]').click();
  check(!(await detailOpen()), 'selection changes must close details');
  await closeDetailIfOpen();
  await waitForChart();
  await page.locator('#evidenceBtn').click();
  check((await page.locator('#aiPopBody').innerText()).includes('[sz001309/day]'), 'loading after a selection change must restore matching details');
  pauseChart = true;
  await page.evaluate(() => window.__chartTest.refresh());
  await waitForPausedChart();
  check(!(await detailOpen()), 'chart refresh must invalidate an open evidence snapshot');
  await closeDetailIfOpen();
  await resumeChart();
  await page.locator('#wlItems .item[data-code="sh000001"]').click();
  await waitForChart();
  await page.locator('.freq [data-freq="day"]').click();
  await waitForChart();

  // Exercise the real pointer path through the rail margin, with and without quotes.
  const originalItems = items.slice();
  const hoverMeasurements = [];
  const settleSidebar = () => page.locator('#sidebar').evaluate(async node => {
    await Promise.all(node.getAnimations().map(a => a.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  for (const count of [6, 30, 100]) {
    items.splice(0, items.length, ...Array.from({length: count}, (_, i) => ({
      code: 'sz' + String(i + 1).padStart(6, '0'), name: '示例股票' + i, starred: false, tags: [],
    })));
    for (const quotesReady of [false, true]) {
      await page.route('**/api/quotes', route => route.fulfill({json: {
        quotes: quotesReady ? Object.fromEntries(items.map(w => [w.code, {price: 10, pct: 1}])) : {},
      }}));
      await page.mouse.move(500, 400);
      await page.evaluate(() => localStorage.setItem('chanapp-sidebar', 'rail'));
      await page.reload();
      await page.waitForFunction(n => document.querySelectorAll('#wlItems .item').length === n, count);
      if (quotesReady) await page.waitForFunction(() => document.querySelector('#wlItems .chg').textContent.includes('%'));
      await settleSidebar();
      await page.locator('.sb-group').evaluate((g, n) => { g.scrollTop = n === 100 ? g.scrollHeight : n > 6 ? 200 : 0; }, count);
      for (const entry of [{edge: true, position: 'first'}, {edge: false, position: 'middle'}, {edge: true, position: 'last'}]) {
        const {edge, position} = entry;
        const before = await page.evaluate(position => {
          const g = document.querySelector('.sb-group'), gr = g.getBoundingClientRect();
          const rows = [...document.querySelectorAll('#wlItems .item')].filter(row => {
            const r = row.getBoundingClientRect(); return r.top >= gr.top && r.bottom < gr.bottom;
          });
          if (!rows.length) throw new Error('fixture has no visible watchlist rows');
          const row = rows[position === 'first' ? 0 : position === 'last' ? rows.length - 1 : Math.min(3, rows.length - 1)], r = row.getBoundingClientRect();
          return {code: row.dataset.code, top: r.top, y: (r.top + r.bottom) / 2, scroll: g.scrollTop,
            railHeight: row.getBoundingClientRect().height};
        }, position);
        // Record the event boundary as well as every painted frame. Final alignment alone
        // misses a layout jump followed by a delayed scroll correction.
        await page.evaluate(({code, top}) => {
          const sidebar = document.getElementById('sidebar');
          const row = [...sidebar.querySelectorAll('.item')].find(n => n.dataset.code === code);
          window.__hoverMotion = [];
          const sample = phase => {
            const group = sidebar.querySelector('.sb-group');
            window.__hoverMotion.push({phase, drift: row.getBoundingClientRect().top - top,
              width: group.getBoundingClientRect().width, horizontalOverflow: group.scrollWidth > group.clientWidth});
          };
          sidebar.addEventListener('mousemove', () => {
            sample('event-end');
            let finished = false;
            Promise.all(sidebar.getAnimations().map(a => a.finished.catch(() => {}))).then(() => { finished = true; });
            window.__hoverMotionDone = new Promise(resolve => {
              const frame = () => { sample('frame'); if (finished) resolve(); else requestAnimationFrame(frame); };
              requestAnimationFrame(frame);
            });
          }, {once: true});
        }, before);
        await page.mouse.move(90, before.y);
        if (edge) await page.mouse.move(86, before.y);
        await page.mouse.move(70, before.y);
        await settleSidebar();
        const motion = await page.evaluate(async () => { await window.__hoverMotionDone; return window.__hoverMotion; });
        check(motion.length >= 2 && motion.every(frame => Math.abs(frame.drift) <= 1),
          'hover must be aligned before paint and throughout reveal: ' + JSON.stringify({count, quotesReady, edge, motion}));
        check(motion.every(frame => !frame.horizontalOverflow && Math.abs(frame.width - motion[0].width) <= 1),
          'hover content must have a stable width without horizontal scrolling: ' + JSON.stringify({count, motion}));
        const formWithin = await page.locator('#wlForm').evaluate(e => e.getBoundingClientRect().bottom <= innerHeight);
        check(formWithin, 'deep scroll compensation pushes add form out of sidebar: ' + count);
        // 逐行等高契约的实测端：rail 态与展开态同一行总高差 ≤1px（含空报价行盒）。
        const openHeight = await page.evaluate(code => {
          const row = [...document.querySelectorAll('#wlItems .item')].find(n => n.dataset.code === code);
          return row ? row.getBoundingClientRect().height : null;
        }, before.code);
        check(openHeight !== null && Math.abs(openHeight - before.railHeight) <= 1,
          'row must keep identical height across rail/open reveal: ' + JSON.stringify({count, quotesReady, edge, code: before.code, railHeight: before.railHeight, openHeight}));
        const after = await page.evaluate(y => ({
          code: document.elementFromPoint(70, y)?.closest('.item')?.dataset.code,
          scroll: document.querySelector('.sb-group').scrollTop,
        }), before.y);
        check(after.code === before.code, 'hover changes stock under pointer: ' + JSON.stringify({count, quotesReady, edge, before, after}));
        await page.mouse.move(500, before.y);
        await settleSidebar();
        const closed = await page.locator('.sb-group').evaluate(g => ({scroll: g.scrollTop, pad: g.style.paddingBottom}));
        check(Math.abs(closed.scroll - before.scroll) <= 1 && closed.pad === '',
          'hover round-trip changes scroll: ' + JSON.stringify({count, quotesReady, edge, before, closed}));
        hoverMeasurements.push({count, quotesReady, edge, position, code: before.code, matched: after.code === before.code, scrollDelta: closed.scroll - before.scroll,
          railHeight: before.railHeight, openHeight});
      }
      await page.unroute('**/api/quotes');
    }
  }
  items.splice(0, items.length, ...originalItems);
  await page.evaluate(() => localStorage.setItem('chanapp-sidebar', 'pinned'));
  await page.reload();
  await page.locator('#cardsCount').filter({hasText: '1/3'}).waitFor();

  for (const width of [1440, 1024, 375]) {
    await page.setViewportSize({width, height: 900});
    await settleSidebar();
    const footerFits = await page.locator('#sidebar').evaluate(sidebar =>
      ['wlForm'].every(id => Math.abs(document.getElementById(id).getBoundingClientRect().width - sidebar.clientWidth) <= 1));
    check(footerFits, 'sidebar footer must fill the expanded width at ' + width);
  }
  await page.setViewportSize({width: 1440, height: 900});
  await settleSidebar();

  // 星/删钮「视觉隐身但可聚焦」：非悬停非选中行透明且不接收指针（须在 star 置顶测试之前采样，
  // 置顶后 .star.on 常显）。SVG 以 currentColor 渲染（stroke 属性是源值，computed 会被解析成颜色）。
  const idleBtns = await page.evaluate(() => {
    const item = document.querySelectorAll('#wlItems .item')[1];
    const btn = sel => { const b = item.querySelector(sel); const c = getComputedStyle(b); const svg = b.querySelector('svg'); return {
      opacity: parseFloat(c.opacity), pointerEvents: c.pointerEvents, display: c.display, visibility: c.visibility,
      svg: !!svg, stroke: svg ? svg.getAttribute('stroke') : null }; };
    return {star: btn('.star'), del: btn('.del')};
  });
  for (const [kind, b] of Object.entries(idleBtns)) {
    check(b.opacity === 0 && b.pointerEvents === 'none', `idle ${kind} button must be visually hidden and unclickable: ` + JSON.stringify(b));
    check(b.display !== 'none' && b.visibility !== 'hidden', `idle ${kind} button must stay keyboard-focusable: ` + JSON.stringify(b));
    check(b.svg && b.stroke === 'currentColor', `${kind} button must render a currentColor SVG: ` + JSON.stringify(b));
  }
  // 选中行可辨识：active 行的背景/投影至少一项与其余行不同
  const activePaint = await page.evaluate(() => {
    const items = document.querySelectorAll('#wlItems .item');
    const act = getComputedStyle(items[0]), rest = getComputedStyle(items[1]);
    return {actBg: act.backgroundColor, actShadow: act.boxShadow, restBg: rest.backgroundColor, restShadow: rest.boxShadow};
  });
  check(activePaint.actBg !== activePaint.restBg || activePaint.actShadow !== activePaint.restShadow,
    'active row must be visually distinguishable: ' + JSON.stringify(activePaint));

  // 焦点迁移守卫：从「非选中行」Tab 进星钮——focus-within 路径必须让按钮显现（不接悬停）。
  // opacity 有 .14s 过渡：pointer-events 即时生效，opacity 等收敛后再断言。
  await page.locator('#wlItems .item').nth(1).focus();
  await page.keyboard.press('Tab');
  const focusedStar = page.locator('#wlItems .item').nth(1).locator('.star');
  check(await focusedStar.evaluate(e => document.activeElement === e), 'Tab from watchlist row reaches star');
  const focusedPaint = await page.evaluate(() => new Promise(resolve => {
    const star = document.querySelectorAll('#wlItems .item')[1].querySelector('.star');
    const t0 = Date.now();
    const probe = () => {
      const c = getComputedStyle(star);
      const v = {opacity: parseFloat(c.opacity), pointerEvents: c.pointerEvents};
      if ((v.opacity === 1 && v.pointerEvents === 'auto') || Date.now() - t0 > 1000) resolve(v);
      else requestAnimationFrame(probe);
    };
    probe();
  }));
  check(focusedPaint.opacity === 1 && focusedPaint.pointerEvents === 'auto',
    'focus-within must reveal row actions without hover: ' + JSON.stringify(focusedPaint));
  await page.keyboard.press('Enter');
  await page.waitForTimeout(100);
  check(mutations.some(m => m.path.endsWith('/star')), 'nested star Enter must activate star instead of selecting row');
  check(await focusedStar.evaluate(e => document.activeElement === e), 'watchlist rerender must preserve the active row action');

  await page.locator('#cards .card').focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  check((await page.locator('#cardsCount').textContent()) === '3/3', 'evidence navigation must retain focus across card replacements');

  await page.evaluate(() => window.__chartTest.setRange({from:10.25,to:40.25}));
  await page.waitForFunction(() => {
    const range = window.__chartTest.read();
    return Math.abs(range.main.from - 10.25) < .01 && Math.abs(range.sub.from - 10.25) < .01;
  });
  pauseChart = true;
  await page.evaluate(() => window.__chartTest.refresh());
  const deadline = Date.now() + 5000;
  while (!releaseChart && Date.now() < deadline) await page.waitForTimeout(20);
  if (!releaseChart) throw new Error('refresh request did not reach the fixture');
  // Drive the in-flight interaction with the pointer. A second main-only setRange
  // can be overwritten by a pending subchart echo before the library's next frame;
  // that test-only API sequence does not represent how a user pans the canvas.
  const dragStart = await page.evaluate(() => window.__chartTest.read());
  const dragBox = await page.locator('#chart').boundingBox();
  await page.mouse.move(dragBox.x + dragBox.width * .6, dragBox.y + dragBox.height * .5);
  await page.mouse.down();
  await page.mouse.move(dragBox.x + dragBox.width * .3, dragBox.y + dragBox.height * .5, {steps: 12});
  await page.mouse.up();
  await page.waitForFunction(start => {
    const range = window.__chartTest.read();
    return range.main.from > start + 1 && Math.abs(range.main.from - range.sub.from) < .01;
  }, dragStart.main.from);
  const before = await page.evaluate(() => window.__chartTest.read());
  check(!before.ready && before.times.length === 80 && before.times[79] === '2026-03-21',
    'chart must remain pannable while the refresh response is still held');
  check(before.main.from > dragStart.main.from + 1 &&
    Math.abs((before.main.to - before.main.from) - (dragStart.main.to - dragStart.main.from)) < .01,
    'pointer drag must move the visible dates without zooming: ' + JSON.stringify({start: dragStart.main, dragged: before.main}));
  kline.shift();
  kline.push({...kline[kline.length-1],time:'2026-03-22'});
  pauseChart = false;
  releaseChart();
  // 同令牌刷新按 mergeWithHistory 保留已加载的更早 K 线：首根仍是 01-01，新末根 03-22 追加在后，
  // 可视区间按日期不动（旧写法等首根滚到 01-02，早于历史合并语义，已随之改写）
  await page.waitForFunction(() => window.__chartTest.read().times.slice(-1)[0] === '2026-03-22');
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const after = await page.evaluate(() => window.__chartTest.read());
  check(after.times[0] === '2026-01-01' && after.times.length === 81, 'same-token refresh keeps loaded older bars: ' + JSON.stringify({first: after.times[0], n: after.times.length}));
  check(Math.abs(after.main.from - before.main.from) < .01 && Math.abs(after.main.to - before.main.to) < .01, 'delayed refresh must preserve the latest viewport by date: ' + JSON.stringify({before:before.main,after:after.main}));
  check(after.times[Math.floor(after.main.from)] === before.times[Math.floor(before.main.from)], 'rolling window must preserve the visible date: ' + JSON.stringify({before:before.main,after:after.main,beforeDate:before.times[Math.floor(before.main.from)],afterDate:after.times[Math.floor(after.main.from)]}));
  check(Math.abs(after.main.from - after.sub.from) < .01 && Math.abs(after.main.to - after.sub.to) < .01, 'refresh keeps both time scales synchronized');
  check((await page.locator('#cardsCount').textContent()) === '3/3', 'refresh must preserve the selected evidence');

  // 视图令牌与 409：分页时令牌已变 → 整窗重载并提示；分析时令牌已变 → 重载、提示重新分析、不自动重发
  historyMore = true;
  await page.evaluate(() => window.__chartTest.refresh());
  await page.waitForFunction(() => window.__chartTest.view().hasMore);
  await page.evaluate(() => window.__chartTest.setToken('stale-token'));
  await page.evaluate(() => window.__chartTest.setRange({from: 0, to: 30}));
  await page.waitForFunction(() => document.getElementById('status').textContent.includes('数据已更新，已重新加载'));
  check(historyConflicts === 1, 'a stale history token must receive exactly one 409: ' + historyConflicts);
  check((await page.evaluate(() => window.__chartTest.view().token)) === fixtureToken, 'whole-window reload must adopt the current token');
  check((await page.evaluate(() => window.__chartTest.read().times.length)) === kline.length, 'whole-window reload must not splice bars from the stale token');
  historyMore = false;
  const analysesBefore409 = analyses;
  await page.evaluate(() => window.__chartTest.setAnalysisTokens({day: 'stale-day', m60: 'fixture-m60', m30: 'fixture-m30'}));
  await page.locator('#aiRefresh').click();
  await page.locator('#aiPanel').filter({hasText: '数据已更新，请重新分析'}).waitFor();
  await page.waitForFunction(() => (window.__chartTest.view().analysisTokens || {}).day === 'fixture-day');
  await page.waitForTimeout(800);
  check(staleAnalyses === 1 && analyses === analysesBefore409 + 1, 'analysis 409 must not auto-resend: ' + JSON.stringify({staleAnalyses, analyses, analysesBefore409}));
  check((await page.locator('#aiPanel').innerText()).includes('请重新分析'), 'the reload keeps the re-analyze prompt until the user asks again');
  check(await page.locator('#aiRefresh').isEnabled(), 'analysis 409 releases the refresh button');

  await page.locator('#sidebarPin').click();
  check((await page.locator('body.sb-rail').count()) === 1, 'unpin collapses to the rail');
  check(await page.locator('#wlItems .item').first().isVisible(), 'rail keeps watchlist rows visible');
  check(await page.locator('#wlForm').isHidden(), 'rail hides the add form');
  // rail 让位契约：主区只让窄栏宽（--rail-width）。margin/clip-path 有过渡，等收敛后再量。
  const railWidth = await page.evaluate(() => parseFloat(getComputedStyle(document.getElementById('sidebar')).getPropertyValue('--rail-width')));
  await page.waitForFunction(w => Math.abs(parseFloat(getComputedStyle(document.getElementById('page')).marginLeft) - w) <= 1, railWidth);
  await settleSidebar();
  const railLayout = await page.evaluate(() => ({
    pageMargin: parseFloat(getComputedStyle(document.getElementById('page')).marginLeft),
    railWidth: parseFloat(getComputedStyle(document.getElementById('sidebar')).getPropertyValue('--rail-width')),
    groupWidth: document.querySelector('.sb-group').clientWidth,
    railBtnVisible: getComputedStyle(document.getElementById('wlRailBtn')).visibility === 'visible',
    railCountVisible: getComputedStyle(document.getElementById('wlCountRail')).visibility === 'visible',
    countVisible: getComputedStyle(document.getElementById('wlCount')).visibility,
  }));
  check(Math.abs(railLayout.pageMargin - railLayout.railWidth) <= 1 && railLayout.groupWidth <= railLayout.railWidth,
    'rail must yield exactly the rail width to the page: ' + JSON.stringify(railLayout));
  check(railLayout.railBtnVisible && railLayout.railCountVisible, 'rail-only affordances must appear in rail');
  check(railLayout.countVisible === 'hidden', 'expanded count must stay hidden inside its slot in rail');
  await page.hover('#chart');
  await page.hover('#sidebar .sb-top');
  await page.waitForFunction(() => document.body.classList.contains('sb-open'));
  check(await page.locator('#wlForm').isVisible(), 'hover expands the floating sidebar');
  const floatLayout = await page.evaluate(() => ({
    pageMargin: parseFloat(getComputedStyle(document.getElementById('page')).marginLeft),
    railBtnVisible: getComputedStyle(document.getElementById('wlRailBtn')).visibility,
    countVisible: getComputedStyle(document.getElementById('wlCount')).visibility,
  }));
  check(Math.abs(floatLayout.pageMargin - railLayout.pageMargin) <= 1, 'floating open must overlay, not push the page: ' + JSON.stringify(floatLayout));
  check(floatLayout.railBtnVisible === 'hidden' && floatLayout.countVisible === 'visible',
    'open must swap rail-only content back to the expanded slot content: ' + JSON.stringify(floatLayout));
  await page.locator('#sidebarPin').click();
  check((await page.locator('body:not(.sb-rail):not(.sb-open)').count()) === 1, 'pin fixes the sidebar open');
  // pinned 让位：margin 过渡到展开宽，收敛后必须大于窄栏宽
  await page.waitForFunction(w => parseFloat(getComputedStyle(document.getElementById('page')).marginLeft) > w + 20, railWidth);
  const pinnedLayout = await page.evaluate(() => ({
    pageMargin: parseFloat(getComputedStyle(document.getElementById('page')).marginLeft),
    itemCount: document.querySelectorAll('#wlItems .item').length,
  }));
  check(pinnedLayout.pageMargin > railLayout.railWidth + 20, 'pinned must push the page by the expanded width: ' + JSON.stringify({pinnedLayout, railLayout}));
  await page.locator('#aiRefresh').click();
  const row = page.locator('#aiPanel .ai-row').first();
  await row.waitFor();
  await row.focus();
  await page.keyboard.press('Enter');
  check(await page.locator('#aiPop').evaluate(e => e.contains(document.activeElement)), 'opening AI details must focus its controls');
  check((await page.locator('#aiPop [aria-modal="true"]').count()) === 0, 'partial overlay must not falsely declare the whole page modal');
  await page.keyboard.press('Escape');
  check(await row.evaluate(e => document.activeElement === e), 'closing AI details must restore the trigger focus');

  failChart = true;
  await page.locator('#rulesBtn').click();
  check(await page.locator('#rulesPop').isVisible(), 'rules button must reveal the layers popover');
  await page.locator('#ruleProfile button:not(.active)').click();
  await page.locator('#chartError').waitFor({state: 'visible'});
  check(await page.locator('#cardsDock').isHidden(), 'failed rule switch must discard old evidence pagination');
  failChart = false;
  await page.locator('#chartError button').click();
  await page.locator('#cardsCount').filter({hasText: '1/3'}).waitFor();

  for (const width of [1440, 1280, 1024, 768, 375]) {
    await page.setViewportSize({width, height: 900});
    if (width <= 1100) {
      // Resize already collapses the drawer asynchronously. Escape is an idempotent
      // user action; toggling with '[' can reopen it after that resize handler runs.
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.body.classList.contains('sb-rail'));
    }
    await page.waitForTimeout(250);
    const sizes = await page.evaluate(() => {
      const ids = ['center', 'chartWrap', 'subBar', 'rail'];
      const result = Object.fromEntries(ids.map(id => {
        const e = document.getElementById(id), r = e.getBoundingClientRect();
        return [id, {x:r.x,y:r.y,width:r.width,height:r.height,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth}];
      }));
      const controls = [...document.querySelectorAll('#maSeg button,#subSegBtn,#rulesBtn,#ruleSummary')];
      result.controlsWithin = controls.every(e => { const r=e.getBoundingClientRect(); return r.x >= result.center.x && r.right <= result.center.x + result.center.width + 1; });
      result.pageOverflow = document.documentElement.scrollWidth > innerWidth;
      result.railMode = document.body.classList.contains('sb-rail');
      result.expandVisible = getComputedStyle(document.getElementById('sidebarExpand')).display !== 'none';
      return result;
    });
    measurements.push({width,...sizes});
    check(sizes.center.width >= Math.min(360, width), 'chart squeezed at ' + width + 'px: ' + sizes.center.width);
    check(sizes.controlsWithin, 'controls cross the chart/rail boundary at ' + width + 'px');
    check(!sizes.pageOverflow, 'document overflows at ' + width + 'px');
    // 窄窗窄栏时整条侧栏隐藏、顶栏展开钮接管；宽窗或非窄栏绝不出现
    check(sizes.expandVisible === (width <= 1100 && sizes.railMode),
      'expand button must appear exactly for narrow-window rail at ' + width + 'px: ' + JSON.stringify({expandVisible: sizes.expandVisible, railMode: sizes.railMode}));
    const heights = [];
    for (const indicator of ['macd','kdj','rsi','boll']) {
      await page.locator('#subSegBtn').click();
      await page.locator('#subSeg [data-ind="' + indicator + '"]').click();
      heights.push(await page.locator('#chartWrap').evaluate(e => e.clientHeight));
      const plotWidths = await page.evaluate(() => ['chart','sub'].map(id => document.querySelector('#'+id+' table tr:first-child td:nth-child(2)').getBoundingClientRect().width));
      check(Math.abs(plotWidths[0] - plotWidths[1]) <= 1, 'main/sub time axes misalign for ' + indicator + ' at ' + width + 'px: ' + plotWidths.join('/'));
    }
    check(Math.max(...heights) - Math.min(...heights) <= 1, 'indicator switch changes chart height at ' + width + 'px');
  }
  await page.setViewportSize({width: 1440, height: 900});
  // The wide-screen pinned preference must finish restoring before unpinning.
  // Writing storage during resize can be overwritten by the pending media event.
  await page.waitForFunction(() => !document.body.classList.contains('sb-rail') &&
    !document.body.classList.contains('sb-open'));
  await page.locator('#sidebarPin').click();
  await page.waitForFunction(() => document.body.classList.contains('sb-rail'));

  // reduced-motion 自动守卫：CSS 过渡全部归零，JS 文字编排类整体不挂（删动画而留类会红）
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.reload();
  await page.locator('#cardsCount').filter({hasText: '1/3'}).waitFor();
  await page.hover('#chart');
  await page.waitForFunction(() => document.body.classList.contains('sb-rail'));
  await page.hover('#sidebar .sb-top');
  await page.waitForFunction(() => document.body.classList.contains('sb-open'));
  const calm = await page.evaluate(() => ({
    animIn: document.body.classList.contains('sb-anim-in'),
    sidebarTransition: getComputedStyle(document.getElementById('sidebar')).transitionDuration,
    itemTransition: getComputedStyle(document.querySelector('#wlItems .item')).transitionDuration,
    row2Animation: getComputedStyle(document.querySelector('#wlItems .item .row2')).animationName,
  }));
  check(calm.sidebarTransition === '0s' && calm.itemTransition === '0s',
    'reduced-motion must zero sidebar transitions: ' + JSON.stringify(calm));
  check(!calm.animIn, 'reduced-motion must skip the enter animation class');
  await page.emulateMedia({reducedMotion: null});
  await page.reload();
  await page.locator('#cardsCount').filter({hasText: '1/3'}).waitFor();

  await page.setViewportSize({width: 1440, height: 900});
  await page.hover('#chart');
  await page.waitForFunction(() => document.body.classList.contains('sb-rail'));
  await page.hover('#sidebar .sb-top');
  await page.waitForFunction(() => document.body.classList.contains('sb-open'));
  for (let remaining = 2; remaining > 0; remaining--) {
    await page.locator('#wlItems .item').first().hover();
    await page.locator('#wlItems .item .del').first().click();
    await page.waitForFunction(n => document.querySelectorAll('#wlItems .item').length === n, remaining - 1);
  }
  // 移出自选退回搜索查看：当前图表保持不动，页头出现「加入自选」
  check((await page.evaluate(() => window.__chartTest.read().times.length)) === 80, 'removing the current stock keeps viewing it');
  check(await page.locator('#trackBtn').isVisible(), 'an unwatched current code offers 加入自选');
  check(await page.locator('#aiRefresh').isEnabled(), 'the viewed code keeps the AI action');
  const views0 = viewPosts.length, mut0 = mutations.length;
  // 搜索选中 = 查看：打开图表并记一次查看，不写自选
  await page.locator('#wlForm input').fill('示例');
  await page.locator('.wl-drop .cand').first().click();
  await page.locator('#cardsCount').filter({hasText:'1/3'}).waitFor();
  await page.waitForFunction(n => document.querySelectorAll('#wlItems .item').length === n, 0);
  check(viewPosts.length === views0 + 1 && viewPosts[viewPosts.length - 1].code === 'sh000001', 'search pick records one view: ' + JSON.stringify(viewPosts));
  check(mutations.length === mut0, 'search pick must not write the watchlist: ' + JSON.stringify(mutations));
  check(await page.locator('#trackBtn').isVisible(), 'viewed code is still unwatched');
  // 候选行的「加自选」是显式加入：写自选、不再记查看（打开后浮动侧栏已收回，先悬停展开）
  const expandSidebar = async () => {
    await page.mouse.move(900, 450);                 // 选中后收回会抑制悬停：先离开侧栏开启新一轮悬停
    await page.hover('#sidebar .sb-top');
    await page.waitForFunction(() => document.body.classList.contains('sb-open'));
  };
  await expandSidebar();
  await page.locator('#wlForm input').fill('示例');
  await page.locator('.wl-drop .cand .c-add').first().click();
  await page.waitForFunction(() => document.querySelectorAll('#wlItems .item').length === 1);
  check(mutations.length === mut0 + 1 && mutations[mutations.length - 1].method === 'POST', 'explicit add writes the watchlist once');
  check(viewPosts.length === views0 + 1, 'explicit add does not record a view');
  check(await page.locator('#trackBtn').isHidden(), 'watched code hides 加入自选');
  // 侧栏「最近查看」：列出查看记录，已自选按自选列表标注；切换持久
  await page.locator('#wlTabRecent').click();
  await page.waitForFunction(() => document.querySelector('#wlItems .item .tag-watched'));
  check(await page.locator('#wlTabRecent').getAttribute('aria-selected') === 'true', 'recent tab selected');
  check((await page.evaluate(() => localStorage.getItem('chanapp-side-tab'))) === 'recent', 'side tab persists');
  check((await page.locator('#wlItems .item[data-code="sh000001"] .add').count()) === 0, 'watched recent row has no add control');
  check((await page.locator('#wlItems .item[data-code="sz001309"] .add').count()) === 1, 'removed-from-watchlist recent row offers add again');
  await page.locator('#wlTabWatch').click();
  await page.waitForFunction(() => document.querySelectorAll('#wlItems .item').length === 1);
  // 手动重拉：带 refetch=1 且不带条件头
  const charts0 = chartRequests.length;
  await page.locator('#refetchBtn').click();
  await page.waitForFunction(() => document.getElementById('status').textContent.indexOf('重拉中') < 0);
  const refetched = chartRequests.slice(charts0);
  check(refetched.length === 1 && refetched[0].refetch === '1' && !refetched[0].conditional, 'refetch sends one unconditional refetch=1 request: ' + JSON.stringify(refetched));
  check((await page.locator('#status').textContent()).indexOf('已重拉当前窗口') >= 0, 'successful refetch says so');
  // 服务端报部分完成：页面不能说「已重拉」
  refetchStatus = 'partial';
  await page.locator('#refetchBtn').click();
  await page.waitForFunction(() => document.getElementById('status').textContent.indexOf('重拉中') < 0);
  const partialNote = await page.locator('#status').textContent();
  check(partialNote.indexOf('部分') >= 0 && partialNote.indexOf('已重拉当前窗口') < 0, 'partial refetch is reported honestly: ' + partialNote);
  // 服务端报忙（同一标的已有重拉或追赶在途，本次没有重取）：提示稍后再试
  refetchStatus = 'busy';
  await page.locator('#refetchBtn').click();
  await page.waitForFunction(() => document.getElementById('status').textContent.indexOf('重拉中') < 0);
  const busyNote = await page.locator('#status').textContent();
  check(busyNote.indexOf('稍后') >= 0 && busyNote.indexOf('已重拉当前窗口') < 0, 'busy refetch asks to retry later: ' + busyNote);
  refetchStatus = 'ok';
  // 冷窗口的忙：没有可服务快照时服务端回 503，页面说明稍后再试，重试恢复
  busyChart = true;
  await page.locator('#refetchBtn').click();
  await page.locator('#chartError').waitFor({state: 'visible'});
  const busyError = await page.locator('#chartError').textContent();
  check(busyError.indexOf('稍后') >= 0, 'cold-window busy explains to retry later: ' + busyError);
  busyChart = false;
  await page.locator('#chartError button').click();
  await page.locator('#chartError').waitFor({state: 'hidden'});
  // 普通加载遇到 503（同一代码的历史规划或缓存刷新在途、没有快照）：不报错，提示自动重试，忙结束后自行恢复
  const prevFreq = await page.$eval('#freqTabs .active', b => b.dataset.freq);
  const otherFreq = prevFreq === 'm60' ? 'm30' : 'm60';
  busyChart = true;
  await page.locator('#freqTabs [data-freq="' + otherFreq + '"]').click();
  await page.waitForFunction(() => document.getElementById('status').textContent.indexOf('自动重试') >= 0);
  check(await page.locator('#chartError').isHidden(), 'busy first open retries instead of showing an error');
  busyChart = false;
  await page.waitForFunction(() => document.getElementById('status').textContent.indexOf('自动重试') < 0, null, {timeout: 15000});
  await waitForChart();
  check(await page.locator('#chartError').isHidden(), 'busy first open recovers by itself');
  await page.locator('#freqTabs [data-freq="' + prevFreq + '"]').click();
  await waitForChart();
  // 第三阶段：只剩四个周期；状态栏按 data_status；旧 m5 链接归一到 30 分；F10 挂起时非自选报价卡先显示
  const freqs = await page.$$eval('#freqTabs [data-freq]', bs => bs.map(b => b.dataset.freq).sort());
  check(JSON.stringify(freqs) === JSON.stringify(['day', 'm30', 'm60', 'week']), 'only 30/60/day/week remain: ' + freqs);
  await waitForChart();
  await page.waitForFunction(() => document.getElementById('status').textContent.indexOf('已收盘 · 待定稿 01-05 15:01') >= 0,
    null, {timeout: 5000}).catch(() => {});
  check((await page.locator('#status').textContent()).indexOf('已收盘 · 待定稿 01-05 15:01') >= 0,
    'status bar shows awaiting-final with the accepted time: ' + await page.locator('#status').textContent());
  holdF10 = true;
  const charts1 = chartRequests.length;
  await page.goto(origin + '/?code=sz000002&freq=m5');
  await page.waitForFunction(() => document.getElementById('quoteSec').style.display !== 'none'
    && document.getElementById('f10Px').textContent === '9.87');
  check(!!releaseF10 && await page.locator('#f10Sec').isHidden(), 'quote card renders while F10 is still pending');
  check(quoteRequests.indexOf('sz000002') < 0, 'A 股代码不再请求单代码报价（内嵌于 chart 应答）: ' + JSON.stringify(quoteRequests));
  await waitForChart();
  check(await page.locator('#freqTabs [data-freq="m30"]').evaluate(b => b.classList.contains('active')), 'old m5 link lands on 30分');
  const newCharts = chartRequests.slice(charts1).filter(r => r.code === 'sz000002');
  check(newCharts.length > 0 && newCharts.every(r => r.freq === 'm30'), 'old m5 link requests 30分 only: ' + JSON.stringify(newCharts));
  holdF10 = false;
  if (releaseF10) { releaseF10(); releaseF10 = null; }
  await page.waitForFunction(() => document.getElementById('f10Sec').style.display !== 'none');
  await page.evaluate(() => localStorage.removeItem('chanapp-side-tab'));
  if (failures.length) throw new Error(JSON.stringify({failures,measurements}));
  return {passed: true, hoverMeasurements, measurements, analyses, mutations, viewPosts};
}
