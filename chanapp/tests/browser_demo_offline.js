// Real API acceptance: start tests/support/demo_offline_server.py, then:
// playwright-cli run-code --filename=tests/browser_demo_offline.js
// No business API is intercepted or mocked. Run separately from the fixture UI regression.
async page => {
  await page.unrouteAll({behavior: 'wait'});
  await page.setViewportSize({width: 1440, height: 900});
  const origin = await page.evaluate(() => location.origin);
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
  check((await page.locator('#quoteSec').innerText()).includes('历史'), 'quote must be marked historical');
  for (const freq of ['week', 'm30', 'm60', 'day']) {
    await waitChart(() => page.locator('#freqTabs [data-freq="' + freq + '"]').click(), 'freq=' + freq + '&');
  }
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
