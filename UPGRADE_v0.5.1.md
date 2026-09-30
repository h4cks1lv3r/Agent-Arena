# Upgrade to Agent Arena 0.5.1

Version 0.5.1 addresses the remaining order-classification and control-scheduling defects identified in the audit. Its Android package installs over our earlier releases with the same application ID and signing identity. The agents still run on your server; installing the APK alone does not update the server.

## Back up, install, and verify

1. Record the experiment's balances, positions, open orders, loss-limit policy, and automation settings. If it should remain stopped during the upgrade, use **Halt automation** before shutting down. Stopping the server alone does not cancel saved automation intent.
2. Stop the old server and its Windows scheduled task or Linux service. Confirm that no process is using its data directory.
3. Back up the **complete data directory and `.env`** privately. Retain both `arena.sqlite3` and `service.sqlite3`, any remaining SQLite `-wal`/`-shm` sidecars, the access token, and the `STOP` marker. Keep the old source as well. A single database file is not a complete backup.
4. Extract v0.5.1 into a separate folder. Copy the stopped installation's `.env` and complete data directory there, or continue using the same explicit `--data-dir` after making the backup. Do not replace credentials, account bindings, or the existing access token with example files. Do not run `--init-access` to replace an existing login secret.
5. Start only the new server and sign in again. Inspect the version, account identities, balances, positions, unresolved orders, automation status, and loss-limit policy. Reconcile each account against its actual Alpaca paper account. An operator halt or account discrepancy still requires attention; a server update does not clear it.
6. Install `Agent_Arena_v0.5.1.apk` over the existing app. Its version code is `501`; its signing certificate matches our v0.3, v0.4, and v0.5.0 packages. Keep the app installed to preserve its saved server address. A server restart ends the login session, so sign in again if prompted.
7. After reviewing the accounts, resume the intended agents and automation. Check recent operations and the next automatic cycle. Closing or disconnecting the phone app does not stop server automation.

If upgrading from v0.3 or either reviewed v0.4 layout, also read [UPGRADE_v0.5.md](UPGRADE_v0.5.md). The layout detection, verified history migration, and paired rollback instructions still apply. Automatic backups of the core database supplement the full backup above; they do not replace its matching service database, credentials, token, or stop state.

## Behavior corrected in this patch

- An order submission rejected with HTTP 403 is treated as a broker rejection and releases that order's reservation. Read requests that fail authentication still require attention.
- A local exit blocked by the closing window is deferred without turning an ordinary session boundary into a permanent integrity pause. The position remains held until an eligible execution opportunity. Local exit checks do not become broker-hosted protective orders.
- An admitted background operation waits for routine monitoring to release the service lock. Its status remains visible in recent operations. A conflicting new control can still receive a busy response. A newer stop or shutdown cancels obsolete waiting work.
- An uncertain order has an explicit operator recovery path after the broker has confirmed it was not accepted. It is never resolved merely because ten minutes elapsed or because a lookup returned 404.

Independent account recovery after restart, full-period chart summaries, valuation-source labeling, history migration, and exact order-ID pagination were already included in v0.5.0. This patch retains those changes.

## An order remains unknown

An uncertain submission may have reached Alpaca even if its request timed out. Keep its reservation intact while investigating. Check the original client order ID in the broker dashboard and obtain broker confirmation before treating it as not accepted. Do not submit a replacement under a different ID to test whether the first order exists.

Open **Activity**, locate the unknown order, and choose **Record broker confirmation**. Verify its client order ID. Enter the support reference, date and confirmation details, select the attestation checkbox, then choose **Verify and record broker confirmation**. Do not include credentials.

The operator resolution control requires the original order identity and a record of the broker's confirmation. It performs fresh broker checks before accepting the resolution. A network failure or contradictory broker state prevents clearance. If the broker reports the order, reconcile that actual order instead. A successful manual resolution is recorded in the audit; it does not authorize an automatic replay or override a deliberate halt. Review the account and use the normal resume control when appropriate.

Never delete an unknown order or edit either database by hand to bypass the reservation. Elapsed time and repeated 404 responses are evidence of an unresolved lookup, not proof that the broker rejected a submission.

## Rollback and validation limits

To roll back, stop the new server and restart mechanism, preserve a separate copy of its current data, then restore the **old code and complete pre-upgrade data backup together**. Do not mix database versions. Broker activity after the backup is not undone by a local rollback; keep automation halted until that activity is reconciled.

The release remains paper-only. The APK was compiled, signed, alignment-checked, and signature-verified with 58 passing JVM checks. Those checks do not establish a successful installation on your phone, provider access, multi-week uptime, or strategy profitability. See `VALIDATION.md` for the server regression evidence and its limits.
