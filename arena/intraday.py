"""Execution policy for short-lived paper positions; no broker or model calls."""
from datetime import datetime, timedelta, timezone
import math

MAX_HOLD_MINUTES = 30
EXIT_CHECK_SECONDS = 5
CLOSE_BUFFER_MINUTES = 10
ENTRY_BUFFER_MINUTES = 15
MAX_SPREAD_BPS = 20
STOP_LOSS_PCT = 1
TAKE_PROFIT_PCT = 1.5


def policy():
    return {"style": "aggressive_intraday", "review_minutes": 1,
            "max_hold_minutes": MAX_HOLD_MINUTES, "exit_check_seconds": EXIT_CHECK_SECONDS,
            "flatten_minutes_before_close": CLOSE_BUFFER_MINUTES,
            "entry_buffer_minutes": ENTRY_BUFFER_MINUTES, "max_entry_spread_bps": MAX_SPREAD_BPS,
            "fallback_stop_loss_pct": STOP_LOSS_PCT, "fallback_take_profit_pct": TAKE_PROFIT_PCT,
            "objective": "Seek short-lived intraday opportunities with positive expected net return. "
                         "Use completed one-minute bars and current executable quotes. Rotate capital "
                         "after confirmed fills; do not hold equities overnight or churn without an edge. "
                         "Risk caps, broker restrictions and API budget always apply."}


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(label + " must be a finite number.")
    return number


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Broker time must include a timezone.")
    return result.astimezone(timezone.utc)


def check_entry(account, quote, amount, clock, at, *, crypto=False):
    """Check current broker power, cash, restrictions and execution costs before POST."""
    if account.get("status") != "ACTIVE" or any(account.get(k) for k in
            ("trading_blocked", "account_blocked", "trade_suspended_by_user")):
        raise ValueError("Broker account currently restricts trading.")
    if account.get("currency", "USD") != "USD":
        raise ValueError("Intraday mode requires a USD account.")
    cash = _finite(account.get("cash"), "Broker cash")
    power = _finite(account.get("buying_power"), "Broker buying power")
    available = min(cash, power)
    if account.get("non_marginable_buying_power") is not None:
        available = min(available, _finite(account["non_marginable_buying_power"], "Non-marginable buying power"))
    amount = _finite(amount, "Order amount")
    if amount <= 0 or amount * 1.001 > available:
        raise ValueError("Current broker cash or buying power cannot cover this cash-only entry.")
    bid, ask = _finite(quote.get("bid"), "Bid"), _finite(quote.get("ask"), "Ask")
    if not 0 < bid <= ask or (ask - bid) / ((ask + bid) / 2) * 10000 > MAX_SPREAD_BPS:
        raise ValueError("The quote spread is too wide for aggressive intraday entry.")
    if not crypto:
        if not clock.get("is_open"):
            raise ValueError("Stock session is closed.")
        closing = _time(clock["next_close"])
        if closing - at <= timedelta(minutes=ENTRY_BUFFER_MINUTES):
            raise ValueError("Stock entry is blocked by the intraday closing buffer.")


def effective_exit(rule, position):
    """A model may tighten these bounds, but cannot disable or extend them."""
    average = _finite(position["avg_price"], "Entry price")
    if average <= 0:
        raise ValueError("Entry price must be positive.")
    result = dict(rule)
    result["stop_loss"] = max(float(rule.get("stop_loss", 0)), average * (1 - STOP_LOSS_PCT / 100))
    target = float(rule.get("take_profit", 0))
    result["take_profit"] = min(target, average * (1 + TAKE_PROFIT_PCT / 100)) if target > 0 else average * (1 + TAKE_PROFIT_PCT / 100)
    hours = float(rule.get("max_hold_hours", 0))
    result["max_hold_hours"] = min(hours, MAX_HOLD_MINUTES / 60) if hours > 0 else MAX_HOLD_MINUTES / 60
    result["policy"] = "aggressive_intraday"
    return result


def session_close_due(symbol, clock, at):
    if symbol.endswith("/USD") or not clock.get("is_open"):
        return False
    closing = _time(clock["next_close"])
    return timedelta(0) < closing - at <= timedelta(minutes=CLOSE_BUFFER_MINUTES)
