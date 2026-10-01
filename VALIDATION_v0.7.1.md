# Server v0.7.1 validation

Checked on Windows on October 1, 2026.

- 386 offline Python tests passed, including 15 new overnight-trading acceptance checks. Existing non-failing socket ResourceWarnings remain in legacy fixtures.
- Dashboard JavaScript syntax and the jsdom control/recovery runtime check passed.
- New cases verify same-day routine and manual exits are blocked before reservation, later-session exits need no model calls, no 30-minute or closing-time liquidation, bounded protective stops, the strict stop block, complete history timing, pending/partial-order capacity, actual broker-session windows, New York date boundaries, no same-day re-entry and cancellation after a newer halt.
- The upgrade helper makes and verifies a full backup, preserves private configuration and the ledger, and restores the previously enabled automation only after account checks. Risk boundaries and model budget are checked after the update.

Tests mock broker/model APIs and establish operational behavior, not profitable performance, legal certification or guaranteed broker classification. No paid provider diagnostics were sent. See SHORT_SWING.md for policy limits and current primary rule sources.
