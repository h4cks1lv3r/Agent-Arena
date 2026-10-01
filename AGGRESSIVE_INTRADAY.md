# Aggressive intraday paper trading (v0.7.0)

Choose **Use aggressive intraday** in Overview → System control. It changes the current experiment in place and journals the change; positions, order history, API keys, allocation and risk caps are retained.

- AI reviews may run once per minute in the stock entry session, up to 390 per agent per UTC day. The existing monthly model budget still applies; exceeding it blocks further paid research.
- Models can research completed one-minute bars from the last two hours. The actual market-data feed is recorded; IEX is not consolidated market coverage.
- While automation is enabled and the agent is unpaused, the monitor attempts local exit checks about every five seconds. It shares the account-work lock, so a slow network/model operation can delay a check.
- Model rules can tighten the policy but cannot extend the 30-minute maximum hold. Missing or wide model exits receive a 1% local stop and a 1.5% local profit target based on actual average entry price.
- Stock entries stop 15 minutes before the broker's actual close; positions are requested closed in the final 10 minutes. Early closes use the broker clock. Closed sessions, stale quotes, rejected orders, paused automation or an unavailable host can prevent an exit. These are local triggers, not guaranteed broker stop orders.
- Each new entry reads current broker cash, buying power and restrictions. It requires cash to cover the order, a two-sided quote with spread at most 20 basis points, and the ordinary allocation/exposure checks. No borrowing, shorting or live-money orders are enabled.
- Existing multi-day positions are subject to the same short holding cap after activation. A pause or newer halt always wins.

Alpaca announced that its new intraday margin framework went live June 4, 2026, replacing PDT counting and the $25,000 PDT threshold. Use current buying_power and broker restrictions, not removed daytrade_count/daytrading_buying_power fields. Other broker implementations may be in FINRA's transition period.

Sources checked October 1, 2026:
- [Alpaca implementation notice](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/)
- [Alpaca intraday margin rule](https://docs.alpaca.markets/us/docs/the-intraday-margin-rule)
- [FINRA intraday rule notice](https://www.finra.org/rules-guidance/notices/26-10)

Faster turnover does not establish profitable performance or a shorter path to the target. Paper fills do not prove live execution quality.
