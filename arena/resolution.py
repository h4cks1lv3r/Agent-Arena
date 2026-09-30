"""Operator-attested recovery of a submission the broker never accepted.

A missing client ID, even after a long wait, is not proof of rejection. This
workflow supplements fresh read-only checks with an operator's record of direct
broker confirmation. Models and automatic reconciliation never invoke it.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math

from .adapters import OrderNotFoundError
from .core import CRYPTO_QTY_EPS, EngineError, SYMBOL_PATTERN, TERMINAL
from .operation_guard import ensure_current_operation


def _text(value, label, maximum=200, minimum=1):
    if (not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise EngineError(f"{label} must contain {minimum} to {maximum} characters.")
    return value.strip()


def _finite(value, label):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = float(value)
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (TypeError, ValueError, OverflowError):
        raise EngineError(f"Broker {label} is invalid; reservation retained.") from None


def _absent(broker, client_order_id):
    try:
        broker.order_by_client_id(client_order_id)
    except OrderNotFoundError:
        return
    raise EngineError("The broker now returns the original order. Reconcile it instead; reservation retained.")


def _account_matches(account, account_id, cash):
    if not isinstance(account, dict) or account.get("id") != account_id:
        raise EngineError("Broker account identity changed; reservation retained.")
    if account.get("status") != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
        raise EngineError("Paper account is not active for verification; reservation retained.")
    if _finite(account.get("cash"), "cash balance") + .02 < cash:
        raise EngineError("Broker cash is below the assigned virtual balance; reservation retained.")


def _inventory_matches(positions, local):
    if not isinstance(positions, list):
        raise EngineError("Broker inventory is invalid; reservation retained.")
    remote, seen = {}, set()
    for position in positions:
        if not isinstance(position, dict):
            raise EngineError("Broker inventory is invalid; reservation retained.")
        symbol = position.get("symbol")
        # Alpaca retains the older BTCUSD form in some position responses.
        # Only a crypto classification or a known local pair authorizes this
        # conversion, so an equity name ending in USD stays an equity.
        if (isinstance(symbol, str) and "/" not in symbol and symbol.endswith("USD")
                and (position.get("asset_class") == "crypto" or symbol[:-3] + "/USD" in local)):
            symbol = symbol[:-3] + "/USD"
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol) or symbol in seen:
            raise EngineError("Broker inventory has invalid or duplicate symbols; reservation retained.")
        seen.add(symbol)
        qty = _finite(position.get("qty"), "position quantity")
        if abs(qty) > (CRYPTO_QTY_EPS if symbol.endswith("/USD") else 1e-8):
            remote[symbol] = qty
    expected = {symbol: float(position["qty"]) for symbol, position in local.items()
                if abs(float(position["qty"])) > (CRYPTO_QTY_EPS if symbol.endswith("/USD") else 1e-8)}
    if set(remote) != set(expected) or any(abs(remote[symbol] - qty) > (1e-9 if symbol.endswith("/USD") else 1e-6) for symbol, qty in expected.items()):
        raise EngineError("Paper inventory differs from the ledger; reservation retained.")


def _orders_match(service, rows, bound, agent_id, original_client_id):
    if not isinstance(rows, list):
        raise EngineError("Broker order inventory is invalid; reservation retained.")
    prior = set(bound.get("prior_orders", []))
    legacy_boundary = bound.get("orders_after") if not bound.get("order_cursor") else None
    if legacy_boundary:
        try:
            legacy_boundary = datetime.fromisoformat(legacy_boundary.replace("Z", "+00:00"))
            if legacy_boundary.tzinfo is None:
                raise ValueError()
        except (TypeError, ValueError):
            raise EngineError("Legacy account boundary is invalid; reconcile before resolving.") from None
    for row in rows:
        if not isinstance(row, dict):
            raise EngineError("Broker order inventory is invalid; reservation retained.")
        client_id = row.get("client_order_id")
        if client_id == original_client_id:
            raise EngineError("The broker order inventory contains the original order. Reconcile it instead.")
        known = service.engine.order_by_client_id(client_id) if isinstance(client_id, str) else None
        if known and known["agent_id"] == agent_id:
            status = row.get("status")
            filled = _finite(row.get("filled_qty"), "filled quantity")
            symbol = row.get("symbol")
            if known["symbol"].endswith("/USD") and symbol == known["symbol"].replace("/", ""):
                symbol = known["symbol"]
            if (known["status"] not in TERMINAL or status != known["status"]
                    or known.get("broker_id") != row.get("id")
                    or symbol != known["symbol"] or row.get("side") != known["side"]
                    or abs(filled - known["filled_qty"]) > (1e-9 if known["symbol"].endswith("/USD") else 1e-8)
                    or (filled > 0 and abs(_finite(row.get("filled_avg_price"), "fill price") - known["filled_avg_price"]) > 1e-8)):
                raise EngineError("A known broker order changed; reconcile before resolving this order.")
        elif row.get("id") in prior and row.get("status") in TERMINAL | {"replaced"}:
            continue
        elif legacy_boundary and row.get("status") in TERMINAL | {"replaced"}:
            try:
                submitted = datetime.fromisoformat(row["submitted_at"].replace("Z", "+00:00"))
                if submitted.tzinfo is None or submitted >= legacy_boundary:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise EngineError("Unrecognized paper-account order; reservation retained.") from None
        else:
            raise EngineError("Unrecognized paper-account order; reservation retained.")


def resolve_unknown_order(service, *, experiment_id, agent_id, order_id,
                          client_order_id, broker_confirmation, confirmed_not_accepted):
    """Release one unknown reservation only after explicit broker confirmation.

    ``broker_confirmation`` is the operator's support case/reference and details;
    the application cannot independently authenticate that external attestation.
    Reads must all succeed and agree, and the original client ID must remain
    absent. No broker order is submitted, canceled, or retried by this function.
    """
    if confirmed_not_accepted is not True:
        raise EngineError("Confirm that Alpaca support or its trading team explicitly verified this order was never accepted.")
    experiment_id = _text(experiment_id, "Experiment id")
    agent_id = _text(agent_id, "Agent id", 32)
    order_id = _text(order_id, "Order id")
    client_order_id = _text(client_order_id, "Original client order id")
    confirmation = _text(broker_confirmation, "Broker support reference and confirmation details", 1000, 8)
    # The support reference is an audit record. Prevent configured credentials
    # from being accidentally copied into this persisted, user-visible field.
    for key, value in service.env.items():
        if any(part in key.upper() for part in ("KEY", "SECRET", "TOKEN", "PASSWORD")) and isinstance(value, str) and len(value) >= 4 and value in confirmation:
            raise EngineError("Remove credentials from the broker confirmation before saving it.")

    with service.lock:
        with service.control_lock:
            ensure_current_operation(service)
            generation = service._control_generation
            stop_version = service._stop_version()
            state = service.engine.snapshot()
            if state["experiment"]["mode"] != "paper" or state["experiment"]["id"] != experiment_id:
                raise EngineError("This resolution does not belong to the current paper experiment.")
            agent = next((item for item in state["agents"] if item["id"] == agent_id), None)
            order = service.engine.order_by_client_id(client_order_id)
            if not agent or not order or order["id"] != order_id or order["agent_id"] != agent_id:
                raise EngineError("The exact original order and agent must match the current ledger.")
            if order["status"] != "unknown" or order.get("broker_id") or order.get("filled_qty", 0) != 0:
                raise EngineError("Only an unknown submission with no broker acknowledgment or fills can use this resolution.")
            if not state["agent_controls"][agent_id].get("paused"):
                raise EngineError("Pause this agent before resolving an unknown order.")
            if any(item["agent_id"] == agent_id and item["id"] != order_id for item in service.engine.pending_orders()):
                raise EngineError("Reconcile this agent's other pending orders before resolving this one.")
            bind_key = f"bound:{experiment_id}:{agent_id}"
            bound = service._get(bind_key)
            if not isinstance(bound, dict) or not bound.get("account") or bound["account"] != agent.get("account_id"):
                raise EngineError("A verified dedicated account binding is required; reservation retained.")
            account_id = bound["account"]
            expected_order = deepcopy(order)
            expected_positions = deepcopy(agent["positions"])
            expected_cash = agent["cash"]

        broker = service.broker(agent)
        _account_matches(broker.account(), account_id, expected_cash)
        _absent(broker, client_order_id)
        cursor = bound.get("order_cursor")
        recent = broker.orders(status="all", after_order_id=cursor) if cursor else broker.orders(status="all")
        _orders_match(service, recent, bound, agent_id, client_order_id)
        open_orders = broker.orders(status="open")
        if not isinstance(open_orders, list) or open_orders:
            raise EngineError("Broker still has working orders; reservation retained.")
        _inventory_matches(broker.positions(), expected_positions)
        _account_matches(broker.account(), account_id, expected_cash)
        _absent(broker, client_order_id)

        with service.control_lock:
            ensure_current_operation(service)
            if generation != service._control_generation or stop_version != service._stop_version():
                raise EngineError("An operator control changed during verification; reservation retained. Review and retry explicitly.")
            if service._get(bind_key) != bound:
                raise EngineError("The account binding changed during verification; reservation retained.")
            service.engine.resolve_unknown_rejection(
                experiment_id=experiment_id, agent_id=agent_id, account_id=account_id,
                expected_order=expected_order, expected_cash=expected_cash,
                expected_positions=expected_positions, broker_confirmation=confirmation)
        return {"agent_id": agent_id, "order_id": order_id, "client_order_id": client_order_id,
                "status": "rejected", "resolution": "operator_broker_confirmation",
                "requires_manual_resume": True}
