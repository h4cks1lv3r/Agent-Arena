# Agent Arena 0.5.1 validation — 2026-09-25

**334 Python tests passed**, with zero failures, errors or skips, in 61.479 seconds (Python 3.12.14). **58 Android JVM checks** and **three dashboard DOM harnesses** also passed. Tests used mocked broker and model responses; local HTTP tests ran a real loopback server.

## What the patch tests establish

- Documented POST 403 refusals release reservations and do not create authentication pauses. Protected read failures retain their handling.
- Closing-window and clock-close races defer execution without a permanent pause. The next eligible-session regression sends one exit and reconciles the fill.
- Admitted background jobs wait through monitor contention, run once, and cancel after a newer stop, experiment change or shutdown. Conflicting operations remain bounded. HTTP accepts slow work promptly.
- A newer global stop, agent pause or external STOP file survives the admission-to-dispatch handoff. Admission state does not leak across threads or later requests. Local cancellation before sending releases only a proven-unsent reservation or unused paid-call reservation.
- Unknown-order recovery requires an explicit broker-confirmation attestation, exact identities, a paused account and consistent fresh reads. Failed reads, changed positions/cash, a discovered order, newer stops, and disk failure prevent clearance. The audit record and reservation release commit together; successful recovery leaves the agent paused.
- The new recovery HTTP route rejects missing CSRF and accepts the exact valid payload asynchronously. Its browser form requires the attestation, binds the original experiment/client ID, preserves Halt availability, and clears stale or completed form state.
- Existing migration, accounting, isolated restart, fill-race, pagination, budget, quote-freshness, authentication and full-period chart regressions continue to pass. The suite retains the 120 mocked fault cycles and 15 service-close/reopen simulations; these are not OS reboot or uptime measurements.

The full Python log and machine-readable result are in `docs/validation_python_v051.log` and `docs/validation_python_v051.json`. Independent before/after results are in `docs/audit_repro_v050.json` and `docs/audit_repro_v051.json`; the probe is `tools/audit_repro_v051.py`.

## Reproduce

```sh
python3 -m unittest discover -s tests -q
python3 tools/audit_repro_v051.py . --output audit-repro.json
node --check arena/static/app.js
NODE_PATH=/path/to/jsdom/node_modules node tests/ui_runtime.cjs
NODE_PATH=/path/to/jsdom/node_modules node tests/test_v05_ui_runtime.cjs
NODE_PATH=/path/to/jsdom/node_modules node tests/test_v051_ui_runtime.cjs
python3 android/tests/back_navigation_test.py
```

DOM checks used Node.js 24.19.0 and jsdom 30.1.1. jsdom is only a test dependency. The Python server has no third-party package dependency. Python compilation and JavaScript syntax checks passed. The Android build also runs the 33-case `OriginPolicyTest.java`; the Back-navigation test has 25 checks.

## APK

`Agent_Arena_v0.5.1.apk`: package `com.agentarena.mobile`, version 0.5.1, code 501, minimum API 26, target API 35, 33,417 bytes. It compiled from the shipped Java source, dexed, aligned and passed v2/v3 signature verification. INTERNET is the only requested permission.

APK SHA-256:

`3c4160b4326295f8c950e19e521222a4a0cf3ff73c71657d7a1deb4e53b3f01a`

Unchanged signing-certificate SHA-256:

`42622eac5e79501e3b6b06bb365706680a6faa9405b63d7a20fe55aca3a2f77c`

See `android/BUILD_VERIFICATION.txt`. Private signing keys and credentials are excluded from the source package. Package verification supports an in-place update; no physical-phone or emulator installation was performed.

## Limits and retained evidence

No broker/model account was accessed, no paid request or broker order was sent, and no server was deployed. Tests do not establish multi-week reliability or strategy profitability. Paper execution differs from live execution. Local exit rules require the server, enabled automation, network access and fresh eligible execution prices; a deferral can leave a position open overnight.

DOM tests validate behavior and markup, not rendered screenshots or phone layout. A rendered-browser check for 0.5 was blocked by an environment socket permission error; no new rendered visual check is claimed here. Windows startup scheduling, TLS deployment and physical Android lifecycle behavior remain untested.

Historical release migration probes and storage benchmarks are retained in `docs/VALIDATION_v0.5.md` and the matching `docs/validation_*_v05` records. They are prior 0.5 measurements, not newly repeated 0.5.1 benchmarks. This patch does not change the storage schema or chart algorithm; the existing migration/chart regressions were included in the 334-test run.

Follow `UPGRADE_v0.5.1.md`: stop and back up the complete installation (both databases), update server and APK, then compare the actual paper accounts before resuming. Collect forward paper observations before making any claim about returns.
