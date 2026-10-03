# Server v0.7.2 validation - October 1, 2026

- Full offline Python regression suite: **399 tests passed** in 74.289 seconds.
- Dashboard JavaScript syntax check passed.
- jsdom dashboard runtime check passed, including retained strictness when changing pace and strict-mode changes that preserve Balanced pace.
- Added 13 regression cases for saved protection in Balanced mode, conflicting intraday selection, restart persistence, retained protection during new experiments/configuration, interleaved working orders, later partial fills, expired session capacity, prior excess activity and delayed history/account/reservation operations.
- Existing halt, agent isolation, fresh quote/clock, broker-rejection, ambiguous submission and complete-history pagination regressions passed.
- Tests use mocked brokers/providers and temporary ledgers. They place no real broker orders and make no paid model calls. Existing temporary-server socket ResourceWarnings were non-failing.

The deployment helper requires this complete suite to pass, creates and verifies a full installation backup, preserves private files and the experiment ledger, and checks server version, protection, strictness and restored automation preferences. The source includes `tools/audit_day_trade_guard.py` for a separate read-only check against the actual accounts after deployment.

This verifies software controls, not profitability, future broker classification, live fill quality or settled-cash handling for other brokers. See PDT_RULES_REVIEW.md for the current FINRA/Alpaca rules and the voluntary guard's scope.
