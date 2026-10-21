import { test, expect, openChart, probedAppSource } from './support';

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

test('structure layer toggle hides only the drawn analysis and survives reload', async ({ app, browser, screen }) => {
  const probe = `window.__structure = function () {
    var ma = Object.keys(maSeries).map(function (n) { return maSeries[n]; });
    var lines = charts.main.panes()[0].getSeries().filter(function (s) {
      return s.seriesType() === 'Line' && ma.indexOf(s) < 0 && s.options().visible && s.data().length > 0; });
    return { lines: lines.length, markers: charts.markers.shown().length };
  };`;
  const source = await probedAppSource(probe);
  await browser.route(/\/app\.js(\?|$)/, route => route.fulfill({ body: source, contentType: 'application/javascript' }));
  // The 60-minute sample has a zhongshu inside the initial visible window; the daily one does not.
  await openChart({ app, browser }, '/?code=sz300308&freq=m60');
  const cards = await browser.locator('#cardsCount').textContent();
  // The zhongshu overlay is the chart's own canvas child; count painted pixels in it.
  const zsInk = () => browser.evaluate(() => {
    const canvas = document.querySelector('#chart > canvas') as HTMLCanvasElement;
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let painted = 0;
    for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) painted++;
    return painted;
  });
  // Lines on the main pane other than candles and moving averages are structure (strokes, segments,
  // forming parts, channels); with the layer hidden none may stay visible, nor may any signal marker.
  const structureShown = () => browser.evaluate(() => window.__structure());
  await expect.poll(zsInk).toBeGreaterThan(0);
  const shown = await structureShown();
  expect(shown.lines).toBeGreaterThan(1);
  expect(shown.markers).toBeGreaterThan(0);
  await screen.getByRole('button', '图层与规则').tap();
  await screen.getByRole('button', '隐藏').tap();
  await expect.poll(zsInk).toBe(0);
  await expect.poll(structureShown).toEqual({ lines: 0, markers: 0 });
  await expect(browser.locator('#ruleSummary')).toHaveText('严格成笔 · 扩展提示 · 结构层隐藏');
  await expect(browser.locator('#cardsCount')).toHaveText(cards);
  await chartAction(browser, () => browser.reload(), /\/api\/chart\?/);
  await expect(browser.locator('#ruleSummary')).toHaveText('严格成笔 · 扩展提示 · 结构层隐藏');
  await expect.poll(zsInk).toBe(0);
  await expect.poll(structureShown).toEqual({ lines: 0, markers: 0 });
  await screen.getByRole('button', '图层与规则').tap();
  await screen.getByRole('button', '显示').tap();
  await expect.poll(zsInk).toBeGreaterThan(0);
  await expect.poll(structureShown).toEqual(shown);
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

test('donchian toggle shows the pinned sample channel, keeps its choice across reload and explains raw', async ({ app, browser, screen }) => {
  await openChart({ app, browser });
  await expect(browser.locator('#dcSegCur')).toHaveText('唐奇安');
  await expect(browser.locator('#ohlc .dc-legend')).toHaveCount(0);
  await browser.locator('#dcSegBtn').tap();
  await browser.locator('#dcSegPop [data-dc="20/10"]').tap();
  // The menu stays open for the mode choice; Esc closes it without touching the drawing tools.
  await expect(browser.locator('#dcSegPop')).toBeVisible();
  await browser.keyboard.press('Escape');
  await expect(browser.locator('#dcSegBtn')).toHaveAttribute('aria-expanded', 'false');
  await screen.getByRole('button', '切换日间/夜间主题').hover();
  // Literal oracle for the bundled 2026-09-24 qfq daily sample, computed outside the app:
  // previous-20 high, previous-10 low, Wilder ATR(20), 1% / (2N / close).
  const latest = 'DC20/10 入974.99 出858.00 N53.19 下一根入 974.99 出 858.00 建议仓位8.4%';
  await expect(browser.locator('#ohlc .dc-legend')).toHaveText(latest);
  await browser.locator('#dcSegBtn').tap();
  await browser.locator('#dcSegPop [data-dcmode="touch"]').tap();
  await browser.keyboard.press('Escape');
  await expect(browser.locator('#dcSegCur')).toHaveText('DC 20/10 · 触价');
  await chartAction(browser, () => browser.reload(), /\/api\/chart\?.*freq=day&/);
  await expect(browser.locator('#dcSegCur')).toHaveText('DC 20/10 · 触价');
  await expect(browser.locator('#dcSegPop [data-dcmode="touch"]')).toHaveAttribute('aria-checked', 'true');
  await chartAction(browser, () => screen.getByRole('button', '不复权').tap(), /\/api\/chart\?.*adjust=raw&/);
  await screen.getByRole('button', '切换日间/夜间主题').hover();
  await expect(browser.locator('#ohlc .dc-legend')).toHaveText('唐奇安仅前复权下显示');
});
