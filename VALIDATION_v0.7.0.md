# Agent Arena server v0.7.0 validation

Checked on Windows on October 1, 2026.

- `python -B -m unittest discover -s tests`: 371 tests passed. Existing tests emitted non-failing unclosed-socket ResourceWarnings.
- `node --check arena/static/app.js`: passed.
- `tests/test_v051_ui_runtime.cjs` with temporary jsdom and the bundled Python: passed. The check exercises authenticated mutations, background work, persistent halt availability and unresolved-order recovery.
- Added checks cover one-minute research data, live profile changes without ledger reset, fresh buying-power/restriction/spread gates before reservation, shorter holding limits, stop/profit thresholds, early session-close exits, halt/pause/automation boundaries, and preservation of actual minute-bar prices when a large model input is condensed.
- Read-only checks of the existing dedicated paper accounts found both ACTIVE in USD, with cash-only buying power (multiplier 1). No live-money routes or leverage were enabled.

The offline suite mocks broker/model calls; it does not establish profitable performance or legal certification. Market/network delays and paper broker behavior remain execution limitations. No paid model diagnostics were sent during validation.

See AGGRESSIVE_INTRADAY.md for the current Alpaca rule sources and the activation behavior for existing positions.
