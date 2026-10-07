# Chart and reading

## Sub-features

Theme, six moving averages, MACD/KDJ/RSI/BOLL, strict/relaxed strokes, standard/expanded signals, adjustment, evidence and resonance details.

## How to get to it (user POV)

Use header theme/adjustment controls, `图层与规则`, the MA buttons and indicator menu below the chart. Open signal evidence through `查看依据与识别时间` and the resonance detail buttons.

## Driving it with e2e

```bash
npm run test:e2e -- chanapp/tests/e2e/chart-controls.e2e.ts --output .e2e/chart-proof
```

Completion: all tests pass against the real sample. Indicator text matches pinned literal values; controls retain the behavior asserted across period changes; persisted theme/rules/adjustment survive reload. Details return focus when closed and cease displaying the old chart after a period change.

## Gotchas

Moving averages and indicator selection are tested across period changes; reload persistence is not promised for those controls. Index adjustment uses raw prices without replacing the saved stock preference. Numeric edge cases also live in `chanapp/tests/test_indicators.js`.
