# Agent Arena v0.6.1 — stocks, 24/7 crypto, Android and web

Each configured AI agent can research eligible assets, develop and revise its own strategy, choose buys/sells/holds and trade sizes, and set exit conditions. The fixed SMA strategy remains only as an optional rules baseline. Fresh experiments now start with two independent strategists: Astra and Claude. The $500 total is split into $250 each. Existing experiments retain their saved roster and settings.

This is a local application that must keep running. It cannot run from a chat message. This release has **paper trading only**, no deposits, no live broker route, and no demonstrated profitable strategy. The $500 total and $10,000 target are experiment settings. A 20× return is a goal, not an expected outcome.

Read **CRYPTO_V0.6.0.md** and **WINDOWS_UPDATE.md**. Version 0.6.1 combines the v0.5.2 order-history cursor recovery with 24/7 USD crypto and after-hours stock research. Existing experiments retain their saved asset scope, positions and audit history.

## Android and shared web dashboard

Install `Agent_Arena_v0.6.1.apk` over the existing Android app, then connect to your existing HTTPS server address. The existing Android client can also load this updated dashboard. The same address opens the dashboard in a desktop browser. Read **ANDROID_SETUP.md** for the Windows + private Tailscale setup and the optional Linux deployment.

The Android app is a secure client. The trading process runs on your always-on computer or server; closing the phone app does not stop that process. Broker and model API keys stay on the server. The app does not create a hosted server or enable live trading.

The comparison board keeps both agents visible on a phone. It displays equity, return after estimated costs, drawdown, current strategy and activity. Browser updates and market-data updates have separate timestamps. This is periodic monitoring, not a tick-by-tick market feed.

## Start on Windows

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
2. Extract this ZIP. Open the `Agent_Arena` folder and run `Start_Agent_Arena.bat`.
3. Open http://127.0.0.1:8765 if the browser does not open automatically.
4. Review **Experiment setup**. The new two-AI roster remains inactive in Synthetic local demo. For actual agent research, connect paper accounts using the steps below. A rules baseline can still be added for a synthetic software check.

On macOS/Linux, run `python3 server.py` in the extracted folder. No third-party Python package is needed. Keep the terminal open and the computer awake. Previously enabled paper automation can recover after a restart when its restart preference allows it and that account passes reconciliation. Operator stops and risk stops remain in effect. The synthetic demo uses invented prices and only runs rules agents. It never fabricates AI research or performance.

## What the AI agents control

- Search the broker's eligible asset directory by symbol or company/fund name.
- Request current quotes, daily price/volume history, market movers, and available news summaries.
- Develop a thesis and say what would invalidate it. No indicator, sector, ETF list or entry signal is imposed on AI strategies.
- Choose an asset, a buy amount in dollars, a sell quantity in shares, or no trade.
- Set or revise local stop-loss prices, take-profit prices and maximum holding times. Zero disables an individual trigger. Agents can also exit during a later model review.
- Choose when to review again, within the configured minimum interval and daily research limit.
- Retain their own strategy, watchlist, exit rules and recent decisions between cycles. Each cycle receives current holdings, cash, net equity and costs. Models can revise their strategy from those records; this is **not model training** and does not prove that revisions improve returns.

The UI's **Research & strategy** view shows hypotheses, invalidation conditions, research sources, model rounds, proposed trades and reasons. Decision records and evidence are stored before broker submission. Inputs and results can be exported for review.

## Supported assets and research limits

Autonomous AI agents can research eligible US equities/ETFs and, in an experiment configured for **US stocks, ETFs and USD crypto**, eligible USD-quoted spot crypto pairs. The broker must confirm each asset as active, tradable and fractionable. Crypto orders use Alpaca US data and GTC market orders, including overnight and weekends. Stock research continues after the close; stock orders wait for a new open-session check. The equity directory can include leveraged or inverse ETFs. These assets have different risks.

The paper integration supports US stocks, ETFs and USD spot crypto pairs. Options, futures, forex, shorts and borrowed-money trades are outside its supported scope. Those need different data, order and risk implementations. It also does not run arbitrary code, shell commands, download links or model-selected URLs.

Research uses Alpaca's approved market-data and news interfaces. It is not an unrestricted browser, a fundamentals database, or a backtest engine. Available coverage depends on the paper account's data permissions. News can be delayed or unavailable. Such failures are logged as unavailable; they are not replaced with invented information. Execution prices use the configured feed; IEX is the default, and `ALPACA_DATA_FEED=sip` selects entitled SIP access. Snapshots record quote and trade times separately. The freshest valid quote or trade can provide a valuation mark; missing or stale primary data can use an explicitly labeled delayed-SIP mark. A trade-only, delayed, or stale mark cannot authorize an order. Historical research uses split-adjusted completed-day bars: `ALPACA_HISTORY_FEED` defaults to `sip`, with an IEX fallback only for an access-denied response. The actual feed and adjustment are recorded in the evidence. Split adjustment is not dividend adjustment or automatic repair of split-related changes in account holdings. Paper simulation omits important real-market effects, including dividends.

Each paid turn has a bounded input and a configurable total output allowance, including reasoning. Set **Maximum output tokens per model turn** in setup (`output_token_limit`). The default remains 8,192 tokens, with an allowed range of 1,024–16,384; a higher limit increases the reserved maximum cost and does not prove better decisions. The provider's actual returned model ID is saved where available. Model requests have a 180-second network timeout; broker requests retain a 20-second timeout. Paid requests and uncertain order submissions are not retried automatically. Read-only broker checks can recover after temporary failures. Astra uses medium reasoning; Opus 5.5 uses adaptive thinking at medium effort. Other model IDs retain their provider default behavior. These settings limit cost and do not guarantee a complete model response. The default allows up to **3 model turns per cycle**, each with up to 5 read-only research requests. A research turn obtains evidence; a later turn produces a final plan. With only 1 allowed model turn, a new trade cannot normally complete the required research-and-plan sequence. No trade is submitted if the agent runs out of turns without a valid final plan.

## Connect paper accounts and models

1. Create a **different, dedicated Alpaca paper account for each trading agent**. Each must have no positions or working orders at first connection. The app verifies actual account IDs and rejects reuse across agents. Alpaca's own limits govern how many paper accounts you can create.
2. Copy `.env.example` to `.env` beside `server.py`. Do not name it `.env.txt`. Enter paper credentials locally; do not send keys or passwords through chat.
3. Environment names for the default two-agent roster:

```ini
ALPACA_OPENAI_KEY=
ALPACA_OPENAI_SECRET=
ALPACA_CLAUDE_KEY=
ALPACA_CLAUDE_SECRET=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
```

Custom agent IDs use `ALPACA_<UPPERCASE_AGENT_ID>_KEY` and `_SECRET`. OpenAI-provider agents use `OPENAI_API_KEY`; Anthropic-provider agents use `ANTHROPIC_API_KEY`. Enter an exact model ID supported by that provider account and its current USD-per-million input/output token prices in Setup. New experiments suggest `gpt-6-astra` and `claude-opus-5-5`. The new Claude preset uses $4 per million input tokens and $20 per million output tokens, checked against Anthropic's official documentation on September 25, 2026. Verify access and pricing in your provider accounts. Existing saved model IDs and prices are not upgraded during an active competition.

4. Restart the app. In **Experiment setup**, choose **Alpaca paper accounts** and **Autonomous strategies**. Set the agent roster, allocations, costs and operating limits, then click **Archive & start new experiment**. You can remove the rules baseline if you want only AI competitors; the remaining weights divide the $500 total.
5. Set a positive monthly model budget to permit real paid API requests. The default is **$0**, so AI research is disabled until you change it.
6. Use **Check paper connections** to inspect broker access without a model call. Diagnostics do not replace account reconciliation. Click **Reconcile**, confirm that both accounts show a successful reconciliation, then **Resume entries**. To check the models before the first cycle, select **Also send one paid model test per AI agent**. This changes the button to **Check paper + paid model connections**. Inspect the displayed maximum estimated charge first. The paid check reserves an estimate for 4,000 input and 1,024 output tokens at the configured rates; global and per-agent remaining budgets both apply. An interrupted or failed call retains its estimate. Exhausted budgets, pauses, and stops block paid checks. Then click **Run paper cycle** during the entry window. Inspect the research record and compare every resulting order/position with Alpaca.
7. Enable **Automatic paper cycles** once that first connection check succeeds. Manual cycle requests still respect the next review time and daily limits; they do not force repeated paid calls.

A paper account can display $100,000 while the agent still has only its assigned share of $500. Never use these paper accounts for unrelated manual trading. Unknown orders, mismatched inventory or unresolved submissions stop execution until investigated.

## Execution checks outside the AI

These checks enforce the experiment boundary, not a required trading strategy. The AI cannot change them, transfer funds, share another agent's cash, or override a halt.

| Setting/control | New-experiment default | Effect |
|---|---|---|
| Total virtual capital | $500 | Divided across the roster by allocation weight; not $500 per agent |
| AI mode | Autonomous | Each AI researches and plans independently; legacy reviewer mode remains available |
| Position / total exposure caps | 30% / 60% per agent | Editable up to 100% / 100%; reject new orders that exceed caps |
| Compound profits | Enabled | Caps use current trading equity; trading profits can expand permitted future sizing |
| Combined trading-loss halt | $50 | New experiments use trading equity, including transaction fees; the explicit saved loss basis preserves legacy experiments |
| Minimum review interval | 60 minutes | Agent can request a longer interval; configurable minimum is 15–1,440 minutes |
| Maximum AI cycles/day | 4 per agent | Configurable 1–24; claimed before research so crashes do not repeat a cycle |
| Maximum model turns/cycle | 3 | Configurable 1–5; failed paid requests are not retried automatically |
| Maximum orders/cycle | 3 per agent | Configurable 1–10; one order per symbol in a plan |
| Monthly model budget | $0 | Disables paid calls, including paid diagnostics; positive budget is shared by AI-agent weights |
| Order format | Fractional DAY market orders | Buy amounts in whole cents, minimum $1; sells cannot exceed the agent's recorded shares |
| Entry window | 09:45–15:45 New York | Broker must report open; current clock, quotes and asset eligibility are checked |
| Polling | Every 60 seconds | Runs while Autopilot is enabled and the app is alive |

A plan is checked before any orders are sent. Unfilled sale proceeds cannot finance a buy in the same plan. Oversized trades are rejected and logged; the app does not silently replace an agent's chosen amount. The whole plan is not a broker-side atomic transaction: prices can move, individual orders can fail, and earlier orders in a plan can already exist when a later one fails. Actual fills are always reconciled.

Caps govern new purchases; market appreciation can push an existing holding above them without automatic trimming. New experiments enable compounding. The optional fixed rules baseline retains SMA20/SMA50 signal decisions on SPY/QQQ/IWM, and still runs at most one strategy cycle per day.

## Exits, stops and the target

Agent-defined exit conditions are **local checks**, evaluated on normal automatic cycles while the market is open, even if the next AI review is not due or the model budget has run out. They require the app, network, fresh data and an enabled experiment. They are not protective orders held by the broker. Holding-time limits use the position's first fill, not the time the model last rewrote its plan. Definite exit rejections use durable retry delays and show a blocked-exit reason: 30 seconds initially for inventory/funds rejections, 300 seconds for invalid orders, then doubling to a maximum of 900 seconds. Authentication errors pause the agent separately. This prevents repeated requests on every tick; it can also postpone an exit. Uncertain submissions stay under original-ID reconciliation and are never replaced by this retry path.

**Halt automation** (called **Halt new entries** in older releases) persists a STOP file and disables all automation, including automatic exits. Existing orders and positions remain. Use **Request cancel** or **Request close** separately. `Stop_New_Entries.bat` or `python3 server.py --halt` creates a persistent stop even when the server is dead. Use Alpaca directly to manage working orders if the app is unavailable. The loss threshold cannot guarantee a maximum loss.

The assigned share of the $10,000 goal is now included in each autonomous agent's instructions and context. When the application records total experiment equity at or above the target, it pauses automation. It does **not** automatically liquidate holdings, cancel existing orders, or lock in that value. The marked balance can subsequently fall. The target does not force a particular risk level or establish that a profitable path exists.

Each agent has a durable **Pause agent** control. It pauses that agent's research, new orders and automatic exits while the other agent can continue. It does not cancel existing broker orders or close positions. Resume requires reconciliation of that account. Temporary read failures put only the affected agent into recovery, with checks delayed from 15 seconds up to 5 minutes. Successful reconciliation clears only a recoverable hold. Startup recovery verifies each account before that agent can research or submit orders; an unavailable peer does not authorize an unverified account to act. **Resume automation after a restart** controls automatic restoration of saved operation. The default restart preference is enabled; an existing explicit opt-out remains disabled. Turning it off during recovery prevents late recovery from turning automation back on. Turning it off while already running changes the next restart behavior; use automatic-cycles-off or Halt to stop current automation. Credential failures, account/inventory discrepancies and operator pauses require attention. A duplicate account, shared ledger failure, the combined trading-loss limit, or the combined target can still stop both.

Held-position quotes can refresh while a paid model request is running. Fill reconciliation can be delayed until that cycle releases its account-processing lock; the UI shows its last reconciliation time. A market-data update is not proof that broker fills were just reconciled.

Unknown submission outcomes are never automatically resubmitted. The original client order ID is used for reconciliation. There is no automatic ledger-repair tool: unresolved account changes require investigation rather than deleting state or guessing.

Definite broker rejections now finish the local order as rejected and release its cash reservation. A timeout, duplicate-ID response, unreadable ambiguous rejection or 404 lookup cannot establish that no order exists. Those outcomes keep the reservation and use read-only recovery. An old v0.3 order that already lost its rejection evidence cannot be safely repaired from a 404 alone.

Missing or zero quotes do not permanently stop an agent. The last valid valuation remains visible with its timestamp. Automatic exits skip symbols without fresh execution quotes; other eligible symbols and research can continue. Preflight and submission still require a valid quote no more than 120 seconds old. The app does not invent a substitute price or treat a delayed mark as executable.

Order reconciliation uses broker ID pagination, a saved cursor, current open orders and original-ID checks for pending submissions. Passing 500 historical orders does not stop reconciliation. Broker pages that repeat or do not advance cause an explicit error rather than silently skipping records.

Slow controls return an operation status instead of keeping an HTTP request open through model research. Conflicting controls receive an immediate busy response and are not queued for later execution. Halt, per-agent pause and automatic-cycles-off remain available. A halt cannot retract an order that has already left the server.

## Model fees and competition evidence

Model API requests incur real charges even while trades use virtual money. Fees are estimated using the prices you enter. The service reserves conservative input/output costs before each request and records actual reported usage afterward. Interrupted or invalid calls retain a conservative estimate. Monthly checks cover all experiments in the same data directory; AI agents receive weighted shares so one cannot consume the entire research allowance. Provider billing can differ from these estimates; set provider spending controls as well.

Estimated model costs reduce the agent's net economic equity, reported performance and progress toward the target. New experiments exclude them from the trading-loss allowance. Legacy fee-inclusive experiments retain their saved policy; the explicit **Count model costs toward the loss halt** setting is shown in setup and recorded in the audit. The monthly model budget controls paid research and diagnostics separately. Equity simulation fees default to 1 basis point; crypto fee activities are accounted for from broker postings. Synthetic-only slippage defaults to 5 basis points and does not alter Alpaca paper fills. Taxes, wash sales, tax lots and total-return benchmarks are not implemented.

Keep separate questions: does execution behave correctly, and do the agents produce useful returns after costs? No profitability result is included. Changing a strategy after losses is not evidence that the new strategy has an advantage. Define a time horizon, benchmarks and sufficient forward observations before ranking agents. Research decisions are recorded before orders; historical AI replay is not used to claim predictive skill.

## Upgrade from an earlier release

Read **[UPGRADE_v0.5.md](UPGRADE_v0.5.md)** before continuing an existing experiment. Stop the old server and its restart mechanism, then back up the complete data directory and `.env`. The backup must include **both `arena.sqlite3` and `service.sqlite3`**, any remaining SQLite sidecars, the access token, and `STOP`.

Version 0.5 identifies and validates the v0.3 and both reviewed v0.4 storage layouts. It preserves detailed evidence, events, raw history, orders, model charges, and the appropriate legacy loss policy. Automatic core migration backups supplement the full paired backup; they do not replace it. Rollback restores the old code and both database backups together. Do not open a migrated database with an older release or copy a demo database over an active experiment.

Install `Agent_Arena_v0.5.1.apk` over the old app. The application ID and signing identity match, so an uninstall is not required. Android Back retains the server address; explicit Disconnect clears it.

Existing v0.1 experiments retain their original **legacy reviewer** behavior and disabled compounding. Existing v0.2 experiments retain their saved mode, agent roster, models and limits. They are not silently converted to another strategy. To use autonomous mode, close/reconcile existing paper holdings and working orders, configure the new mode, then archive/start a new experiment. A fresh extraction starts in demo mode with autonomous mode selected for future paper use. Settings lock once an experiment starts.

## Validation and code

Run `python3 -m unittest discover -s tests -v` (Windows: `py -3 -m unittest discover -s tests -v`). These tests use temporary databases and mocked APIs, not your accounts. See `VALIDATION.md` for results and unresolved checks.

- `arena/core.py`: durable portfolios, reservations, fills, eligible-asset registry and limits.
- `arena/autonomous.py`: bounded research tools, source records, model rounds and plan evidence checks.
- `arena/adapters.py`: fixed-host Alpaca/model HTTP clients and strict response schemas.
- `arena/service.py`: schedules, cost journal, independent strategy memory, monitoring, preflight and execution.
- `arena/remote_auth.py`: remote login sessions, CSRF tokens and access-secret handling.
- `android/`: Android client source, build instructions and HTTPS origin policy tests.
- `ANDROID_SETUP.md` and `deploy/`: private phone connection and server deployment examples.
- `server.py` and `arena/static/`: local dashboard, API and worker.
- `data/`: local databases and STOP marker; created at runtime and excluded from this ZIP.

The dashboard limits recent detail records to reduce phone data transfer, but its chart covers the full period using bucket extrema. Raw observations and full decision evidence remain in the audit export. Holding-price freshness uses the oldest or missing mark among current holdings so a fresh price cannot hide a stale peer.

The app binds to localhost by default. Remote use requires an explicit HTTPS public origin, an access token and an HTTPS reverse proxy as described in ANDROID_SETUP.md. Authenticated sessions use Secure, HttpOnly cookies; mutation requests also require a session CSRF token. Do not expose port 8765 directly or send access tokens in URLs. Research text is untrusted; model output cannot select network hosts, run code, change budget settings or call the broker directly. These structural controls do not prove that a model will ignore every persuasive or malicious statement in news.

## Official interfaces checked

- [Alpaca paper environment and simulation limits](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca eligible asset directory](https://docs.alpaca.markets/us/reference/get-v2-assets-1)
- [Alpaca news](https://docs.alpaca.markets/us/reference/news-3)
- [Alpaca market movers](https://docs.alpaca.markets/us/reference/movers-1)
- [Alpaca fractional orders](https://docs.alpaca.markets/us/docs/fractional-trading)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Anthropic current model pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Alpaca order listing and pagination](https://docs.alpaca.markets/us/reference/getallorders-1)

See `AUDIT_CORRECTIONS.md` for the audit findings, changes and remaining limits.

Official documentation, mocked application tests, and observed account behavior are distinct evidence. Actual account/provider connectivity and model behavior have not been verified with your credentials. A live-money version and a Schwab adapter are not included.

## Release scope

Two accounts and two independent strategy histories are implemented in one server process. This is account and decision isolation, not separate fault-tolerant servers. A server outage affects both agents. The agents receive their own positions and results; neither receives the other agent's strategy or bankroll.

The objective is to grow each agent's net equity quickly within the chosen risk and API spending limits. There is no promised return or completion date. Names do not assign a strategy; both agents choose their own. Fast profits can require risks that conflict with the fixed limits. Keep the same limits and start time when comparing the two agents, and compare net return together with drawdown and API cost.
