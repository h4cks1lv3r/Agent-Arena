"""Short overnight trades with conservative protection against same-day round trips."""
from copy import deepcopy
from datetime import date
import math

from .autonomous import _ny_date

LIMIT = 3
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


def policy(style="fast_swing"):
    strict = style == "fast_swing_strict"
    return {"style": style, "review_minutes": 1, "exit_check_seconds": 5,
            "routine_same_day_exits": False, "protective_same_day_exits": not strict,
            "protective_round_trip_limit": LIMIT, "window_trading_sessions": 5,
            "objective": "Seek short overnight opportunities with positive expected net return. "
                         "Do not plan same-day stock round trips, pyramiding or same-day re-entry. "
                         "Routine exits begin at the next eligible stock session, without a 30-minute "
                         "holding cap or forced closing-time liquidation. Use minute bars for timing, "
                         "but assess overnight risk. Cash, broker restrictions, risk caps and API budget apply.",
            "protection": ("All same-day stock exits are blocked, including stops; overnight losses can grow."
                           if strict else "Same-day stop-loss exits are allowed only within a conservative "
                           "three-round-trip capacity over five broker trading sessions. Entries reserve "
                           "capacity for protective exits. This is a voluntary guard; Alpaca removed PDT.")}


def session_dates(calendar, today):
    if not isinstance(calendar, list):
        raise ValueError("The broker trading calendar is unavailable.")
    values = []
    for row in calendar:
        value = row["date"]
        parsed = date.fromisoformat(value)
        if parsed > today or value in values:
            raise ValueError("The broker trading calendar is inconsistent.")
        values.append(value)
    values.sort()
    if len(values) < 5 or today.isoformat() not in values:
        raise ValueError("Five broker trading sessions including today are required.")
    return values[-5:]


def _groups(orders, positions, sessions, at):
    today = _ny_date(at.isoformat()).isoformat()
    if len(sessions) != 5 or len(set(sessions)) != 5 or today not in sessions:
        raise ValueError("A current five-session trading window is required.")
    if not isinstance(orders, list):
        raise ValueError("Complete broker order history is required.")
    groups, seen = {}, set()
    for row in orders:
        symbol = row["symbol"]
        if not isinstance(symbol, str):
            raise ValueError("Broker order symbol is invalid.")
        if symbol.endswith("/USD"):
            continue
        side = row["side"]
        if side not in ("buy", "sell"):
            raise ValueError("Broker order side is invalid.")
        raw_qty = row.get("filled_qty", 0)
        qty = float(raw_qty)
        if isinstance(raw_qty, bool) or not math.isfinite(qty) or qty < 0:
            raise ValueError("Broker fill quantity is invalid.")
        active = row.get("status") not in TERMINAL
        if qty == 0 and not active:
            continue
        identifier = row.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("Broker history contains missing or duplicate order IDs.")
        seen.add(identifier)
        stamp = row.get("filled_at") or row.get("updated_at") or row.get("submitted_at") or row.get("created_at")
        if not stamp:
            raise ValueError("Broker fill timing cannot be verified.")
        latest = _ny_date(stamp)
        earliest = _ny_date(row.get("submitted_at") or row.get("created_at") or stamp)
        if latest > date.fromisoformat(today) or earliest > latest:
            raise ValueError("Broker fill timing is inconsistent.")
        # Partial fills may span dates; count each possible fill session conservatively.
        for session in sessions:
            if (qty > 0 and earliest <= date.fromisoformat(session) <= latest) or (active and session == today):
                groups.setdefault((session, symbol), {"buy": set(), "sell": set()})[side].add(identifier)
    for symbol, position in positions.items():
        if symbol.endswith("/USD"):
            continue
        opened = position.get("opened_at")
        if not opened:
            raise ValueError("Position purchase timing cannot be verified.")
        if _ny_date(opened).isoformat() == today:
            group = groups.setdefault((today, symbol), {"buy": set(), "sell": set()})
            if not group["buy"]:
                group["buy"].add("unverified-current-position")
    return groups, today


def _capacity(groups, today):
    total = 0
    for (session, _), sides in groups.items():
        buys, sells = len(sides["buy"]), len(sides["sell"])
        if buys and (sells or session == today):
            total += max(buys, sells)
    return total


def check_order(orders, positions, sessions, at, symbol, side, *, protective=False, strict=False):
    groups, today = _groups(orders, positions, sessions, at)
    current = groups.get((today, symbol), {"buy": set(), "sell": set()})
    if side == "buy":
        if symbol in positions or current["buy"] or current["sell"]:
            raise ValueError("No stock pyramiding or same-day re-entry in short-swing mode.")
    elif side == "sell":
        if current["buy"] and (not protective or strict):
            raise ValueError("A stock bought today must wait until a later trading session for this exit.")
    else:
        raise ValueError("Invalid order side.")
    projected = deepcopy(groups)
    projected.setdefault((today, symbol), {"buy": set(), "sell": set()})[side].add("proposed-order")
    capacity = _capacity(projected, today)
    if capacity > LIMIT:
        raise ValueError("Protective round-trip capacity is exhausted in the five-session window.")
    return {"reserved_round_trip_capacity": capacity, "limit": LIMIT,
            "trading_sessions": list(sessions), "strict_same_day_block": strict}


def effective_exit(rule, position):
    average = float(position["avg_price"])
    result = dict(rule)
    result["stop_loss"] = max(float(rule.get("stop_loss", 0)), average * .99)
    target = float(rule.get("take_profit", 0))
    result["take_profit"] = min(target, average * 1.015) if target > 0 else average * 1.015
    result["max_hold_hours"] = 0
    result["exit_after_purchase_day"] = True
    result["policy"] = "fast_swing"
    return result


def next_session_due(position, clock, at):
    if not clock.get("is_open"):
        return False
    opened = position.get("opened_at")
    return bool(opened and _ny_date(opened) < _ny_date(at.isoformat()))
