"""Offline regression tests for the v0.5 data and provider boundaries."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch
from arena.adapters import (AlpacaPaper, ApiError, AuthenticationApiError,
                            TransientApiError, autonomous_turn, review_candidate, model_diagnostic)
from arena.autonomous import AutonomousResearch, _compact_evidence
from test_adapters import openai_response, anthropic_response
from test_autonomy_adapter import response, snapshot
from test_autonomous import asset, request

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
def stamp(age=0):
    return (NOW - timedelta(seconds=age)).isoformat()
def quote(age=0, **values):
    return {"bp": 99, "ap": 101, "t": stamp(age), **values}
def bar():
    return {"t": "2026-09-24T04:00:00Z", "o": 100, "h": 102, "l": 99, "c": 101, "v": 1000}

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.broker = AlpacaPaper("K", "S")
        clock = patch("arena.adapters.datetime", wraps=datetime)
        clock.start().now.return_value = NOW
        self.addCleanup(clock.stop)

    def test_trade_only_mark_does_not_invent_bid_ask_or_authorize_order(self):
        self.broker._get = Mock(return_value={"SPY": {"latestQuote": quote(bp=0), "latestTrade": {"p": 102, "t": stamp()}}})
        row = self.broker.quotes(["SPY"])["SPY"]
        self.assertEqual(row["price"], 102)
        self.assertEqual(row["price_source"], "trade")
        self.assertIsNone(row["bid"])
        self.assertIsNone(row["quote_timestamp"])
        self.assertFalse(row["execution_eligible"])
        self.assertIsNone(row["execution_price"])
        self.broker._get.assert_called_once()

    def test_fresh_trade_cannot_refresh_stale_bid_ask(self):
        self.broker._get = Mock(return_value={"SPY": {"latestQuote": quote(600), "latestTrade": {"p": 101, "t": stamp()}}})
        row = self.broker.quotes(["SPY"])["SPY"]
        self.assertEqual(row["price_timestamp"], stamp())
        self.assertEqual(row["quote_timestamp"], stamp(600))
        self.assertFalse(row["execution_eligible"])

    def test_execution_reference_remains_quote_midpoint_when_trade_is_newer(self):
        self.broker = AlpacaPaper("K", "S", feed="sip")
        self.broker._get = Mock(return_value={"SPY": {"latestQuote": quote(10), "latestTrade": {"p": 150, "t": stamp()}}})
        row = self.broker.quotes(["SPY"])["SPY"]
        self.assertTrue(row["execution_eligible"])
        self.assertEqual(row["price"], 150)
        self.assertEqual(row["execution_price"], 100)
        self.assertEqual(row["execution_timestamp"], stamp(10))
        self.assertEqual(row["feed"], "sip")
        self.assertEqual(self.broker._get.call_args.args[1]["feed"], "sip")

    def test_stale_present_primary_uses_newer_delayed_mark_never_for_execution(self):
        self.broker._get = Mock(side_effect=[{"SPY": {"latestQuote": quote(3600)}}, {"SPY": {"latestQuote": quote(900)}}])
        row = self.broker.quotes(["SPY"])["SPY"]
        self.assertEqual(row["t"], stamp(900))
        self.assertEqual(row["feed"], "delayed_sip")
        self.assertEqual(row["price_quality"], "delayed")
        self.assertFalse(row["execution_eligible"])

    def test_delayed_feed_cannot_authorize_even_with_recent_timestamp(self):
        self.broker._get = Mock(side_effect=[{}, {"SPY": {"latestQuote": quote(1)}}])
        self.assertFalse(self.broker.quotes(["SPY"])["SPY"]["execution_eligible"])

    def test_older_fallback_does_not_replace_newer_primary_mark(self):
        self.broker._get = Mock(side_effect=[{"SPY": {"latestQuote": quote(121)}}, {"SPY": {"latestQuote": quote(900)}}])
        row = self.broker.quotes(["SPY"])["SPY"]
        self.assertEqual(row["feed"], "iex")
        self.assertEqual(row["price_quality"], "stale")
        self.assertFalse(row["execution_eligible"])

    def test_symbol_and_quote_trade_validation_failures_are_isolated(self):
        self.broker._get = Mock(side_effect=[{
            "SPY": {"latestQuote": quote()},
            "QQQ": {"latestQuote": quote(bp="bad"), "latestTrade": {"p": 110, "t": stamp()}},
            "AAPL": {"latestQuote": quote(bp=float("inf")), "latestTrade": {"p": 100, "t": "bad"}}}, {}])
        rows = self.broker.quotes(["SPY", "QQQ", "AAPL"])
        self.assertEqual(set(rows), {"SPY", "QQQ"})
        self.assertTrue(rows["SPY"]["execution_eligible"])
        self.assertFalse(rows["QQQ"]["execution_eligible"])

    def test_future_primary_and_failed_fallback_cannot_create_mark(self):
        self.broker._get = Mock(side_effect=[{"SPY": {"latestQuote": quote(-60)}}, ApiError("fallback unavailable")])
        self.assertEqual(self.broker.quotes(["SPY"]), {})

    def test_primary_error_stays_visible(self):
        self.broker._get = Mock(side_effect=TransientApiError("read failed"))
        with self.assertRaises(TransientApiError):
            self.broker.quotes(["SPY"])
        self.broker._get.assert_called_once()

    def test_research_retains_provenance_and_isolates_bad_symbol(self):
        self.broker.quotes = Mock(return_value={
            "SPY": {"price": 100, "bid": None, "ask": None, "t": stamp(900),
                    "price_timestamp": stamp(900), "quote_timestamp": None, "feed": "delayed_sip",
                    "price_source": "trade", "price_quality": "delayed", "execution_eligible": False},
            "QQQ": {"price": float("nan"), "t": stamp()}})
        evidence = AutonomousResearch(self.broker, [asset("SPY"), asset("QQQ")], stamp()).run(request("quotes", ["SPY", "QQQ"]))
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["data"]["unavailable_symbols"], ["QQQ"])
        self.assertEqual(evidence["data"]["quotes"]["SPY"]["feed"], "delayed_sip")
        self.assertFalse(_compact_evidence(evidence)["data"]["quotes"]["SPY"]["execution_eligible"])

class HistoricalTests(unittest.TestCase):
    def test_split_sip_end_excludes_current_session_and_records_actual_feed(self):
        broker = AlpacaPaper("K", "S")
        broker._get = Mock(return_value={"bars": {"SPY": [bar()]}})
        result = broker.bars(["SPY"], "2026-06-01", "2026-09-25")
        params = broker._get.call_args.args[1]
        self.assertEqual(params["end"], "2026-09-24T23:59:59Z")
        self.assertEqual(params["feed"], "sip")
        self.assertEqual(params["adjustment"], "split")
        self.assertEqual(result["SPY"][0]["feed"], "sip")
        self.assertEqual(result["SPY"][0]["adjustment"], "split")

    def test_permission_fallback_and_compacted_evidence_name_actual_feed(self):
        broker = AlpacaPaper("K", "S")
        broker._get = Mock(side_effect=[AuthenticationApiError("forbidden", status_code=403), {"bars": {"SPY": [bar()]}}])
        evidence = AutonomousResearch(broker, [asset("SPY")], stamp()).run(request("bars", ["SPY"]))
        self.assertEqual(evidence["status"], "ok")
        provenance = evidence["data"]["provenance"]["SPY"]
        self.assertEqual(provenance["feeds"], ["iex"])
        self.assertEqual(provenance["adjustments"], ["split"])
        self.assertEqual(provenance["fallbacks"], ["sip_permission_denied"])
        self.assertEqual(_compact_evidence(evidence)["data"]["provenance"]["SPY"], provenance)
        self.assertNotIn("page_token", broker._get.call_args.args[1])

    def test_credentials_network_and_invalid_query_do_not_change_feed(self):
        for status in (400, 401, 422, 429, 503):
            with self.subTest(status=status):
                broker = AlpacaPaper("K", "S")
                broker._get = Mock(side_effect=ApiError("unavailable", status_code=status))
                with self.assertRaises(ApiError):
                    broker.bars(["SPY"], "2026-06-01", "2026-09-25")
                broker._get.assert_called_once()

    def test_explicit_iex_history_preserved_and_bad_feed_rejected(self):
        broker = AlpacaPaper("K", "S", historical_feed="iex")
        broker._get = Mock(return_value={"bars": {"SPY": [bar()]}})
        self.assertEqual(broker.bars(["SPY"], "2026-06-01", "2026-09-25")["SPY"][0]["feed"], "iex")
        with self.assertRaises(ApiError):
            AlpacaPaper("K", "S", historical_feed="delayed_sip")

class ModelAllowanceTests(unittest.TestCase):
    @patch("arena.adapters._request")
    def test_configured_cap_and_actual_model_in_both_adapters(self, paid):
        for provider, envelope in (("openai", openai_response()), ("anthropic", anthropic_response())):
            with self.subTest(provider=provider):
                paid.return_value = envelope
                result = review_candidate(provider, "selected-model", "K", {}, max_output_tokens=4096)
                body = paid.call_args.args[3]
                self.assertEqual(body.get("max_output_tokens", body.get("max_tokens")), 4096)
                self.assertEqual(result["model"], envelope["model"])
                paid.return_value = response(provider)
                result = autonomous_turn(provider, "selected-model", "K", snapshot(), max_output_tokens=16384)
                body = paid.call_args.args[3]
                self.assertEqual(body.get("max_output_tokens", body.get("max_tokens")), 16384)
                self.assertEqual(result["model"], paid.return_value["model"])

    @patch("arena.adapters._request")
    def test_bad_allowance_rejected_before_paid_call(self, paid):
        for value in (True, 1023, 16385, 1024.0, "8192", None):
            for function in (review_candidate, autonomous_turn):
                with self.subTest(value=value, function=function.__name__), self.assertRaises(ApiError):
                    function("openai", "selected-model", "K", {}, max_output_tokens=value)
        paid.assert_not_called()

    @patch("arena.adapters._request", return_value=anthropic_response())
    def test_diagnostic_one_small_structured_call_with_actual_usage(self, paid):
        result = model_diagnostic("anthropic", "selected-model", "K")
        self.assertEqual(result, {"ok": True, "input_tokens": 200, "output_tokens": 30, "model": "configured-claude"})
        paid.assert_called_once()
        self.assertEqual(paid.call_args.args[3]["max_tokens"], 1024)
        self.assertLess(len(str(paid.call_args.args[3])), 4000)
