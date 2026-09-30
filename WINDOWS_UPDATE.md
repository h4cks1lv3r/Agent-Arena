# Agent Arena v0.6.1 on Windows

The server changes were recovered from the recorded source diffs in Agent Arena Handoff and TRADING BOT. This local package also fixes the manual USD crypto close control and the reconciliation threshold for small crypto balances, and closes test-only SQLite connections explicitly for Windows cleanup. Its Python suite uses temporary ledgers and mocked providers. It does not use the configured paid API keys.

The update backs up the stopped installation, preserves `.env`, the access token, and the entire `data` folder, then starts the updated server at the existing HTTPS origin. Automatic cycles and automatic restart recovery are disabled during the update. Existing broker positions and orders are preserved.

After the server is updated:

1. Open your existing HTTPS dashboard address and use the existing access token if prompted. Confirm server version 0.6.1. The address is preserved in the private `data/public-origin.txt` file and reused by `Start_Agent_Arena.bat`.
2. Reconcile both paper accounts. Leave each agent paused until its account check succeeds and any discrepancy is resolved.
3. To continue the current stocks-only experiment, resume each agent after reviewing the ledger and run one manual paper cycle before enabling automatic cycles.
4. To enable crypto, first finish and reconcile existing positions and working orders. In Experiment setup choose **US stocks, ETFs and USD crypto**, then **Archive & start new experiment**. Preserve the archive.
5. Check connections, resume entries and inspect one manual paper cycle before enabling automatic cycles and restart recovery.

The existing experiment is stocks-only. Updating source does not change that saved scope. Creating a new crypto experiment while the current experiment still has positions is intentionally blocked by the application.

The signed APK from the original handoff has SHA-256 `48db099deaec0573d91392df168c81755b2dff2841bd30024a3c3095cde84a19`. Install it over the existing Android app when available; no uninstall is needed.
