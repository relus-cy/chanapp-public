import { test as base } from '@e2e-dev/web';
import { expect } from 'e2e';

export { expect };

// Public HTTP routes restore periods and watchlist between tests. Views/cache are
// run-scoped; each test must create the view records it needs through the UI.
export async function api(app, path: string, method = 'GET', body?) {
  const response = await fetch(new URL(path, app.baseUrl), {
    method,
    ...(body === undefined ? {} : {
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  });
  expect(response.status, `${method} ${path}`).toBe(200);
  return response.json();
}

export const test = base.extend({
  isolatedDemo: async ({ app, browser }, use) => {
    const periods = await api(app, '/api/periods');
    await api(app, '/api/periods', 'PUT', {
      revision: periods.revision, selected: ['m30', 'm60', 'day', 'week'],
    });
    for (const item of await api(app, '/api/watchlist')) {
      await api(app, `/api/watchlist/${item.code}`, 'DELETE');
    }
    await api(app, '/api/watchlist', 'POST', { code: 'sz300308', name: '中际旭创' });
    await browser.addInitScript(() => {
      window.__e2eErrors = [];
      addEventListener('error', event => window.__e2eErrors.push(event.message));
      addEventListener('unhandledrejection', event => window.__e2eErrors.push(String(event.reason)));
    });
    // Only an external network boundary is blocked. All application routes stay real.
    await browser.route(/^https?:\/\/(?!127\.0\.0\.1:)/, async route => {
      await route.abort();
      throw new Error('The offline application attempted a browser request outside loopback');
    });
    try {
      await use(undefined);
    } finally {
      if ((await browser.url()).startsWith('http')) {
        expect(await browser.evaluate(() => window.__e2eErrors || [])).toEqual([]);
        await app.screenshot('flow-result');
      }
    }
  },
});

export async function openChart({ app, browser }, path = '/?code=sz300308&freq=day') {
  await app.open(path);
  await expect(browser.locator('#status')).toContainText('历史');
  await expect(browser.locator('#chart canvas').first()).toBeVisible();
  await expect(browser.locator('#center')).toHaveAttribute('aria-busy', 'false');
}
