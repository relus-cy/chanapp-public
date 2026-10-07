import { test, expect, openChart } from './support';

async function chartAction(browser, action, query: RegExp) {
  const response = browser.waitForResponse(query);
  await action();
  const result = await response;
  expect(result.status).toBe(200);
  const body = await result.json();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await expect(browser.locator('#chartError')).toBeHidden();
  return body;
}

test('day and night themes survive reload and retain a rendered chart', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await expect(browser.locator('html')).toHaveAttribute('data-theme', 'light');
  await screen.getByRole('button', '切换日间/夜间主题').tap();
  await expect(browser.locator('html')).toHaveAttribute('data-theme', 'dark');
  await browser.reload();
  await expect(browser.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(browser.locator('#subNote')).toContainText('DIF');
  await screen.getByRole('button', '切换日间/夜间主题').tap();
  await browser.reload();
  await expect(browser.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(browser.locator('#chart canvas').first()).toBeVisible();
});

test('all six moving-average controls toggle independently and survive period changes', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  for (const [period, selected] of [[5, true], [13, true], [20, true], [60, false], [144, false], [250, false]] as const) {
    const button = screen.getByRole('button', `MA${period}`);
    await expect.poll(async () => (await button.getAttribute('class') || '').split(' ').includes('active')).toBe(selected);
    await button.tap();
    await expect.poll(async () => (await button.getAttribute('class') || '').split(' ').includes('active')).toBe(!selected);
  }
  await chartAction(browser, () => screen.getByRole('button', '60分').tap(), /\/api\/chart\?.*freq=m60&/);
  for (const period of [5, 13, 20]) await expect(browser).toHaveClass(screen.getByRole('button', `MA${period}`), /^$/);
  for (const period of [60, 144, 250]) await expect(browser).toHaveClass(screen.getByRole('button', `MA${period}`), /active/);
});

test('four subchart indicators show the pinned sample values and remain selected across periods', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await chartAction(browser, () => screen.getByRole('button', '不复权').tap(), /\/api\/chart\?.*adjust=raw&/);
  // Literal oracle for the bundled 2026-09-24 raw daily sample; no app calculation is reused.
  const readouts = [
    ['MACD', 'MACD · DIF -1.167 · DEA -9.208 · MACD 16.083 · hist=2×(DIF−DEA)'],
    ['KDJ', 'KDJ(9,3,3) · K 53.47 · D 62.08 · J 36.24 · 显示用'],
    ['RSI', 'RSI(14) · RSI 48.27 · 显示用'],
    ['BOLL', 'BOLL(20,2) · MID 884.96 · UP 960.85 · LOW 809.06 · C 895.86 · 显示用'],
  ];
  for (const [name, text] of readouts) {
    await browser.locator('#subSegBtn').tap();
    await browser.locator('#subSegPop').getByRole('button', name).tap();
    // Closing the menu can put the pointer over a historical candle; leave the chart
    // before checking the latest bar's readout.
    await screen.getByRole('button', '切换日间/夜间主题').hover();
    await expect(browser.locator('#subNote')).toHaveText(text);
    await expect(browser.locator('#subSegBtn')).toHaveAttribute('aria-expanded', 'false');
  }
  await chartAction(browser, () => screen.getByRole('button', '60分').tap(), /\/api\/chart\?.*freq=m60&/);
  await expect(browser.locator('#subNote')).toContainText('BOLL(20,2)');
  await chartAction(browser, () => screen.getByRole('button', '日线').tap(), /\/api\/chart\?.*freq=day&/);
  await expect(browser.locator('#subNote')).toHaveText(readouts[3][1]);
});

test('stroke rules and signal scope reach the real analysis and survive reload', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await screen.getByRole('button', '图层与规则').tap();
  const relaxed = await chartAction(browser, () => screen.getByRole('button', '宽松').tap(), /\/api\/chart\?.*rule_profile=relaxed&/);
  expect(relaxed.rule_profile).toBe('relaxed');
  expect(relaxed.signal_scope).toBe('expanded');
  const standard = await chartAction(browser, () => screen.getByRole('button', '标准').tap(), /\/api\/chart\?.*signal_scope=standard/);
  expect(standard.rule_profile).toBe('relaxed');
  expect(standard.signal_scope).toBe('standard');
  await expect(browser.locator('#ruleSummary')).toHaveText('宽松成笔 · 标准提示');
  await browser.keyboard.press('Escape');
  await expect(screen.getByRole('button', '图层与规则')).toBeFocused();
  await chartAction(browser, () => browser.reload(), /\/api\/chart\?.*rule_profile=relaxed&signal_scope=standard/);
  await expect(browser.locator('#ruleSummary')).toHaveText('宽松成笔 · 标准提示');
  await screen.getByRole('button', '图层与规则').tap();
  const strict = await chartAction(browser, () => screen.getByRole('button', '严格').tap(), /\/api\/chart\?.*rule_profile=strict&/);
  expect(strict.rule_profile).toBe('strict');
  const expanded = await chartAction(browser, () => screen.getByRole('button', '扩展').tap(), /\/api\/chart\?.*signal_scope=expanded/);
  expect(expanded.signal_scope).toBe('expanded');
  await expect(browser.locator('#ruleSummary')).toHaveText('严格成笔 · 扩展提示');
});

test('adjustment is persistent while an index forces raw without replacing the stock preference', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  const raw = await chartAction(browser, () => screen.getByRole('button', '不复权').tap(), /\/api\/chart\?.*adjust=raw&/);
  expect(raw.meta.fqf).toBe('不复权');
  await chartAction(browser, () => browser.reload(), /\/api\/chart\?.*adjust=raw&/);
  await expect(browser).toHaveClass(screen.getByRole('button', '不复权'), /active/);
  const adjusted = await chartAction(browser, () => screen.getByRole('button', '前复权').tap(), /\/api\/chart\?.*adjust=qfq&/);
  expect(adjusted.meta.fqf).toBe('前复权');
  expect(adjusted.kline[0].close).toBeLessThan(raw.kline[0].close);
  await openChart({ app, browser }, '/?code=sh000001&freq=day');
  await expect(screen.getByRole('button', '前复权')).toBeDisabled();
  await expect(screen.getByRole('button', '不复权')).toBeDisabled();
  await expect(browser).toHaveClass(screen.getByRole('button', '不复权'), /active/);
  await expect(browser.locator('#adjustTabs')).toHaveAttribute('title', '指数恒为不复权');
  await openChart({ app, browser });
  await expect(screen.getByRole('button', '前复权')).toBeEnabled();
  await expect(browser).toHaveClass(screen.getByRole('button', '前复权'), /active/);
});

test('evidence and resonance details return focus and close on a period change', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await expect(browser.locator('#cardsCount')).toHaveText('1/5');
  await expect(screen.getByRole('button', '较新信号')).toBeDisabled();
  await screen.getByRole('button', '较早信号').tap();
  await expect(browser.locator('#cardsCount')).toHaveText('2/5');
  await browser.locator('#cards .card').focus();
  await browser.keyboard.press('ArrowLeft');
  await expect(browser.locator('#cardsCount')).toHaveText('1/5');
  await expect(browser.locator('#cards .card')).toBeFocused();
  await browser.keyboard.press('Enter');
  const evidence = screen.getByRole('button', '查看依据与识别时间›');
  await evidence.tap();
  await expect(browser.locator('#aiPop')).toHaveAttribute('aria-hidden', 'false');
  await expect(browser.locator('#aiPopBody')).toContainText('信号依据卡');
  await expect(browser.locator('#aiPopBody')).toContainText('793.03');
  await expect(browser.locator('#aiPopBody')).toContainText('07-30');
  await expect(screen.getByRole('button', '← 返回资料与研究')).toBeFocused();
  await browser.keyboard.press('Escape');
  await expect(evidence).toBeFocused();
  await expect(browser.locator('#aiPop')).toHaveAttribute('aria-hidden', 'true');
  const signal = screen.getByRole('button', '30分信号详情');
  await signal.tap();
  await expect(browser.locator('#aiPopBody')).toContainText('30分信号');
  await expect(browser.locator('#aiPopBody')).toContainText('895.86');
  await expect(browser.locator('#aiPopBody')).toContainText('09-24 15:00');
  await screen.getByRole('button', '← 返回资料与研究').tap();
  await expect(signal).toBeFocused();
  await signal.tap();
  await chartAction(browser, () => screen.getByRole('button', '60分').tap(), /\/api\/chart\?.*freq=m60&/);
  await expect(browser.locator('#aiPop')).toHaveAttribute('aria-hidden', 'true');
  await expect(browser.locator('#cardsDock')).not.toHaveAttribute('inert');
});
