# Fast overnight trading (server v0.7.2)

This profile follows the request to trade actively without routine day trading. It preserves the existing experiment, allocations, asset scope, positions, credentials, audit history, loss/position/exposure boundaries and paid-model budget.

- AI reviews may run once per minute during the stock entry session, within the shared model budget. Local exit checks attempt to run about every five seconds while the server and enabled automation are available; network or model work can delay them.
- Ordinary profit, timer and model-requested stock exits are blocked on the stock's purchase day in New York. Routine rotation begins at the next eligible stock session. There is no 30-minute liquidation or forced pre-close liquidation in this profile.
- No pyramiding into an existing stock position and no repurchase of a stock sold that day. Minute bars can guide entries, but models must consider overnight risk.
- By default, same-day protective stop exits remain possible, subject to a voluntary conservative three-round-trip capacity across the last five actual broker trading sessions. A new position or working buy reserves protective capacity before entry. Filled and working broker orders are read afresh; partial-fill timing and multiple orders are counted conservatively. This count can be stricter than a broker's own matching method.
- In Overview under System control, **Block all same-day stock exits** selects strict mode. This also blocks protective stops until a later stock session and can increase losses. The loss threshold triggers a halt; it cannot guarantee a maximum realized loss.
- Current broker cash, buying power, restrictions, eligible assets, fresh quotes and existing allocation/exposure limits still apply. No borrowing, shorts, live-money orders or new asset classes are enabled. Stops are local triggers, not guaranteed broker orders.

Alpaca's current framework has removed the PDT designation and trade-count restriction. The five-session guard above is an additional user preference, not a claim that Alpaca still enforces its old PDT rule. Other firms can be in FINRA's transition period or apply their own account restrictions. No implementation can guarantee another broker's classification.

Sources checked October 1, 2026:

- [Alpaca intraday margin rule](https://docs.alpaca.markets/us/docs/the-intraday-margin-rule)
- [Alpaca implementation notice](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/)
- [FINRA regulatory notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)

The dashboard no longer offers the former intraday shortcut. Same-day protection persists across pace changes, restarts and new experiments. Strictness is retained when switching pace; the conflicting intraday profile is rejected while the guard is enabled. A working order in the same stock must finish or be canceled before another order is admitted. Read PDT_RULES_REVIEW.md for the sourced rules review. Existing Android clients load the server/dashboard changes after Refresh, without a new APK.
