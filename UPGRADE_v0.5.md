# Upgrade to Agent Arena 0.5.0

This release can continue an existing experiment from v0.3 or either reviewed v0.4 variant. The two v0.4 packages use different database layouts despite having the same display version. Use the migration in this release; do not copy tables by hand or use a display version as proof of compatibility.

## Before replacing files

1. Record the current balances, positions, open orders, model charges, loss-limit policy, and automation state. Export the current audit if the old release supports it. Keep the old application files for rollback.
2. If you want the experiment to stay stopped through the upgrade, use **Halt automation** first. A server shutdown alone is not an operator halt: saved automatic operation can recover on startup when the restart preference allows it.
3. Stop the old server. Stop its Windows scheduled task or Linux service as well, so it cannot start again during the upgrade. Confirm that no process uses the data directory.
4. Back up the **complete data directory and `.env`** to a separate private folder. The data backup must include **both `arena.sqlite3` and `service.sqlite3`**, all SQLite `-wal`/`-shm` files that remain, the access token, and any `STOP` marker. Use the same point in time for the pair. Do not copy only `arena.sqlite3` or only a running SQLite main file.

After a clean server shutdown, a complete directory copy is the simplest backup. SQLite's backup API is another option for each database while the service remains stopped. A backup of the core ledger alone cannot restore scheduling, budgets, account bindings, recovery state, and operator controls stored in the service database.

## Install and inspect

1. Extract the new source into a separate folder. Copy your stopped installation's `.env` and complete `data/` directory into that folder, or keep using the same explicit `--data-dir` path after taking the backup. Do not use another experiment's database or a demo database.
2. Retain the existing `STOP` marker, access token, account credentials, and account bindings. Do not run `--init-access` to replace an existing login secret. Do not overwrite `.env` with `.env.example`.
3. Start only the new server. The core migration identifies the storage layout before changing it, makes a consistent SQLite backup (including committed WAL contents), and validates record counts and SHA-256 content manifests before committing storage schema 3. The backup is named `arena.sqlite3.pre-v05-<UTCtimestamp>-<8hex>.bak` beside the core database. The migration report records its path and the verified manifests. Source legacy tables remain in the database. A failed conversion rolls back and retains the backup. A failed or unknown migration must be investigated; do not bypass it by deleting tables or starting a new ledger against accounts that already hold positions.
4. Check the upgrade event, balances, orders, detailed research evidence, model charges, history, and loss-limit policy. Reconcile each account and compare it with the actual Alpaca paper account. An inconclusive lookup cannot establish that an uncertain order was rejected.
5. Check the restart preference. If you halted before the upgrade, resume only after reviewing the accounts. The restart preference cannot clear an operator halt, account-integrity hold, or risk stop.
6. Install `Agent_Arena_v0.5.0.apk` **over the existing APK**. It has the same application ID and signing certificate, with a higher version code. Do not uninstall the old app: uninstalling removes its local configuration. Back preserves the saved server address. A server restart requires a new login because it clears server sessions.

The automatic core backup supplements the complete backup in the first section. It does not replace the matching `service.sqlite3`, `.env`, token, or `STOP` backup.

## What settings carry forward

- Agent roster, model IDs and prices, allocation, strategy mode, position caps, and saved experiment rules remain in effect.
- New experiments exclude model costs from the trading-loss limit and still subtract them from net performance and target progress. Legacy fee-inclusive experiments keep that loss rule; experiments from our v0.4 layout retain its trading-only loss rule. The selected loss basis is explicit in v0.5. Model spending remains subject to its separate monthly budget in all cases.
- Raw audit observations are retained. The chart shows a bounded summary across the full experiment and preserves bucket extremes; the full export is the source for exact observations.
- A retained old price can support a visibly time-stamped valuation. It does not authorize a new order. Execution requires a fresh eligible price and successful account verification.
- Definite order rejections release their reservation. Uncertain submissions retain the original order ID and committed cash; they are never replayed automatically.

Migration preserves data that still exists. It cannot reconstruct history that a prior release already deleted, or automation intent that an earlier restart already overwrote. Use an older complete backup if you need to investigate such a loss; never overwrite the current broker ledger with it while automation is active.

Preserved local records are not proof of current broker order state. A fill, rejection, or cancellation may have occurred while the server was stopped. Reconcile the actual paper account before permitting new execution; migration does not clear uncertain submissions or repair unknown account activity.

## Roll back as a pair

Stop the new server and its restart mechanism. Preserve a separate copy of its current data for investigation. Restore the **old code and complete pre-upgrade data backup together**, including both database files, the matching sidecars if present, `.env`, the access token, and the original `STOP` state. Never combine an old core database with a newer service database or the reverse. Do not open a migrated database with the old code.

A rollback does not reverse orders placed at Alpaca after the backup. If any activity occurred after the backup, keep the experiment halted and reconcile that activity before allowing either release to trade. Do not overwrite those records and assume the broker account also rolled back.

The Android client can remain at v0.5 while you inspect server recovery. Do not uninstall it merely to attempt a downgrade. Preserve the private release signing identity for future updates; never put the key or password in the source package.

## First forward check

Use paper accounts. Verify the two account identities and allocations, then run one cycle. Test a pause during a slow request, a restart with a working order, and a short connection failure. Confirm that a newer Stop remains in effect and no uncertain order is replayed. Local tests are evidence for the tested failure paths; they do not prove multi-week uptime, account permissions, or a profitable strategy.
