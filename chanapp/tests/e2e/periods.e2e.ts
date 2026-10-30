import { test, expect, openChart, api } from './support';

test('period dialog cancels edits and saves an empty selection across reload', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await screen.getByRole('button', '展示周期').click();
  await browser.locator('#periodOptions input[value="day"]').uncheck();
  await screen.getByRole('button', '取消').click();
  await expect(browser.locator('#freqTabs button:not([hidden])')).toHaveCount(5);
  await screen.getByRole('button', '展示周期').click();
  await expect(browser.locator('#periodOptions input[value="day"]')).toBeChecked();
  for (const freq of ['day', 'week', 'm30', 'm60', 'm15']) {
    await browser.locator(`#periodOptions input[value="${freq}"]`).uncheck();
  }
  await screen.getByRole('button', '保存并继续').click();
  await expect(browser.locator('#freqTabs button:not([hidden])')).toHaveCount(0);
  await expect(browser.locator('#status')).toContainText('没有已勾选');
  expect((await api(app, '/api/periods')).selected).toEqual([]);
  await browser.reload();
  await expect(browser.locator('#status')).toContainText('没有已勾选');
  await expect(browser.locator('#periodDialog')).toBeHidden();
  await expect(browser.locator('#freqTabs button:not([hidden])')).toHaveCount(0);
  await screen.getByRole('button', '展示周期').click();
  await browser.locator('#periodOptions input[value="m30"]').check();
  await screen.getByRole('button', '保存并继续').click();
  await expect(browser.locator('#freqTabs button:not([hidden])')).toHaveCount(1);
  await expect(browser.locator('#freqTabs button:not([hidden])')).toHaveText('30分');
  await expect(browser.locator('#status')).toContainText('历史');
  expect((await api(app, '/api/periods')).selected).toEqual(['m30']);
});

test('stale period edits show a conflict and preserve the other client selection', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await screen.getByRole('button', '展示周期').click();
  await browser.locator('#periodOptions input[value="week"]').uncheck();
  const before = await api(app, '/api/periods');
  await api(app, '/api/periods', 'PUT', { revision: before.revision, selected: ['day'] });
  await screen.getByRole('button', '保存并继续').click();
  await expect(browser.locator('#periodError')).toContainText('重新确认');
  await expect(browser.locator('#periodDialog')).toBeVisible();
  expect((await api(app, '/api/periods')).selected).toEqual(['day']);
  await expect(browser.locator('#periodOptions input[value="week"]')).not.toBeChecked();
  await expect(browser.locator('#periodOptions input[value="day"]')).toBeChecked();
  await screen.getByRole('button', '保存并继续').click();
  await expect(browser.locator('#periodDialog')).toBeHidden();
  await expect(browser.locator('#freqTabs button:not([hidden])')).toHaveText(['日线']);
});

test('A-share 15-minute chart serves the sample session bars and stays out of the multi-period summary', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  const response = browser.waitForResponse(/\/api\/chart\?.*freq=m15&/);
  await screen.getByRole('button', '15分').tap();
  const result = await response;
  expect(result.status).toBe(200);
  const body = await result.json();
  const last = body.kline.map(bar => bar.time).filter(time => time.startsWith(body.kline.at(-1).time.slice(0, 10)));
  expect([last.length, last[0], last.at(-1).slice(11)]).toEqual([16, last[0].slice(0, 11) + '09:45', '15:00']);
  expect(body.resonance.map(item => item.freq)).not.toContain('m15');
  await expect(browser.locator('#freqTabs .active')).toHaveText('15分');
  await expect(browser.locator('#chartError')).toBeHidden();
});

test('legacy minute links normalize to 30 minutes and demo AI explains its boundary', async ({ app, browser }) => {
  await openChart({ app, browser }, '/?code=sz300308&freq=m5');
  await expect(browser.locator('#freqTabs .active')).toHaveText('30分');
  await browser.locator('#aiRefresh').click();
  await expect(browser.locator('#aiPanel')).toContainText('demo 模式不调用 AI');
  await expect(browser.locator('#aiRefresh')).toBeEnabled();
  await browser.locator('#refetchBtn').click();
  await expect(browser.locator('#status')).toContainText('历史');
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await expect(browser.locator('#chartError')).toBeHidden();
});
