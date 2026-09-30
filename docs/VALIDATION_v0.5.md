# Agent Arena v0.5 validation — 2026-09-25

These checks establish behavior under the tested software conditions. They do not establish several weeks of uptime, successful connections to your accounts, Android device behavior, or profitable trading.

## Android build checks

- **58 Android JVM checks passed**: 33 origin-policy checks and 25 Back-navigation regression checks. The Back test compiles the actual production handler and detects the prior configuration-clearing behavior.
- The APK compiled from the shipped Java source, dexed, aligned, and passed v2/v3 signature verification. Package `com.agentarena.mobile`, version `0.5.0`, version code `500`, minimum API 26, target API 35. INTERNET is its only requested permission.
- The signing certificate matches the earlier v0.3/v0.4 identity. The higher version code permits an in-place update. This is package verification, not an installation test on a phone.
- `android/build.py` passed Python compilation. `systemd-analyze verify deploy/agent-arena.service` passed.

## Integrated server and dashboard checks

**279 Python tests passed** in the integrated run (37.512 seconds, Python 3.12.14 on Linux). The suite covers accounting, migration, adapters, research, recovery, budgeted diagnostics, authentication, local HTTP, and control races. Three integration tests include **120 fault-injected cycles and 15 service restart simulations**; they check reservations, original order IDs, balances, account isolation, and durable stop behavior. These are accelerated mocked scenarios, not 120 real trading cycles or a measured period of continuous uptime.

Migration probes in `docs/validation_migrations_v05.json` using databases created by all three original engines passed: common v0.3, our v0.4, and Claude's v0.4. The probes checked backup payloads and SQLite integrity. The Claude fixture retained 3,100 identical event occurrences across active and archived experiments; content equality did not cause valid repeated events to be discarded. This validates preservation of available records, not recovery of data that an earlier release had already deleted.

Both dashboard DOM harnesses and the local HTTP checks passed. The UI probes check connection diagnostics, budget estimates, separate broker/data health, escaped results, restart opt-out during work, price freshness, exit retry status, loss policy, and the full-period chart. JavaScript syntax checks also passed. A rendered-browser check was attempted but browser startup failed with an `AF_UNIX` socket permission error (`EPERM`). No screenshot or rendered visual validation is claimed.

The dashboard DOM checks used Node.js with jsdom 30.1.1.

Reproducible commands:

```sh
python3 -m unittest discover -s tests -q
python3 android/tests/back_navigation_test.py
node --check arena/static/app.js
NODE_PATH=/path/to/jsdom/node_modules node tests/ui_runtime.cjs
NODE_PATH=/path/to/jsdom/node_modules node tests/test_v05_ui_runtime.cjs
```

The DOM harnesses need jsdom only in the test environment; the server has no JavaScript package dependency. Replace the example `NODE_PATH` with the parent `node_modules` directory that contains jsdom. To repeat the release migration probes, extract the three packages from the comparison evidence into `base/Agent_Arena`, `ours/Agent_Arena`, and `claude/Agent_Arena`, then run `python3 tools/validate_release_migrations.py --references /path/to/comparison`. This creates temporary test databases only.

The Android build additionally runs `android/tests/OriginPolicyTest.java`. Python tests use temporary databases and mocked broker/model responses. Local HTTP tests exercise a real local server without broker or model requests. No production credentials are required. No paid model requests, broker submissions, or live trades were used.

## Storage benchmark

The synthetic benchmark used two legacy datasets: 1,600 history rows with 320 full-evidence decisions, and 120,000 history rows with 8,000 decisions. Each also included 650 terminal orders, 350 events, and 150 model charges. Ten sequential mark/snapshot samples were taken after each migration. Both cases retained complete exported record counts and the older detailed evidence.

For the larger case, migration took 6.638 seconds, the active JSON record was 170,750 bytes, the median mark update was 16.818 ms, and the median local engine snapshot was 6.281 ms. These timings measure local synthetic data handling in this environment. They exclude network latency and do not establish long-running server performance. See `docs/validation_storage_v05.json` and `tools/benchmark_audit_storage.py` for the environment, method, and full results.

## Required regression boundaries

- Definite rejections release committed cash; unknown submissions keep the original ID and reservation. A broker 404 does not prove rejection. Recovery never sends a replacement order.
- ID pagination retains more than 500 orders and records sharing a timestamp. Pending original-ID lookups remain independent of the historical cursor.
- Newer Stop, Pause, and restart opt-out actions override in-flight older operations. An unverified account cannot make paid research calls or submit an order while its healthy peer recovers.
- Budgeted diagnostics respect the global and per-agent remaining monthly limits. Failed or lost paid responses retain their reserved cost estimate.
- Both reviewed v0.4 storage variants retain full evidence and audit records through migration. Failed validation must roll back; an unknown storage layout must fail clearly. Migration backups are retained.
- The full-period chart keeps intermediate extrema and raw history remains available in exports. Holding freshness includes stale and missing prices among current holdings.
- Research bars use split adjustment and disclose the actual feed. Malformed symbols do not poison healthy symbols. Trade-only, delayed, or stale marks cannot pass execution checks.
- Definite exit retries use a bounded delay; uncertain exits never enter an order-replacement path. Operator and authentication holds remain effective.
- Existing remote authentication, CSRF, Host, origin, and secret-redaction protections remain in force.

## Build evidence

APK: `Agent_Arena_v0.5.0.apk`, 33,417 bytes.

APK SHA-256:

`f83ab48a95ec81528ae7addfd89d5f0c77053cefe3215bb94643a10a0c80327d`

Signing certificate SHA-256, unchanged from v0.3/v0.4:

`42622eac5e79501e3b6b06bb365706680a6faa9405b63d7a20fe55aca3a2f77c`

See `android/BUILD_VERIFICATION.txt` for package and signature output. The private signing identity is excluded from the source and APK deliverables.

## Limits

The APK has not been installed on a physical Android phone or emulator during this release. JVM navigation checks do not verify Android lifecycle dispatch or cookie storage on a phone. The successful DOM checks do not establish rendered layout or physical-phone behavior; the blocked browser launch prevented screenshot validation.

Windows Task Scheduler, Tailscale/Caddy deployment, TLS certificates, and actual broker/model account access remain untested for this release. No server was deployed. Feed entitlement, provider billing, and current account behavior need account-level checks.

Synthetic data-volume and fault tests are not a multi-week run. Historical v0.3 orders that lost their original rejection evidence cannot be safely cleared from a 404 alone. Local exit rules still depend on the running server, network, enabled automation, and fresh eligible prices. Corporate-action repair, taxes, live-money execution, and profitability are not established.

## Forward paper check after upgrade

Follow `UPGRADE_v0.5.md`. Back up the complete stopped installation, including both databases, then install v0.5 and inspect migration output and the saved loss policy. Compare orders, positions, balances, and client IDs with Alpaca. Test a deliberate pause, a restart with a working order, and recovery after a short connection outage. Confirm that no uncertain order is replayed and that new stops remain effective. Collect forward observations before judging trading performance.
