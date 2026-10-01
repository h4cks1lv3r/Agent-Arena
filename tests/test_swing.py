"""Short-swing acceptance: ordinary exits wait for a later trading session."""
from copy import deepcopy
import datetime as dt
import unittest
from unittest.mock import Mock, patch

from arena.service import ExecutionDeferred
from arena.swing import check_order, next_session_due, session_dates
import test_intraday as intraday
import test_service_autonomous as fixtures

SESSIONS = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]


def filled(symbol, side, stamp, identifier):
    return {"id": identifier, "symbol": symbol, "side": side, "status": "filled",
            "filled_qty": "1", "filled_at": stamp}


class SwingGuardTests(unittest.TestCase):
    def setUp(self):
        self.at = fixtures.START
        self.buy = filled("MSFT", "buy", self.at.isoformat(), "buy-1")
        self.positions = {"MSFT": {"qty": 1, "avg_price": 100, "opened_at": self.at.isoformat()}}

    def test_calendar_window_uses_real_sessions_including_holiday_gaps(self):
        today = dt.date(2026, 9, 17)
        self.assertEqual(session_dates([{"date": d} for d in SESSIONS], today), SESSIONS)
        with self.assertRaises(ValueError):
            session_dates([{"date": d} for d in SESSIONS[:-1]], today)

    def test_same_day_routine_exit_is_blocked_but_protection_has_capacity(self):
        with self.assertRaises(ValueError):
            check_order([self.buy], self.positions, SESSIONS, self.at, "MSFT", "sell")
        result = check_order([self.buy], self.positions, SESSIONS, self.at, "MSFT", "sell", protective=True)
        self.assertEqual(result["reserved_round_trip_capacity"], 1)
        with self.assertRaises(ValueError):
            check_order([self.buy], self.positions, SESSIONS, self.at, "MSFT", "sell", protective=True, strict=True)

    def test_three_prior_round_trips_block_a_new_entry(self):
        records = []
        for index, symbol in enumerate(("AMD", "NVDA", "QQQ")):
            stamp = "2026-09-16T14:00:00+00:00"
            records += [filled(symbol, "buy", stamp, "buy-" + str(index)),
                        filled(symbol, "sell", stamp, "sell-" + str(index))]
        with self.assertRaises(ValueError):
            check_order(records, {}, SESSIONS, self.at, "MSFT", "buy")

    def test_new_positions_reserve_capacity_for_protective_exits(self):
        records = [filled(symbol, "buy", self.at.isoformat(), symbol) for symbol in ("AMD", "NVDA", "QQQ")]
        positions = {symbol: {"opened_at": self.at.isoformat()} for symbol in ("AMD", "NVDA", "QQQ")}
        with self.assertRaises(ValueError):
            check_order(records, positions, SESSIONS, self.at, "MSFT", "buy")
        result = check_order(records, positions, SESSIONS, self.at, "AMD", "sell", protective=True)
        self.assertEqual(result["reserved_round_trip_capacity"], 3)

    def test_prior_day_sale_is_allowed_but_same_day_reentry_is_blocked(self):
        old = filled("MSFT", "buy", "2026-09-16T14:00:00+00:00", "old")
        check_order([old], {"MSFT": {"opened_at": old["filled_at"]}}, SESSIONS, self.at, "MSFT", "sell")
        sold = filled("MSFT", "sell", self.at.isoformat(), "sale")
        with self.assertRaises(ValueError):
            check_order([old, sold], {}, SESSIONS, self.at, "MSFT", "buy")

    def test_missing_fill_times_and_duplicate_history_fail_closed(self):
        bad = {**self.buy}
        bad.pop("filled_at")
        for records in ([bad], [self.buy, self.buy]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                check_order(records, self.positions, SESSIONS, self.at, "MSFT", "sell", protective=True)

    def test_day_boundary_is_new_york_not_utc(self):
        at = dt.datetime(2026, 9, 18, 0, 30, tzinfo=dt.timezone.utc)
        bought = filled("MSFT", "buy", "2026-09-17T23:30:00+00:00", "late")
        position = {"opened_at": bought["filled_at"]}
        self.assertFalse(next_session_due(position, {"is_open": True}, at))
        with self.assertRaises(ValueError):
            check_order([bought], {"MSFT": position}, SESSIONS, at, "MSFT", "sell")

    def test_pending_orders_and_partial_exit_retries_consume_capacity(self):
        records = [{"id": symbol, "symbol": symbol, "side": "buy", "status": "new",
                    "filled_qty": "0", "submitted_at": self.at.isoformat()} for symbol in ("AMD", "NVDA", "QQQ")]
        with self.assertRaises(ValueError):
            check_order(records, {}, SESSIONS, self.at, "MSFT", "buy")
        records = [self.buy, filled("MSFT", "sell", self.at.isoformat(), "partial-1"),
                   filled("MSFT", "sell", self.at.isoformat(), "partial-2"),
                   filled("MSFT", "sell", self.at.isoformat(), "partial-3")]
        with self.assertRaises(ValueError):
            check_order(records, self.positions, SESSIONS, self.at, "MSFT", "sell", protective=True)


class SwingServiceTests(unittest.TestCase):
    tearDown = intraday.IntradayServiceTests.tearDown
    make_service = intraday.IntradayServiceTests.make_service
    agent = intraday.IntradayServiceTests.agent
    advance = intraday.IntradayServiceTests.advance
    buy = intraday.IntradayServiceTests.buy

    def setUp(self):
        intraday.IntradayServiceTests.setUp(self)
        self.svc.set_trading_style({"style": "fast_swing"})
        for broker in self.brokers.values():
            broker.calendar = Mock(side_effect=lambda start, end: [
                {"date": (dt.date.fromisoformat(end) - dt.timedelta(days=i)).isoformat()}
                for i in range(29, -1, -1)
                if (dt.date.fromisoformat(end) - dt.timedelta(days=i)).weekday() < 5])

    def test_no_thirty_minute_or_closing_time_liquidation(self):
        self.buy()
        self.svc.engine.set_autopilot(True)
        self.advance(31 * 60)
        self.svc.monitor()
        self.assertIn("MSFT", self.agent()["positions"])
        self.advance((19 * 3600 + 51 * 60) - (14 * 3600 + 31 * 60))
        self.svc.monitor()
        self.assertIn("MSFT", self.agent()["positions"])
        self.brokers["openai"].submit_order.assert_not_called()

    def test_next_session_exit_needs_no_paid_model_call(self):
        self.buy()
        self.svc.engine.set_autopilot(True)
        self.advance(24 * 3600)
        with patch("arena.service.autonomous_turn", side_effect=AssertionError("No model exit call")):
            self.svc.monitor()
        self.assertNotIn("MSFT", self.agent()["positions"])
        self.assertEqual(self.brokers["openai"].submit_order.call_args.args[0]["side"], "sell")

    def test_stop_is_allowed_with_capacity_and_blocked_in_strict_mode(self):
        self.buy()
        self.svc.engine.set_autopilot(True)
        self.brokers["openai"].prices["MSFT"] = 98
        self.svc.set_trading_style({"style": "fast_swing_strict"})
        self.svc.monitor()
        self.assertIn("MSFT", self.agent()["positions"])
        self.brokers["openai"].submit_order.assert_not_called()
        self.svc.set_trading_style({"style": "fast_swing"})
        self.svc.monitor()
        self.assertNotIn("MSFT", self.agent()["positions"])

    def test_manual_and_model_same_day_sells_are_blocked_before_reservation(self):
        self.buy()
        count = len(self.svc.engine.orders())
        with self.assertRaises(ExecutionDeferred):
            self.svc._submit(self.agent(), "MSFT", "sell", 100, qty=self.agent()["positions"]["MSFT"]["qty"])
        self.assertEqual(len(self.svc.engine.orders()), count)
        self.brokers["openai"].submit_order.assert_not_called()

    def test_same_day_reentry_after_overnight_close_is_blocked(self):
        self.buy()
        self.svc.engine.set_autopilot(True)
        self.advance(24 * 3600)
        self.svc.monitor()
        self.brokers["openai"].submit_order.reset_mock()
        with self.assertRaises(ExecutionDeferred):
            self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=25)
        self.brokers["openai"].submit_order.assert_not_called()

    def test_style_change_preserves_history_and_risk_settings(self):
        self.buy()
        before = self.svc.engine.state()
        self.svc.set_trading_style({"style": "fast_swing_strict"})
        after = self.svc.engine.state()
        self.assertEqual(before["agents"], after["agents"])
        self.assertEqual(before["orders"], after["orders"])
        for key in ("id", "loss_limit", "position_cap_pct", "exposure_cap_pct", "target", "monthly_model_budget"):
            self.assertEqual(before["experiment"][key], after["experiment"][key])
        self.assertEqual(after["experiment"]["cycle_minutes"], 1)

    def test_newer_halt_during_history_read_prevents_broker_submission(self):
        self.buy()
        self.advance(24 * 3600)
        broker = self.brokers["openai"]
        original = broker.orders

        def stop_during_read(**kwargs):
            result = original(**kwargs)
            self.svc.halt("Operator stopped during broker-history read.")
            return result

        broker.orders = Mock(side_effect=stop_during_read)
        try:
            self.svc._submit(self.agent(), "MSFT", "sell", 100, qty=self.agent()["positions"]["MSFT"]["qty"], automatic=True)
        except (ValueError, ExecutionDeferred):
            pass
        broker.submit_order.assert_not_called()
        self.assertFalse(self.svc.engine.pending_orders())
