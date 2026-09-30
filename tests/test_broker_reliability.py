"""Broker failure and long-run pagination boundaries. No external calls."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from arena.adapters import (
    AlpacaPaper, ApiError, AuthenticationApiError, MAX_MODEL_OUTPUT_TOKENS,
    OrderNotFoundError, OrderRejectedError, SubmissionUncertainError,
    TransientApiError, _request, autonomous_turn, review_candidate,
)


PAYLOAD = {"symbol": "SPY", "notional": "10", "side": "buy", "type": "market",
           "time_in_force": "day", "client_order_id": "arena-original-client"}


def http_error(status, body=None):
    raw = json.dumps(body if body is not None else {"message": "Insufficient buying power SECRET"}).encode()
    return urllib.error.HTTPError("https://paper-api.alpaca.markets/v2/orders?secret=SECRET", status,
                                  "SECRET", {}, io.BytesIO(raw))


def page(start, stop):
    # Every record deliberately has exactly the same timestamp.
    return [{"id": f"order-{i:06d}", "client_order_id": f"arena-{i}",
             "submitted_at": "2026-09-25T14:00:00.123456789Z", "status": "filled"}
            for i in range(start, stop)]


def distinct_page(start, stop):
    rows = page(start, stop)
    for i, row in enumerate(rows, start):
        row["submitted_at"] = f"2026-09-25T14:00:{i:02d}Z"
    return rows


class FailureClassificationTests(unittest.TestCase):
    @patch("arena.adapters.urllib.request.build_opener")
    def test_definitive_submission_rejections_are_typed_safe_and_not_retried(self, build):
        broker = AlpacaPaper("KEY", "SECRET")
        for code in (400, 401, 403, 404, 405, 422):
            with self.subTest(status=code):
                build.return_value.open.reset_mock()
                build.return_value.open.side_effect = http_error(code)
                with self.assertRaises(OrderRejectedError) as caught:
                    broker.submit_order(PAYLOAD)
                self.assertEqual(caught.exception.status_code, code)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(build.return_value.open.call_count, 1)

    @patch("arena.adapters.urllib.request.build_opener")
    def test_ambiguous_status_never_claims_rejection_or_replays(self, build):
        broker = AlpacaPaper("KEY", "SECRET")
        for code in (307, 408, 409, 429, 500, 502, 503):
            with self.subTest(status=code):
                build.return_value.open.reset_mock()
                build.return_value.open.side_effect = http_error(code)
                with self.assertRaises(SubmissionUncertainError) as caught:
                    broker.submit_order(PAYLOAD)
                self.assertEqual(caught.exception.status_code, code)
                self.assertEqual(build.return_value.open.call_count, 1)

    @patch("arena.adapters.urllib.request.build_opener")
    def test_duplicate_client_id_422_does_not_release_unknown_order(self, build):
        for diagnostic in (
            {"code": 42210000, "message": "client_order_id must be unique SECRET"},
            {"code": "42210000", "message": "Client order ID already exists SECRET"},
            {"message": "client-order-id already in use SECRET"},
            {"error_code": "duplicate", "message": "Order with this client ID exists SECRET"},
            {"message": "Duplicate order identifier SECRET"},
        ):
            with self.subTest(diagnostic=diagnostic):
                build.return_value.open.reset_mock()
                build.return_value.open.side_effect = http_error(422, diagnostic)
                with self.assertRaises(SubmissionUncertainError) as caught:
                    AlpacaPaper("KEY", "SECRET").submit_order(PAYLOAD)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(build.return_value.open.call_count, 1)

    @patch("arena.adapters.urllib.request.build_opener")
    def test_unreadable_422_diagnostic_remains_uncertain(self, build):
        build.return_value.open.side_effect = urllib.error.HTTPError("https://paper-api.alpaca.markets/v2/orders", 422, "", {}, io.BytesIO(b"not json"))
        with self.assertRaises(SubmissionUncertainError):
            AlpacaPaper("KEY", "SECRET").submit_order(PAYLOAD)

    @patch("arena.adapters.urllib.request.build_opener")
    def test_timeout_and_malformed_success_remain_uncertain(self, build):
        broker = AlpacaPaper("KEY", "SECRET")
        for error in (TimeoutError("SECRET"), urllib.error.URLError("SECRET")):
            build.return_value.open.side_effect = error
            with self.assertRaises(SubmissionUncertainError):
                broker.submit_order(PAYLOAD)
        build.return_value.open.side_effect = None
        response = build.return_value.open.return_value.__enter__.return_value
        response.status = 200
        for body in (b"bad json", b"[]", b"null", b"{}", b'{"id":"broker-id"}',
                     b'{"id":"broker-id","client_order_id":"wrong-id","status":"accepted"}',
                     b'{"id":"broker-id","client_order_id":"arena-original-client","status":"garbage"}'):
            response.read.return_value = body
            with self.assertRaises(SubmissionUncertainError):
                broker.submit_order(PAYLOAD)
        for status in ("new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
                       "pending_cancel", "pending_replace", "done_for_day", "stopped", "suspended",
                       "calculated", "replaced", "filled", "canceled", "expired", "rejected"):
            response.read.return_value = json.dumps({"id": "broker-id", "client_order_id": PAYLOAD["client_order_id"], "status": status}).encode()
            self.assertEqual(broker.submit_order(PAYLOAD)["status"], status)

    @patch("arena.adapters.urllib.request.build_opener")
    def test_read_failures_distinguish_recovery_from_auth(self, build):
        for code in (408, 429, 500, 502, 503):
            build.return_value.open.side_effect = http_error(code)
            with self.subTest(code=code), self.assertRaises(TransientApiError):
                AlpacaPaper("KEY", "SECRET").account()
        for code in (401, 403):
            build.return_value.open.side_effect = http_error(code)
            with self.subTest(code=code), self.assertRaises(AuthenticationApiError):
                AlpacaPaper("KEY", "SECRET").account()
        build.return_value.open.side_effect = TimeoutError("SECRET")
        with self.assertRaises(TransientApiError):
            AlpacaPaper("KEY", "SECRET").positions()

    @patch("arena.adapters.urllib.request.build_opener")
    def test_only_specific_order_404_is_not_found(self, build):
        build.return_value.open.side_effect = http_error(404)
        with self.assertRaises(OrderNotFoundError):
            AlpacaPaper("KEY", "SECRET").order_by_client_id("arena-original-client")
        with self.assertRaises(ApiError) as caught:
            AlpacaPaper("KEY", "SECRET").account()
        self.assertNotIsInstance(caught.exception, OrderNotFoundError)


class OrderPaginationTests(unittest.TestCase):
    @patch("arena.adapters._request")
    def test_1501_orders_with_same_timestamp_complete_using_id_cursors(self, request):
        request.side_effect = [page(0, 500), page(500, 1000), page(1000, 1500), page(1500, 1501)]
        result = AlpacaPaper("KEY", "SECRET").orders()
        self.assertEqual([row["id"] for row in result], [row["id"] for row in page(0, 1501)])
        params = [parse_qs(urlsplit(call.args[1]).query) for call in request.call_args_list]
        self.assertTrue(all(query["direction"] == ["asc"] for query in params))
        self.assertNotIn("after_order_id", params[0])
        self.assertEqual(params[1]["after_order_id"], ["order-000499"])
        self.assertEqual(params[2]["after_order_id"], ["order-000999"])
        self.assertTrue(all("after" not in query and "until" not in query for query in params))

    @patch("arena.adapters._request")
    def test_exact_500_requires_empty_next_page(self, request):
        request.side_effect = [page(0, 500), []]
        self.assertEqual(len(AlpacaPaper("KEY", "SECRET").orders("open")), 500)
        self.assertEqual(request.call_count, 2)

    @patch("arena.adapters._request")
    def test_incremental_query_begins_at_persisted_order_id(self, request):
        request.return_value = page(2001, 2003)
        result = AlpacaPaper("KEY", "SECRET").orders(after_order_id="order-002000")
        self.assertEqual(len(result), 2)
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[1]).query)["after_order_id"], ["order-002000"])

    @patch("arena.adapters._request")
    def test_ignored_saved_cursor_recovers_only_after_complete_small_history(self, request):
        # Observed paper behavior: the saved cursor is last among three records.
        request.side_effect = [distinct_page(0, 3), distinct_page(0, 3)]
        self.assertEqual(AlpacaPaper("KEY", "SECRET").orders(after_order_id="order-000002"), [])
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("after_order_id", parse_qs(urlsplit(request.call_args_list[1].args[1]).query))

    @patch("arena.adapters._request")
    def test_ignored_saved_cursor_returns_only_new_orders(self, request):
        request.side_effect = [distinct_page(0, 4), distinct_page(0, 4)]
        result = AlpacaPaper("KEY", "SECRET").orders(after_order_id="order-000002")
        self.assertEqual([row["id"] for row in result], ["order-000003"])

    @patch("arena.adapters._request")
    def test_ignored_cursor_does_not_trust_a_full_or_missing_history(self, request):
        for history in (page(0, 500), page(0, 2), page(1, 3) + page(1, 2)):
            with self.subTest(history_size=len(history)):
                request.side_effect = [distinct_page(0, 3), history]
                with self.assertRaisesRegex(ApiError, "incomplete"):
                    AlpacaPaper("KEY", "SECRET").orders(after_order_id="order-000002")
                self.assertEqual(request.call_count, 2)
                request.reset_mock()

    @patch("arena.adapters._request")
    def test_ignored_cursor_with_equal_submission_time_remains_paused(self, request):
        request.side_effect = [page(0, 3), page(0, 3)]
        with self.assertRaisesRegex(ApiError, "ambiguous"):
            AlpacaPaper("KEY", "SECRET").orders(after_order_id="order-000002")

    @patch("arena.adapters._request")
    def test_timestamp_cursor_changes_to_id_without_combining_filters(self, request):
        request.side_effect = [page(0, 500), page(500, 501)]
        AlpacaPaper("KEY", "SECRET").orders(after="2026-09-25T13:00:00Z")
        params = [parse_qs(urlsplit(call.args[1]).query) for call in request.call_args_list]
        self.assertIn("after", params[0])
        self.assertNotIn("after", params[1])
        self.assertEqual(params[1]["after_order_id"], ["order-000499"])

    @patch("arena.adapters._request")
    def test_ignored_cursor_fails_instead_of_claiming_complete(self, request):
        request.side_effect = [page(0, 500), page(0, 500)]
        with self.assertRaisesRegex(ApiError, "cursor|progress"):
            AlpacaPaper("KEY", "SECRET").orders()
        self.assertEqual(request.call_count, 2)

    @patch("arena.adapters._request")
    def test_overlapping_page_deduplicates_updated_snapshot(self, request):
        overlap = {**page(0, 1)[0], "status": "canceled"}
        request.side_effect = [page(0, 500), [overlap] + page(500, 501)]
        result = AlpacaPaper("KEY", "SECRET").orders()
        self.assertEqual(len(result), 501)
        self.assertEqual(result[0]["status"], "canceled")
        self.assertEqual(result[-1]["id"], "order-000500")

    @patch("arena.adapters._request")
    def test_invalid_cursors_fail_without_network(self, request):
        broker = AlpacaPaper("KEY", "SECRET")
        for args in ({"after": "2026-09-25T00:00:00"}, {"after": "bad"}, {"after_order_id": "../../id"},
                     {"after": "2026-09-25T00:00:00Z", "after_order_id": "order-id"}):
            with self.subTest(args=args), self.assertRaises(ApiError):
                broker.orders(**args)
        request.assert_not_called()


class SparseMarketDataTests(unittest.TestCase):
    @patch("arena.adapters._request")
    def test_invalid_or_missing_symbol_does_not_suppress_usable_quotes(self, request):
        valid = {"bp": 99, "ap": 101, "t": "2026-09-24T14:00:00Z"}
        request.return_value = {"SPY": {"latestQuote": valid}, "QQQ": {"latestQuote": {**valid, "bp": 0}},
                                "XYZ": {"latestQuote": {**valid, "t": "invalid"}}}
        result = AlpacaPaper("KEY", "SECRET").quotes(["SPY", "QQQ", "XYZ", "AAPL"])
        self.assertEqual(set(result), {"SPY"})
        self.assertEqual(result["SPY"]["t"], valid["t"])  # Never fabricate freshness.

    @patch("arena.adapters._request")
    def test_sip_feed_applies_to_quotes_and_bars(self, request):
        broker = AlpacaPaper("KEY", "SECRET", feed="sip")
        request.return_value = {}
        self.assertEqual(broker.quotes(["SPY"]), {})
        self.assertEqual(parse_qs(urlsplit(request.call_args_list[0].args[1]).query)["feed"], ["sip"])
        request.return_value = {"bars": {}, "next_page_token": None}
        broker.bars(["SPY"], "2026-09-01", "2026-09-25")
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[1]).query)["feed"], ["sip"])
        self.assertEqual(broker.feed, "sip")

    def test_delayed_or_arbitrary_feed_cannot_be_used_for_execution(self):
        for feed in ("delayed_sip", "overnight", "https://evil.test", None):
            with self.subTest(feed=feed), self.assertRaises(ApiError):
                AlpacaPaper("KEY", "SECRET", feed=feed)


class Opus55CompatibilityTests(unittest.TestCase):
    @patch("arena.adapters._request")
    def test_reviewer_accepts_always_on_thinking_but_never_tool_authority(self, request):
        review = {"approve": False, "reason": "No entry evidence."}
        response = {"stop_reason": "end_turn", "model": "claude-opus-5-5",
                    "content": [{"type": "thinking", "thinking": "", "signature": "private"},
                                {"type": "text", "text": json.dumps(review)}],
                    "usage": {"input_tokens": 20, "output_tokens": 1500}}
        request.return_value = response
        result = review_candidate("anthropic", "claude-opus-5-5", "KEY", {})
        self.assertEqual(result["reason"], review["reason"])
        self.assertNotIn("signature", json.dumps(result))
        body = request.call_args.args[3]
        self.assertEqual(body["max_tokens"], MAX_MODEL_OUTPUT_TOKENS)
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"]["effort"], "medium")
        response["content"].insert(0, {"type": "tool_use", "name": "place_order"})
        with self.assertRaises(ApiError):
            review_candidate("anthropic", "claude-opus-5-5", "KEY", {})

    @patch("arena.adapters._request")
    def test_autonomous_opus55_uses_existing_compatible_prefix_settings(self, request):
        plan = {"phase": "plan", "strategy": {"name": "Hold", "thesis": "No entry yet.",
                "invalidation": "New information", "lessons": "None"}, "research": [],
                "orders": [], "exits": [], "watchlist": [], "review_minutes": 60,
                "reason": "Await research."}
        request.return_value = {"stop_reason": "end_turn", "model": "claude-opus-5-5",
                               "content": [{"type": "thinking", "thinking": "", "signature": "private"},
                                           {"type": "text", "text": json.dumps(plan)}],
                               "usage": {"input_tokens": 100, "output_tokens": 200}}
        result = autonomous_turn("anthropic", "claude-opus-5-5", "KEY", {"evidence": []})
        self.assertEqual(result["orders"], [])
        body = request.call_args.args[3]
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("temperature", body)


if __name__ == "__main__":
    unittest.main()
