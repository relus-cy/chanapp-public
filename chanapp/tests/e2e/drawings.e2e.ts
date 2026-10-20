import { test, expect, api, openChart, probedAppSource } from './support';
import { createRequire } from 'node:module';

// User drawings (#67): placed with the real mouse on the real demo chart, persisted by
// the public /api/drawings routes, shown again after reload and hidden outside their
// adjustment mode. Drawings are run-scoped server state, so each test clears its code.
const CODE = 'sz300308';
const require = createRequire(import.meta.url);
const { chromium } = require(require.resolve('playwright-core', { paths: [require.resolve('@e2e-dev/web')] }));

// Probe: bar coordinates, price → y, and the bars the drawing library actually placed a drawing on.
const PROBE = `window.__draw = {
  bars: function () { var ts = charts.main.timeScale();
    return klineData.map(function (b) { return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close,
      x: ts.timeToCoordinate(toTime(b.time)) }; }); },
  y: function (price) { return charts.candleSeries.priceToCoordinate(price); },
  labels: function (id) { var d = drawCtl && drawCtl.manager.get(id); if (!d) return null;
    return d.points.map(function (p) { var hit = klineData.filter(function (b) { return String(toTime(b.time)) === String(p.time); })[0];
      return hit ? hit.time : 'off-bar'; }); },
};`;
let appSource: string | null = null;
async function probedApp() {
  appSource ??= await probedAppSource(PROBE);
  return appSource;
}

async function drawings(app) {
  return (await api(app, `/api/drawings?code=${CODE}`)).drawings;
}

const settle = () => new Promise(resolve => setTimeout(resolve, 1500));   // longer than the 1 s save debounce

// `b` is the e2e browser fixture or a raw Playwright page: both have evaluate() and mouse.
async function chartPoint(b, fx: number, fy: number) {
  const box = await b.evaluate(() => {
    const r = document.querySelector('#chart').getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  });
  return { x: box.x + box.w * fx, y: box.y + box.h * fy };
}

async function clickAt(b, point) {
  await b.mouse.move(point.x, point.y);
  await b.mouse.down();
  await b.mouse.up();
}

// Visible bars between two fractions of the chart width, in page coordinates, with their high/low y.
async function barsBetween(b, from: number, to: number) {
  return b.evaluate(([from, to]) => {
    const r = document.querySelector('#chart').getBoundingClientRect();
    return window.__draw.bars().filter(bar => bar.x != null && bar.x > r.width * from && bar.x < r.width * to)
      .map(bar => ({ ...bar, x: r.left + bar.x, yHigh: r.top + window.__draw.y(bar.high), yLow: r.top + window.__draw.y(bar.low),
        yBody: r.top + window.__draw.y((bar.open + bar.close) / 2) }));
  }, [from, to]);
}

// Colour actually shown at a page point: the main pane's stacked canvases blended in DOM order.
async function shownColor(b, point) {
  return b.evaluate(({ x, y }) => {
    const out = [255, 255, 255];
    const bg = getComputedStyle(document.body).backgroundColor.match(/\d+/g).map(Number);
    out.splice(0, 3, ...bg.slice(0, 3));
    for (const canvas of document.querySelectorAll('#chart table canvas')) {
      const r = canvas.getBoundingClientRect();
      if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) continue;
      const k = canvas.width / r.width;
      const [cr, cg, cb, ca] = canvas.getContext('2d').getImageData(Math.floor((x - r.left) * k), Math.floor((y - r.top) * k), 1, 1).data;
      const a = ca / 255;
      out[0] = cr * a + out[0] * (1 - a); out[1] = cg * a + out[1] * (1 - a); out[2] = cb * a + out[2] * (1 - a);
    }
    return out.map(Math.round);
  }, point);
}

async function openWide(context, path = `/?code=${CODE}&freq=day`, clear = true) {
  if (clear) await api(context.app, `/api/drawings/${CODE}`, 'DELETE');
  await context.browser.route(/\/app\.js(\?|$)/, async route => {
    await route.fulfill({ body: await probedApp(), contentType: 'application/javascript' });
  });
  await context.browser.setViewport({ width: 1440, height: 900 });
  await openChart(context, path);
}

// A second device: its own browser, same instance, same viewport.
async function openDevice(app, path = `/?code=${CODE}&freq=day`) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route(/\/app\.js(\?|$)/, async route => {
    await route.fulfill({ body: await probedApp(), contentType: 'application/javascript' });
  });
  await page.goto(new URL(path, app.baseUrl).href);
  await page.locator('#center[aria-busy="false"]').waitFor();
  await page.waitForFunction(() => window.__draw && window.__draw.bars().length > 0);
  return { browser, page, errors };
}

test('a segment drawn with the mouse is saved, survives reload and follows its adjustment mode', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  await screen.getByRole('button', '线段').tap();
  await expect(screen.getByRole('button', '线段')).toHaveAttribute('aria-pressed', 'true');
  const a = await chartPoint(browser, 0.3, 0.45), b = await chartPoint(browser, 0.6, 0.3);
  await clickAt(browser, a);
  await clickAt(browser, b);
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const [saved] = await drawings(app);
  expect(saved.revision).toBe(1);
  expect(saved.body.kind).toBe('trend-line');
  expect(saved.body.adjust).toBe('qfq');
  expect(saved.body.anchors.map(x => x.freq)).toEqual(['day', 'day']);
  expect(saved.body.anchors[0].dt < saved.body.anchors[1].dt).toBe(true);
  // Reference close of the last complete bar of the bundled daily sample (2026-09-23).
  expect(saved.body.ref).toEqual({ day: '2026-09-23', close: 922.5 });
  // The stored labels are the bars the library placed the anchors on.
  expect(await browser.evaluate(id => window.__draw.labels(id), saved.id)).toEqual(saved.body.anchors.map(x => x.dt));

  await browser.reload();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await expect(browser.locator('#drawNote')).toBeHidden();
  await screen.getByRole('button', '不复权').tap();
  await expect(browser.locator('#drawNote')).toHaveText('当前复权下隐藏 1 条画线');
  await screen.getByRole('button', '前复权').tap();
  await expect(browser.locator('#drawNote')).toBeHidden();
  await expect.poll(() => browser.evaluate(id => window.__draw.labels(id), saved.id)).toEqual(saved.body.anchors.map(x => x.dt));

  // Select the line at its midpoint, recolour it, then delete it with the keyboard.
  await clickAt(browser, { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  await expect(browser.locator('#drawStyle')).toBeVisible();
  await browser.locator('#dsColor').getByRole('button', '#f23645').tap();
  await expect.poll(async () => (await drawings(app))[0].body.style.color).toBe('#f23645');
  expect((await drawings(app))[0].revision).toBe(2);
  await browser.keyboard.press('Delete');
  await expect.poll(async () => (await drawings(app)).length).toBe(0);
  await expect(browser.locator('#drawStyle')).toBeHidden();
});

test('a line drawn on 60 minutes keeps its bar on the daily chart and clearing removes every drawing', async ({ app, browser, screen }) => {
  await openWide({ app, browser }, `/?code=${CODE}&freq=m60`);
  await screen.getByRole('button', '水平线').tap();
  await clickAt(browser, await chartPoint(browser, 0.5, 0.5));
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const [line] = await drawings(app);
  expect(line.body.anchors[0].freq).toBe('m60');
  expect(line.body.anchors[0].dt).toMatch(/^2026-\d\d-\d\d \d\d:\d\d$/);
  await screen.getByRole('button', '日线').tap();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  // The 60-minute anchor sits on the daily bar of the same day.
  await expect.poll(() => browser.evaluate(id => window.__draw.labels(id), line.id)).toEqual([line.body.anchors[0].dt.slice(0, 10)]);
  // Viewing on another period never rewrites an anchor the user did not move.
  await settle();
  expect((await drawings(app))[0].revision).toBe(1);
  await screen.getByRole('button', '矩形').tap();
  await clickAt(browser, await chartPoint(browser, 0.3, 0.3));
  await clickAt(browser, await chartPoint(browser, 0.5, 0.5));
  await expect.poll(async () => (await drawings(app)).length).toBe(2);
  await browser.onDialog('accept');
  await screen.getByRole('button', '清空当前标的的画线').tap();
  await expect.poll(async () => (await drawings(app)).length).toBe(0);
});

test('a daily rectangle lands on the containing week and 60-minute bars and stays there after history paging', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  const [p] = await barsBetween(browser, 0.55, 0.6), [q] = (await barsBetween(browser, 0.7, 0.75)).slice(-1);
  await screen.getByRole('button', '矩形').tap();
  await clickAt(browser, { x: p.x, y: p.yHigh - 30 });
  await clickAt(browser, { x: q.x, y: q.yLow + 30 });
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const [rect] = await drawings(app);
  expect(rect.body.anchors.map(x => x.dt)).toEqual([p.time, q.time]);

  await screen.getByRole('button', '周线').tap();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  // A week bar is labelled with the last trading day of its week: the first week label on or after the day.
  const weekOf = async day => (await browser.evaluate(() => window.__draw.bars().map(bar => bar.time))).find(t => t >= day);
  await expect.poll(() => browser.evaluate(id => window.__draw.labels(id), rect.id))
    .toEqual([await weekOf(p.time), await weekOf(q.time)]);

  await screen.getByRole('button', '60分').tap();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  // A daily anchor sits on the first 60-minute bar of its day.
  const expected = [`${p.time} 10:30`, `${q.time} 10:30`];
  await expect.poll(() => browser.evaluate(id => window.__draw.labels(id), rect.id)).toEqual(expected);
  // Page older history by scrolling the time axis (a drag here would grab the rectangle and move it).
  await browser.keyboard.press('Escape');
  await expect(browser.locator('#drawStyle')).toBeHidden();
  const before = (await browser.evaluate(() => window.__draw.bars().length));
  const axis = await chartPoint(browser, 0.5, 0.97);
  await browser.mouse.move(axis.x, axis.y);
  for (let i = 0; i < 30 && (await browser.evaluate(() => window.__draw.bars().length)) === before; i++) {
    await browser.mouse.wheel(-600, 0);
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  expect(await browser.evaluate(() => window.__draw.bars().length)).toBeGreaterThan(before);
  await expect.poll(() => browser.evaluate(id => window.__draw.labels(id), rect.id)).toEqual(expected);
  await settle();
  expect((await drawings(app))[0].revision).toBe(1);
});

test('an anchor placed right of the last bar is stored as a future trading day and shown again', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  // The right offset leaves three empty bar slots before the 72px price axis; aim at the last one.
  const right = await browser.evaluate(() => {
    const r = document.querySelector('#chart').getBoundingClientRect();
    return { x: r.right - 72 - 4, y: r.top + r.height * 0.35 };
  });
  await screen.getByRole('button', '线段').tap();
  await clickAt(browser, await chartPoint(browser, 0.7, 0.4));
  await clickAt(browser, right);
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const future = (await drawings(app))[0].body.anchors[1];
  // The bundled sample ends on Thursday 2026-09-24; the next slots are Friday and the following Monday.
  expect(['2026-09-25', '2026-09-28', '2026-09-29']).toContain(future.dt);
  await screen.getByRole('button', '30分').tap();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await screen.getByRole('button', '日线').tap();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await settle();
  expect((await drawings(app))[0].body.anchors[1].dt).toBe(future.dt);
  expect((await drawings(app))[0].revision).toBe(1);
});

test('on 30 minutes a future anchor takes a trading-session slot, never the lunch break', async ({ app, browser, screen }) => {
  await openWide({ app, browser }, `/?code=${CODE}&freq=m30`);
  const last = (await browser.evaluate(() => window.__draw.bars().slice(-1)[0].time));
  const right = await browser.evaluate(() => {
    const r = document.querySelector('#chart').getBoundingClientRect();
    return { x: r.right - 72 - 4, y: r.top + r.height * 0.35 };
  });
  await screen.getByRole('button', '线段').tap();
  await clickAt(browser, await chartPoint(browser, 0.7, 0.4));
  await clickAt(browser, right);
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const future = (await drawings(app))[0].body.anchors[1];
  expect(future.dt > last).toBe(true);
  expect(future.dt).toMatch(/^2026-\d\d-\d\d (10:00|10:30|11:00|11:30|13:30|14:00|14:30|15:00)$/);
  await browser.reload();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await settle();
  expect((await drawings(app))[0].body.anchors[1].dt).toBe(future.dt);
  expect((await drawings(app))[0].revision).toBe(1);
});

test('the end-point magnet snaps to the bar high, Shift and the off setting do not, and the tool style is remembered', async ({ app, browser }) => {
  // Shift has to be held during the drag, which needs a raw Playwright page; the fixture page only watches.
  await openWide({ app, browser });
  const device = await openDevice(app);
  const page = device.page;
  try {
    const bars = await barsBetween(page, 0.3, 0.7);
    const p = bars[2], r = bars[bars.length - 3];
    await page.getByRole('button', { name: '斐波那契回撤' }).click();
    await clickAt(page, { x: p.x, y: p.yHigh - 15 });          // weak magnet (default): within 50px of the high
    await clickAt(page, { x: r.x, y: r.yLow + 120 });
    await expect.poll(async () => (await drawings(app)).length).toBe(1);
    const [fib] = await drawings(app);
    expect(fib.body.anchors[0].price).toBe(p.high);

    // Drag that end to another bar's high with Shift held: no snapping.
    await page.locator('#drawStyle').waitFor();
    await page.mouse.move(p.x, p.yHigh);
    await page.mouse.down();
    await page.keyboard.down('Shift');
    await page.mouse.move(r.x, r.yHigh - 15, { steps: 8 });
    await page.mouse.up();
    await page.keyboard.up('Shift');
    await expect.poll(async () => (await drawings(app))[0].revision).toBe(2);
    const dragged = (await drawings(app))[0].body.anchors[0];
    expect(dragged.dt).toBe(r.time);
    expect(dragged.price).not.toBe(r.high);
    expect(dragged.price).toBeGreaterThan(r.high);

    // Recolour it: the next fib starts with that colour, also after a reload.
    await page.locator('#dsColor [data-color="#089981"]').click();
    await expect.poll(async () => (await drawings(app))[0].body.style.color).toBe('#089981');

    // Magnet weak → strong → off.
    await page.locator('#drawMagnet').click();
    await page.locator('#drawMagnet').click();
    await page.locator('#drawMagnetMode', { hasText: '关' }).waitFor();
    await page.getByRole('button', { name: '斐波那契回撤' }).click();
    await clickAt(page, { x: p.x, y: p.yHigh - 15 });
    await clickAt(page, { x: r.x, y: r.yLow + 120 });
    await expect.poll(async () => (await drawings(app)).length).toBe(2);
    const loose = (await drawings(app))[1];
    expect(loose.body.anchors[0].price).toBeGreaterThan(p.high);
    expect(loose.body.style.color).toBe('#089981');
    expect(loose.revision).toBe(1);

    await page.reload();
    await page.locator('#center[aria-busy="false"]').waitFor();
    await page.waitForFunction(() => window.__draw && window.__draw.bars().length > 0);
    await page.locator('#drawMagnetMode', { hasText: '关' }).waitFor();
    await page.getByRole('button', { name: '斐波那契回撤' }).click();
    await clickAt(page, { x: p.x, y: p.yHigh - 60 });
    await clickAt(page, { x: r.x, y: r.yLow + 60 });
    await expect.poll(async () => (await drawings(app)).length).toBe(3);
    expect((await drawings(app))[2].body.style.color).toBe('#089981');
    expect(device.errors).toEqual([]);
  } finally {
    await device.browser.close();
  }
});

test('a rectangle filled at transparency 80 still shows the candles under it', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  const bars = await barsBetween(browser, 0.4, 0.6);
  // Corners more than 50px from any price so the weak magnet leaves them where they are clicked.
  const top = Math.min(...bars.map(b => b.yHigh)) - 70, bottom = Math.max(...bars.map(b => b.yLow)) + 70;
  await screen.getByRole('button', '矩形').tap();
  await clickAt(browser, { x: bars[0].x, y: top });
  await clickAt(browser, { x: bars[bars.length - 1].x, y: bottom });
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  await expect(browser.locator('#drawStyle')).toBeVisible();
  await browser.locator('#dsFillColor [data-fill="#787b86"]').tap();
  await browser.evaluate(() => {
    const alpha = document.querySelector('#dsAlpha') as HTMLInputElement;
    alpha.value = '80';
    alpha.dispatchEvent(new Event('change', { bubbles: true }));
  });
  // The rectangle is filled by default, so only the colour and transparency differ from the tool defaults.
  await expect.poll(async () => (await drawings(app))[0].body.style).toEqual({ backgroundColor: '#787b86', transparency: 80 });
  await clickAt(browser, await chartPoint(browser, 0.9, 0.9));                // deselect: no anchor handles over the probe
  await expect(browser.locator('#drawStyle')).toBeHidden();
  const inside = bars[Math.floor(bars.length / 2)];
  const candle = await shownColor(browser, { x: inside.x, y: inside.yBody });
  const fill = await shownColor(browser, { x: inside.x, y: bottom - 15 });   // inside the rectangle, below every candle
  const outside = await shownColor(browser, { x: inside.x, y: bottom + 30 });
  // The candle keeps its red or teal hue through the grey fill; the fill itself is visibly painted.
  expect(Math.max(...candle) - Math.min(...candle)).toBeGreaterThan(60);
  expect(Math.max(...fill.map((c, i) => Math.abs(c - outside[i])))).toBeGreaterThan(8);
});

test('two devices share drawings, a stale edit gets the server version and a deleted drawing is never written back', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  await screen.getByRole('button', '线段').tap();
  const a = await chartPoint(browser, 0.3, 0.45), b = await chartPoint(browser, 0.6, 0.3), mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  await clickAt(browser, a);
  await clickAt(browser, b);
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const id = (await drawings(app))[0].id;

  const device = await openDevice(app);
  const other = device.page;
  try {
    // B opened the code after A drew: the drawing is there.
    await expect.poll(() => other.evaluate(id => window.__draw.labels(id), id)).not.toBeNull();
    let message = '';
    other.once('dialog', dialog => { message = dialog.message(); return dialog.dismiss(); });
    await other.getByRole('button', { name: '清空当前标的的画线' }).click();
    await expect.poll(() => message).toContain('全部 1 条画线');

    // A recolours (revision 2); B returns to the foreground and edits on top of revision 2.
    await clickAt(browser, mid);
    await browser.locator('#dsColor').getByRole('button', '#f23645').tap();
    await expect.poll(async () => (await drawings(app))[0].revision).toBe(2);
    const reread = other.waitForResponse(response => response.url().includes('/api/drawings?code='));
    await other.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await reread;
    await clickAt(other, mid);
    await other.locator('#dsColor [data-color="#089981"]').click();
    await expect.poll(async () => (await drawings(app))[0].revision).toBe(3);
    expect((await drawings(app))[0].body.style.color).toBe('#089981');

    // A still holds revision 2: its edit is refused, A shows the server version and says so.
    await browser.locator('#dsColor').getByRole('button', '#ff9800').tap();
    await expect(browser.locator('#drawNote')).toContainText('画线已在其他设备修改或删除，已载入最新版本');
    await settle();
    expect((await drawings(app))[0]).toMatchObject({ revision: 3, body: { style: { color: '#089981' } } });

    // B deletes it; A's next edit on the stale copy must not bring it back.
    await other.keyboard.press('Delete');
    await expect.poll(async () => (await drawings(app)).length).toBe(0);
    await browser.locator('#dsColor').getByRole('button', '#f23645').tap();
    await expect(browser.locator('#drawStyle')).toBeHidden();
    await settle();
    expect(await drawings(app)).toEqual([]);
    expect(await browser.evaluate(id => window.__draw.labels(id), id)).toBeNull();
    expect(device.errors).toEqual([]);
  } finally {
    await device.browser.close();
  }

  // A failed write keeps the drawing on screen marked unsaved; the next change saves it.
  await browser.route(/\/api\/drawings\/sz300308\/[^/?]+$/, async route => {
    if (route.request.method === 'PUT') await route.fulfill({ status: 503, json: { detail: '存储暂不可用' } });
    else await route.fallback();
  });
  await screen.getByRole('button', '线段').tap();
  await clickAt(browser, a);
  await clickAt(browser, b);
  await expect(browser.locator('#drawNote')).toContainText('画线未保存：存储暂不可用');
  await expect(browser.locator('#drawNote')).toContainText('1 条画线未保存');
  expect(await drawings(app)).toEqual([]);
  await browser.unroute(/\/api\/drawings\/sz300308\/[^/?]+$/);
  await clickAt(browser, mid);
  await browser.locator('#dsColor').getByRole('button', '#f23645').tap();
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  expect((await drawings(app))[0]).toMatchObject({ revision: 1, body: { style: { color: '#f23645' } } });
  await expect(browser.locator('#drawNote')).not.toContainText('未保存');
});

test('after a new ex-dividend the qfq drawing is rescaled with the bars and saved in the new basis', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  await screen.getByRole('button', '线段').tap();
  await clickAt(browser, await chartPoint(browser, 0.3, 0.45));
  await clickAt(browser, await chartPoint(browser, 0.6, 0.3));
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  const [line] = await drawings(app);
  expect(line.body.ref).toEqual({ day: '2026-09-23', close: 922.5 });

  // Simulated ex-dividend on the last day of the sample: every earlier qfq price is multiplied by 0.5.
  await browser.route(/\/api\/chart\?.*adjust=qfq/, async route => {
    const response = await fetch(route.request.url);
    const data = await response.json();
    data.kline = data.kline.map(bar => bar.time < '2026-09-24'
      ? { ...bar, open: bar.open / 2, high: bar.high / 2, low: bar.low / 2, close: bar.close / 2 } : bar);
    data.meta.data_version = 'ex-dividend';
    await route.fulfill({ json: data });
  });
  await browser.reload();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await expect.poll(async () => (await drawings(app))[0].revision).toBe(2);
  const [scaled] = await drawings(app);
  expect(scaled.body.ref).toEqual({ day: '2026-09-23', close: 461.25 });
  scaled.body.anchors.forEach((anchor, i) => {
    expect(anchor.dt).toBe(line.body.anchors[i].dt);
    expect(anchor.price).toBeCloseTo(line.body.anchors[i].price / 2, 6);
  });
  await browser.unroute(/\/api\/chart\?.*adjust=qfq/);
});

test('switching code while the old code\'s drawings are still loading never shows them on the new code', async ({ app, browser, screen }) => {
  await api(app, `/api/drawings/${CODE}`, 'DELETE');
  await api(app, `/api/drawings/${CODE}/seeded-line-1`, 'PUT', { base_revision: null, body: {
    v: 1, kind: 'horizontal-line', adjust: 'qfq', anchors: [{ freq: 'day', dt: '2026-09-10', price: 900 }], style: {}, ref: null } });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await browser.route(/\/api\/drawings\?code=sz300308/, async route => { await held; await route.continue(); });
  await openWide({ app, browser }, `/?code=${CODE}&freq=day`, false);
  const search = screen.getByRole('combobox');
  await search.fill('上证');
  await expect(screen.getByRole('option').filter({ hasText: '上证指数' })).toBeVisible();
  await search.press('ArrowDown');
  await search.press('Enter');
  await expect(browser.locator('#f10Code')).toHaveText('sh000001');
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  release();
  await settle();
  expect(await browser.evaluate(() => window.__draw.labels('seeded-line-1'))).toBeNull();
  await screen.getByRole('button', '清空当前标的的画线').tap();
  await expect(browser.locator('#drawNote')).toHaveText('当前标的没有画线');
  await browser.unroute(/\/api\/drawings\?code=sz300308/);
  expect((await drawings(app)).map(d => d.id)).toEqual(['seeded-line-1']);
});

test('Delete in a text field keeps the drawing and Escape leaves the tool without closing the floating sidebar', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  await screen.getByRole('button', '线段').tap();
  await clickAt(browser, await chartPoint(browser, 0.3, 0.45));
  await clickAt(browser, await chartPoint(browser, 0.6, 0.3));
  await expect.poll(async () => (await drawings(app)).length).toBe(1);
  await expect(browser.locator('#drawStyle')).toBeVisible();
  await screen.getByRole('combobox').tap();
  await browser.keyboard.press('Delete');
  await settle();
  expect(await drawings(app)).toHaveLength(1);
  await expect(browser.locator('#drawStyle')).toBeVisible();

  const away = await chartPoint(browser, 0.5, 0.5);
  await browser.mouse.move(away.x, away.y);                                   // not over the sidebar when it collapses
  await browser.evaluate(() => { (document.activeElement as HTMLElement).blur(); });   // '[' is ignored inside the search field
  await browser.keyboard.press('[');                                          // pinned → rail
  await expect(browser.locator('body')).toHaveAttribute('class', /sb-rail/);
  await screen.getByRole('button', '线段').tap();
  await new Promise(resolve => setTimeout(resolve, 500));                     // let the collapse transition finish
  const rail = await browser.evaluate(() => {
    const r = document.querySelector('#sidebar').getBoundingClientRect();
    return { x: r.left + Math.min(16, r.width / 2), y: r.top + r.height / 2 };
  });
  await browser.mouse.move(rail.x + 4, rail.y);
  await browser.mouse.move(rail.x, rail.y);                                   // hovering the rail floats it open
  await expect(browser.locator('body')).toHaveAttribute('class', /sb-open/);
  await browser.keyboard.press('Escape');
  await expect(screen.getByRole('button', '线段')).toHaveAttribute('aria-pressed', 'false');
  await expect(browser.locator('body')).toHaveAttribute('class', /sb-open/);
});

test('switching theme keeps a user colour and leaves the saved drawing untouched', async ({ app, browser, screen }) => {
  await openWide({ app, browser });
  await screen.getByRole('button', '线段').tap();
  const a = await chartPoint(browser, 0.3, 0.45), b = await chartPoint(browser, 0.6, 0.3);
  await clickAt(browser, a);
  await clickAt(browser, b);
  await browser.locator('#dsColor').getByRole('button', '#f23645').tap();
  await expect.poll(async () => (await drawings(app))[0]?.body.style.color).toBe('#f23645');
  const before = (await drawings(app))[0];
  await clickAt(browser, await chartPoint(browser, 0.9, 0.9));
  await screen.getByRole('button', '切换日间/夜间主题').tap();
  await expect(browser.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
  await settle();
  expect((await drawings(app))[0]).toEqual(before);
  const shown = await shownColor(browser, { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  expect(shown[0]).toBeGreaterThan(180);                                      // still the user's red on the dark chart
  expect(shown[1]).toBeLessThan(110);
  await screen.getByRole('button', '切换日间/夜间主题').tap();
  await expect(browser.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('a stored drawing the page cannot read is skipped with a note while the others still show', async ({ app, browser }) => {
  await api(app, `/api/drawings/${CODE}`, 'DELETE');
  await api(app, `/api/drawings/${CODE}/broken-line-1`, 'PUT', { base_revision: null, body: { v: 1, kind: 'trend-line' } });
  await api(app, `/api/drawings/${CODE}/good-line-01`, 'PUT', { base_revision: null, body: {
    v: 1, kind: 'horizontal-line', adjust: 'qfq', anchors: [{ freq: 'day', dt: '2026-09-10', price: 900 }], style: {}, ref: null } });
  await browser.addInitScript(() => {
    window.__pageErrors = [];
    addEventListener('error', event => window.__pageErrors.push(event.message));
    addEventListener('unhandledrejection', event => window.__pageErrors.push(String(event.reason)));
  });
  await openWide({ app, browser }, `/?code=${CODE}&freq=day`, false);
  await expect(browser.locator('#drawNote')).toHaveText('1 条画线无法识别，已跳过');
  expect(await browser.evaluate(() => window.__draw.labels('good-line-01'))).toEqual(['2026-09-10']);
  expect(await browser.evaluate(() => window.__pageErrors)).toEqual([]);
  await settle();
  expect((await drawings(app)).map(d => [d.id, d.revision])).toEqual([['broken-line-1', 1], ['good-line-01', 1]]);
});
