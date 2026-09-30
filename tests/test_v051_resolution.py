"""Operator recovery regressions. No external APIs, credentials, or funds."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from arena import adapters
from arena.core import EngineError
from arena.operation_guard import admitted_operation, OperationCanceled
from arena.resolution import resolve_unknown_order
import test_audit_acceptance as fixtures


class ResolutionTests(unittest.TestCase):
    setUp = fixtures.AuditAcceptanceTests.setUp
    tearDown = fixtures.AuditAcceptanceTests.tearDown
    make_service = fixtures.AuditAcceptanceTests.make_service
    agent = fixtures.AuditAcceptanceTests.agent
    advance = fixtures.AuditAcceptanceTests.advance

    def unknown(self):
        broker = self.brokers["openai"]
        broker.submit_order.side_effect = adapters.SubmissionUncertainError("Mock lost submission response.")
        with self.assertRaises(ValueError):
            self.svc._submit(self.agent(), "MSFT", "buy", 100, notional=25)
        self.submissions_before_resolution = broker.submit_order.call_count
        self.order = self.svc.engine.pending_orders()[0]
        self.args = {
            "experiment_id": self.svc.engine.snapshot()["experiment"]["id"],
            "agent_id": "openai", "order_id": self.order["id"],
            "client_order_id": self.order["client_order_id"],
            "broker_confirmation": "Mock support case TEST-2026: broker trading team confirmed this original client ID was never accepted.",
            "confirmed_not_accepted": True,
        }
        return broker

    def resolve(self, **kwargs):
        return resolve_unknown_order(self.svc, **{**self.args, **kwargs})

    def assert_reserved(self):
        order = self.svc.engine.order_by_client_id(self.order["client_order_id"])
        self.assertEqual(order["status"], "unknown")
        self.assertGreater(order["reserved"], 0)
        self.assertGreater(self.agent()["reserved_cash"], 0)
        self.assertNotIn("operator_resolution", order)
        self.assertTrue(self.svc.engine.snapshot()["agent_controls"]["openai"]["paused"])
        self.assertEqual(self.brokers["openai"].submit_order.call_count, self.submissions_before_resolution)

    def test_confirmation_releases_once_audits_and_requires_manual_resume(self):
        broker = self.unknown()
        lookup = broker.order_by_client_id = Mock(wraps=broker.order_by_client_id)
        result = self.resolve()
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(result["requires_manual_resume"])
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(lookup.call_args.args, (self.order["client_order_id"],))
        self.assertEqual(self.agent()["reserved_cash"], 0)
        self.assertEqual(self.agent()["cash"], 250)
        saved = self.svc.engine.order_by_client_id(self.order["client_order_id"])
        audit = saved["operator_resolution"]
        self.assertEqual(audit["broker_confirmation"], self.args["broker_confirmation"])
        self.assertEqual(audit["client_order_id"], self.order["client_order_id"])
        self.assertEqual(audit["account_id"], self.agent()["account_id"])
        self.assertEqual(audit["attested_by"], "operator")
        self.assertFalse(audit["support_confirmation_independently_verified"])
        events = self.svc.engine.state()["events"]
        self.assertEqual(sum(event.get("resolution_id") == audit["id"] for event in events), 1)
        self.svc.reconcile(force=True)
        control = self.svc.engine.snapshot()["agent_controls"]["openai"]
        self.assertTrue(control["paused"])
        self.assertEqual(control["pause_kind"], "operator")
        self.svc = self.make_service()
        self.svc.reconcile(force=True)
        self.assertTrue(self.svc.engine.snapshot()["agent_controls"]["openai"]["paused"])
        self.assertEqual(self.svc.engine.order_by_client_id(self.order["client_order_id"])["operator_resolution"], audit)
        with self.assertRaises(EngineError):
            self.resolve()
        self.svc.resume_agent("openai")
        self.assertFalse(self.svc.engine.snapshot()["agent_controls"]["openai"]["paused"])
        self.assertEqual(broker.submit_order.call_count, 1)

    def test_confirmation_and_exact_current_identifiers_are_mandatory(self):
        self.unknown()
        cases = [
            {"confirmed_not_accepted": False}, {"confirmed_not_accepted": 1},
            {"broker_confirmation": ""}, {"broker_confirmation": "404"},
            {"broker_confirmation": "x" * 1001}, {"broker_confirmation": "case\x00details"},
            {"experiment_id": "another-experiment"}, {"agent_id": "claude"},
            {"order_id": "another-order"}, {"client_order_id": "another-client-id"},
        ]
        self.svc.broker.reset_mock()
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(EngineError):
                self.resolve(**fields)
            self.assert_reserved()
        self.svc.broker.assert_not_called()

    def test_credentials_cannot_be_persisted_in_support_reference(self):
        self.unknown()
        with self.assertRaisesRegex(EngineError, "Remove credentials"):
            self.resolve(broker_confirmation="Support case details accidentally contain " + self.env["ALPACA_OPENAI_SECRET"])
        self.assert_reserved()
        self.assertNotIn(self.env["ALPACA_OPENAI_SECRET"], json.dumps(self.svc.engine.state()))

    def test_missing_or_changed_account_binding_keeps_reservation(self):
        self.unknown()
        key = f"bound:{self.args['experiment_id']}:openai"
        bound = self.svc._get(key)
        for changed in (None, {**bound, "account": "different-account"}):
            self.svc._put(key, changed)
            with self.assertRaisesRegex(EngineError, "binding"):
                self.resolve()
            self.assert_reserved()

    def test_changed_authenticated_account_identity_keeps_reservation(self):
        broker = self.unknown()
        broker.account = Mock(return_value={"id": "another-account", "status": "ACTIVE", "cash": "100000"})
        with self.assertRaisesRegex(EngineError, "identity changed"):
            self.resolve()
        self.assert_reserved()

    def test_another_pending_order_prevents_manual_resolution(self):
        self.svc.engine.register_asset("NVDA", self.brokers["openai"].asset("NVDA"))
        self.svc.engine.reserve_order("openai", "NVDA", "buy", 100, notional=10)
        self.unknown()
        # unknown() initially selected the earlier pending reservation.
        self.order = next(order for order in self.svc.engine.pending_orders() if order["status"] == "unknown")
        self.args.update(order_id=self.order["id"], client_order_id=self.order["client_order_id"])
        with self.assertRaisesRegex(EngineError, "other pending orders"):
            self.resolve()
        self.assert_reserved()

    def test_original_order_appearing_at_either_lookup_cannot_be_rejected(self):
        broker = self.unknown()
        found = {"id": "found-original", "client_order_id": self.order["client_order_id"], "status": "accepted"}
        absent = adapters.OrderNotFoundError("Mock not found.", status_code=404)
        for responses in ([found], [absent, found]):
            broker.order_by_client_id = Mock(side_effect=responses)
            with self.assertRaisesRegex(EngineError, "Reconcile it instead"):
                self.resolve()
            self.assert_reserved()

    def test_any_failed_read_preserves_unknown_and_reservation(self):
        broker = self.unknown()
        for name in ("account", "order_by_client_id", "orders", "positions"):
            original = getattr(broker, name)
            setattr(broker, name, Mock(side_effect=adapters.TransientApiError("Mock failed read.")))
            with self.subTest(read=name), self.assertRaises(adapters.TransientApiError):
                self.resolve()
            self.assert_reserved()
            setattr(broker, name, original)

    def test_changed_positions_and_bad_balances_do_not_release_cash(self):
        broker = self.unknown()
        broker.holdings["MSFT"] = .25
        with self.assertRaisesRegex(EngineError, "inventory differs"):
            self.resolve()
        self.assert_reserved()
        broker.holdings.clear()
        for cash in ("nan", "inf", "-1", "249"):
            broker.account = Mock(return_value={"id": self.agent()["account_id"], "status": "ACTIVE", "cash": cash})
            with self.subTest(cash=cash), self.assertRaises(EngineError):
                self.resolve()
            self.assert_reserved()

    def test_original_in_order_inventory_or_foreign_working_order_blocks_resolution(self):
        broker = self.unknown()
        for row in (
            {"id": "original-id", "client_order_id": self.order["client_order_id"], "status": "rejected"},
            {"id": "foreign-id", "client_order_id": "foreign-client", "status": "accepted"},
        ):
            broker.book = [row]
            broker.order_by_client_id = Mock(side_effect=adapters.OrderNotFoundError("Mock absent."))
            with self.subTest(row=row), self.assertRaises(EngineError):
                self.resolve()
            self.assert_reserved()

    def test_known_broker_fill_changed_requires_reconciliation_first(self):
        broker = self.brokers["openai"]
        self.svc._submit(self.agent(), "NVDA", "buy", 100, notional=10)
        self.unknown()
        broker.book[0]["filled_qty"] = ".2"
        with self.assertRaisesRegex(EngineError, "known broker order changed"):
            self.resolve()
        self.assert_reserved()

    def test_operator_halt_during_verification_cancels_resolution(self):
        broker = self.unknown()
        original = broker.positions
        def stop_then_read():
            self.svc.halt_agent("openai", "New operator stop must prevail.")
            return original()
        broker.positions = stop_then_read
        with self.assertRaisesRegex(EngineError, "operator control changed"):
            self.resolve()
        self.assert_reserved()
        self.assertEqual(self.svc.engine.snapshot()["agent_controls"]["openai"]["reason"], "New operator stop must prevail.")

    def test_stop_between_job_admission_and_helper_entry_cancels_resolution(self):
        self.unknown()
        guard = {"control_generation": self.svc._control_generation, "stop_version": self.svc._stop_version()}
        self.svc.halt_agent("openai", "Stop after the request was admitted.")
        self.svc.broker.reset_mock()
        with admitted_operation(guard, self.args["experiment_id"]):
            with self.assertRaises(OperationCanceled):
                self.resolve()
        self.svc.broker.assert_not_called()
        self.assert_reserved()

    def test_acknowledged_or_partially_filled_unknown_cannot_use_never_accepted_attestation(self):
        self.unknown()
        original = deepcopy(self.order)
        for update in ({"broker_id": "known-accepted-order"}, {"filled_qty": .1}):
            # Reproduce a persisted unknown outcome after an acknowledgment or
            # partial fill. These require normal broker reconciliation instead.
            with self.svc.engine._write():
                order = self.svc.engine._order(self.order["id"])
                order.clear()
                order.update({**original, **update})
            with self.subTest(update=update), self.assertRaisesRegex(EngineError, "no broker acknowledgment or fills"):
                self.resolve()
            self.assert_reserved()

    def test_external_stop_during_verification_cancels_resolution(self):
        broker = self.unknown()
        original = broker.positions
        def stop_then_read():
            self.svc.stop_file.write_text("New external stop")
            return original()
        broker.positions = stop_then_read
        with self.assertRaisesRegex(EngineError, "operator control changed"):
            self.resolve()
        self.assert_reserved()

    def test_preexisting_external_stop_is_preserved_when_resolution_succeeds(self):
        self.unknown()
        self.svc.halt("Existing external stop")
        self.resolve()
        self.assertTrue(self.svc.stop_file.exists())
        state = self.svc.engine.snapshot()
        self.assertEqual(state["experiment"]["status"], "halted")
        self.assertTrue(state["agent_controls"]["openai"]["paused"])

    def test_concurrent_ledger_change_is_rechecked_in_atomic_transition(self):
        broker = self.unknown()
        calls = 0
        def lookup(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.svc.engine.unknown_order(self.order["id"], "Different unknown order evidence", halt=False)
            raise adapters.OrderNotFoundError("Mock not found.")
        broker.order_by_client_id = lookup
        with self.assertRaisesRegex(EngineError, "unknown order changed"):
            self.resolve()
        self.assert_reserved()

    def test_persistence_failure_rolls_back_resolution_audit_and_cash(self):
        self.unknown()
        before = deepcopy(self.svc.engine.state())
        with patch.object(self.svc.engine, "_save_state", side_effect=RuntimeError("Mock disk failure")):
            with self.assertRaisesRegex(RuntimeError, "Mock disk failure"):
                self.resolve()
        self.assert_reserved()
        self.assertEqual(self.svc.engine.state(), before)


if __name__ == "__main__":
    unittest.main()
