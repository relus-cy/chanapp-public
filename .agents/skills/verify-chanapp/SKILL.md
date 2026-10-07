---
name: verify-chanapp
description: Verify chanapp browser flows after UI changes or when checking user-visible regressions; use the feature map to select real-app tests and preserve evidence.
---

# Verify chanapp

Primary surface: the web page. CLI and HTTP contract coverage is listed in [testing boundaries](../../../docs/testing.md). Run commands from the repository root.

## Launch

1. Follow [test installation](../../../CONTRIBUTING.md#用户流程验收). Set `PYTHON` only when the interpreter differs from `.venv/bin/python`.
2. Choose the affected entries in [features](features/README.md). Run their command. For the complete flow suite:

   ```bash
   npm run test:e2e
   ```

   Each run creates an isolated demo directory and its own service on a random available loopback port. `target "demo" command ready` confirms readiness. Each test opens a fresh browser context; the regular flow fixture resets periods and watchlist. Recent views and caches remain shared within a run, so tests create their required records explicitly. The exit code is the completion criterion; an infrastructure failure is not a passing test.

## Doctor

Before driving an unexpected environment, run this read-only dependency check from the repository root:

```bash
npm ls --depth=0 && "${PYTHON:-.venv/bin/python}" -c 'import uvicorn, chanapp.api.main'
```

If launch fails, inspect `logs/demo.log` under the chosen output directory and the reported command. Use the runner-owned address; a previously running development or production service is not the verification target.

## Drive

Follow every relevant entry point in the chosen feature page. Exact UI operations use the `screen` and `browser` fixtures in `chanapp/tests/e2e/`; `support.ts` supplies `openChart` and the real HTTP reader. Prefer accessible labels such as `搜索股票` and `自选股标签`. Use browser CSS locators for existing controls without unique accessible names.

Use `expect` or `expect.poll` to wait for the observable result. Mutate user state through the page; use real API reads to prove side effects. New tests belong beside the existing feature tests. External provider/LLM fixtures and front-end HTTP mocks have different evidence boundaries, detailed in the feature pages.

## Evidence

Record the command, exit code, test names and report path. Default evidence is `.e2e/report.json` with linked screenshots/traces; legacy records are in `.e2e/logs/` and `tmp/demo-offline-browser/demo.png`. Use `--output .e2e/<name>` for a named proof run; auxiliary logs follow into that directory's `logs/`. The old demo screenshot keeps its fixed path. Do not infer untested online availability from local success.

For a defect, run its regression before the fix and retain that failure, then run it after the fix. For acceptance, finish all checks required by [CONTRIBUTING](../../../CONTRIBUTING.md#测试), including the existing browser and manual regression requirements.

## Cleanup

The runner stops its service/browser and removes its temporary instance on exit. Retain `.e2e/` and the named screenshots. Confirm the runner's logged server PID has exited and the report still exists. If an interrupted run leaves a process, stop only its recorded PID and remove only its recorded temporary instance directory.

## Helpers

`npm run test:e2e` invokes `bash chanapp/scripts/test_e2e.sh`; it is the existing helper and owns setup/teardown. No separate daemon or scheduled task is required.
