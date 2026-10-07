import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';

if (!process.env.CHANAPP_E2E_ROOT) {
  throw new Error('Run npm run test:e2e so each run has an isolated instance.');
}

export default {
  tests: 'chanapp/tests/e2e/**/*.e2e.ts',
  workers: 1,
  retries: 0,
  timeout: 120_000,
  assertionTimeout: 10_000,
  trace: 'on',
  cache: 'off',
  targets: [{
    name: 'demo',
    engine: web({ viewport: { width: 1440, height: 900 } }),
    app: {
      url: 'http://127.0.0.1:0',
      command: {
        executable: process.env.PYTHON || '.venv/bin/python',
        args: ['-m', 'chanapp.tests.support.demo_offline_server',
          '--root', process.env.CHANAPP_E2E_ROOT, '--port', '{port}'],
        log: `${process.env.CHANAPP_E2E_OUTPUT || '.e2e'}/logs/demo.log`,
      },
    },
  }],
} satisfies E2EConfig;
