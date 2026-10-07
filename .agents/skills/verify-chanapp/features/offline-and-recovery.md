# Offline and recovery

## Sub-features

Historical status and absent sample data, four periods, paging and synchronized crosshairs, responsive sidebar, no polling/external transport, manual refresh, disabled demo AI, failure/busy recovery and stale-data protection.

## How to get to it (user POV)

Open the demo, change period, drag the chart toward older history, resize the viewport and collapse/expand the sidebar. Use `重拉` and the AI refresh button. Recovery paths begin when loading fails or the underlying data changes.

## Driving it with e2e

```bash
npm run test:e2e -- chanapp/tests/e2e/existing-flows.e2e.ts --output .e2e/recovery-proof
```

Completion: all three explicitly named tests pass. The real-demo test confirms historical rendering, paging, layout, no external resources and no timed polling; the runner also checks server outbound attempts. This command's evidence includes `.e2e/recovery-proof/logs/demo-acceptance.json` and `tmp/demo-offline-browser/demo.png`.

## Gotchas

The test named `frontend fixtures` intercepts business APIs to drive deterministic errors and AI detail content. It proves frontend recovery and accessibility only. The period-selection test uses real APIs with external provider/LLM fixtures. Keep these labels in any result summary; demo has no live F10, fund-flow, Hong Kong or AI sample.
