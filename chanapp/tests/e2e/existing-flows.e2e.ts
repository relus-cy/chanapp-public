// Retain the existing canvas and multi-process acceptance probes in the same run.
// The fixture UI regression is explicitly separate from real-API acceptance.
import { test, expect } from 'e2e';
import { createRequire } from 'node:module';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import vm from 'node:vm';
import path from 'node:path';

const evidence = path.join(process.env.CHANAPP_E2E_OUTPUT || '.e2e', 'logs');
const require = createRequire(import.meta.url);
const playwrightPath = require.resolve('playwright-core', {
  paths: [require.resolve('@e2e-dev/web')],
});
const { chromium } = require(playwrightPath);

test('real demo: charts, history paging, crosshair, responsive layout and no polling',
  { timeout: 180_000, tags: ['real-api', 'legacy'] }, async ({ app }) => {
    const browser = await chromium.launch({ headless: true });
    try {
      const page = await browser.newPage();
      await page.goto(app.baseUrl);
      await mkdir('tmp/demo-offline-browser', { recursive: true });
      const drive = vm.runInThisContext(await readFile('chanapp/tests/browser_demo_offline.js', 'utf8'),
        { filename: 'chanapp/tests/browser_demo_offline.js' });
      const result = await drive(page);
      expect(result.passed).toBe(true);
      expect(result.external).toEqual([]);
      await mkdir(evidence, { recursive: true });
      await writeFile(path.join(evidence, 'demo-acceptance.json'), JSON.stringify(result, null, 2));
    } finally {
      await browser.close();
    }
  });

test('frontend fixtures: busy and failure recovery, stale tokens, AI details and keyboard navigation',
  { timeout: 180_000, tags: ['frontend-fixture', 'legacy'] }, async ({ app }) => {
    const browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
    try {
      const page = await context.newPage();
      await page.goto(app.baseUrl);
      const drive = vm.runInThisContext(await readFile('chanapp/tests/browser_ui_regression.js', 'utf8'),
        { filename: 'chanapp/tests/browser_ui_regression.js' });
      const result = await drive(page);
      expect(result.passed).toBe(true);
      await mkdir(evidence, { recursive: true });
      await writeFile(path.join(evidence, 'frontend-regression.json'), JSON.stringify(result, null, 2));
    } finally {
      try {
        await context.tracing.stop({ path: path.join(evidence, 'frontend-regression.trace.zip') });
      } finally {
        await browser.close();
      }
    }
  });

test('real API with provider and LLM fixtures: first use, collection, AI combinations, conflicts and restart',
  { timeout: 300_000, tags: ['real-api', 'legacy'] }, async () => {
    await mkdir(evidence, { recursive: true });
    try {
      const result = await promisify(execFile)(process.execPath,
        ['chanapp/tests/browser_period_selection_e2e.js'], {
          env: { ...process.env, PLAYWRIGHT_MODULE: playwrightPath },
          timeout: 280_000, maxBuffer: 2 * 1024 * 1024,
        });
      await writeFile(path.join(evidence, 'period-selection.log'), result.stdout + result.stderr);
      expect(result.stdout).toContain('PASS all period selection browser E2E scenarios');
    } catch (error) {
      await writeFile(path.join(evidence, 'period-selection.log'), (error.stdout || '') + (error.stderr || ''));
      throw error;
    }
  });
