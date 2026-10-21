# Chart and reading

## Sub-features

Theme, six moving averages, Donchian channel, MACD/KDJ/RSI/BOLL, strict/relaxed strokes, standard/expanded signals, structure-layer visibility, user drawings, adjustment, evidence and resonance details.

## How to get to it (user POV)

Use header theme/adjustment controls, the toolbar above the chart (`画线`, the Donchian menu `唐奇安`, `缠论`), and the MA buttons and indicator menu below the chart. The Donchian rule-state card sits in the right rail while the overlay is on. Open signal evidence through `查看依据与识别时间` and the resonance detail buttons.

## Driving it with e2e

```bash
npm run test:e2e -- chanapp/tests/e2e/chart-controls.e2e.ts --output .e2e/chart-proof
```

Completion: all tests pass against the real sample. Indicator text matches pinned literal values; controls retain the behavior asserted across period changes; persisted theme/rules/adjustment/Donchian choice survive reload. Details return focus when closed and cease displaying the old chart after a period change.

## Gotchas

Moving averages and indicator selection are tested across period changes; reload persistence is not promised for those controls. Index adjustment uses raw prices without replacing the saved stock preference. Numeric edge cases also live in `chanapp/tests/test_indicators.js`. Donchian numbers come from the server (`chanapp/tests/test_donchian.py`); the page only draws them, and raw stock charts carry none.
