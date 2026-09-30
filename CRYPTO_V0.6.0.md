# Agent Arena v0.6.0 — 24/7 research and USD crypto paper trading

This release extends the **server and its shared web/Android dashboard**. The existing v0.5.1 Android APK is a web client and already displays the server's updated interface. Keep the installed APK and its saved server address. The phone itself does not run the agents; the server must stay awake and connected.

## Upgrade an existing installation

1. Turn **Automatic paper cycles** off and stop the old server. Back up the entire `data/` directory and `.env` together. Extract the v0.6.0 source into a new folder and copy the backed-up `data/` and `.env` into it. Never put `.env` in a source ZIP or share API keys.
2. Start the v0.6.0 server. Existing experiments keep their saved **US stocks and ETFs** scope. If your experiment has already started, first close and reconcile any positions and working orders, select **US stocks, ETFs and USD crypto** in Experiment setup, and choose **Archive & start new experiment**. The former audit history stays archived. Set a review interval and a daily research cap that suit your real model API budget; a 60-minute interval and cap of 24 allow at most one AI cycle per hour per agent. Keep the model budget finite.
3. Use **Check paper connections** to inspect stock and BTC/USD data without an order. Resolve any provider billing or paid model test error before enabling automatic cycles. Reconcile both dedicated paper accounts, resume, run a manual paper cycle, and inspect the order, position, and research record in both Agent Arena and Alpaca. Turn automation on only after this check succeeds.

The previous Anthropic paid Messages test had not passed at handoff. Updating the trading code does not resolve Anthropic account credit or provider errors. Keep automation off until both agents pass their paid tests and paper account checks.

## What changes

- Each autonomous agent may research eligible US stocks and ETFs at any hour. Equity orders still require the broker to report an open stock market, a fresh executable quote, and the 09:45–15:45 New York entry window. Near-close equity orders are deferred.
- An overnight equity idea is recorded as `deferred_equity_ideas`, with the strategy and watchlist. It is **not** sent automatically at the next open. A new session requires fresh research and order checks.
- When crypto is enabled for the experiment, the agents may discover broker-eligible **USD-quoted spot pairs** such as `BTC/USD`, inspect Alpaca US crypto prices and completed UTC daily bars, and rank pairs by change from the prior completed UTC close. Fresh crypto market orders use `gtc` and can be submitted on weekends and overnight.
- Each agent keeps its own virtual cash, positions, evidence, plan, cost budget and broker account. The existing stop controls, unresolved-order recovery and account reconciliation still apply. Explicit Alpaca `CFEE`/`FEE` activities are recorded once; unexplained inventory changes stop the agent.
- The scheduler checks enabled automation about every 60 seconds. Research happens only when an agent's next review is due and its daily and monthly model budgets allow it. Local exit checks run on scheduled cycles when that asset has an executable quote. There is no guarantee of uninterrupted uptime, timely fills or profitable decisions.

## Asset and cost limits

The integration remains **paper only** and long only. It supports US equities/ETFs and USD crypto pairs, not options, futures, forex trading, stablecoin-quoted crypto pairs, leverage or shorts. Alpaca lists crypto order types and 24/7 trading separately from stock orders. Alpaca's entry-tier market-taking crypto fee is 0.25% per side; actual fees vary by volume and order behavior, and may post after the trade date. Stock `fee_bps` remains the app's simulation assumption. Crypto accounting uses broker-posted fee activities rather than inventing a fee from the stock setting.

The crypto ranking compares a price with a prior UTC close. It cannot identify a globally best trade, prove that a strategy has an edge, or replace forward paper observations. A stale, one-sided or missing crypto quote blocks a crypto order. A paused, stopped or disconnected server cannot run local exits. Inspect both Alpaca paper accounts regularly.

## Verification and references

The source tests use mocked paper endpoints and temporary ledgers. Run `python3 -m unittest discover -s tests -q`. They do not prove current permissions on your two accounts, model billing, paper fee timing in your account, Android device behavior, continuous uptime, or profitability.

- [Alpaca crypto trading hours and fees](https://docs.alpaca.markets/us/docs/crypto-trading)
- [Crypto order types and time in force](https://docs.alpaca.markets/us/docs/crypto-orders)
- [Crypto market-data snapshots](https://docs.alpaca.markets/us/reference/cryptosnapshots-1)
- [Account activity types and pagination](https://docs.alpaca.markets/us/docs/account-activities)
- [Paper trading limits](https://docs.alpaca.markets/us/docs/paper-trading)
