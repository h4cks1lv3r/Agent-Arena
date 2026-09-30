# Response to the follow-up audit — Agent Arena 0.5.1

The audit describes our **0.4.0 package with 227 tests and Android version code 400**. Our intervening combined **0.5.0 release had 279 tests and version code 500**. That matters for the restart, chart and data-fallback findings. It does not excuse defects that remained: independent mocked reproductions confirmed the 403 pause, closing-window pause and monitor-lock collision in 0.5.0. The missing operator recovery path was also a real usability gap.

This patch corrects those remaining defects. There is no meaningful model “winner” established by this review. The earlier shared Claude source was available for comparison; the newly claimed repaired Claude build was not supplied with this message, so its current behavior has not been independently verified here.

## Findings and disposition

| Finding | Evidence against 0.5.0 | 0.5.1 behavior |
|---|---|---|
| Definitive POST-order HTTP403 pauses agent | Reproduced: reservation released, but integrity pause survived five healthy monitors. | Treat as an order refusal, release reservation, keep agent usable; automatic rejected exits retain bounded backoff. HTTP 401 and authentication failures on read endpoints remain protected. |
| Exit in last 90 seconds permanently pauses | Reproduced: position stayed held and agent stayed paused into the next session. | Record a temporary execution deferral. Keep all market/time/price gates. In the same scenario, agent stayed usable and sold once in the next eligible session. |
| Market closes between clock reads | Reproduced: no order sent, but agent was integrity-paused. | No order sent and no integrity pause. The final gate is rechecked before POST; a reservation proven unsent is released. |
| Monitor lock makes admitted jobs fail “busy” | Reproduced: queued scheduled cycle became error without running. | One admitted background job waits up to 300 seconds; HTTP acceptance stays prompt. Reproduction waited queued, then completed exactly once. Conflicting new operations can still be rejected deliberately. |
| Timeout followed404 has no operator resolution | Reservation deliberately retained, but no suitable recovery control existed. | Activity now offers **Record broker confirmation** for an eligible unknown order. Explicit broker-confirmation attestation plus fresh checks precede atomic audit/ledger changes; agent stays manually paused. No timer-based release or order replay. |
| A bad account blocks healthy restart recovery | Already corrected in 0.5. Reproduction kept the bad peer paused while the healthy peer completed a model turn. | Preserved. |
| First chart point plus latest511 drops intervening performance | Already corrected in 0.5: a bounded full-period summary retains extrema; raw history is retained. The 5,001-point regression includes interior extrema for both agents. | Preserved. |
| Missing/stale IEX book has no fallback | Already improved in 0.5 with quote/trade snapshots and labeled delayed-SIP valuation fallback. | Preserved, with a deliberate distinction: valuation fallback cannot authorize execution. A fresh eligible quote is still required. |
| Fill race, exact order pagination, large database migration | Already retained or improved in 0.5. | Preserved, with existing regression coverage. |
| Model costs and trading loss limit | 0.5 exposes an explicit saved policy. New experiments exclude model costs from the trading-loss halt; each existing experiment preserves its established basis. | Preserved. An upgrade does not silently change an existing mandate. |
| Android Back and installable signed APK | Already corrected. | Rebuilt0.5.1/code 501, same application identity and signing certificate. |

The audit's estimated 7–25% real-world collision rate was not measured here. The forced-collision test establishes that the defect existed and tests the corrected response; it does not estimate its field frequency.

## Why a ten-minute404 rule is not adopted

[Alpaca's order-creation reference](https://docs.alpaca.markets/us/reference/postorder) explicitly lists403 for insufficient buying power or shares. The old service conflated that refusal with authentication failure. This patch corrects that classification without weakening protected read failures.

[Alpaca's order-timeout FAQ](https://docs.alpaca.markets/us/docs/working-with-orders) says a timed-out submission may already have reached the market and directs the operator to obtain confirmation from support or the trading team before resending or marking it canceled. Elapsed time and a missing lookup do not provide that confirmation. The patch therefore keeps the original client ID and its reservation until actual order evidence or the explicit operator workflow resolves it. Official pages checked 2026-09-25.

The operator workflow records the support reference, details and an explicit attestation that the exact original order was never accepted. The app cannot independently authenticate a support conversation; the record labels this as an operator attestation. It requires the same experiment, bound account and unknown unacknowledged order; a paused agent; no other pending orders for that agent; two original-client-ID 404 results; and consistent authenticated account, order-inventory, position and cash checks. Contradictions or read failures retain the reservation. The audit record and rejection/release commit in one transaction. A later Resume requires fresh reconciliation. The AI cannot invoke this route through its trading tools.

This deliberately does not clear an acknowledged order that temporarily disappears from a lookup. Reconcile an order the broker can identify; do not reinterpret it as never accepted.

## Additional stop-race correction

Independent review found that validating a queued command only before dispatch still left a small gap: a newer stop could arrive before the service captured its initial generation, and an older Resume could then clear it. The runner now carries its original admission identity through dispatch. Resume/enable commits and execution boundaries check that identity without holding the stop lock across network calls. Tests inject global stop, agent stop and an external STOP file at that exact handoff. A local cancellation before submission releases only a proven-unsent reservation; a timeout after submission remains unknown.

## Evidence and practical limits

`docs/audit_repro_v050.json` and `docs/audit_repro_v051.json` record the independent before/after scenarios. Run the included probe from the source root:

```sh
python3 tools/audit_repro_v051.py . --output audit-repro.json
python3 -m unittest discover -s tests -q
```

See `VALIDATION.md` for final release test counts and reproducible UI/build commands. All broker/model responses in regression tests are mocked. No actual brokerage login, model charge, broker order, deployment, physical-phone installation or multi-week run was performed.

A closing-window deferral can leave a position open overnight. A missing fresh execution quote can postpone an exit even when a delayed valuation mark exists. These remain visible execution limitations; this patch does not promise continuous liquidity, a maximum realized loss or profitability. Follow `UPGRADE_v0.5.1.md` to update the server and APK, then collect forward paper observations.
