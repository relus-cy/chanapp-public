# Search and watchlist

## Sub-features

Search keyboard selection, empty result and Escape; candidate/header/recent-view addition; removal; recent-view reopening; star ordering; tag Enter/blur save, Escape cancel, clear and reload persistence.

## How to get to it (user POV)

Open the page with the sidebar visible. Search `上证`; the candidate opens its chart, while its `加自选` button leaves the current chart selected. An untracked chart exposes header `加入自选`. Row actions appear on hover or keyboard focus. Tags are edited beside the watched stock's quote card.

## Driving it with e2e

```bash
npm run test:e2e -- chanapp/tests/e2e/watchlist.e2e.ts --output .e2e/watchlist-proof
```

Completion: all five tests pass. The displayed stock remains stable for add/remove actions; API reads confirm membership, ordering and literal tags; reload confirms persistence. Selectors and complete interactions are in the test file.

## Gotchas

The fixture restores a single watched sample before each test. Recent views are separately persisted; assert the relevant code and order rather than assuming an empty log. This is a real demo/API flow, with no intercepted business response.
