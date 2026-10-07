# Period preferences

## Sub-features

Cancel/save/empty selection, revision conflicts, old minute links, first use, provider capability changes, cross-page synchronization, restart and AI analysis period combinations.

## How to get to it (user POV)

Open `展示周期` in the header. Select periods and save, or cancel to retain the prior selection. New real-mode instances may show a first-use dialog; another page can change the same instance's preferences.

## Driving it with e2e

```bash
npm run test:e2e -- chanapp/tests/e2e/periods.e2e.ts --output .e2e/periods-proof
npm run test:e2e -- chanapp/tests/e2e/existing-flows.e2e.ts --output .e2e/periods-extended-proof
```

Completion: both runs pass. Visible tabs agree with saved preferences; cancellation preserves the previous selection; stale edits require confirmation instead of overwriting another save. The extended test observes collection, AI combinations, synchronization and restart through the real application.

## Gotchas

The first command uses real demo data. The extended period server supplies provider and LLM fixtures at external boundaries; it does not prove supplier or model availability. Its neighboring frontend-fixture test has a narrower scope, described in [offline and recovery](offline-and-recovery.md).
