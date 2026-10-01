# Agent Arena server 0.7.0

- Add an in-place aggressive intraday profile: one-minute AI reviews, completed minute-bar research, local exit checks about every five seconds, a 30-minute holding cap, and stock exits requested ten minutes before the broker close.
- Keep existing allocation, loss/exposure caps, credentials, audit history and paid-model budget. Existing positions receive the short holding cap when the profile is enabled.
- Read fresh broker cash, buying power and account restrictions before every intraday entry; reject wide spreads and entries near close. Existing halt, pause, freshness, reconciliation and order-recovery controls still apply.
- Add a dashboard profile switch and a Windows upgrade helper with a verified full backup. Existing Android clients can use the updated dashboard; this release does not rebuild the APK.
- Validation: 371 offline Python tests, dashboard JavaScript syntax and the jsdom recovery/control runtime check passed. Read AGGRESSIVE_INTRADAY.md and VALIDATION_v0.7.0.md.

# Agent Arena 0.6.1

- Combine the v0.5.2 complete-history cursor fallback with 24/7 USD crypto paper trading and after-hours stock research.
- Preserve the existing experiment asset scope, positions, credentials and complete data folder.
- Use the Android 0.6.1 version metadata (601).
- Close test-only SQLite fixture connections explicitly so the regression suite can clean up temporary ledgers on Windows.
- Allow manual USD crypto closes while the stock market is closed, and reconcile crypto quantities at the ledger's existing precision.

# Agent Arena 0.5.1

- Treat a definitive POST-order HTTP 403 refusal as a rejected order; retain authentication handling for HTTP 401 and protected read failures.
- Defer closed-market, stale-clock and closing-window execution without creating an integrity pause. Recheck time guards before submission; keep the position until an eligible exit can execute.
- Allow the single admitted background command to wait up to five minutes for monitoring. Keep HTTP acceptance and emergency stops responsive; cancel older work after a newer stop, experiment change, or shutdown.
- Add operator-only recovery for unknown submissions after explicit broker confirmation that the original order was never accepted. Fresh account/order/position checks precede an atomic audit record and reservation release. The agent remains paused for manual review and resume.
- Preserve the v0.5 independent-account restart recovery, strict execution freshness, full-period chart, history migration, fill-race refresh, and ID-cursor paging.
- Rebuild the Android client as 0.5.1, code 501, with the original signing certificate.

Read `AUDIT_RESPONSE_v0.5.1.md`, `VALIDATION.md`, and `UPGRADE_v0.5.1.md`. Update both server and Android client. This is still a paper-only research prototype.

---

# Agent Arena 0.5.0

This release combines the tested order, recovery, storage, and control paths from our v0.4 with selected improvements from Claude's v0.4. It remains paper-only. No live account route or profitable strategy is included.

## Changes

- Recognize and migrate v0.3 and both reviewed v0.4 storage layouts. Import Claude's separately stored full decision evidence and archived events, retain raw history, and validate the result before committing. Preserve an automatic pre-migration core backup. See `UPGRADE_v0.5.md` for the required complete backup and rollback procedure.
- Preserve the original loss-limit basis for migrated experiments. New experiments separate the trading-loss allowance from model expenses. Net results and target progress still deduct model costs, and paid activity remains subject to the monthly model budget.
- Add an explicit restart preference to durable recovery. Each account must pass verification before its agent can act. Newer stops, pauses, and restart opt-outs override older operations.
- Add visible retry delays for definite exit rejections. An uncertain outcome keeps its reservation and original client order ID; no replacement order is sent.
- Use split-adjusted completed-day history with recorded source/feed metadata and an access-denied fallback. Add snapshot/trade valuation marks and an explicit delayed-data fallback while keeping strict fresh-quote requirements for orders.
- Make the model output limit configurable, retaining the 8,192-token default. Record the actual returned model ID when the provider supplies it.
- Add separate broker diagnostics and paid model diagnostics. Paid checks reserve and settle costs through the normal budget path and run as background operations.
- Show the full experiment period in the chart with bucket extremes while keeping every raw observation. Mark holding freshness by the oldest or missing holding price. Disconnected synthetic AI agents report hold.
- Correct deployment guidance and rebuild the Android wrapper as version 0.5.0, code 500. Back still preserves the server address; explicit Disconnect clears it. Package identity and signing certificate remain unchanged.

## Behaviors retained

Definite rejections release reservations. Timeouts, duplicate client IDs, malformed acknowledgments, and inconclusive 404 lookups do not establish rejection and never cause automatic replay. ID-based pagination retains orders at shared timestamp boundaries. Per-agent recovery uses bounded read retries. Stop/Pause controls stay available during slow work. Full evidence and audit records remain separate from the bounded routine state.

## Upgrade

Stop the old server and its restart mechanism. Back up the complete data directory and `.env`, including **both `arena.sqlite3` and `service.sqlite3`**, any remaining SQLite sidecars, the login token, and `STOP`. Retain the old code. Do not combine old and new database files when rolling back. An automatic core backup alone is not a complete service backup. Read `UPGRADE_v0.5.md` before opening an existing experiment.

Migration preserves the original rows and evidence that still exist in the database. It cannot recover history that Claude's earlier release already thinned or deleted, or automation intent that a prior restart already overwrote. Local migration also cannot establish the current broker order state: reconcile the real paper account before permitting execution. No recovery of previously lost data is claimed.

Install `Agent_Arena_v0.5.0.apk` over the old app; do not uninstall it. The APK remains a client of an always-on HTTPS server, and closing it does not stop server automation.

See `VALIDATION.md` for actual checks and limits. Offline tests and an APK signature check do not establish multi-week uptime, device behavior, account access, or trading profitability.

## Previous release: 0.4.0

Audit corrections for unattended paper operation:

- Separate definite broker rejections from uncertain submissions. Release reservations only when the rejection is established; never replay an uncertain order.
- Recover temporary account read failures with per-agent backoff. Preserve deliberate stops and account-integrity holds.
- Restore previously enabled automation after restart and successful startup reconciliation.
- Separate trading-loss limits from model expenses. Continue to deduct expenses from net results and enforce the model budget.
- Tolerate missing quote rows and stale holding prices without permanent pauses. Require fresh prices for execution.
- Migrate historical records to incremental SQLite audit storage with bounded routine snapshots.
- Remove the 500-order ceiling with ID pagination and incremental reconciliation.
- Return operation status for slow controls and reject conflicting commands promptly. Keep halt and pause responsive.
- Preserve the Android server address on Back. Explicit Disconnect still clears it.
- Use Opus 5.5 and $4/$20 rates for fresh Claude presets; retain saved configurations.

Back up the stopped server's complete data directory before upgrading. Database migration is automatic and requires the pre-upgrade backup for rollback to an older version. The APK retains its original package and signing certificate.

See `AUDIT_CORRECTIONS.md` and `VALIDATION.md` for evidence and remaining limits. These tests do not establish multi-week uptime or profitability.

## Previous release: 0.3.0

A two-agent paper competition with Android and web monitoring.

- Fresh experiments: Astra and Claude, $250 each from the $500 total.
- Independent strategy plans, research records, holdings, budgets and persistent agent pauses.
- Side-by-side mobile scorecards, per-agent strategy detail and time-stamped account/price data.
- Background monitoring during model work, asynchronous cycles and responsive stop controls.
- Private HTTPS access with access-token login, secure session cookies and CSRF checks.
- Signed Android client with a saved server address. Broker and model keys stay on the server.
- Windows/Tailscale and Linux/Caddy setup examples.

The default combined loss limit and target still apply to both agents. Ordinary account errors are isolated, but both agents use one server process. The Android client does not provide hosting or live-money trading.

Back up your data and stop the old server before replacing its source. Existing experiments keep their saved settings. Close and reconcile old paper holdings/orders before creating a new experiment with the Astra vs Claude preset.

See VALIDATION.md for verified checks and untested conditions. The separate signing backup must remain private and is needed to sign future APK updates with the same identity.
