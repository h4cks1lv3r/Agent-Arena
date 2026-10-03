# Trading rules review - October 1, 2026

This review covers the existing Astra and Claude Alpaca **paper** accounts and the Agent Arena server. The user requested active trading while avoiding routine day trading and a PDT flag.

## Applicable rules

| Framework | Finding and source | Application here |
| --- | --- | --- |
| FINRA replacement | Rule 4210 changed effective June 4, 2026. Firms may phase in through October 20, 2027. The replacement removes the PDT trade-count designation and $25,000 minimum and requires margin equity appropriate to intraday exposure. [Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10) | Confirm the specific broker's adoption rather than assuming every firm has changed. |
| Alpaca implementation | Alpaca states its Trading and Broker APIs adopted the replacement on June 4. It removed PDT restrictions and retired the old PDT fields; current `buying_power` and broker pre-trade checks apply. [Implementation notice](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/) | The integration uses current buying power, never retired PDT fields for admission decisions. |
| Legacy PDT test | The removed rule used four or more day trades in five business days, subject to a 6%-of-total-trades exception. Brokers could also designate based on reasonable belief. Overnight long positions sold before a new same-symbol purchase were excluded. [Deleted rule text in Attachment A](https://www.finra.org/sites/default/files/2026-04/Regulatory-Notice-26-10-Attachment-A.pdf) | The voluntary guard stays below four, avoids percentage-based exceptions, and disallows same-day re-entry. It cannot erase earlier trades or determine another broker's classification. |
| Funding and settlement | A margin-enabled account can operate without borrowing; using one's own cash does not establish a cash account. [Alpaca non-leverage margin FAQ](https://docs.alpaca.markets/us/docs/understanding-the-new-intraday-margin-rule) | Multiplier 1 is not evidence of cash-account classification. Agent Arena only funds long entries from available broker cash and buying power. |
| Cash accounts | Most equity settlement is T+1. Cash-account freeriding and good-faith violations concern payment and unsettled proceeds. [FINRA investor guide](https://www.finra.org/investors/insights/frequent-intraday-trading) | Do not claim that this Alpaca paper implementation verifies settled cash or supports an arbitrary live cash broker. |
| Paper environment | Alpaca describes paper trading as simulated execution; live results can differ. [Paper specification](https://docs.alpaca.markets/us/docs/paper-trading) | No live broker endpoint or live-money compliance certification is provided. |

## Enforced controls in v0.7.2

- Routine stock exits wait until after the New York purchase day. Fast overnight mode requests routine exits at the next eligible stock session, with no forced same-day timer or closing-time liquidation.
- No stock pyramiding or repurchase after selling that stock on the same day.
- Protective same-day stops remain possible by default. Before entry, each new or working buy reserves capacity within a conservative maximum of three round trips over five actual broker trading sessions, including holidays and early-close calendars. Strict mode blocks same-day stops as well.
- Full broker order history is read before each guarded stock order. Multiple orders and fills spanning dates are counted conservatively. A working order in the same stock must complete or be canceled before another order is admitted. Missing or ambiguous history blocks submission.
- Protection persists when switching to Balanced, after restart, and when starting another experiment. Strictness is retained across pace changes. The intraday profile is rejected while this saved protection is enabled.
- Fresh broker cash, buying power, restrictions and existing allocation/exposure boundaries apply before entries. Quote and session freshness are checked again before reservation and broker submission. Local rejections release reservations; uncertain broker submissions retain their original recovery path.
- Prior excess history blocks new entries and same-day protective exits. It does not block a sale of an overnight holding that adds no same-day round trip.

These checks implement the user's preference in addition to Alpaca's current framework. A three-round-trip counter is a conservative application policy, not Alpaca's regulatory counter. Manual trades outside the app, broker liquidations, pre-existing flags, unavailable data and broker-specific matching remain outside any guarantee. Local stops depend on the running server and enabled automation.

## Verification

The full offline suite includes regressions for mode changes, restart persistence, strict exits, partial fills, working orders, actual-session windows, incomplete broker history, delayed reads and stop/halt races. A dashboard runtime check verifies strict protection survives pace changes.

Run `tools/audit_day_trade_guard.py` from the prepared or installed source to perform an authenticated, read-only policy check against both actual paper accounts. It verifies complete order history, distinct accounts, broker restrictions, position agreement and current voluntary capacity. It outputs no account identifiers or credentials and places zero broker orders. This audit needs a current broker trading day; a missing current calendar session fails closed.
