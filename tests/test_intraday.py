"""Intraday execution acceptance with mocked brokers and temporary ledgers."""
from copy import deepcopy
import datetime as dt
import unittest
from unittest.mock import Mock, patch

from arena.adapters import AlpacaPaper, ApiError, validate_autonomous_response
from arena.autonomous import AutonomousResearch, _model_input
from arena.intraday import check_entry, effective_exit, session_close_due
from arena.service import ExecutionDeferred
import test_audit_acceptance as acceptance
import test_service_autonomous as fixtures


class IntradayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.at = fixtures.START
        self.account = {"status": "ACTIVE", "cash": "250", "buying_power": "250"}
        self.quote = {"bid": 99.99, "ask": 100.01}
        self.clock = {"is_open": True, "next_close": self.at.replace(hour=20).isoformat()}

    def test_fresh_cash_only_entry_uses_current_buying_power(self):
        check_entry(self.account, self.quote, 25, self.clock, self.at)
        for key, value in (("cash", "10"), ("buying_power", "10"), ("non_marginable_buying_power", "10")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_entry({**self.account, key: value}, self.quote, 25, self.clock, self.at)

    def test_missing_or_invalid_broker_buying_power_fails_closed(self):
        for power in (None, "NaN", "Infinity", -1, True):
            with self.subTest(power=power), self.assertRaises((ValueError, TypeError)):
                check_entry({**self.account, "buying_power": power}, self.quote, 25, self.clock, self.at)

    def test_broker_restrictions_and_wide_spread_block_entries(self):
        for key in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_entry({**self.account, key: True}, self.quote, 25, self.clock, self.at)
        with self.assertRaises(ValueError):
            check_entry(self.account, {"bid": 99, "ask": 101}, 25, self.clock, self.at)

    def test_early_close_blocks_entries_and_flattens_existing_stock(self):
        clock = {**self.clock, "next_close": (self.at + dt.timedelta(minutes=9)).isoformat()}
        with self.assertRaises(ValueError):
            check_entry(self.account, self.quote, 25, clock, self.at)
        self.assertTrue(session_close_due("MSFT", clock, self.at))
        self.assertFalse(session_close_due("BTC/USD", clock, self.at))
        self.assertFalse(session_close_due("MSFT", {**clock, "is_open": False}, self.at))

    def test_model_cannot_disable_or_extend_intraday_exit_policy(self):
        for hours in (0, 120, 240):
            rule = effective_exit({"stop_loss": 0, "take_profit": 0, "max_hold_hours": hours}, {"avg_price": 100})
            self.assertEqual(rule["max_hold_hours"], .5)
            self.assertEqual(rule["stop_loss"], 99)
            self.assertAlmostEqual(rule["take_profit"], 101.5)
        tighter = effective_exit({"stop_loss": 99.5, "take_profit": 101, "max_hold_hours": .1}, {"avg_price": 100})
        self.assertEqual(tighter["max_hold_hours"], .1)
        self.assertEqual(tighter["stop_loss"], 99.5)

    def test_one_minute_model_review_is_valid(self):
        self.assertEqual(validate_autonomous_response(fixtures.response(minutes=1))["review_minutes"], 1)
        with self.assertRaises(ApiError):
            validate_autonomous_response(fixtures.response(minutes=0))

    def test_minute_bar_adapter_excludes_incomplete_and_future_bars(self):
        broker = AlpacaPaper("test-key", "test-secret")
        complete = {"t": (self.at - dt.timedelta(minutes=1)).isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 5}
        incomplete = {**complete, "t": self.at.isoformat()}
        future = {**complete, "t": (self.at + dt.timedelta(minutes=1)).isoformat()}
        with patch.object(broker, "_get", return_value={"bars": {"MSFT": [complete, incomplete, future]}}) as get:
            result = broker.intraday_bars(["MSFT"], self.at.isoformat())
        self.assertEqual(len(result["MSFT"]), 1)
        self.assertEqual(get.call_args.args[1]["timeframe"], "1Min")
        self.assertEqual(get.call_args.args[1]["feed"], "iex")

    def test_intraday_research_has_completed_one_minute_evidence(self):
        broker = fixtures.FakePaper("openai", lambda: self.at)
        row = {"t": (self.at - dt.timedelta(minutes=1)).isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 2, "feed": "iex"}
        broker.intraday_bars = Mock(return_value={"MSFT": [row, {**row, "t": self.at.isoformat()}]})
        record = AutonomousResearch(broker, broker.assets(), self.at.isoformat()).run(
            {"kind": "intraday_bars", "query": "", "symbols": ["MSFT"], "lookback_days": 15})
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["data"]["bars"]["MSFT"]["returned_count"], 1)


    def test_large_model_input_keeps_actual_minute_prices_and_policy(self):
        broker = fixtures.FakePaper("openai", lambda: self.at)
        rows = [{"t": (self.at - dt.timedelta(minutes=60-index)).isoformat(),
                 "o": 100, "h": 101, "l": 99, "c": 100 + index / 200,
                 "v": 100000, "feed": "iex"} for index in range(60)]
        broker.intraday_bars = Mock(return_value={"MSFT": rows})
        record = AutonomousResearch(broker, broker.assets(), self.at.isoformat()).run(
            {"kind": "intraday_bars", "query": "", "symbols": ["MSFT"], "lookback_days": 15})
        payload = _model_input({"history": "x" * 36000, "trading_policy": {"max_hold_minutes": 30}},
                               [record], set(), 2, 3)
        self.assertGreater(payload["evidence_condensed_count"], 0)
        minute = payload["evidence"][0]["data"]["bars"]["MSFT"]
        self.assertEqual(minute["bars"][-1]["c"], rows[-1]["c"])
        self.assertEqual(minute["bars"][-1]["feed"], "iex")
        self.assertEqual(minute["timeframe"], "1Min")
        self.assertTrue(minute["completed_bars_only"])
        self.assertEqual(payload["available_evidence_ids"], [record["id"]])
        self.assertEqual(payload["trading_policy"]["max_hold_minutes"], 30)


class IntradayServiceTests(unittest.TestCase):
    tearDown = acceptance.AuditAcceptanceTests.tearDown
    make_service = acceptance.AuditAcceptanceTests.make_service
    agent = acceptance.AuditAcceptanceTests.agent
    advance = acceptance.AuditAcceptanceTests.advance

    def setUp(self):
        acceptance.AuditAcceptanceTests.setUp(self)
        for broker in self.brokers.values():
            account = broker.account()
            broker.account = Mock(return_value={**account, "buying_power": "100000"})
        self.svc.set_trading_style({"style": "aggressive_intraday"})

    def buy(self):
        self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=25)
        self.brokers["openai"].submit_order.reset_mock()

    def test_style_change_preserves_started_experiment_history_and_caps(self):
        self.buy()
        before = self.svc.engine.state()
        self.svc.set_trading_style({"style": "balanced"})
        self.svc.set_trading_style({"style": "aggressive_intraday"})
        after = self.svc.engine.state()
        self.assertEqual(before["experiment"]["id"], after["experiment"]["id"])
        self.assertEqual(before["orders"], after["orders"])
        self.assertEqual(before["agents"], after["agents"])
        for field in ("loss_limit", "position_cap_pct", "exposure_cap_pct", "target", "monthly_model_budget"):
            self.assertEqual(before["experiment"][field], after["experiment"][field])
        self.assertEqual(after["experiment"]["cycle_minutes"], 1)
        self.assertEqual(after["experiment"]["max_cycles_per_day"], 390)

    def test_monitor_closes_old_holding_without_model_calls_or_saved_exits(self):
        self.buy()
        self.svc.engine.set_autopilot(True)
        self.advance(31 * 60)
        with patch("arena.service.autonomous_turn", side_effect=AssertionError("No paid model needed")):
            self.svc.monitor()
        self.assertEqual(self.brokers["openai"].submit_order.call_args.args[0]["side"], "sell")
        self.assertNotIn("MSFT", self.agent()["positions"])

    def test_local_stop_and_take_profit_are_enforced_without_model_review(self):
        for price in (98.5, 102):
            with self.subTest(price=price):
                self.brokers["openai"].prices["MSFT"] = 100
                self.buy()
                self.svc.engine.set_autopilot(True)
                self.brokers["openai"].prices["MSFT"] = price
                self.svc.monitor()
                self.assertEqual(self.brokers["openai"].submit_order.call_args.args[0]["side"], "sell")

    def test_monitor_preserves_halt_agent_pause_and_disabled_automation(self):
        self.buy()
        self.advance(31 * 60)
        self.svc.monitor()
        self.brokers["openai"].submit_order.assert_not_called()
        self.svc.engine.set_autopilot(True)
        self.svc.halt_agent("openai")
        self.svc.monitor()
        self.brokers["openai"].submit_order.assert_not_called()
        self.svc.halt()
        self.svc.monitor()
        self.brokers["openai"].submit_order.assert_not_called()

    def test_session_close_exits_without_waiting_for_hold_timeout(self):
        self.time = self.time.replace(hour=19, minute=40)
        self.buy()
        self.advance(11 * 60)
        self.svc.engine.set_autopilot(True)
        self.svc.monitor()
        self.assertEqual(self.brokers["openai"].submit_order.call_args.args[0]["side"], "sell")

    def test_intraday_buy_is_blocked_before_reservation_if_broker_power_falls(self):
        self.brokers["openai"].account.return_value["buying_power"] = "1"
        count = len(self.svc.engine.state()["orders"])
        with self.assertRaises(ExecutionDeferred):
            self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=25)
        self.assertEqual(len(self.svc.engine.state()["orders"]), count)
        self.brokers["openai"].submit_order.assert_not_called()

    def test_active_display_reports_effective_short_exit_limits(self):
        self.buy()
        state = self.svc.public_state()
        rule = state["autonomy"]["openai"]["effective_exits"]["MSFT"]
        self.assertEqual(rule["max_hold_hours"], .5)
        self.assertEqual(state["monitor"]["refresh_seconds"], 5)
