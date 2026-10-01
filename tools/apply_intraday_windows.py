"""Update an existing Windows paper arena while preserving its ledger and controls."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
import urllib.request

EXCLUDED = {"data", "__pycache__", ".git", "build", "node_modules"}


class LocalArena:
    def __init__(self, target):
        self.target = target
        self.origin = (target / "data" / "public-origin.txt").read_text().strip()
        parsed = urlsplit(self.origin)
        if parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment:
            raise RuntimeError("The saved HTTPS origin needs review.")
        self.host = parsed.netloc
        self.cookie = self.csrf = ""
        result, headers = self.request("/api/login", {"access_token": (target / "data" / "access-token").read_text().strip()})
        self.cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        self.csrf = result["token"]
        if not self.cookie:
            raise RuntimeError("Server authentication failed.")

    def request(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=20)
        headers = {"Host": self.host, "Origin": self.origin}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf:
            headers["X-Arena-Token"] = self.csrf
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request("GET" if body is None else "POST", path, body=payload, headers=headers)
            response = conn.getresponse()
            data = json.loads(response.read())
            if not 200 <= response.status < 300:
                raise RuntimeError("Arena request failed (HTTP %s)." % response.status)
            return data, dict(response.getheaders())
        finally:
            conn.close()

    def state(self):
        state = self.request("/api/state")[0]
        with sqlite3.connect((self.target / "data" / "arena.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            saved = json.loads(db.execute("SELECT payload FROM arena_state WHERE id=1").fetchone()[0])
        if state["experiment"]["id"] != saved["experiment"]["id"]:
            raise RuntimeError("The authenticated server does not match this installation.")
        return state

    def wait_idle(self, seconds=50):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = self.state()
            if state.get("server_job", {}).get("status") not in ("queued", "running") and not state.get("monitor", {}).get("busy"):
                return state
            time.sleep(.5)
        raise RuntimeError("Account work has not stopped yet. No source files were replaced.")

    def mutate(self, route, body):
        self.request(route, body)
        state = self.wait_idle()
        if state.get("server_job", {}).get("status") == "error":
            raise RuntimeError("Arena control did not complete; inspect the dashboard.")
        return state


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def powershell(code):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", code],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Windows process verification failed.")
    return result.stdout.strip()


def inspect_brokers(target):
    env = {}
    for line in (target / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    results = {}
    for agent in ("openai", "claude"):
        prefix = "ALPACA_" + agent.upper()
        request = urllib.request.Request("https://paper-api.alpaca.markets/v2/account",
            headers={"APCA-API-KEY-ID": env[prefix + "_KEY"],
                     "APCA-API-SECRET-KEY": env[prefix + "_SECRET"]})
        with urllib.request.urlopen(request, timeout=20) as response:
            account = json.load(response)
        results[agent] = {key: account.get(key) for key in
            ("status", "currency", "cash", "equity", "buying_power", "non_marginable_buying_power",
             "multiplier", "trading_blocked", "account_blocked", "trade_suspended_by_user")}
    print(json.dumps({"paper_account_read_only": results}, indent=2))


def deploy(args):
    source, target = args.source.resolve(), args.target.resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise RuntimeError("Source and installed directories must be separate.")
    validation = args.validation.read_text(encoding="utf-8-sig")
    if "Ran 371 tests" not in validation or "\nOK" not in validation or "FAILED" in validation:
        raise RuntimeError("The complete offline regression suite must pass before deployment.")
    for root in (source, target):
        for path in root.rglob("*"):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise RuntimeError("A source or installation link needs review before deployment.")
    client = LocalArena(target)
    before = client.state()
    exp = before["experiment"]
    preferences_path = target / "data" / "intraday-upgrade-controls.json"
    preferences = {"experiment": exp["id"], "status": exp["status"], "autopilot": exp.get("autopilot", False),
                   "auto_resume": exp.get("auto_resume", False), "complete": False}
    if preferences_path.exists():
        prior = json.loads(preferences_path.read_text())
        if prior.get("experiment") == exp["id"] and not prior.get("complete"):
            preferences = prior
    preferences_path.write_text(json.dumps(preferences))
    client.request("/api/halt", {})
    client.request("/api/auto-resume", {"enabled": False})
    client.wait_idle()
    port = json.loads(powershell("$arenaPort = @(Get-NetTCPConnection -State Listen -LocalPort 8765); "
        "if ($arenaPort.Count -ne 1 -or $arenaPort[0].LocalAddress -ne '127.0.0.1') { exit 1 }; "
        "$arenaProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $arenaPort[0].OwningProcess); "
        "if ($arenaProcess.Name -notin @('python.exe','pythonw.exe') -or $arenaProcess.CommandLine -notmatch '(?i)\\bserver\\.py\\b') { exit 1 }; "
        "@{ pid=$arenaPort[0].OwningProcess } | ConvertTo-Json -Compress"))
    powershell("Stop-Process -Id " + str(int(port["pid"])) + " -ErrorAction Stop")
    for _ in range(30):
        if not powershell("@(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue).Count") == "1":
            break
        time.sleep(.3)
    else:
        raise RuntimeError("The previous listener has not exited.")
    # The old process must also have released its ledger lock.
    import msvcrt
    with (target / "data" / "server.lock").open("r+b") as lock:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    backup = target.with_name(target.name + "_backup_intraday_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if backup.exists() or backup.parent != target.parent:
        raise RuntimeError("Backup path needs review.")
    shutil.copytree(target, backup)
    saved = {p.relative_to(target): digest(p) for p in target.rglob("*") if p.is_file()}
    for relative, expected in saved.items():
        if digest(backup / relative) != expected:
            raise RuntimeError("Backup verification failed; source was not replaced.")
    source_files = [p for p in source.rglob("*") if p.is_file() and
                    not set(p.relative_to(source).parts) & EXCLUDED and p.name != ".env"
                    and p.suffix not in (".pyc", ".apk", ".p12", ".jks", ".keystore", ".orig", ".rej")]
    for path in source_files:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    for relative, expected in saved.items():
        if relative.parts[0] == "data" or str(relative) == ".env":
            if digest(target / relative) != expected:
                raise RuntimeError("Private configuration or ledger preservation failed.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with (target / "data" / ("server-intraday-" + stamp + "-stdout.log")).open("wb") as out, \
         (target / "data" / ("server-intraday-" + stamp + "-stderr.log")).open("wb") as err:
        child = subprocess.Popen([sys.executable, "-u", str(target / "server.py"), "--data-dir",
            str(target / "data"), "--no-browser", "--public-origin", client.origin], cwd=target,
            stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    replacement = None
    for _ in range(40):
        if child.poll() is not None:
            raise RuntimeError("The new server exited; private startup logs and the full backup were retained.")
        try:
            replacement = LocalArena(target)
            state = replacement.state()
            break
        except (OSError, ValueError, RuntimeError):
            time.sleep(.5)
    if replacement is None or state["version"] != "0.7.0" or state["experiment"]["id"] != exp["id"]:
        raise RuntimeError("The updated server could not be verified.")
    budget = exp["monthly_model_budget"] if args.model_budget is None else args.model_budget
    state = replacement.mutate("/api/trading-style", {"style": "aggressive_intraday", "monthly_model_budget": budget})
    for key in ("target", "loss_limit", "position_cap_pct", "exposure_cap_pct", "asset_scope"):
        if state["experiment"].get(key) != exp.get(key):
            raise RuntimeError("An existing experiment boundary changed unexpectedly.")
    replacement.request("/api/auto-resume", {"enabled": preferences["auto_resume"]})
    if preferences["status"] == "ready":
        state = replacement.mutate("/api/resume", {})
    if preferences["autopilot"]:
        state = replacement.mutate("/api/autopilot", {"enabled": True})
    state = replacement.state()
    preferences["complete"] = True
    preferences_path.write_text(json.dumps(preferences))
    print(json.dumps({"version": state["version"], "style": state["experiment"]["trading_style"],
        "automatic_cycles": state["experiment"]["autopilot"], "status": state["experiment"]["status"],
        "monthly_model_budget": state["experiment"]["monthly_model_budget"],
        "cycle_minutes": state["experiment"]["cycle_minutes"],
        "monitor_seconds": state["monitor"]["refresh_seconds"],
        "agent_paused": {k: v["paused"] for k, v in state["agent_controls"].items()},
        "backup": str(backup), "server_pid": child.pid,
        "source_files": len(source_files)}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / "Trading Agents" / "Agent_Arena")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--model-budget", type=float)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    try:
        if args.inspect:
            inspect_brokers(args.target)
        elif args.source and args.validation:
            deploy(args)
        else:
            parser.error("Supply --inspect, or prepared --source and --validation paths.")
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else
              "Intraday workflow could not complete; no credentials were displayed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
