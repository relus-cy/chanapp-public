// Real API acceptance. One command: bash chanapp/scripts/test_browser.sh (starts
// tests/support/demo_offline_server.py on a free port, runs this file, cleans up).
// No business API is intercepted or mocked; app.js only gains a read-only chart probe.
// Run separately from the fixture UI regression.
async page => {
  await page.unrouteAll({behavior: 'wait'});
  await page.setViewportSize({width: 1440, height: 900});
  const origin = await page.evaluate(() => location.origin);
  // Read-only probe into the real closures: visible time ranges and bar count, no state writes.
  await page.route('**/app.js', async route => {
    const response = await route.fetch();
    const source = await response.text();
    if (!/\}\)\(\);\s*$/.test(source)) throw new Error('app entry point changed');
    const probe = 'window.__chartProbe = function () { if (!charts) return null; var m = charts.main.timeScale(), s = charts.macdChart.timeScale();' +
      ' return {bars: klineData.length, main: m.getVisibleRange(), sub: s.getVisibleRange(),' +
      ' mainLogical: m.getVisibleLogicalRange(), subLogical: s.getVisibleLogicalRange(), mainWidth: m.width(), subWidth: s.width()}; };';
    await route.fulfill({response, body: source.replace(/\}\)\(\);\s*$/, probe + '})();')});
  });
  const probe = () => page.evaluate(() => window.__chartProbe && window.__chartProbe());
  // Install before navigation so the application's own intervals use this clock.
  await page.clock.install();
  const requests = [], failures = [], charts = [];
  const observe = request => requests.push(request.url());
  page.on('request', observe);
  const check = (ok, message) => { if (!ok) failures.push(message); };
  const waitChart = async (action, expected = "") => {
    const response = page.waitForResponse(r => r.url().includes('/api/chart?') && !r.url().includes('before=') && r.url().includes(expected));
    await action();
    const r = await response;
    const body = await r.json();
    charts.push({url: r.url(), status: r.status(), bars: (body.kline || []).length});
    check(r.ok() && body.kline.length > 0, 'sample chart must contain bars: ' + r.status());
    if (r.status() === 502) {
      await page.locator('#chartError').waitFor({state:'visible'});
      check((await page.locator('#chartError').innerText()).trim().length > 0, 'missing sample must explain failure');
    }
    await page.waitForFunction(() => !document.getElementById('status').textContent.includes('加载中'));
  };
  await page.goto(origin + '/?code=sz300308&freq=day');
  await page.locator('#f10Sec').waitFor({state:'visible'});
  check(await page.locator('#periodDialog').isHidden(), 'fresh demo must not open period dialog');
  check(await page.locator('#freqTabs button').count() === 4, 'fresh demo must preset all four periods');
  await page.waitForFunction(() => document.getElementById('status').textContent.includes('历史'));
  await page.waitForFunction(() => document.body.innerText.includes('历史样本仅 243 根'));
  check((await page.locator('body').innerText()).includes('243'), 'short day history must disclose 243 bars');
  check((await page.locator('#f10Sec').innerText()).includes('样本未提供基本资料'), 'F10 must explain missing sample');
  // The quote request is independent of the chart and may land after it; wait for it before reading.
  await page.locator('#quoteSec').filter({hasText: '历史'}).waitFor({timeout: 5000}).catch(() => {});
  check((await page.locator('#quoteSec').innerText()).includes('历史'), 'quote must be marked historical');
  for (const freq of ['week', 'm30', 'm60', 'day']) {
    await waitChart(() => page.locator('#freqTabs [data-freq="' + freq + '"]').click(), 'freq=' + freq + '&');
  }
  // History paging: drag the main chart right like a user until older bars are prepended;
  // the subchart must then show exactly the same time span as the main chart.
  await waitChart(() => page.locator('#freqTabs [data-freq="m60"]').click(), 'freq=m60&');
  {
    const box = await page.locator('#chart').boundingBox();
    const first = await probe();
    for (let i = 0; i < 12 && (await probe()).bars === first.bars; i++) {
      await page.mouse.move(box.x + 60, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width - 80, box.y + box.height / 2, {steps: 12});
      await page.mouse.up();
      await page.waitForTimeout(400);
    }
    await page.waitForFunction(() => !document.getElementById('status').textContent.includes('加载历史'));
    await page.waitForTimeout(300);
    const paged = await probe();
    check(paged.bars > first.bars, 'm60 drag must prepend history: ' + first.bars + ' -> ' + paged.bars);
    check(JSON.stringify(paged.main) === JSON.stringify(paged.sub),
      'after paging subchart must show the main chart time span: ' + JSON.stringify({main: paged.main, sub: paged.sub}));
  }
  await waitChart(() => page.locator('#freqTabs [data-freq="day"]').click(), 'freq=day&');
  await waitChart(() => page.locator('#adjustTabs [data-adjust="raw"]').click());
  await waitChart(() => page.locator('#adjustTabs [data-adjust="qfq"]').click());
  await page.locator('#rulesBtn').click();
  await waitChart(() => page.locator('#ruleProfile button:not(.active)').click());
  await page.keyboard.press('Escape');
  await waitChart(() => page.locator('#refetchBtn').click());
  await page.locator('#aiRefresh').click();
  await page.locator('#aiPanel').filter({hasText:'demo 模式不调用 AI'}).waitFor();
  await page.evaluate(() => localStorage.setItem('chanapp-sidebar', 'pinned'));
  await page.reload();
  if (await page.locator('body.sb-rail').count()) await page.locator('#sidebarPin').click();
  const chartReady = () => page.waitForFunction(() => {
    const p = window.__chartProbe && window.__chartProbe();
    return p && p.bars > 0 && document.querySelectorAll('.res-chip').length > 0;
  });
  const isPinned = () => page.waitForFunction(() => !document.body.classList.contains('sb-rail') &&
    !document.body.classList.contains('sb-open'), null, {timeout: 3000}).then(() => true, () => false);
  const sidebarOverlap = () => page.evaluate(() => {
    const s = document.getElementById('sidebar'), a = s.getBoundingClientRect();
    const b = document.getElementById('chart').getBoundingClientRect();
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return getComputedStyle(s).visibility !== 'hidden' && w > 0 && h > 0 ? Math.round(w) + 'x' + Math.round(h) : '';
  });
  // Narrow windows: a pin saved on a wide screen must neither open the drawer over the chart nor be overwritten.
  for (const size of [{width: 820, height: 900}, {width: 390, height: 844}]) {
    await page.setViewportSize(size);
    await page.reload();
    await chartReady();
    await page.waitForTimeout(600);  // drawer slide and visibility transition
    const overlap = await sidebarOverlap();
    check(overlap === '', size.width + 'px: saved pinned sidebar must not cover the chart, overlap ' + overlap);
    check(await page.evaluate(() => localStorage.getItem('chanapp-sidebar')) === 'pinned',
      size.width + 'px: the wide-screen pin preference must be kept');
  }
  await page.setViewportSize({width: 1440, height: 900});
  check(await isPinned() && await page.locator('#sidebarPin[aria-pressed="true"]').count() === 1,
    'back at 1440px the sidebar must be pinned again');
  await page.waitForTimeout(400);
  check(await sidebarOverlap() === '', '1440px pinned sidebar must sit beside the chart');
  // Resonance chips: when space runs out the time goes first, then the label ellipsizes; no glyph is cut.
  const chipIssues = () => page.evaluate(() => {
    const issues = [];
    const chips = [...document.querySelectorAll('.res-chip')];
    const shown = e => { const cs = getComputedStyle(e); return cs.display !== 'none' && cs.visibility !== 'hidden'; };
    const inside = (r, c) => r.left >= c.left - .5 && r.right <= c.right + .5 && r.top >= c.top - .5 && r.bottom <= c.bottom + .5;
    const outside = (r, c) => r.left >= c.right - .5 || r.right <= c.left + .5 || r.top >= c.bottom - .5 || r.bottom <= c.top + .5;
    // Visible area of an element inside the chip: intersection of the padding boxes of its clipping ancestors.
    const clipOf = (e, chip) => {
      let c = {left: -Infinity, top: -Infinity, right: Infinity, bottom: Infinity};
      for (let a = e.parentElement; a; a = a.parentElement) {
        const cs = getComputedStyle(a);
        if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
          const r = a.getBoundingClientRect(), left = r.left + a.clientLeft, top = r.top + a.clientTop;
          c = {left: Math.max(c.left, left), top: Math.max(c.top, top),
            right: Math.min(c.right, left + a.clientWidth), bottom: Math.min(c.bottom, top + a.clientHeight)};
        }
        if (a === chip) break;
      }
      return c;
    };
    for (const chip of chips) {
      const name = chip.dataset.freq;
      for (const e of [chip, ...chip.querySelectorAll('*')]) {
        if (!shown(e) || !e.textContent.trim()) continue;
        const cs = getComputedStyle(e);
        if (cs.display === 'inline') continue;  // inline boxes have no scrollWidth; their block ancestor is measured
        if (e !== chip && outside(e.getBoundingClientRect(), clipOf(e, chip))) continue;  // wholly clipped away
        if (e.scrollWidth > e.clientWidth && cs.textOverflow !== 'ellipsis') {
          issues.push(name + ' ' + (e.className || e.tagName) + ' clips "' + e.textContent.trim() + '" ' + e.scrollWidth + '>' + e.clientWidth);
        }
      }
      const times = [...chip.querySelectorAll('.res-time')].filter(t => shown(t) && !outside(t.getBoundingClientRect(), clipOf(t, chip)));
      for (const t of times) {
        if (!inside(t.getBoundingClientRect(), clipOf(t, chip))) issues.push(name + ' time partly visible "' + t.textContent + '"');
      }
      if (times.length && [...chip.querySelectorAll('.res-sig')].some(s => s.scrollWidth > s.clientWidth)) {
        issues.push(name + ' label ellipsized while its time is still shown');
      }
    }
    return {chips: chips.length, signals: chips.filter(c => c.classList.contains('has-signal')).length, issues};
  });
  for (const width of [1440, 1100]) {
    await page.setViewportSize({width, height: 900});
    if (width === 1440) check(await isPinned(), '1440px chip check runs with the sidebar pinned');
    await page.waitForTimeout(400);
    const chips = await chipIssues();
    check(chips.chips === 3 && chips.signals > 0, width + 'px: day view must show three resonance chips with signals: ' + JSON.stringify(chips));
    check(chips.issues.length === 0, width + 'px resonance chips: ' + chips.issues.join('; '));
  }
  // Resizing keeps bar density (bars per pixel) and keeps both charts on one logical range; the
  // sidebar and layout transitions must not freeze the bar count at a transient narrow width.
  await page.setViewportSize({width: 1440, height: 900});
  await isPinned();
  await page.waitForTimeout(600);
  {
    const density = p => (p.mainLogical.to - p.mainLogical.from) / p.mainWidth;
    const base = await probe();
    for (const size of [{width: 820, height: 900}, {width: 390, height: 844}, {width: 1440, height: 900}]) {
      await page.setViewportSize(size);
      await page.waitForTimeout(800);
      const now = await probe();
      const drift = Math.abs(density(now) / density(base) - 1);
      check(drift < 0.1, size.width + 'px resize changed bar density by ' + Math.round(drift * 100) + '%: ' +
        JSON.stringify({before: base.mainLogical, beforeWidth: base.mainWidth, after: now.mainLogical, afterWidth: now.mainWidth}));
      check(Math.abs(now.mainLogical.from - now.subLogical.from) < .5 && Math.abs(now.mainLogical.to - now.subLogical.to) < .5,
        size.width + 'px resize must keep subchart on the main logical range: ' + JSON.stringify({main: now.mainLogical, sub: now.subLogical}));
    }
  }
  await isPinned();
  await page.locator('#wlForm input').fill('上证');
  await page.locator('.wl-drop .cand').first().click();
  await page.locator('#trackBtn').waitFor({state:'visible'});
  await page.locator('#trackBtn').click();
  await page.locator('#wlItems .item[data-code="sh000001"]').waitFor();
  for (const freq of ['m30', 'm60', 'week', 'day']) {
    await waitChart(() => page.locator('#freqTabs [data-freq="' + freq + '"]').click(), 'freq=' + freq + '&');
  }
  await page.locator('#wlItems .item[data-code="sh000001"]').hover();
  await page.locator('#wlItems .item[data-code="sh000001"] .del').click();
  await page.locator('#wlItems .item[data-code="sh000001"]').waitFor({state:'detached'});
  await page.locator('#wlTabRecent').click();
  await page.locator('#wlItems .item[data-code="sh000001"]').waitFor();
  await page.locator('#wlTabWatch').click();
  for (const code of ['sz300308', 'sh000001']) {
    for (const freq of ['day', 'week', 'm30', 'm60']) {
      check(charts.some(c => c.url.includes(code) && c.url.includes('freq=' + freq + '&') && c.status === 200 && c.bars > 0), code + ' ' + freq + ' must render');
    }
  }
  await page.waitForTimeout(500);
  const before = requests.length;
  // Virtual passage of time runs the actual timer callbacks without a minute-long sleep.
  await page.evaluate(() => {
    window.__offlineTimerTicks = 0;
    window.__offlineTimer = setInterval(() => window.__offlineTimerTicks++, 1000);
  });
  await page.clock.fastForward(65000);
  await page.waitForTimeout(500);
  check(await page.evaluate(() => window.__offlineTimerTicks > 0), 'virtual time must execute registered interval callbacks');
  await page.evaluate(() => clearInterval(window.__offlineTimer));
  check(requests.length === before, 'demo must not poll after 65 seconds: ' + requests.slice(before));
  const external = requests.filter(url => !url.startsWith(origin + '/'));
  check(external.length === 0, 'browser requested external resources: ' + external);
  const text = await page.locator('body').innerText();
  check(!/抓取[:：]\s*null/.test(text), 'historical footer must not show null fetch time');
  check(!text.includes('加入自选以补完整历史'), 'demo pagination must not promise online backfill');
  check(await page.locator('#flowSec').isHidden(), 'missing sample flow must stay hidden');
  await page.screenshot({path:'tmp/demo-offline-browser/demo.png', fullPage:true});
  page.off('request', observe);
  const result = {passed:failures.length === 0, failures, charts, requests:requests.length, external};
  if (failures.length) throw new Error(JSON.stringify(result));
  return result;
}
