# Server deployment examples

These examples configure an existing machine. They do not create accounts, buy hosting, publish a service or start trading. The Android APK is a client. The Python server performs the work and needs an always-on machine.

Use [ANDROID_SETUP.md](../ANDROID_SETUP.md) for the recommended private Windows/Tailscale connection. This directory also includes an optional Linux systemd service and Caddy reverse proxy. Both routes use the application's authenticated HTTPS mode.

## Server interface

```text
python server.py --data-dir PATH --init-access
python server.py --data-dir PATH --port 8765 --no-browser --public-origin https://HOST
```

`--init-access` creates `PATH/access-token` and prints the new secret once, then exits. It refuses to overwrite an existing token. Remote startup requires a valid access secret from `ARENA_ACCESS_TOKEN` or the data directory's `access-token`; the environment variable takes precedence. These examples use the file and leave `ARENA_ACCESS_TOKEN` unset.

The server always binds to `127.0.0.1`. The proxy must run on that same machine and preserve the public **Host** header. Set an exact HTTPS origin without a path, query, fragment, credentials or trailing slash. Do not use an IP/port forwarding rule that exposes the Python HTTP listener. `X-Forwarded-Host` is not used to bypass the allowlist. Caddy preserves Host for the supplied HTTP upstream; Tailscale's Serve implementation also preserves the incoming Host for this TCP-backed HTTP proxy. [Caddy header defaults](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#defaults), [Tailscale implementation: reverseProxy.ServeHTTP](https://github.com/tailscale/tailscale/blob/main/ipn/ipnlocal/serve.go)

Runtime loads `.env` beside `server.py`; process environment values take precedence. Broker and model secrets stay there. `.env` is not a web endpoint and is never copied into the APK. The access secret is entered on the web login page; successful login sets a Secure, HttpOnly, SameSite=Strict session cookie with a 12-hour lifetime. Restart clears active sessions. The public login page is reachable before authentication, but private account/experiment data requires login.

## Windows: start the server at boot

Complete the manual connection in `ANDROID_SETUP.md` first. Stop the manually started server with Ctrl+C before running a scheduled instance. Only one instance may use a data directory.

1. Keep Tailscale's **Run unattended** preference enabled. The Serve command using `--bg` restores its proxy configuration after restart. [Official Windows unattended guidance](https://tailscale.com/docs/how-to/run-unattended)
2. In the application folder, find the Python executable:

   ```powershell
   py -3 -c "import sys; print(sys.executable)"
   ```

3. Open Windows **Task Scheduler → Create Task**. Name it `Agent Arena server`. Use the same Windows account that owns the application and data. Select **Run whether user is logged on or not**. Do not select highest privileges. Windows may request that account's password in its own task dialog.
4. Add an **At startup** trigger with a short delay. For the action, choose **Start a program** and use the following fields, substituting your actual paths and HTTPS origin:

   | Field | Value |
   |---|---|
   | Program/script | The full path printed by the Python command |
   | Add arguments | `"C:\Users\YOUR_NAME\Agent_Arena\server.py" --data-dir "C:\Users\YOUR_NAME\Agent_Arena\data" --port 8765 --no-browser --public-origin https://my-pc.example-tailnet.ts.net` |
   | Start in | `C:\Users\YOUR_NAME\Agent_Arena` |

5. In Settings, clear any rule that stops the task after a fixed number of days. For an existing running instance, choose **Do not start a new instance**. Enable restart on failure if you want Windows to restore a failed process. The app can recover previously enabled paper automation when its restart preference allows it and each active account passes reconciliation; it does not clear operator or risk stops.
6. In Windows power settings, keep the plugged-in PC from sleeping. Lock the screen normally. On a laptop, check the lid and power-loss behavior rather than assuming a closed lid leaves it running.
7. Run the task manually once. Open the HTTPS URL, sign in, reconcile, then resume only the intended paper experiment. Verify a reboot separately: the dashboard should return, login should be required again, and previously enabled paper automation should pass startup reconciliation before it trades. Verify separately that a deliberately stopped experiment stays stopped.

When moving the folder or changing Python, edit the task's paths. When updating application files, stop the task, then follow [UPGRADE_v0.5.md](../UPGRADE_v0.5.md). Back up the full `data` directory and `.env`, including both `arena.sqlite3` and `service.sqlite3`, remaining SQLite sidecars, and `STOP`, before replacing source files. Keep backups private. Do not run the old and new server at the same time.

The bundled `Start_Agent_Arena.bat` launches local desktop mode; it does not supply a remote HTTPS origin. `Stop_New_Entries.bat` writes a STOP marker in the default `data` folder beside `server.py`. If you chose another data directory, use `py -3 server.py --data-dir "YOUR_ACTUAL_DATA_PATH" --halt` from the application folder to stop the correct experiment.

## Linux: systemd and Caddy

Use this route if you already operate a Linux server and control a domain such as `arena.example.com`. The hostname below is a placeholder. This exposes an authenticated login page on the internet. The private Tailscale route has a smaller exposure surface for a personal experiment.

Prerequisites: Python 3.11+, systemd, sudo access, and Caddy installed using its [official package instructions](https://caddyserver.com/docs/install). Point the domain's A/AAAA records to this server. Make TCP 80/443 reachable for Caddy's standard certificate setup and HTTPS access. Keep 8765 closed externally. Remove an incorrect AAAA record rather than publishing an unreachable IPv6 address. Caddy obtains and renews certificates when its domain, network and persistent data requirements are met. [Automatic HTTPS](https://caddyserver.com/docs/automatic-https)

### Install application files and private state

Run the following **from the extracted Agent Arena application folder**, not from its parent:

```bash
sudo useradd --system --user-group --home-dir /var/lib/agent-arena --shell /usr/sbin/nologin agent-arena
sudo install -d -o root -g root -m 0755 /opt/agent-arena
sudo cp -R arena server.py .env.example /opt/agent-arena/
sudo chown -R root:root /opt/agent-arena
sudo chmod -R go-w /opt/agent-arena
sudo install -d -o agent-arena -g agent-arena -m 0700 /var/lib/agent-arena
sudo install -o agent-arena -g agent-arena -m 0600 .env.example /opt/agent-arena/.env
sudoedit /opt/agent-arena/.env
sudo -u agent-arena /usr/bin/python3 /opt/agent-arena/server.py --data-dir /var/lib/agent-arena --init-access
```

Create the service account only once; if it already exists, check that it is the intended account. The example `.env` initially contains no secrets. Fill it with the dedicated paper and model keys. Save the generated application login secret privately. Do not put either kind of secret into Caddyfile, service command arguments or shell history. Confirm `/usr/bin/python3 --version` is 3.11 or newer; adjust the unit's Python path if required.

For an existing experiment, stop its old server and move its complete data directory while it is stopped, before initializing access. Set the new directory owner to `agent-arena`. Keep the original private backup; do not overwrite the only copy. The example install commands are for a **new** deployment, so do not overwrite an existing `.env`.

### Configure and start the services

Edit `deploy/agent-arena.service` and `deploy/Caddyfile` to replace **both** `arena.example.com` placeholders with your hostname. Keep the backend on `127.0.0.1:8765`.

```bash
sudo install -o root -g root -m 0644 deploy/agent-arena.service /etc/systemd/system/agent-arena.service
sudo systemd-analyze verify /etc/systemd/system/agent-arena.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-arena
sudo systemctl status agent-arena --no-pager
```

If Caddy already hosts other applications, merge the supplied site block into the existing Caddyfile instead of replacing it. On a dedicated new Caddy installation:

```bash
sudo install -o root -g root -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

The supplied Caddy block proxies HTTP over loopback and preserves the requested Host. It does not serve the application directory as static files. See [Caddy reverse proxy](https://caddyserver.com/docs/quick-starts/reverse-proxy) and [Caddy's system service](https://caddyserver.com/docs/running#using-the-service).

Open `https://YOUR_HOST` on the phone and enter it in the APK. Sign in with the application access secret. In a separate private browser window, confirm that unauthenticated access to `/api/state` does not return experiment data. Reconcile and manually resume the paper experiment only after the connected accounts and allocations are correct.

### Operation

```bash
sudo systemctl status agent-arena --no-pager
sudo journalctl -u agent-arena -n 100 --no-pager
sudo systemctl restart agent-arena
sudo systemctl stop agent-arena
```

The unit restarts a failed Python process and starts it after a boot. Previously enabled paper automation can enter startup recovery when its restart preference allows it. Each active account must reconcile before its agent resumes. Operator pauses, STOP and risk stops remain active. Stopping the server disables its local exit checks; broker orders and positions remain. Manage any outstanding paper orders at Alpaca if the application cannot be reached.

For a persistent experiment halt from the machine:

```bash
sudo -u agent-arena /usr/bin/python3 /opt/agent-arena/server.py --data-dir /var/lib/agent-arena --halt
```

This halts automation; it does not cancel existing broker orders or close holdings. Keep data backups, the API keys and the login secret private. Back up the full data directory with the service stopped. Keep `arena.sqlite3` and `service.sqlite3` as a matching pair, including any remaining sidecars. Copying a running SQLite main file alone can omit active transactions. A rollback must restore the old code and the complete matching data backup together.

## Change the application login secret

Stop Agent Arena first. Ensure `ARENA_ACCESS_TOKEN` is unset in both `.env` and the process environment if using the token file. Remove only the old `access-token` file from the configured data directory, then run `--init-access` with that same directory and operating-system user. Save the new secret and restart the server. Do not delete databases or the whole data directory. Existing browser sessions end on restart; enter the new secret on the phone. Broker/model key revocation is separate and must be performed with those providers.

## Verification boundary

The configuration files and commands are deployment examples, not evidence of a running deployment. Tailscale/Caddy instructions were checked against official documentation on 2026-09-18. A real Windows task, tailnet, DNS name, TLS certificate, Android connection and provider credentials still require checks on the user's machine. No hosting was purchased or deployed as part of these examples.
