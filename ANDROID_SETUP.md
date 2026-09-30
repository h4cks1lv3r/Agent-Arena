# Agent Arena v0.5.1: phone and server setup

The Android app connects to Agent Arena on a computer that stays on. The APK does not include a hosted server or keep the trading agents running on your phone. You can close the Android app while the server continues its enabled paper cycles.

This release uses **paper trading only**. A new experiment has two agents, Astra (OpenAI) and Claude, with **$250 virtual capital each: $500 total**. Existing experiments keep their saved allocations. No live deposits, Schwab trading or live-money execution are included. Model API calls can cost real money.

## Recommended: your Windows PC and a private HTTPS connection

This route uses Tailscale on your PC and Galaxy S24 Ultra. Both devices must be connected to the same private Tailscale network. You do not need a router port-forward or a purchased domain. The PC must remain awake, connected to the internet and running Agent Arena.

### 1. Prepare the PC

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
2. Extract the application into a stable folder owned by your Windows user, such as `C:\Users\YOUR_NAME\Agent_Arena`. Keep it out of public/shared folders. Replace this example path with your actual path below.
3. Copy `.env.example` to `.env` beside `server.py`. Put the two dedicated **Alpaca paper** accounts and model API keys in this file. Do not use live account keys. Do not copy this file to the phone. See the main README for account and model setup.
4. Use the folder's **Properties → Security** settings to restrict access to your Windows user and necessary administrators/system services. Protect `.env` and `data\` in the same way. The access-token file's POSIX permission setting alone does not establish Windows access controls.

Each trading agent requires its own eligible Alpaca paper account. The application rejects reuse of the same account for both agents. Provider account limits, data permissions and billing still apply. A broker's displayed paper cash does not increase the agent's assigned virtual capital.

### 2. Connect the PC and phone with Tailscale

Install Tailscale using the official [Windows instructions](https://tailscale.com/docs/install/windows) and [Android instructions](https://tailscale.com/docs/install/android). Sign in on both devices and connect them. On Windows, right-click the Tailscale tray icon and select **Preferences → Run unattended** so Tailscale can remain connected when you sign out. This controls Tailscale only; Agent Arena needs its own running process. [Unattended mode](https://tailscale.com/docs/how-to/run-unattended)

Open PowerShell on the PC and run:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status
```

If PowerShell cannot find `tailscale`, reopen it after installation or use the installed executable, normally `C:\Program Files\Tailscale\tailscale.exe`.

Follow the command's HTTPS consent link if prompted. Copy the resulting address, for example `https://my-pc.example-tailnet.ts.net`. Use **your actual address**, without a final slash or path. Serve is private to permitted devices in your tailnet. Use Serve for this setup; do not enable public Funnel sharing. The `--bg` setting persists across Tailscale/device restarts. Until Agent Arena starts, the address may show a proxy connection error. [Serve setup](https://tailscale.com/docs/features/tailscale-serve), [Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve)

HTTPS certificate names are recorded in public certificate-transparency logs. Choose a PC name that does not disclose sensitive information. This does not make the service public. [Tailscale HTTPS certificates](https://tailscale.com/docs/how-to/set-up-https-certificates)

### 3. Create the application login and start the server

In PowerShell, enter the extracted application folder:

```powershell
Set-Location 'C:\Users\YOUR_NAME\Agent_Arena'
py -3 server.py --init-access
```

The command creates `data\access-token` and displays the generated access secret once. Save it in your password manager. This is the **Agent Arena login secret**, not an Alpaca, OpenAI or Anthropic key. It allows control of this paper experiment and paid model activity. Do not put it in a URL or send it through chat. An existing file is not overwritten; if you already initialized access, retain the existing secret.

Start the server using the exact HTTPS origin copied from Tailscale:

```powershell
py -3 server.py --no-browser --public-origin https://my-pc.example-tailnet.ts.net
```

Leave this terminal open for the first connection check. The server listens only on `127.0.0.1:8765`; Tailscale terminates HTTPS and forwards requests locally. `--public-origin` configures the allowed address. It does not make the service public. Do not open port 8765 in the router or Windows firewall.

Open the HTTPS address on the PC. Enter the Agent Arena access secret on the login page. A server restart ends login sessions. Login does not enable trading. If you want the process to survive Windows sign-out or start at boot, follow [Windows Task Scheduler setup](deploy/README.md#windows-start-the-server-at-boot).

### 4. Connect the Android app

1. Install the supplied `Agent_Arena_v0.5.1.apk` on the S24 Ultra. Android may require you to allow installation from the app used to open the APK.
2. Keep Tailscale connected on the phone. Enter the **HTTPS server origin** in Agent Arena, for example `https://my-pc.example-tailnet.ts.net`.
3. On the server's login page, enter the Agent Arena access secret. The APK has no embedded account or model keys. Those keys remain in `.env` on the server.
4. Confirm that the dashboard shows the expected two agents and experiment balances. Choose the intended models and their current API prices, set a model budget, connect/reconcile the dedicated paper accounts, then enable the paper experiment through the dashboard.

The Android app accepts HTTPS with a valid certificate. A phone's `127.0.0.1` address refers to the phone, so do not enter the desktop's local HTTP address in the app. Do not accept certificate errors or use an `http://` URL.

Android Back preserves the saved server address and login session. Use **Disconnect** if you want the app to forget that server. The v0.5.1 APK can be installed over v0.3 or our v0.4 APK without uninstalling it; all use the same signing identity. For the server update, first follow [UPGRADE_v0.5.1.md](UPGRADE_v0.5.1.md), including the backup of both databases.

## What keeps running, and how often

| Operation | Default interval | Meaning |
|---|---:|---|
| Open dashboard requests server state | About 3 seconds | Refreshes the visible dashboard while it is active. |
| Server refreshes paper broker status | About 15 seconds | Best effort; reconciliation and network operations can delay it. |
| Enabled automatic paper worker | About 60 seconds | Checks scheduled work and local exit conditions. |
| Each AI agent's strategy review | At least 60 minutes | Configurable minimum, also limited by its next review time, daily limits and model budget. |

These are different clocks. A screen update does not run another AI review. This is polling, not a tick-by-tick real-time feed. Slow requests, locks, device sleep or connection failures can delay updates and actions. Check the dashboard's timestamps and status.

If paper automation was enabled before a restart and the restart preference allows recovery, the server restores that intent and enters **startup recovery**. Each agent must pass its own account reconciliation before it can research or trade. Automatic restart is enabled by default, while an existing explicit opt-out is retained. Disabling it during recovery prevents recovery from enabling automation; changing it while already running affects the next restart. To stop current automation, use the automatic-cycles-off or Halt control. It does not clear deliberate stops. A temporary connection failure causes delayed read-only recovery checks. Operator pauses, the STOP file, trading-loss stops and account discrepancies are not automatically cleared. A stopped experiment remains stopped. Login sessions still end on restart.

Local stop/exit rules require an operating server, fresh data and enabled automation; they are not protective orders hosted at the broker. A missing or stale quote skips the affected symbol's automatic exit until fresh data returns. A retained last price is not a current executable price.

## Costs and limits

- The trade balances are virtual. Do not deposit real money to run this release.
- Paid model usage is separate from ChatGPT/Claude chat subscriptions. Enter models available to your API accounts and check provider prices. A $0 monthly model budget blocks paid AI requests.
- New experiments exclude model expenses from the trading-loss limit. Migrated experiments preserve their prior loss basis. Net performance and target progress still deduct model expenses. The monthly model budget controls both research and paid connection checks.
- IEX remains the default execution feed. Set `ALPACA_DATA_FEED=sip` only with that entitlement. Historical research uses split-adjusted completed-day bars and records the actual feed and any fallback. A delayed or stale valuation is not an executable price. The app does not purchase data entitlements.
- The PC consumes electricity and must remain awake. Hosting, a domain or upgraded market data can add costs if you choose them; none are purchased by these instructions.
- Paper fills and available data do not reproduce all live-market conditions. Performance is not a profit promise.

## If the phone cannot connect

| Symptom | Check |
|---|---|
| Name cannot be resolved or connection times out | Both devices have Tailscale connected; the PC is awake; tailnet permissions allow the phone to reach the PC. |
| Proxy/502 error | Agent Arena is running on port 8765 and `tailscale serve status` shows that same backend. |
| Address/Origin rejected | The server's `--public-origin` exactly matches the phone's HTTPS hostname, with no path, query or final slash. Restart the server after changing it. |
| Login expired | Sign in again. Sessions expire after 12 hours and on server restart. This does not erase experiment data. |
| Connected, but no AI actions | Check pause status, automatic cycles, model budget, missing keys, next review time and broker market status. |
| Computer restarted and agents show recovery | Wait for account reconciliation; inspect connection errors and next retry time. An operator/risk stop requires attention and is not cleared by restart. |
| A control reports that an operation is busy | The conflicting command was not queued. Wait for that operation, then try again. Halt remains available. |

For a Linux computer/server with a conventional HTTPS domain, use the supplied [systemd and Caddy examples](deploy/README.md#linux-systemd-and-caddy).

An accepted background command can display `queued` while account monitoring finishes. It waits up to five minutes without blocking the dashboard. A newer stop cancels obsolete queued work. For a broker-confirmed missing order, open Activity and select **Record broker confirmation**; follow the recovery instructions in the upgrade guide.
