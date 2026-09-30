# Agent Arena v0.6.0 verification

## Local checks

- The full Python regression suite passed **338 tests** (`python3 -m unittest discover -s tests -q`) after the 24/7 and crypto changes.
- A later, separate focused run passed **5 crypto tests**, including one additional check that a stocks-only experiment rejects crypto registration. This final added test was not part of the earlier 338-test full run.
- `node --check arena/static/app.js` passed JavaScript syntax validation.
- The optional jsdom-based dashboard runtime test could not run here because `jsdom` is not installed. The Python suite includes existing dashboard/API tests, but a real browser/Android session has not been verified.

The new tests use mocked paper quotes, GTC crypto order payloads, a weekend broker clock, deferred stock ideas, a held stock lacking a fresh overnight quote, and an explicit fee activity applied once across two reconciliations. They use no paid model keys or actual Alpaca credentials.

## Checks still needed on the user's setup

1. Confirm both Alpaca paper accounts provide crypto data and are permitted to trade the selected USD pairs. Verify the actual paper fee activity and inventory behavior on a small controlled paper order.
2. Resolve the failed Claude paid Messages diagnostic, then verify both models' paid diagnostics and their shared monthly budget before enabling automation.
3. Keep the host awake and observe the scheduled paper runs, fee posting, inventory reconciliation, and stock-session transitions over multiple days. No profit or 24/7 uptime claim follows from the local tests.

The previous v0.5.1 `VALIDATION.md` is retained as historical evidence of that release; this file describes the new changes.
