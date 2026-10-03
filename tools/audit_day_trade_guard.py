"""Read-only verification of stock protection and the actual Alpaca paper accounts."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arena.adapters import AlpacaPaper
from arena.service import load_env, ny_time
from arena.swing import audit_history, session_dates
from apply_intraday_windows import LocalArena


def audit(target):
    state = LocalArena(target).state()
    exp = state["experiment"]
    guard = exp.get("day_trade_guard", {})
    load_env(target / ".env")
    at = dt.datetime.now(dt.timezone.utc)
    today = ny_time(at).date()
    results, account_ids = {}, []
    passed = (exp["mode"] == "paper" and guard.get("enabled") is True
              and exp.get("trading_style") != "aggressive_intraday")
    for agent in state["agents"]:
        prefix = "ALPACA_" + agent["id"].upper()
        broker = AlpacaPaper(os.environ[prefix + "_KEY"], os.environ[prefix + "_SECRET"])
        try:
            account = broker.account()
            account_ids.append(account["id"])
            sessions = session_dates(broker.calendar(
                (today - dt.timedelta(days=30)).isoformat(), today.isoformat()), today)
            orders = broker.orders(status="all")
            capacity = audit_history(orders, agent["positions"], sessions, at,
                                     strict=guard.get("strict", False))
            holdings = {row["symbol"]: float(row["qty"]) for row in broker.positions()}
            local = {symbol: float(row["qty"]) for symbol, row in agent["positions"].items()}
            matches = (holdings.keys() == local.keys() and all(
                math.isfinite(qty) and qty >= 0 and abs(qty - local[symbol]) < 1e-6
                for symbol, qty in holdings.items()))
            unrestricted = account.get("status") == "ACTIVE" and account.get("currency") == "USD" and all(
                account.get(key) is False for key in
                ("trading_blocked", "account_blocked", "trade_suspended_by_user"))
            within_capacity = capacity["reserved_round_trip_capacity"] <= capacity["limit"]
            results[agent["id"]] = {
                "complete_broker_history_verified": True, "broker_order_count": len(orders),
                "position_match": matches, "broker_unrestricted": unrestricted,
                "within_voluntary_capacity": within_capacity, **capacity,
                "account": {key: account.get(key) for key in
                    ("status", "currency", "cash", "buying_power", "non_marginable_buying_power", "multiplier")},
                "retired_pdt_fields_present": sorted(set(account) &
                    {"pattern_day_trader", "daytrade_count", "daytrading_buying_power"})}
            passed = passed and matches and unrestricted and within_capacity
        except Exception:
            results[agent["id"]] = {"complete_broker_history_verified": False,
                                     "checks_passed": False}
            passed = False
    distinct = len(account_ids) == len(state["agents"]) == len(set(account_ids))
    return {"checked_at": at.isoformat(), "server_version": state["version"],
            "trading_style": exp["trading_style"], "saved_guard": guard,
            "distinct_paper_accounts": distinct, "agents": results,
            "checks_passed": bool(passed and distinct), "broker_orders_submitted": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / "Trading Agents" / "Agent_Arena")
    args = parser.parse_args()
    try:
        result = audit(args.target.resolve())
        print(json.dumps(result, indent=2))
        return 0 if result["checks_passed"] else 1
    except Exception:
        print(json.dumps({"checks_passed": False, "broker_orders_submitted": 0,
                          "error": "Account or policy verification could not complete."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
