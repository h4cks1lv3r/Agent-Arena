# Windows validation for v0.6.1

- Python 3.12.14: all **355 tests passed** with temporary ledgers and mocked providers.
- Dashboard broker-confirmation recovery check: passed with jsdom. The test verifies attestation, exact order identity, CSRF, asynchronous controls, responsive halt, and stale form cleanup.
- Dashboard JavaScript syntax check: passed.
- Windows updater: parsed successfully and executed. The complete old installation was copied and every backup file was checked by SHA-256 before replacing source.
- The existing `.env`, access token and entire data folder were verified unchanged by the source replacement.
- The updated server authenticated successfully at version **0.6.1**. Both existing paper accounts passed fresh reconciliation with five positions retained and zero working orders.
- Automation and automatic restart recovery remain disabled for operator review. No model call or new trading order was issued by this update.

This Windows validation adds two regression checks to the earlier 353-test suite: manual crypto closing outside stock hours and reconciliation of a small crypto holding. SQLite test fixtures close explicitly so Windows can remove temporary ledgers.

Android source includes version 0.6.1, code 601. The original signed APK was built and checked in the earlier Agent Arena Handoff task. It has not been rebuilt, transferred or tested on an Android device in this Windows session.
