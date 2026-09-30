> Historical response to the original audit. For the latest follow-up findings and corrections, see [AUDIT_RESPONSE_v0.5.1.md](AUDIT_RESPONSE_v0.5.1.md).

# Agent Arena v0.4 audit corrections

Date: 2026-09-25. Base: the saved v0.3 source package. Scope: paper trading, server, web client and Android client.

The reported failure paths were valid. This release changes recovery and storage, rather than just removing the errors that stopped execution. Software checks use simulated broker/model responses. No account connection, paid model call, paper order or live trade was made during this work.

## Findings and corrections

| Finding | Correction | Evidence and limits |
|---|---|---|
| Rejected submission becomes permanently unknown | A definite broker rejection creates a terminal rejected order and releases the cash reservation. Authentication rejection also pauses the affected account for attention. | Regression tests check the released reservation, later reconciliation and creation of another experiment. |
| Temporary read errors require manual resume | Typed temporary errors cause per-agent recovery. Read-only checks use a 15-second delay that grows to a 5-minute maximum. Successful reconciliation clears only a recoverable hold. | Tests cover an outage affecting both accounts and recovery without another submission. Operator, credential, inventory and integrity stops are not cleared. |
| Every restart disables enabled automation | Enabled paper automation retains its intent, enters startup recovery, and must pass fresh broker reconciliation before it can trade. | Tests cover restart, STOP, one or both deliberate agent pauses, trading-loss stops and a new halt during reconciliation. The host must also restart the Python process; use Task Scheduler or the supplied service example. |
| Model fees consume the trading-loss allowance | Trading equity and trading P&L are separate from net equity. The loss limit uses trading equity, including transaction fees. The monthly model budget controls research expenses. | Model fees still reduce reported net performance and progress toward the goal. Tests check the separate ledgers and repeated-charge protection after restart. |
| Sparse or stale IEX quotes cause permanent pauses | Invalid quote rows are omitted per symbol. Valuation retains the last valid mark and its timestamp. Automatic exits skip symbols without a fresh quote; other available symbols and research can continue. | Submission still requires a valid quote at most 120 seconds old. Missing quotes are never replaced with invented execution prices. Research evidence for an unavailable symbol cannot support a trade. |
| Every tick copies the full history | Separate SQLite audit rows retain historical evidence. The active state and routine snapshots are bounded. Migration from the old JSON record is atomic. | Scale and migration tests check full record counts, research evidence, archived data, old order lookup and model-charge idempotency. Full export intentionally costs more than a routine snapshot. |
| Reconciliation stops at 500 orders | Broker requests follow exclusive order-ID pagination. Routine reconciliation uses a saved cursor, current open orders and original-ID reads for pending orders. | Tests cover 1,501 orders at the same timestamp, repeated-page rejection, and updates to an order older than the saved cursor. No timestamp subtraction is used to skip a page boundary. |
| Android Back deletes the saved address | Back uses page history or Android navigation. Explicit Disconnect remains the action that clears the server and session. | The production Back handler passes 25 JVM checks. The regression harness also detects the previous behavior. The APK retains its original package and signing certificate. |
| Claude preset is outdated | Fresh experiments and the explicit two-agent preset use `claude-opus-5-5`, with $4 input / $20 output per million tokens. | Existing saved model IDs and prices remain unchanged. Reviewer mode also accepts non-action thinking blocks without storing them. Official docs and adapter fixtures were checked; actual API-account access was not tested. |
| Controls wait through a long research call | Slow commands have observable operation status. Conflicting commands return a prompt busy response and are not queued for later execution. Halt, agent pause and automatic-cycles-off remain responsive. | Real local HTTP tests cover a blocked model call, duplicate requests, conflict rejection, stop races, and redacted background errors. |

A further reconciliation race was corrected: a fill can arrive between the order-list read and the position read. When local orders are still pending, the service performs a bounded original-ID refresh before declaring a discrepancy. If broker data is still inconsistent, it blocks new execution and continues read recovery. A discrepancy that remains after orders become terminal requires investigation.

The final integrated run passed **227 Python tests**. Android passed **33 origin checks and 25 Back-navigation checks**. JavaScript syntax, a DOM runtime check, Python compilation, APK signing and the supplied systemd unit checks also passed. See `VALIDATION.md` for the exact scope.

## Two qualifications to the audit

The default model budget is $0. Therefore a model-fee halt around day 19 requires paid research to have been enabled, with particular usage and prices. It is not a result produced by untouched defaults. The underlying accounting issue was still real and is corrected.

An HTTP 4xx response is not always proof that a submission failed. Duplicate client IDs, ambiguous responses and some temporary statuses can refer to an order that already exists. These outcomes keep the original ID and cash reservation. A later 404 alone does not justify a replacement order or release of the reservation. The app never automatically replays such a submission.

## Storage experiment

The generated large fixture contained 120,000 history records, 8,000 decisions with detailed evidence, and 650 orders. This is a data-volume experiment, not elapsed operating time.

| Measurement | Smaller fixture | Large fixture |
|---|---:|---:|
| Active state size | 169,679 bytes | 169,882 bytes |
| Median quote update | 13.610 ms | 13.192 ms |
| Median snapshot | 4.403 ms | 4.519 ms |

The old large JSON record was 21,354,513 bytes. Migration took about 1.92 seconds on the test host. Full audit counts and detailed evidence survived migration. Each ordinary quote update wrote one audit record. Timing varies by machine, disk and workload. See `tools/benchmark_audit_storage.py` and `docs/validation_storage.json` for the fixture and measured results.

## Remaining limits

- An old v0.3 unknown order with no retained rejection evidence still needs broker confirmation or operator investigation. This release does not guess that it was rejected or modify that account's ledger without evidence.
- Startup requires both configured account ledgers to be verified. An unresolved order, missing credentials or account discrepancy can keep startup recovery pending. During normal operation, recoverable connection problems are isolated by agent.
- Read recovery does not retry paid model calls or uncertain order submissions. A later scheduled research cycle is separate work and still respects cost and daily limits.
- The process, internet connection and fresh quotes are still required for local exit rules. Sparse data can prevent an exit. Existing broker orders and positions remain when automation is halted.
- No new APK phone/emulator test or browser visual test was completed in this environment. Browser startup was blocked by the environment's socket restriction. Automated UI checks do not prove visual correctness.
- These checks do not prove several weeks of unattended operation, actual broker/API compatibility, profitability, tax accuracy or live-trading readiness. Those need separate forward paper tests.

## Upgrade

1. Stop the current server. Back up the complete `data/` directory and `.env` while it is stopped.
2. Replace application files with v0.4, retaining the existing private configuration and data.
3. Start one server instance. The ledger migration is automatic. Keep the original backup for rollback; older versions must not use the migrated database.
4. Install the updated APK over the old APK. Do not uninstall first. The signing identity is unchanged.
5. Inspect recovery status, account identities, balances and pending orders. Test a restart and deliberate stop in paper mode before leaving it unattended.

## Primary documentation checked

- [Alpaca order listing, 500-per-page limit and order-ID cursors](https://docs.alpaca.markets/us/reference/getallorders-1)
- [Alpaca order submission](https://docs.alpaca.markets/us/reference/postorder)
- [Alpaca latest quote data](https://docs.alpaca.markets/us/reference/stocklatestquotes-1)
- [Alpaca paper simulation limits](https://docs.alpaca.markets/us/docs/paper-trading)
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Claude Opus 5.5 overview](https://platform.claude.com/docs/en/models/opus-5-5/overview)
- [Claude Opus 5.5 migration](https://platform.claude.com/docs/en/models/opus-5-5/migration-guide)
- [GPT-6 Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra)
