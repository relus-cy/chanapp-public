import { test, expect, openChart, api } from './support';

test('search supports keyboard selection, Escape, and an empty result without adding a watch', async ({ app, browser, screen }) => {
  await openChart({ app, browser, screen });
  const search = screen.getByRole('combobox', '搜索股票');
  await search.fill('不存在的股票xyz');
  await expect(screen.getByText('无匹配结果')).toBeVisible();
  await search.press('Escape');
  await expect(screen.getByRole('listbox')).toBeHidden();
  await expect(search).toHaveAttribute('aria-expanded', 'false');
  await expect(browser.locator('#f10Code')).toHaveText('sz300308');

  await search.fill('上证');
  await expect(screen.getByRole('option').filter({ hasText: '上证指数' })).toBeVisible();
  await search.press('ArrowDown');
  await expect(screen.getByRole('option').filter({ hasText: '上证指数' })).toHaveAttribute('aria-selected', 'true');
  await search.press('Enter');
  await expect(browser.locator('#f10Code')).toHaveText('sh000001');
  await expect(search).toHaveValue('');
  await expect(screen.getByRole('listbox')).toBeHidden();
  await expect(browser.locator('#trackBtn')).toBeVisible();
  expect((await api(app, '/api/watchlist')).map((item: any) => item.code)).toEqual(['sz300308']);
  await expect.poll(async () => (await api(app, '/api/views')).recent[0].code).toBe('sh000001');
});

test('candidate add preserves the current chart and disables duplicate addition', async ({ app, browser, screen }) => {
  await openChart({ app, browser, screen });
  const search = screen.getByRole('combobox', '搜索股票');
  await search.fill('上证');
  await screen.getByRole('button', '加入自选 上证指数').tap();
  await expect(browser.locator('#wlItems .item[data-code="sh000001"]')).toBeVisible();
  await expect(browser.locator('#f10Code')).toHaveText('sz300308');
  await expect.poll(async () => (await api(app, '/api/watchlist')).map((item: any) => item.code)).toEqual(['sz300308', 'sh000001']);
  await search.fill('上证');
  await expect(screen.getByRole('button', '上证指数 已在自选')).toBeDisabled();
  await search.press('Escape');
  await browser.reload();
  await expect(browser.locator('#wlItems .item[data-code="sh000001"]')).toBeVisible();
  expect((await api(app, '/api/watchlist')).filter((item: any) => item.code === 'sh000001')).toHaveLength(1);
});

test('view, track, remove, and reopen through recent views preserve the displayed stock', async ({ app, browser, screen }) => {
  await openChart({ app, browser, screen });
  const search = screen.getByRole('combobox', '搜索股票');
  await search.fill('上证');
  await screen.getByRole('option').filter({ hasText: '上证指数' }).tap();
  await expect(browser.locator('#f10Code')).toHaveText('sh000001');
  await browser.locator('#trackBtn').tap();
  const row = browser.locator('#wlItems .item[data-code="sh000001"]');
  await expect(row).toBeVisible();
  await expect(browser.locator('#trackBtn')).toBeHidden();
  await row.hover();
  await row.getByRole('button', '移出自选').tap();
  await expect(row).toHaveCount(0);
  await expect(browser.locator('#f10Code')).toHaveText('sh000001');
  await expect(browser.locator('#trackBtn')).toBeVisible();
  await expect(browser.locator('#f10TagEdit')).toHaveCount(0);
  await screen.getByRole('tab', '最近查看').tap();
  await expect(row).toBeVisible();
  await row.hover();
  await row.getByRole('button', '加入自选').tap();
  await expect(row).toContainText('已自选');
  await screen.getByRole('tab', '自选股').tap();
  await browser.locator('#wlItems .item[data-code="sz300308"]').tap();
  await expect(browser.locator('#f10Code')).toHaveText('sz300308');
  await screen.getByRole('tab', '最近查看').tap();
  await row.tap();
  await expect(browser.locator('#f10Code')).toHaveText('sh000001');
  await expect.poll(async () => (await api(app, '/api/views')).recent[0].code).toBe('sh000001');
  await browser.reload();
  await expect(screen.getByRole('tab', '最近查看')).toHaveAttribute('aria-selected', 'true');
  await expect(row).toContainText('已自选');
});

test('star pins a stock, survives reload, and unpin restores insertion order', async ({ app, browser, screen }) => {
  await openChart({ app, browser, screen });
  await screen.getByRole('combobox', '搜索股票').fill('上证');
  await screen.getByRole('button', '加入自选 上证指数').tap();
  const row = browser.locator('#wlItems .item[data-code="sh000001"]');
  await expect(row).toBeVisible();
  await row.hover();
  await row.getByRole('button', '置顶').tap();
  await expect(browser.locator('#wlItems .item').first()).toHaveAttribute('data-code', 'sh000001');
  await expect.poll(async () => (await api(app, '/api/watchlist'))[0]).toMatchObject({ code: 'sh000001', starred: true });
  await expect(browser.locator('#f10Code')).toHaveText('sz300308');
  await browser.reload();
  await expect(browser.locator('#wlItems .item').first()).toHaveAttribute('data-code', 'sh000001');
  await row.hover();
  await row.getByRole('button', '置顶').tap();
  await expect(browser.locator('#wlItems .item').first()).toHaveAttribute('data-code', 'sz300308');
  await expect.poll(async () => (await api(app, '/api/watchlist')).find((item: any) => item.code === 'sh000001').starred).toBe(false);
  await browser.reload();
  await expect(browser.locator('#wlItems .item').first()).toHaveAttribute('data-code', 'sz300308');
});

test('tags save on Enter and blur, cancel on Escape, persist, and clear', async ({ app, browser, screen }) => {
  await openChart({ app, browser, screen });
  const edit = browser.locator('#f10TagEdit');
  const input = screen.getByRole('textbox', '自选股标签');
  const chips = browser.locator('#f10Tags > span.chip');
  const tags = async () => (await api(app, '/api/watchlist')).find((item: any) => item.code === 'sz300308').tags;
  await edit.tap();
  await input.fill('成长、观察');
  await input.press('Enter');
  await expect(chips).toHaveText(['成长', '观察']);
  await expect.poll(tags).toEqual(['成长', '观察']);
  await browser.reload();
  await expect(chips).toHaveText(['成长', '观察']);

  await edit.tap();
  await expect(input).toHaveValue('成长、观察');
  await input.fill('不应保存');
  await input.press('Escape');
  await expect(chips).toHaveText(['成长', '观察']);
  expect(await tags()).toEqual(['成长', '观察']);

  await edit.tap();
  await input.fill('趋势,复盘；趋势');
  await screen.getByRole('button', '切换日间/夜间主题').tap();
  await expect(chips).toHaveText(['趋势', '复盘']);
  await expect.poll(tags).toEqual(['趋势', '复盘']);
  await browser.reload();
  await expect(chips).toHaveText(['趋势', '复盘']);

  await edit.tap();
  await input.clear();
  await input.press('Enter');
  await expect(chips).toHaveCount(0);
  await expect(edit).toHaveText('+ 标签');
  await expect.poll(tags).toEqual([]);
  await browser.reload();
  await expect(edit).toHaveText('+ 标签');
  await expect(chips).toHaveCount(0);
});
