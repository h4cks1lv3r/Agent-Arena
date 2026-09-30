"""Endpoint-specific order refusals and uncertain outcomes at the HTTP boundary."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from arena.adapters import (
    AlpacaPaper, AuthenticationApiError, OrderNotFoundError,
    OrderRejectedError, SubmissionUncertainError,
)


PAYLOAD = {"symbol": "SPY", "qty": "1", "side": "buy", "type": "market",
           "time_in_force": "day", "client_order_id": "arena-original-uncertain"}


def refusal(status, body):
    return urllib.error.HTTPError(
        "https://paper-api.alpaca.markets/v2/orders?private=PRIVATE",
        status, "PRIVATE reason", {"X-Private": "PRIVATE"},
        io.BytesIO(json.dumps(body).encode()),
    )


class OrderEndpointClassificationTests(unittest.TestCase):
    @patch("arena.adapters.urllib.request.build_opener")
    def test_documented_403_resource_refusals_remain_orders_not_access_failures(self, build):
        broker = AlpacaPaper("KEY", "PRIVATE")
        for side, diagnostic in (
            ("buy", {"code": 40310000, "message": "insufficient buying power PRIVATE"}),
            ("sell", {"code": 40310000, "message": "insufficient qty available for order PRIVATE"}),
        ):
            with self.subTest(side=side):
                build.return_value.open.reset_mock()
                build.return_value.open.side_effect = refusal(403, diagnostic)
                with self.assertRaises(OrderRejectedError) as caught:
                    broker.submit_order({**PAYLOAD, "side": side})
                self.assertEqual(caught.exception.status_code, 403)
                self.assertNotIsInstance(caught.exception, AuthenticationApiError)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(build.return_value.open.call_count, 1)

    @patch("arena.adapters.urllib.request.build_opener")
    def test_identical_403_diagnostic_on_account_read_requires_access_attention(self, build):
        build.return_value.open.side_effect = refusal(
            403, {"code": 40310000, "message": "insufficient buying power PRIVATE"},
        )
        with self.assertRaises(AuthenticationApiError) as caught:
            AlpacaPaper("KEY", "PRIVATE").account()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(build.return_value.open.call_count, 1)
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlsplit(request.full_url).path, "/v2/account")

    @patch("arena.adapters.urllib.request.build_opener")
    def test_lost_submit_then_not_found_only_reads_original_client_id(self, build):
        broker = AlpacaPaper("KEY", "PRIVATE")
        build.return_value.open.side_effect = TimeoutError("PRIVATE transport detail")
        with self.assertRaises(SubmissionUncertainError) as lost:
            broker.submit_order(PAYLOAD)
        self.assertNotIn("PRIVATE", str(lost.exception))
        build.return_value.open.side_effect = refusal(404, {"message": "PRIVATE not found"})
        for _ in range(3):
            with self.assertRaises(OrderNotFoundError) as missing:
                broker.order_by_client_id(PAYLOAD["client_order_id"])
            self.assertNotIn("PRIVATE", str(missing.exception))

        # An eventual acknowledgement must still be for the original request.
        build.return_value.open.side_effect = None
        response = build.return_value.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = json.dumps({
            "id": "broker-late-id", "client_order_id": PAYLOAD["client_order_id"],
            "status": "filled",
        }).encode()
        found = broker.order_by_client_id(PAYLOAD["client_order_id"])
        self.assertEqual(found["id"], "broker-late-id")
        calls = build.return_value.open.call_args_list
        self.assertEqual([call.args[0].get_method() for call in calls],
                         ["POST", "GET", "GET", "GET", "GET"])
        for call in calls[1:]:
            self.assertEqual(parse_qs(urlsplit(call.args[0].full_url).query),
                             {"client_order_id": [PAYLOAD["client_order_id"]]})


if __name__ == "__main__":
    unittest.main()
