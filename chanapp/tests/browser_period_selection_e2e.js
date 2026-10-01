/* Real browser + real business API; no page.route or API response mocks.
 * Run from the package parent:
 *   PYTHON=/path/to/venv/bin/python PLAYWRIGHT_MODULE=/path/to/playwright node chanapp/tests/browser_period_selection_e2e.js
 * Failure modes specified BEFORE implementation:
 * - New instance misses first-use dialog; existing instance incorrectly shows one.
 * - Capability change is missed, repeated after acknowledgement, or loses selection.
 * - Unsupported period can be selected or has no market-specific explanation.
 * - Deselected periods still cause chart requests or appear in controls.
 * - All minute periods disabled but collector keeps issuing upstream requests.
 * - Re-enabling fails to fill the initial window and tracked history.
 * - Other tabs or a restarted server leave an old minute chart/selection visible.
 * - Unchanged preferences cause duplicate chart refreshes or a 400 retry loop.
 */
'use strict';
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'period-selection-'));
const pkgParent = path.resolve(__dirname, '../..');
const port = 18947;
const origin = `http://127.0.0.1:${port}`;
let server, browser;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function start(dir, freq = 'm15') {
  server = spawn(process.env.PYTHON || 'python3', ['-m', 'chanapp.tests.period_selection_server', dir, String(port), freq],
    {cwd: pkgParent, env: process.env, stdio: ['ignore', 'pipe', 'pipe']});
  let logs = '';
  server.stdout.on('data', chunk => { logs += chunk; });
  server.stderr.on('data', chunk => { logs += chunk; });
  for (let n = 0; n < 200; n++) {
    if (server.exitCode != null) throw new Error(`server exited: ${logs}`);
    try { if ((await fetch(origin + '/api/session')).ok) return; } catch (_) {}
    await pause(100);
  }
  throw new Error(`server timeout: ${logs}`);
}
async function stop() {
  const child = server;
  server = null;
  if (!child || child.exitCode != null || child.signalCode != null) return;
  await new Promise(resolve => { child.once('exit', resolve); child.kill('SIGTERM'); });
}
async function json(endpoint, method = 'GET') {
  const response = await fetch(origin + endpoint, {method});
  assert.equal(response.status, 200, await response.clone().text());
  return response.json();
}
async function select(page, selected) {
  if (!await page.locator('#periodDialog').isVisible()) await page.locator('#periodBtn').click();
  for (const freq of ['m30', 'm60', 'day', 'week']) {
    const input = page.locator(`#periodOptions input[value="${freq}"]`);
    if (await input.isEnabled()) await input.setChecked(selected.includes(freq));
  }
  await page.locator('#periodSave').click();
  await page.locator('#periodDialog').waitFor({state: 'hidden'});
}
async function analyzeUI(page, freqs, label) {
  await page.waitForFunction(expected => JSON.stringify([...document.querySelectorAll('.res-chip')].map(x => x.dataset.freq)) === JSON.stringify(expected), freqs);
  const response = page.waitForResponse(r => r.url().includes('/api/analysis?'));
  await page.locator('#aiRefresh').click();
  const result = await response;
  assert.equal(result.status(), 200, await result.text());
  const body = await result.json();
  assert.deepEqual(body.freqs, freqs);
  assert.equal(body.current_state, '本次：' + freqs.join('/'));
  await page.locator('#aiPanel').filter({hasText: '分析级别 · ' + label}).waitFor();
  await page.locator('#aiRefresh').waitFor({state: 'visible'});
  await page.waitForFunction(() => !document.getElementById('aiRefresh').disabled);
  return body;
}
(async () => {
  try {
    const directory = path.join(root, 'new');
    await start(directory);
    browser = await chromium.launch({headless: true, channel: 'chrome'});
    const page = await browser.newPage();
    await page.clock.install();
    const chartRequests = [], mainChartRequests = [], periodReads = [];
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.pathname === '/api/chart') chartRequests.push(url.searchParams.get('freq'));
      if (url.pathname === '/api/chart' && !url.searchParams.get('before')) mainChartRequests.push(url.searchParams.get('freq'));
      if (url.pathname === '/api/periods' && request.method() === 'GET') periodReads.push(request.url());
    });
    await page.goto(origin);
    await page.getByRole('dialog').waitFor({state: 'visible', timeout: 10000});
    assert.match(await page.getByRole('dialog').innerText(), /周期/);
    assert.equal(await page.locator('#periodOptions input:checked').count(), 4);
    console.log('PASS first-use dialog, four defaults');
    await select(page, []);
    await pause(300);
    assert.equal(chartRequests.length, 0, 'first-use none must never request a chart');
    assert.equal(await page.locator('#freqTabs button:visible').count(), 0);
    await json('/__test/history', 'POST');
    const disabled = await json('/__test/evidence');
    assert.equal(disabled.calls.filter(c => c[0].startsWith('m')).length, 0, 'none must stop upstream minutes');
    await page.reload();
    await pause(500);
    assert.equal(await page.getByRole('dialog').isVisible(), false);
    assert.equal(chartRequests.length, 0, 'reload must retain zero chart requests');
    console.log('PASS none persists, no chart requests, history tick has zero minute requests');

    const chartDone = page.waitForResponse(r => r.url().includes('/api/chart?') && r.status() === 200, {timeout: 90000});
    await select(page, ['m30']);
    const chart = await (await chartDone).json();
    assert.ok(chart.kline.length >= 500, 're-enabled period must fill the analysis window with real chart bars');
    const windowEvidence = await json('/__test/evidence');
    const initial = windowEvidence.minute_facts.find(r => r.code === 'sh600036');
    assert.ok(initial && initial.count > 0, 're-enable must fill minute window');
    for (let n = 0; n < 3; n++) await json('/__test/history', 'POST');
    const historical = (await json('/__test/evidence')).minute_facts.find(r => r.code === 'sh600036');
    assert.ok(historical.oldest < initial.oldest, `history must extend past window: ${JSON.stringify({initial, historical})}`);
    console.log('PASS re-enable fills initial window and earlier tracked history', JSON.stringify({initial, historical}));

    // 历史回填推进了令牌，先像用户刷新页面一样取得新快照。
    await page.reload();
    // 日线即使不勾选展示仍参与分析；点击该摘要不可请求隐藏图表。
    await analyzeUI(page, ['day', 'm30'], '日线 / 30分');
    assert.equal(await page.locator('#freqTabs [data-freq="day"]').isVisible(), false);
    const hiddenDayMark = chartRequests.length;
    await page.locator('.res-chip[data-freq="day"]').click();
    await pause(100);
    assert.equal(chartRequests.length, hiddenDayMark);
    for (const [selected, label] of [
      [['day'], '日线'], [['day', 'm60'], '日线 / 60分'],
      [['day', 'm30'], '日线 / 30分'], [['day', 'm60', 'm30'], '日线 / 60分 / 30分'],
    ]) {
      await select(page, selected);
      await analyzeUI(page, selected, label);
    }
    await select(page, ['m30']);
    await page.waitForFunction(() => document.querySelector('#freqTabs [data-freq="m30"].active'));
    console.log('PASS four AI combinations, explicit labels, hidden day remains analysis-only');

    // Two real pages share the instance. Foreground sync is required even outside trading hours.
    const other = await browser.newPage();
    await other.goto(origin);
    await other.locator('#freqTabs button[data-freq="m30"]').waitFor({state: 'visible'});
    await select(other, ['day']);
    const foregroundMark = chartRequests.length;
    await page.bringToFront();
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.locator('#freqTabs button[data-freq="m30"]').waitFor({state: 'hidden', timeout: 5000});
    assert.ok(chartRequests.slice(foregroundMark).every(freq => freq === 'day'), 'foreground must sync before requesting charts');
    console.log('PASS second-page selection synchronizes on foreground without stale minute requests');

    // The existing 60s timer also synchronizes preferences in a visible, closed-market page.
    await select(other, ['m30', 'day']);
    const synchronized = page.waitForResponse(r => r.url().includes('/api/chart?') && r.status() === 200);
    await page.clock.fastForward(60000);
    await synchronized;
    await page.locator('#freqTabs button[data-freq="m30"]').waitFor({state: 'visible', timeout: 5000});
    const unchangedMark = mainChartRequests.length, readMark = periodReads.length;
    await page.clock.fastForward(60000);
    await pause(300);
    assert.equal(periodReads.length, readMark + 1, 'existing timer performs one preference read');
    assert.ok(mainChartRequests.length - unchangedMark <= 1, 'unchanged preferences allow only the existing market/finalization refresh, never a second load');
    await page.clock.resume();
    console.log('PASS 60s synchronization works outside trading hours without duplicate loads');

    // A manual request can race another page's save; recover from the real 400 response.
    const minuteDone = page.waitForResponse(r => r.url().includes('/api/chart?') && r.status() === 200);
    await page.locator('#freqTabs button[data-freq="m30"]').click();
    await minuteDone;
    await select(other, ['day']);
    const rejected = page.waitForResponse(r => r.url().includes('/api/chart?') && r.status() === 400);
    await page.locator('#refetchBtn').click();
    await rejected;
    await page.locator('#freqTabs button[data-freq="m30"]').waitFor({state: 'hidden', timeout: 5000});
    assert.equal(await page.locator('#center.period-empty').count() > 0 ||
      await page.locator('#freqTabs button[data-freq="day"].active').count() > 0, true);
    await page.locator('#periodBtn').click();
    await page.locator('#periodOptions input[value="m60"]').check();
    await select(other, ['m30', 'day']);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(() => document.getElementById('periodError').textContent.includes('重新确认'), null, {timeout: 5000});
    assert.equal(await page.locator('#periodOptions input[value="m30"]').isChecked(), true);
    assert.equal(await page.locator('#periodOptions input[value="m60"]').isChecked(), false, 'editing form must be rebuilt with the changed revision');
    await select(other, ['day']);
    await page.locator('#periodOptions input[value="m60"]').check();
    const conflict = page.waitForResponse(r => r.url().endsWith('/api/periods') && r.status() === 409);
    await page.locator('#periodSave').click();
    await conflict;
    await page.waitForFunction(() => document.getElementById('periodError').textContent.includes('重新确认'), null, {timeout: 5000});
    assert.deepEqual([...(await json('/api/periods')).selected].sort(), ['day']);
    assert.equal(await page.locator('#periodOptions input[value="m60"]').isChecked(), false);
    await page.locator('#periodCancel').click();
    await other.close();
    console.log('PASS conflicting save returns 409, preserves the other page selection and reloads the dialog');
    console.log('PASS stale manual chart 400 synchronizes preferences and removes the old minute view');

    await select(page, ['day']);
    await pause(500);
    const mark = chartRequests.length;
    await page.reload();
    await pause(1000);
    assert.ok(chartRequests.slice(mark).every(freq => freq === 'day'));
    assert.equal(await page.locator('#freqTabs button[data-freq="m30"]').isVisible(), false);
    const before = (await json('/__test/evidence')).calls.length;
    await json('/__test/history', 'POST');
    assert.equal((await json('/__test/evidence')).calls.slice(before).filter(c => c[0].startsWith('m')).length, 0);
    console.log('PASS deselected minute hidden, zero minute chart/upstream requests');

    await select(page, ['m30', 'day']);
    await pause(500);
    await stop();
    const prefsFailure = page.waitForEvent('requestfailed', request => request.url().endsWith('/api/periods'));
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await prefsFailure;
    await page.waitForFunction(() => [...document.querySelectorAll('#freqTabs button')].every(button => button.hidden), null, {timeout: 5000});
    const offlineMark = chartRequests.length;
    await page.locator('#refetchBtn').click();
    await pause(100);
    assert.equal(chartRequests.length, offlineMark, 'failed preference synchronization cannot reuse stale selection');
    await start(directory, 'm60');
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.getByRole('dialog').waitFor({state: 'visible'});
    assert.match(await page.locator('#periodTitle').innerText(), /变化/);
    const unsupported = page.locator('#periodOptions input[value="m30"]');
    assert.equal(await unsupported.isDisabled(), true);
    assert.equal(await unsupported.isChecked(), true, 'capability loss preserves selection');
    assert.match(await unsupported.locator('..').innerText(), /A 股.*m60.*30/);
    assert.match(await page.locator('#periodChanges').innerText(), /30/);
    await select(page, ['day', 'm60']);
    await analyzeUI(page, ['day', 'm60'], '日线 / 60分');
    await select(page, ['day']);
    await page.reload();
    await pause(500);
    assert.equal(await page.getByRole('dialog').isVisible(), false, 'acknowledged capability change only prompts once');
    assert.ok((await json('/api/periods')).selected.includes('m30'));
    await page.goto('about:blank');
    await stop();
    await start(directory, 'm60');
    await page.goto(origin);
    await pause(500);
    assert.equal(await page.getByRole('dialog').isVisible(), false, 'same capabilities after restart must not prompt again');
    console.log('PASS capability change prompts once across reload/restart, disabled reason and saved selection retained');
    await page.goto('about:blank');
    await stop();
    await start(directory, 'm15');
    await page.goto(origin);
    await page.getByRole('dialog').waitFor({state: 'visible'});
    assert.equal(await page.locator('#periodOptions input[value="m30"]').isEnabled(), true);
    assert.equal(await page.locator('#periodOptions input[value="m30"]').isChecked(), true);
    await page.locator('#periodSave').click();
    await page.locator('#periodDialog').waitFor({state: 'hidden'});
    assert.equal(await page.locator('#freqTabs button[data-freq="m30"]').isVisible(), true);
    console.log('PASS restored capability automatically restores selected period');

    await page.goto('about:blank');
    await stop();
    await start(directory, 'none');
    await page.goto(origin);
    await page.getByRole('dialog').waitFor({state: 'visible'});
    assert.equal(await page.locator('#periodOptions input[value="m30"]').isDisabled(), true);
    assert.equal(await page.locator('#periodOptions input[value="m60"]').isDisabled(), true);
    await page.locator('#periodSave').click();
    await page.locator('#periodDialog').waitFor({state: 'hidden'});
    await json('/__test/history', 'POST');
    assert.equal((await json('/__test/evidence')).calls.filter(c => c[0].startsWith('m')).length, 0);
    await analyzeUI(page, ['day'], '日线');
    assert.equal((await json('/api/periods')).catalog.length, 4);
    console.log('PASS absent minute capability stops minute collection despite retained selection');
    await page.goto('about:blank');
    await stop();
    const upgraded = path.join(root, 'upgrade');
    fs.mkdirSync(path.join(upgraded, 'state'), {recursive: true});
    fs.writeFileSync(path.join(upgraded, 'state', 'watchlist.json'), '[]');
    await start(upgraded);
    await page.goto(origin);
    await pause(1000);
    assert.equal(await page.getByRole('dialog').isVisible(), false);
    const migrated = await json('/api/periods');
    assert.equal(migrated.notice, null);
    assert.deepEqual([...migrated.selected].sort(), ['day', 'm30', 'm60', 'week']);
    console.log('PASS existing instance upgrades silently with four periods');
    console.log('PASS all period selection browser E2E scenarios');
  } finally {
    if (browser) await browser.close();
    await stop();
    fs.rmSync(root, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
