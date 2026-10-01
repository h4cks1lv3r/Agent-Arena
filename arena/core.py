"""Durable, isolated virtual portfolios for the paper-only Agent Arena.

No network access or credentials belong in this module. Broker fills, including
unexpected fills, are accounted for rather than hidden by pre-trade validation.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import uuid

SYMBOLS = ("SPY", "QQQ", "IWM")
TERMINAL = frozenset(("filled", "canceled", "expired", "rejected"))
PROVIDERS = frozenset(("rules", "openai", "anthropic", "manual"))
ASSET_EXCHANGES = frozenset(("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA"))
SYMBOL_PATTERN = re.compile(r"(?:[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,4})?|[A-Z0-9]{2,15}/USD)\Z")
EPS = 1e-8
CRYPTO_QTY_EPS = 1e-10
AUDIT_LIMITS = {"history": 512, "decisions": 100, "events": 200, "closed_orders": 200, "model_charges": 100}
DECISION_FIELDS = ("id", "agent_id", "at", "action", "symbol", "reason", "status")


class EngineError(ValueError):
    """A safe, user-facing validation or portfolio-state error."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value, label, *, minimum=0, positive=False, maximum=1e12):
    if isinstance(value, bool):
        raise EngineError(f"{label} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise EngineError(f"{label} must be a finite number.") from None
    if not math.isfinite(number) or number > maximum or number < minimum or (positive and number <= 0):
        raise EngineError(f"{label} is outside its allowed range.")
    return number


def _text(value, label, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise EngineError(f"{label} must be nonempty text of at most {limit} characters.")
    return value.strip()


def _id():
    return uuid.uuid4().hex


def _iso_time(value):
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _defaults():
    return {
        "mode": "demo", "total_capital": 500, "target": 10000,
        "loss_limit": 50, "position_cap_pct": 30, "exposure_cap_pct": 60,
        "slippage_bps": 5, "fee_bps": 1, "monthly_model_budget": 0,
        "agent_mode": "autonomous", "trading_style": "balanced", "asset_scope": "equities", "research_rounds": 3, "cycle_minutes": 60,
        "max_cycles_per_day": 24, "max_orders_per_cycle": 3, "compound_profits": True,
        "loss_limit_includes_model_costs": False, "auto_resume": True, "output_token_limit": 8192,
        "agents": [
            {"id": "openai", "name": "Astra", "provider": "openai", "model": "gpt-6-astra", "weight": 1, "input_price": 10, "output_price": 50},
            {"id": "claude", "name": "Claude", "provider": "anthropic", "model": "claude-opus-5-5", "weight": 1, "input_price": 4, "output_price": 20},
        ],
    }


def _config(raw, base=None):
    if not isinstance(raw, dict):
        raise EngineError("Configuration must be an object.")
    defaults = _defaults()
    if set(raw) - set(defaults):
        raise EngineError("Unknown configuration field.")
    cfg = deepcopy(base or defaults)
    cfg.update(deepcopy(raw))
    if cfg["mode"] not in ("demo", "paper"):
        raise EngineError("Mode must be demo or paper.")
    if cfg["agent_mode"] not in ("autonomous", "reviewer"):
        raise EngineError("Agent mode must be autonomous or reviewer.")
    if cfg["trading_style"] not in ("balanced", "aggressive_intraday", "fast_swing", "fast_swing_strict"):
        raise EngineError("Choose balanced or aggressive intraday trading.")
    if cfg["trading_style"] in ("aggressive_intraday", "fast_swing", "fast_swing_strict") and cfg["agent_mode"] != "autonomous":
        raise EngineError("Fast trading trading requires autonomous agents.")
    if cfg["asset_scope"] not in ("equities", "equities_crypto"):
        raise EngineError("Choose equities or equities and USD crypto.")
    if cfg["agent_mode"] == "reviewer" and cfg["asset_scope"] != "equities":
        raise EngineError("Legacy reviewer mode supports equities only.")
    for key in ("compound_profits", "loss_limit_includes_model_costs", "auto_resume"):
        if not isinstance(cfg[key], bool):
            raise EngineError(f"{key} must be true or false.")
    for key, low, high in (("research_rounds", 1, 5), ("cycle_minutes", 1 if cfg["trading_style"] in ("aggressive_intraday", "fast_swing", "fast_swing_strict") else 15, 1440),
                           ("max_cycles_per_day", 1, 1440 if cfg["trading_style"] in ("aggressive_intraday", "fast_swing", "fast_swing_strict") else 24), ("max_orders_per_cycle", 1, 10),
                           ("output_token_limit", 1024, 16384)):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise EngineError(f"{key} must be an integer between {low} and {high}.")
    for key in ("total_capital", "target", "loss_limit"):
        cfg[key] = _number(cfg[key], key, positive=True)
    total = Decimal(str(cfg["total_capital"]))
    if total != total.quantize(Decimal(".01")):
        raise EngineError("Total capital must use whole cents.")
    if cfg["loss_limit"] > cfg["total_capital"]:
        raise EngineError("Loss limit cannot exceed total capital.")
    for key in ("position_cap_pct", "exposure_cap_pct"):
        cfg[key] = _number(cfg[key], key, positive=True, maximum=100)
    if cfg["position_cap_pct"] > cfg["exposure_cap_pct"]:
        raise EngineError("Position cap cannot exceed exposure cap.")
    for key in ("slippage_bps", "fee_bps"):
        cfg[key] = _number(cfg[key], key, maximum=1000)
    cfg["monthly_model_budget"] = _number(cfg["monthly_model_budget"], "monthly_model_budget")
    if not isinstance(cfg["agents"], list) or not 1 <= len(cfg["agents"]) <= 6:
        raise EngineError("Configure between one and six agents.")
    agents, used = [], set()
    for item in cfg["agents"]:
        if not isinstance(item, dict):
            raise EngineError("Each agent must be an object.")
        if set(item) - {"id", "name", "provider", "model", "weight", "input_price", "output_price"}:
            raise EngineError("Unknown agent configuration field.")
        agent_id = _text(item.get("id"), "Agent id", 32)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", agent_id) or agent_id in used:
            raise EngineError("Agent ids must be unique lowercase identifiers.")
        used.add(agent_id)
        provider = item.get("provider", "rules")
        if provider not in PROVIDERS:
            raise EngineError("Unsupported agent provider.")
        model = item.get("model", "")
        if not isinstance(model, str) or len(model) > 200:
            raise EngineError("Model must be text of at most 200 characters.")
        agents.append({"id": agent_id, "name": _text(item.get("name", agent_id), "Agent name", 80),
                       "provider": provider, "model": model.strip(),
                       "weight": _number(item.get("weight", 1), "Agent weight", positive=True),
                       "input_price": _number(item.get("input_price", 0), "Input price"),
                       "output_price": _number(item.get("output_price", 0), "Output price")})
    cfg["agents"] = agents
    return cfg


def _allocations(total, agents):
    """Largest-remainder allocation: exact cents, deterministic tie breaking."""
    cents = int(Decimal(str(total)) * 100)
    weights = [Decimal(str(agent["weight"])) for agent in agents]
    portions = [Decimal(cents) * weight / sum(weights) for weight in weights]
    rounded = [int(portion) for portion in portions]
    for index in sorted(range(len(agents)), key=lambda i: (portions[i] - rounded[i], -i), reverse=True)[:cents - sum(rounded)]:
        rounded[index] += 1
    if any(value < 1 for value in rounded):
        raise EngineError("Each agent must receive at least one cent.")
    return [float(Decimal(value) / 100) for value in rounded]


class Engine:
    def __init__(self, db_path):
        self.lock = threading.RLock()
        self._lock = self.lock
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        from .migrations import inspect_database, MigrationError
        self._chart_cache = []
        self._chart_changed = set()
        self.migration_report = None
        try:
            state, version, tables = inspect_database(self._db)
            if version != 3:
                self._migrate(db_path, state, version, tables)
            else:
                self._state = state
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._load_chart_cache()
        except BaseException as exc:
            self._db.close()
            if isinstance(exc, MigrationError):
                raise EngineError(str(exc)) from exc
            raise
        with self._write():
            experiment = self._state["experiment"]
            # A pre-v0.2 experiment retains its original reviewer and sizing
            # behavior. Never silently turn an existing experiment autonomous.
            experiment.setdefault("agent_mode", "reviewer")
            # A server update never broadens an existing account's trading scope.
            experiment.setdefault("asset_scope", "equities")
            experiment.setdefault("trading_style", "balanced")
            experiment.setdefault("compound_profits", False)
            for key in ("research_rounds", "cycle_minutes", "max_cycles_per_day", "max_orders_per_cycle"):
                experiment.setdefault(key, _defaults()[key])
            self._state.setdefault("asset_registry", {})
            self._state.setdefault("crypto_fee_ids", [])
            controls = self._state.setdefault("agent_controls", {})
            for agent in self._state["agents"]:
                controls.setdefault(agent["id"], {"paused": False, "reason": "", "updated_at": _now(), "last_reconciled_at": None})
            for agent in self._state["agents"]:
                for symbol, position in agent["positions"].items():
                    if not position.get("opened_at"):
                        candidates = [_iso_time(order.get("filled_at")) or _iso_time(order.get("created_at"))
                                      for order in self._records("orders", agent["id"])
                                      if order["symbol"] == symbol
                                      and order["side"] == "buy" and order.get("filled_qty", 0) > 0]
                        candidates = [value for value in candidates if value]
                        position["opened_at"] = min(candidates) if candidates else experiment["created_at"]
            auto_restart = experiment.get("auto_resume", True) and experiment.get("autopilot") and experiment["mode"] == "paper" and experiment["status"] in ("ready", "recovering")
            if auto_restart:
                experiment["status"] = "recovering"
                experiment["startup_pending"] = True
                experiment["startup_at"] = _now()
                experiment["halt_reason"] = "Startup reconciliation is required before trading."
            elif self._pending():
                self._halt("Unresolved orders on startup require reconciliation.")
            elif experiment["status"] != "halted":
                experiment["status"] = "paused"
                experiment["autopilot"] = False
                experiment["startup_pending"] = False
            self._refresh()

    def _create_tables(self):
        self._db.execute("CREATE TABLE IF NOT EXISTS arena_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS arena_archives (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self._db.execute("""CREATE TABLE IF NOT EXISTS arena_records (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
            kind TEXT NOT NULL, record_key TEXT NOT NULL, agent_id TEXT,
            client_order_id TEXT, broker_id TEXT, payload TEXT NOT NULL,
            UNIQUE(experiment_id, kind, record_key))""")
        self._db.execute("CREATE INDEX IF NOT EXISTS arena_records_scan ON arena_records(experiment_id,kind,seq)")
        self._db.execute("CREATE INDEX IF NOT EXISTS arena_records_agent ON arena_records(experiment_id,kind,agent_id,seq)")
        self._db.execute("CREATE INDEX IF NOT EXISTS arena_records_client ON arena_records(experiment_id,client_order_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS arena_records_broker ON arena_records(experiment_id,broker_id)")
        self._db.execute("""CREATE TABLE IF NOT EXISTS arena_chart (
            experiment_id TEXT NOT NULL, level INTEGER NOT NULL, bucket INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(experiment_id,level,bucket))""")

    def _migrate(self, db_path, state, version, tables):
        from .migrations import backup_locked, inspect_database, source_states, record_digest, MigrationError
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # Validate again under the write lock before the consistent backup.
            state, version, tables = inspect_database(self._db)
            backup = backup_locked(db_path) if state is not None else None
            is_new = state is None
            if state is None:
                state = self._fresh(_config({}))
                states = {state["experiment"]["id"]: state}
            else:
                states = source_states(self._db, state, version, tables)
            active_id = state["experiment"]["id"]
            self._create_tables()
            verified = {}
            for experiment_id, imported in states.items():
                self._state = imported
                imported["audit_counts"] = {}
                for agent in imported["agents"]:
                    agent["trade_count"] = 0
                self._sync_records({})
                manifest = {}
                for kind, owner, records in self._record_groups(imported):
                    key = kind + (":" + owner if owner else "")
                    actual = self._records(kind, owner)
                    # No row can disappear through key collisions or an import
                    # that ignores a separate legacy evidence/event table.
                    if len(actual) != len(records) or record_digest(actual) != record_digest(records):
                        raise MigrationError(f"Audit verification failed for {experiment_id}/{key}; conversion rolled back.")
                    manifest[key] = {"count": len(actual), "sha256": record_digest(actual)}
                verified[experiment_id] = manifest
                imported["audit_counts"] = dict(self._db.execute("SELECT kind,count(*) FROM arena_records WHERE experiment_id=? GROUP BY kind", (experiment_id,)))
                for kind in ("history", "decisions", "events", "orders", "model_charges"):
                    imported["audit_counts"].setdefault(kind, 0)
                for agent in imported["agents"]:
                    agent["trade_count"] = sum(item.get("filled_qty", 0) > 0 for item in self._records("orders", agent["id"]))
                imported["storage_version"] = 3
                if experiment_id != active_id:
                    self._db.execute("UPDATE arena_archives SET payload=? WHERE id=?", (json.dumps(imported, allow_nan=False), experiment_id))
            self._state = states[active_id]
            self._rebuild_chart()
            self._trim_state()
            self.migration_report = {"from_version": "new" if is_new else version or ("claude_v04" if "arena_decisions" in tables else "legacy_blob"),
                                     "to_version": 3, "backup": backup, "verified": verified}
            self._state["migration"] = deepcopy(self.migration_report)
            self._save_state()
            self._db.execute("COMMIT")
            self._chart_changed = set()
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    @staticmethod
    def _chart_extrema(points):
        """Exact first/last and per-series extrema for one persistent bucket."""
        if not points:
            return []
        found = {point[0]: point for point in points}
        ordered = sorted(found.values(), key=lambda point: point[0])
        selected = {ordered[0][0]: ordered[0], ordered[-1][0]: ordered[-1]}
        series = {agent for _, point in ordered for agent in point.get("agents", {})}
        for series_id in (None, *sorted(series)):
            values = [(seq, point) for seq, point in ordered if series_id is None or series_id in point.get("agents", {})]
            def value(point):
                return point[1]["equity"] if series_id is None else point[1]["agents"][series_id]
            for extreme in (min(values, key=value), max(values, key=value)):
                selected[extreme[0]] = extreme
        return sorted(selected.values(), key=lambda point: point[0])

    def _put_chart(self, level, bucket, points):
        self._db.execute("INSERT OR REPLACE INTO arena_chart(experiment_id,level,bucket,payload) VALUES(?,?,?,?)",
                         (self._state["experiment"]["id"], level, bucket, json.dumps(points, allow_nan=False, separators=(",", ":"))))

    def _rebuild_chart(self):
        """Once per conversion; normal writes update one short tree path."""
        experiment_id = self._state["experiment"]["id"]
        self._db.execute("DELETE FROM arena_chart WHERE experiment_id=?", (experiment_id,))
        buckets = {}
        for seq, payload in self._db.execute("SELECT seq,payload FROM arena_records WHERE experiment_id=? AND kind='history' ORDER BY seq", (experiment_id,)):
            buckets.setdefault(seq // 32, []).append((seq, json.loads(payload)))
        level = 0
        while buckets:
            parents = {}
            for bucket, points in buckets.items():
                summary = self._chart_extrema(points)
                self._put_chart(level, bucket, summary)
                parents.setdefault(bucket // 2, []).extend(summary)
            if len(buckets) == 1 and next(iter(buckets)) == 0:
                break
            buckets = parents
            level += 1

    def _update_chart(self):
        if not self._chart_changed:
            return
        experiment_id = self._state["experiment"]["id"]
        max_seq = self._db.execute("SELECT seq FROM arena_records WHERE experiment_id=? AND kind='history' ORDER BY seq DESC LIMIT 1", (experiment_id,)).fetchone()[0]
        highest = (max_seq // 32).bit_length()
        changed = {seq // 32 for seq in self._chart_changed}
        for bucket in changed:
            rows = self._db.execute("SELECT seq,payload FROM arena_records WHERE experiment_id=? AND kind='history' AND seq>=? AND seq<? ORDER BY seq", (experiment_id, bucket * 32, (bucket + 1) * 32))
            self._put_chart(0, bucket, self._chart_extrema([(seq, json.loads(payload)) for seq, payload in rows]))
        for level in range(1, highest + 1):
            changed = {bucket // 2 for bucket in changed}
            for bucket in changed:
                points = []
                for (payload,) in self._db.execute("SELECT payload FROM arena_chart WHERE experiment_id=? AND level=? AND bucket IN (?,?)", (experiment_id, level - 1, bucket * 2, bucket * 2 + 1)):
                    points.extend(json.loads(payload))
                self._put_chart(level, bucket, self._chart_extrema(points))
        self._load_chart_cache()

    def _load_chart_cache(self):
        experiment_id = self._state["experiment"]["id"]
        row = self._db.execute("SELECT seq FROM arena_records WHERE experiment_id=? AND kind='history' ORDER BY seq DESC LIMIT 1", (experiment_id,)).fetchone()
        if row is None:
            self._chart_cache = []
            return
        # At most 512 vertices even for six agents. Each bucket contributes
        # <= first+last+2*(aggregate+agents), retaining both up/down extremes.
        capacity = max(1, AUDIT_LIMITS["history"] // (4 + 2 * len(self._state["agents"])))
        max_bucket = row[0] // 32
        level = 0
        while (max_bucket >> level) + 1 > capacity:
            level += 1
        selected = {}
        for (payload,) in self._db.execute("SELECT payload FROM arena_chart WHERE experiment_id=? AND level=? ORDER BY bucket", (experiment_id, level)):
            for seq, point in json.loads(payload):
                selected[seq] = point
        # Use remaining space for recent detail. This does not remove extrema.
        if len(selected) < AUDIT_LIMITS["history"]:
            for seq, payload in self._db.execute("SELECT seq,payload FROM arena_records WHERE experiment_id=? AND kind='history' ORDER BY seq DESC LIMIT ?", (experiment_id, AUDIT_LIMITS["history"])):
                selected.setdefault(seq, json.loads(payload))
                if len(selected) >= AUDIT_LIMITS["history"]:
                    break
        self._chart_cache = [point for _, point in sorted(selected.items())]

    def close(self):
        with self.lock:
            self._db.close()

    @contextmanager
    def _write(self):
        with self.lock:
            before = deepcopy(self._state)
            chart_before = self._chart_cache
            self._chart_changed = set()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._sync_records(before)
                self._trim_state()
                self._update_chart()
                self._state["snapshot_at"] = _now()
                self._state["storage_version"] = 3
                self._save_state()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._state = before
                self._chart_cache = chart_before
                self._chart_changed = set()
                raise

    def _save_state(self):
        self._db.execute("INSERT OR REPLACE INTO arena_state(id,payload) VALUES(1,?)",
                         (json.dumps(self._state, allow_nan=False, separators=(",", ":")),))

    @staticmethod
    def _record_key(kind, record):
        if kind == "history":
            return record["at"]
        if kind == "model_charges":
            return record.get("call_id") or record.setdefault("id", _id())
        return record.setdefault("id", _id())

    def _record_groups(self, state):
        for kind in ("history", "decisions", "events", "orders"):
            yield kind, None, state.get(kind, [])
        for agent in state.get("agents", []):
            yield "model_charges", agent["id"], agent.get("model_charges", [])

    def _sync_records(self, before):
        """Upsert only changed recent records. Historical rows are never copied
        during normal writes and stay available to explicit full audit export."""
        exp_id = self._state["experiment"]["id"]
        counts = self._state.setdefault("audit_counts", {})
        for kind in ("history", "decisions", "events", "orders", "model_charges"):
            counts.setdefault(kind, 0)
        reset = getattr(self, "_reset_records", False)
        self._reset_records = False
        old_groups = {(kind, agent): records for kind, agent, records in self._record_groups(before)} if before.get("experiment", {}).get("id") == exp_id and not reset else {}
        for kind, owner, records in self._record_groups(self._state):
            old = {self._record_key(kind, item): item for item in old_groups.get((kind, owner), [])}
            for item in records:
                key = self._record_key(kind, item)
                if key in old and item == old[key]:
                    continue
                agent_id = owner or item.get("agent_id")
                db_key = (owner + ":" + key) if owner else key
                previous = self._db.execute("SELECT payload FROM arena_records WHERE experiment_id=? AND kind=? AND record_key=?", (exp_id, kind, db_key)).fetchone()
                self._db.execute("""INSERT INTO arena_records(experiment_id,kind,record_key,agent_id,client_order_id,broker_id,payload)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(experiment_id,kind,record_key) DO UPDATE SET
                    agent_id=excluded.agent_id,client_order_id=excluded.client_order_id,broker_id=excluded.broker_id,payload=excluded.payload""",
                    (exp_id, kind, db_key, agent_id, item.get("client_order_id"), item.get("broker_id"), json.dumps(item, allow_nan=False, separators=(",", ":"))))
                if kind == "history":
                    seq = self._db.execute("SELECT seq FROM arena_records WHERE experiment_id=? AND kind='history' AND record_key=?", (exp_id, db_key)).fetchone()[0]
                    self._chart_changed.add(seq)
                if previous is None:
                    counts[kind] = counts.get(kind, 0) + 1
                if kind == "orders" and item.get("filled_qty", 0) > 0 and (previous is None or json.loads(previous[0]).get("filled_qty", 0) <= 0):
                    agent = self._agent(agent_id)
                    agent["trade_count"] = agent.get("trade_count", 0) + 1

    def _trim_state(self):
        self._state["archives"] = self._state.get("archives", [])[-100:]
        history = self._state["history"]
        if len(history) > AUDIT_LIMITS["history"]:
            self._state["history"] = [history[0]] + history[-(AUDIT_LIMITS["history"] - 1):]
        self._state["events"] = self._state["events"][-AUDIT_LIMITS["events"]:]
        self._state["decisions"] = [{key: item[key] for key in DECISION_FIELDS if key in item}
                                    for item in self._state["decisions"][-AUDIT_LIMITS["decisions"]:]]
        orders = self._state["orders"]
        closed = {item["id"] for item in orders if item["status"] in TERMINAL}
        keep = {item["id"] for item in [item for item in orders if item["id"] in closed][-AUDIT_LIMITS["closed_orders"]:]}
        self._state["orders"] = [item for item in orders if item["status"] not in TERMINAL or item["id"] in keep]
        for agent in self._state["agents"]:
            agent["model_charges"] = agent.get("model_charges", [])[-AUDIT_LIMITS["model_charges"]:]

    def _records(self, kind, agent_id=None):
        sql = "SELECT payload FROM arena_records WHERE experiment_id=? AND kind=?"
        args = [self._state["experiment"]["id"], kind]
        if agent_id is not None:
            sql += " AND agent_id=?"
            args.append(agent_id)
        return [json.loads(row[0]) for row in self._db.execute(sql + " ORDER BY seq", args)]

    def snapshot(self):
        """Bounded local state for polling, control checks, and model context."""
        with self.lock:
            result = deepcopy(self._state)
            result["history"] = deepcopy(self._chart_cache)
            result["chart"] = {"method": "full_period_bucket_extrema", "raw_history_retained": True}
            counts = result.get("audit_counts", {})
            result["view_limits"] = {**AUDIT_LIMITS, "full_counts": {key: counts.get(key, 0) for key in ("history", "decisions", "events", "orders")},
                                    "truncated": {key: len(result[key]) < counts.get(key, 0) for key in ("history", "decisions", "events", "orders")},
                                    "full_audit_available": True}
            return result

    def orders(self, agent_id=None):
        with self.lock:
            return self._records("orders", agent_id)

    def order_by_client_id(self, client_id):
        with self.lock:
            row = self._db.execute("SELECT payload FROM arena_records WHERE experiment_id=? AND kind='orders' AND client_order_id=?",
                                   (self._state["experiment"]["id"], client_id)).fetchone()
            return json.loads(row[0]) if row else None

    def has_broker_order(self, broker_id):
        with self.lock:
            return self._db.execute("SELECT 1 FROM arena_records WHERE experiment_id=? AND kind='orders' AND broker_id=?",
                                    (self._state["experiment"]["id"], broker_id)).fetchone() is not None

    def _fresh(self, cfg, archives=None):
        created = _now()
        experiment = {key: deepcopy(value) for key, value in cfg.items() if key != "agents"}
        experiment.update({"id": _id(), "status": "paused", "halt_reason": "", "step": 0,
                           "created_at": created, "autopilot": False, "started": False})
        agents = []
        for item, allocation in zip(cfg["agents"], _allocations(cfg["total_capital"], cfg["agents"])):
            agents.append({**item, "allocation": allocation, "cash": allocation, "positions": {},
                           "fees": 0.0, "model_cost": 0.0, "equity": allocation, "net_pnl": 0.0,
                           "return_pct": 0.0, "max_drawdown_pct": 0.0, "peak_equity": allocation,
                           "model_charges": []})
        return {"experiment": experiment, "agents": agents,
                "market": {"prices": {}, "as_of": "", "source": "synthetic" if cfg["mode"] == "demo" else "unconnected"},
                "history": [{"at": created, "equity": cfg["total_capital"], "agents": {a["id"]: a["allocation"] for a in agents}}],
                "decisions": [], "orders": [], "events": [], "archives": archives or [], "asset_registry": {}, "crypto_fee_ids": [],
                "agent_controls": {a["id"]: {"paused": False, "reason": "", "updated_at": created, "last_reconciled_at": None} for a in agents}}

    def _get_config(self):
        keys = set(_defaults()) - {"agents"}
        return {**{key: self._state["experiment"][key] for key in keys},
                "agents": [{key: a[key] for key in ("id", "name", "provider", "model", "weight", "input_price", "output_price")} for a in self._state["agents"]]}

    def _agent(self, agent_id):
        for agent in self._state["agents"]:
            if agent["id"] == agent_id:
                return agent
        raise EngineError("Unknown agent.")

    def _order(self, order_id):
        for order in self._state["orders"]:
            if order["id"] == order_id or order.get("client_order_id") == order_id:
                return order
        row = self._db.execute("SELECT payload FROM arena_records WHERE experiment_id=? AND kind='orders' AND (record_key=? OR client_order_id=?)",
                               (self._state["experiment"]["id"], order_id, order_id)).fetchone()
        if row:
            order = json.loads(row[0])
            self._state["orders"].append(order)
            return order
        raise EngineError("Unknown order.")

    def _pending(self):
        return [order for order in self._state["orders"] if order["status"] not in TERMINAL]

    def _capital_basis(self, agent):
        equity = max(0.0, agent.get("trading_equity", agent["equity"] + agent["model_cost"]))
        return equity if self._state["experiment"]["compound_profits"] else min(agent["allocation"], equity)

    def register_asset(self, symbol, metadata):
        """Register eligible broker metadata; callers must fetch it from Alpaca.

        This is a structural eligibility check, not independent provenance
        verification. No model response is allowed to serve as broker metadata.
        """
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise EngineError("Asset symbol is invalid.")
        if not isinstance(metadata, dict):
            raise EngineError("Asset metadata must be a broker object.")
        if metadata.get("symbol", symbol) != symbol:
            raise EngineError("Broker asset symbol does not match the requested symbol.")
        if metadata.get("status") != "active":
            raise EngineError("Only active broker assets are eligible.")
        if metadata.get("tradable") is not True or metadata.get("fractionable") is not True:
            raise EngineError("Assets must be broker-confirmed tradable and fractionable.")
        asset_class = metadata.get("asset_class", metadata.get("class"))
        if asset_class not in ("us_equity", "crypto") or ("class" in metadata and metadata["class"] != asset_class):
            raise EngineError("Only US equities and USD crypto pairs are supported.")
        exchange = metadata.get("exchange")
        if (asset_class == "crypto" and (self._state["experiment"].get("asset_scope", "equities") != "equities_crypto"
                or not symbol.endswith("/USD") or exchange not in ("ALPACA", "CRXL"))):
            raise EngineError("USD crypto pair is outside this experiment's enabled scope.")
        if asset_class == "us_equity" and ("/" in symbol or exchange not in ASSET_EXCHANGES):
            raise EngineError("Asset exchange is not supported.")
        registered = {"symbol": symbol, "status": "active", "tradable": True,
                      "fractionable": True, "asset_class": asset_class, "exchange": exchange,
                      "registered_at": _now()}
        # Persist only public descriptive fields, never arbitrary metadata.
        for key, limit in (("id", 200), ("name", 300)):
            if key in metadata:
                registered[key] = _text(metadata[key], "Asset " + key, limit)
        with self._write():
            self._state["asset_registry"][symbol] = registered
        return deepcopy(registered)

    def _event(self, message, level="info"):
        self._state["events"].append({"at": _now(), "message": str(message)[:1000], "level": level})

    def _halt(self, reason):
        experiment = self._state["experiment"]
        # Preserve the original cause across restarts and repeated risk checks.
        if experiment["status"] != "halted" or not experiment["halt_reason"]:
            experiment["halt_reason"] = str(reason)[:1000]
            self._event(experiment["halt_reason"], "error")
        experiment["status"] = "halted"
        experiment["autopilot"] = False
        experiment["startup_pending"] = False

    def _loss_used(self):
        exp = self._state["experiment"]
        basis = exp["equity"] if exp["loss_limit_includes_model_costs"] else exp["trading_equity"]
        return exp["total_capital"] - basis

    def _refresh(self, record=False):
        prices = self._state["market"]["prices"]
        for agent in self._state["agents"]:
            value = sum(position["qty"] * prices.get(symbol, position["avg_price"]) for symbol, position in agent["positions"].items())
            agent["trading_equity"] = agent["cash"] + value
            agent["trading_pnl"] = agent["trading_equity"] - agent["allocation"]
            agent["equity"] = agent["trading_equity"] - agent["model_cost"]
            agent["net_pnl"] = agent["equity"] - agent["allocation"]
            agent["return_pct"] = 100 * agent["net_pnl"] / agent["allocation"]
            agent["peak_equity"] = max(agent.get("peak_equity", agent["allocation"]), agent["equity"])
            drawdown = 100 * (agent["peak_equity"] - agent["equity"]) / agent["peak_equity"]
            agent["max_drawdown_pct"] = max(agent["max_drawdown_pct"], drawdown)
            agent["reserved_cash"] = sum(order["reserved"] for order in self._pending() if order["agent_id"] == agent["id"])
            agent["available_cash"] = max(0.0, agent["cash"] - agent["reserved_cash"])
            if agent["cash"] < -EPS:
                self._halt("A fill exceeded an agent's reserved cash; reconcile the account.")
        total = sum(agent["equity"] for agent in self._state["agents"])
        exp = self._state["experiment"]
        exp["trading_equity"] = sum(agent["trading_equity"] for agent in self._state["agents"])
        exp["trading_pnl"] = exp["trading_equity"] - exp["total_capital"]
        exp["model_cost"] = sum(agent["model_cost"] for agent in self._state["agents"])
        exp["equity"] = total
        exp["net_pnl"] = total - exp["total_capital"]
        exp["return_pct"] = 100 * exp["net_pnl"] / exp["total_capital"]
        if self._loss_used() >= exp["loss_limit"] - EPS:
            basis = "net" if exp["loss_limit_includes_model_costs"] else "trading"
            self._halt(f"Experiment {basis} loss limit reached. New entries are disabled.")
        if record:
            item = {"at": self._state["market"]["as_of"] or _now(), "equity": total,
                    "agents": {a["id"]: a["equity"] for a in self._state["agents"]}}
            if self._state["history"] and self._state["history"][-1]["at"] == item["at"]:
                self._state["history"][-1] = item
            else:
                self._state["history"].append(item)

    def state(self):
        """Explicit full audit export; use snapshot() on all routine paths."""
        with self.lock:
            result = deepcopy(self._state)
            for kind in ("history", "decisions", "events", "orders"):
                result[kind] = self._records(kind)
            for agent in result["agents"]:
                agent["model_charges"] = self._records("model_charges", agent["id"])
            result["archives"] = []
            for row in self._db.execute("SELECT payload FROM arena_archives ORDER BY rowid"):
                archived = json.loads(row[0])["experiment"]
                result["archives"].append({key: archived[key] for key in ("id", "created_at", "mode", "total_capital")})
            return result

    def set_trading_style(self, style, model_budget=None):
        """Change execution pace without resetting history, positions or risk caps."""
        with self._write():
            raw = {"trading_style": style, "cycle_minutes": 1 if style in ("aggressive_intraday", "fast_swing", "fast_swing_strict") else 60,
                   "max_cycles_per_day": 390 if style in ("aggressive_intraday", "fast_swing", "fast_swing_strict") else 24}
            if model_budget is not None:
                raw["monthly_model_budget"] = model_budget
            cfg = _config(raw, self._get_config())
            exp = self._state["experiment"]
            exp["trading_style"] = cfg["trading_style"]
            exp["monthly_model_budget"] = cfg["monthly_model_budget"]
            exp["cycle_minutes"] = 1 if style in ("aggressive_intraday", "fast_swing", "fast_swing_strict") else 60
            exp["max_cycles_per_day"] = 390 if style in ("aggressive_intraday", "fast_swing", "fast_swing_strict") else 24
            self._event("Trading style changed by operator: " + style + ". Existing risk caps and history retained.")
        return self.snapshot()

    def configure(self, config):
        with self._write():
            if self._state["experiment"]["started"] or self._state["orders"] or self._state["decisions"]:
                raise EngineError("Configuration is locked after an experiment starts. Create a new experiment.")
            old = self._state["experiment"]
            fresh = self._fresh(_config(config, self._get_config()), deepcopy(self._state["archives"]))
            fresh["experiment"]["id"] = old["id"]
            fresh["experiment"]["created_at"] = old["created_at"]
            if old["status"] == "halted":
                fresh["experiment"]["status"] = "halted"
                fresh["experiment"]["halt_reason"] = old["halt_reason"]
            self._db.execute("DELETE FROM arena_records WHERE experiment_id=?", (old["id"],))
            self._db.execute("DELETE FROM arena_chart WHERE experiment_id=?", (old["id"],))
            fresh["audit_counts"] = {}
            self._state = fresh
            self._reset_records = True
            self._refresh()
        return self.snapshot()

    def new_experiment(self, config):
        with self._write():
            if self._pending():
                raise EngineError("Reconcile all pending orders before creating an experiment.")
            cfg = _config(config)
            old = self.state()
            self._db.execute("INSERT INTO arena_archives(id,payload) VALUES(?,?)", (old["experiment"]["id"], json.dumps(old, allow_nan=False)))
            archives = deepcopy(old["archives"])
            archives.append({key: old["experiment"][key] for key in ("id", "created_at", "mode", "total_capital")})
            self._state = self._fresh(cfg, archives)
            self._event("New experiment created. Existing broker positions and funds were not changed.")
            self._refresh()
        return self.snapshot()

    def halt(self, reason="Stopped by operator."):
        with self._write():
            self._halt(_text(reason, "Halt reason", 1000))
        return self.snapshot()

    def resume(self, allow_isolated=False):
        with self._write():
            self._refresh()
            pending = self._pending()
            if allow_isolated:
                pending = [o for o in pending if not self._state["agent_controls"].get(o["agent_id"], {}).get("paused")]
            if pending:
                raise EngineError("Reconcile unresolved orders before resuming.")
            exp = self._state["experiment"]
            if self._loss_used() >= exp["loss_limit"] - EPS:
                raise EngineError("Loss budget is exhausted; this experiment cannot resume.")
            exp.update({"status": "ready", "halt_reason": "", "started": True, "startup_pending": False})
            self._event("Experiment resumed by operator.")
        return self.snapshot()

    def halt_agent(self, agent_id, reason="Agent paused by operator; working orders and positions remain.", *, kind="operator"):
        if kind not in ("operator", "transient", "unknown_order", "integrity"):
            raise EngineError("Unsupported agent pause kind.")
        with self._write():
            self._agent(agent_id)
            control = self._state["agent_controls"][agent_id]
            # Temporary failures must never overwrite an intentional stop or
            # an unresolved integrity incident and later auto-clear it.
            existing_kind = control.get("pause_kind", "operator")
            if control.get("paused") and existing_kind in ("operator", "integrity") and kind in ("transient", "unknown_order"):
                return self.snapshot()
            control.update(paused=True, reason=_text(reason, "Pause reason", 1000), pause_kind=kind, updated_at=_now(), paused_at=_now())
            self._event(f"{agent_id}: {control['reason']}", "warning")
        return self.snapshot()

    def note_reconciled(self, agent_id, at=None):
        at = _iso_time(at or _now())
        if at is None:
            raise EngineError("Reconciliation timestamp must use ISO format.")
        with self._write():
            self._agent(agent_id)
            self._state["agent_controls"][agent_id].update(last_reconciled_at=at, updated_at=_now())
        return self.snapshot()

    def note_quotes(self, agent_id, quote_times):
        if not isinstance(quote_times, dict) or any(_iso_time(at) is None for at in quote_times.values()):
            raise EngineError("Quote timestamps must use ISO format.")
        with self._write():
            self._agent(agent_id)
            control = self._state["agent_controls"][agent_id]
            saved = control.setdefault("quote_times", {})
            for symbol, at in quote_times.items():
                normalized = _iso_time(at)
                if symbol not in saved or normalized >= saved[symbol]:
                    saved[symbol] = normalized
            if saved:
                control["last_quote_at"] = max(saved.values())
        return self.snapshot()

    def resume_agent(self, agent_id, require_reconciled=False, *, automatic=False):
        with self._write():
            self._agent(agent_id)
            control = self._state["agent_controls"][agent_id]
            if automatic and control.get("paused") and control.get("pause_kind", "operator") not in ("transient", "unknown_order"):
                raise EngineError("Automatic recovery cannot clear an operator or integrity stop.")
            pending = [o for o in self._pending() if o["agent_id"] == agent_id]
            unresolved = [o for o in pending if o["status"] in ("reserved", "submitting", "unknown")]
            if unresolved or (pending and not (require_reconciled or automatic)):
                raise EngineError("Reconcile this agent's unresolved orders before resuming.")
            if (require_reconciled or automatic) and not control.get("last_reconciled_at"):
                raise EngineError("Reconcile this agent before resuming.")
            if automatic and control.get("paused_at") and control["last_reconciled_at"] < control["paused_at"]:
                raise EngineError("Reconcile this agent after the current incident before automatic recovery.")
            control.update(paused=False, reason="", pause_kind="", updated_at=_now())
            self._event(f"{agent_id}: resumed after reconciliation." if automatic else f"{agent_id}: resumed by operator.")
        return self.snapshot()

    def note_recovery(self, agent_id, fields):
        if not isinstance(fields, dict) or set(fields) - {"active", "attempts", "next_retry_at", "last_error", "reason", "last_success_at", "quote_warning", "execution_warning"}:
            raise EngineError("Unsupported recovery metadata.")
        clean = {}
        for key, value in fields.items():
            if key == "active":
                if not isinstance(value, bool):
                    raise EngineError("Recovery active must be boolean.")
                clean[key] = value
            elif key == "attempts":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000000:
                    raise EngineError("Recovery attempts must be a nonnegative integer.")
                clean[key] = value
            elif key.endswith("_at"):
                if value is not None and _iso_time(value) is None:
                    raise EngineError("Recovery time must use ISO format.")
                clean[key] = _iso_time(value) if value is not None else None
            else:
                if not isinstance(value, str) or len(value) > 1000:
                    raise EngineError("Recovery message must be at most 1000 characters.")
                clean[key] = value
        with self._write():
            self._agent(agent_id)
            self._state["agent_controls"][agent_id].setdefault("recovery", {}).update(clean)
        return self.snapshot()

    def recover_startup(self, allow_isolated=True):
        """Service invokes this only after verifying broker identity, orders,
        positions and balances. Never clear an operator or financial risk stop."""
        with self._write():
            self._refresh()
            exp = self._state["experiment"]
            if exp["status"] != "recovering" or not exp.get("startup_pending") or not exp.get("autopilot") or not exp.get("auto_resume", True):
                raise EngineError("This experiment is not eligible for startup recovery.")
            active = [a["id"] for a in self._state["agents"] if not (allow_isolated and self._state["agent_controls"][a["id"]].get("paused"))]
            # Both agents may be intentionally paused. After fresh broker
            # checks the server can be ready while those independent stops stay
            # in force; do not leave a recoverable startup permanently wedged.
            checked_agents = active or [a["id"] for a in self._state["agents"]]
            for agent_id in checked_agents:
                control = self._state["agent_controls"][agent_id]
                reconciled = control.get("last_reconciled_at")
                if not reconciled or reconciled < exp.get("startup_at", ""):
                    raise EngineError("Reconcile all active agents after this server restart.")
            if any(o["agent_id"] in active and o["status"] in ("reserved", "submitting", "unknown") for o in self._pending()):
                raise EngineError("Unresolved orders block startup recovery.")
            exp.update(status="ready", halt_reason="", startup_pending=False)
            self._event("Automatic operation restored after startup reconciliation.")
        return self.snapshot()

    def mark(self, prices, as_of, source="alpaca_iex"):
        if not isinstance(prices, dict) or not prices:
            raise EngineError("Prices must be a nonempty object.")
        checked = {symbol: _number(value, "Quote price", positive=True) for symbol, value in prices.items()}
        at = _text(as_of, "Quote timestamp", 80)
        try:
            datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise EngineError("Quote timestamp must use ISO format.") from None
        with self._write():
            if set(checked) - (set(SYMBOLS) | set(self._state["asset_registry"])):
                raise EngineError("Market symbols must be registered eligible broker assets.")
            market = self._state["market"]
            times = market.setdefault("quote_times", {})
            normalized = _iso_time(at)
            for symbol, price in checked.items():
                if symbol not in times or normalized >= times[symbol]:
                    market["prices"][symbol] = price
                    times[symbol] = normalized
            market.update({"as_of": max(times.values()) if times else at, "source": _text(source, "Quote source", 80)})
            self._refresh(record=True)
        return self.snapshot()

    def record_decision(self, agent_id, decision):
        if not isinstance(decision, dict):
            raise EngineError("Decision must be an object.")
        try:
            clean = json.loads(json.dumps(decision, allow_nan=False))
        except (TypeError, ValueError):
            raise EngineError("Decision must contain finite JSON values.") from None
        # Avoid accidentally persisting connection credentials in arbitrary metadata.
        forbidden = re.compile(r"(?i)(api_?key|secret|password|authorization|access_?token|refresh_?token)")
        def inspect(value):
            if isinstance(value, dict):
                if any(forbidden.search(str(key)) for key in value):
                    raise EngineError("Secrets cannot be stored in a decision.")
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(clean)
        with self._write():
            self._agent(agent_id)
            clean.update({"id": _id(), "agent_id": agent_id, "at": clean.get("at") or _now()})
            clean.setdefault("action", "hold")
            clean.setdefault("reason", "")
            clean.setdefault("status", "recorded")
            self._state["decisions"].append(clean)
        return deepcopy(clean)

    def reserve_order(self, agent_id, symbol, side, price, notional=None, qty=None, decision_id=None):
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol) or side not in ("buy", "sell"):
            raise EngineError("Only valid long-only eligible asset orders are supported.")
        price = _number(price, "Order price", positive=True)
        if (notional is None) == (qty is None):
            raise EngineError("Supply exactly one of order notional or quantity.")
        kind = "notional" if notional is not None else "qty"
        if notional is not None:
            amount = _number(notional, "Order notional", positive=True)
            quantity = amount / price
        else:
            quantity = _number(qty, "Order quantity", positive=True)
            amount = quantity * price
        if not math.isfinite(amount) or not math.isfinite(quantity) or quantity <= 0 or amount <= 0:
            raise EngineError("Order size is invalid.")
        with self._write():
            agent = self._agent(agent_id)
            exp = self._state["experiment"]
            self._refresh()
            if self._state["agent_controls"].get(agent_id, {}).get("paused"):
                raise EngineError("This agent is paused; resume it before submitting orders.")
            if symbol not in SYMBOLS and symbol not in self._state["asset_registry"]:
                raise EngineError("Order symbol is not a registered eligible broker asset.")
            if any(order["agent_id"] == agent_id and order["symbol"] == symbol for order in self._pending()):
                raise EngineError("An unresolved order already exists for this agent and symbol.")
            if decision_id is not None and self._db.execute("SELECT 1 FROM arena_records WHERE experiment_id=? AND kind='decisions' AND record_key=? AND agent_id=?",
                    (exp["id"], decision_id, agent_id)).fetchone() is None:
                raise EngineError("Order decision does not belong to this agent.")
            fee_rate = 0 if self._state["asset_registry"].get(symbol, {}).get("asset_class") == "crypto" else exp["fee_bps"] / 10000
            reserve = 0.0
            if side == "buy":
                if exp["status"] != "ready":
                    raise EngineError("Resume the experiment before opening a position.")
                reserve = amount * (1 + fee_rate)
                if reserve > agent["available_cash"] + EPS:
                    raise EngineError("Order exceeds this agent's unreserved cash.")
                prices = self._state["market"]["prices"]
                position_value = sum(p["qty"] * prices.get(s, p["avg_price"]) for s, p in agent["positions"].items())
                current_symbol = agent["positions"].get(symbol, {}).get("qty", 0) * prices.get(symbol, price)
                pending_value = sum(o["reserved"] / (1 + (0 if self._state["asset_registry"].get(o["symbol"], {}).get("asset_class") == "crypto" else exp["fee_bps"] / 10000))
                                    for o in self._pending() if o["agent_id"] == agent_id and o["side"] == "buy")
                capital_basis = self._capital_basis(agent)
                if current_symbol + amount > capital_basis * exp["position_cap_pct"] / 100 + EPS:
                    raise EngineError("Order exceeds this agent's position cap.")
                if position_value + pending_value + amount > capital_basis * exp["exposure_cap_pct"] / 100 + EPS:
                    raise EngineError("Order exceeds this agent's exposure cap.")
            elif quantity > agent["positions"].get(symbol, {}).get("qty", 0) + (CRYPTO_QTY_EPS if self._state["asset_registry"].get(symbol, {}).get("asset_class") == "crypto" else EPS):
                raise EngineError("Sell quantity exceeds this agent's position.")
            order_id = _id()
            order = {"id": order_id, "client_order_id": "arena_" + order_id, "agent_id": agent_id,
                     "symbol": symbol, "side": side, "price": price, "notional": amount, "qty": quantity,
                     "size_type": kind, "reserved": reserve, "status": "reserved", "filled_qty": 0.0,
                     "filled_notional": 0.0, "filled_avg_price": 0.0, "estimated_fees": 0.0,
                     "decision_id": decision_id, "created_at": _now(), "updated_at": _now()}
            self._state["orders"].append(order)
            exp["started"] = True
            self._refresh()
        return deepcopy(order)

    def update_order(self, order_id, broker_order):
        if not isinstance(broker_order, dict):
            raise EngineError("Broker order must be an object.")
        with self._write():
            order = self._order(order_id)
            agent = self._agent(order["agent_id"])
            symbol = order["symbol"]
            crypto = self._state["asset_registry"].get(symbol, {}).get("asset_class") == "crypto"
            qty_epsilon = CRYPTO_QTY_EPS if crypto else EPS
            filled = _number(broker_order.get("filled_qty", order["filled_qty"]), "Filled quantity")
            status = str(broker_order.get("status", order["status"])).lower()
            status = {"cancelled": "canceled"}.get(status, status)
            if filled < order["filled_qty"] - qty_epsilon:
                return deepcopy(order)
            average = _number(broker_order.get("filled_avg_price") or order["filled_avg_price"], "Fill average", positive=filled > 0)
            delta_qty = filled - order["filled_qty"]
            if delta_qty > qty_epsilon:
                cumulative = filled * average
                delta_notional = cumulative - order["filled_notional"]
                if not math.isfinite(cumulative) or delta_notional <= 0:
                    raise EngineError("Cumulative fill data is inconsistent.")
                fees = 0 if crypto else delta_notional * self._state["experiment"]["fee_bps"] / 10000
                position = agent["positions"].get(symbol, {"qty": 0.0, "avg_price": 0.0})
                if order["side"] == "buy":
                    old_cost = position["qty"] * position["avg_price"]
                    new_qty = position["qty"] + delta_qty
                    opened_at = position.get("opened_at") or _iso_time(broker_order.get("filled_at")) or _iso_time(broker_order.get("updated_at")) or _now()
                    agent["positions"][symbol] = {"qty": new_qty, "avg_price": (old_cost + delta_notional) / new_qty,
                                                   "opened_at": opened_at}
                    agent["cash"] -= delta_notional + fees
                else:
                    remainder = position["qty"] - delta_qty
                    agent["cash"] += delta_notional - fees
                    if remainder < -qty_epsilon:
                        # Broker truth must remain visible even after an unexpected short fill.
                        self._halt("Broker reported a sell larger than the recorded position; reconcile immediately.")
                    if abs(remainder) <= qty_epsilon:
                        agent["positions"].pop(symbol, None)
                    else:
                        agent["positions"][symbol] = {"qty": remainder, "avg_price": position["avg_price"] or average,
                                                       "opened_at": position.get("opened_at") or _now()}
                agent["fees"] += fees
                order["estimated_fees"] += fees
                order.update({"filled_qty": filled, "filled_notional": cumulative, "filled_avg_price": average})
                if order["size_type"] == "qty" and filled > order["qty"] + qty_epsilon:
                    self._halt("Broker fill exceeded submitted quantity; reconcile immediately.")
                if order["size_type"] == "notional" and cumulative > order["notional"] + 0.02:
                    self._halt("Broker fill exceeded submitted notional; reconcile immediately.")
            if order["status"] not in TERMINAL or delta_qty > qty_epsilon or status in TERMINAL:
                order["status"] = status
            if status == "filled" and order["filled_qty"] <= 0:
                order["status"] = "unknown"
                self._halt("Broker reported a filled order without fill details; reconcile immediately.")
            if broker_order.get("id"):
                order["broker_id"] = str(broker_order["id"])
            if order["status"] in TERMINAL:
                order["reserved"] = 0.0
            elif order["side"] == "buy":
                remaining = max(0.0, order["notional"] - order["filled_notional"]) if order["size_type"] == "notional" else max(0.0, order["qty"] - order["filled_qty"]) * order["price"]
                crypto = self._state["asset_registry"].get(order["symbol"], {}).get("asset_class") == "crypto"
                order["reserved"] = remaining * (1 + (0 if crypto else self._state["experiment"]["fee_bps"] / 10000))
            order["updated_at"] = _now()
            self._refresh(record=True)
        return deepcopy(order)

    def apply_crypto_fee(self, agent_id, activity):
        """Journal a broker-posted USD-pair fee once, in the same ledger write."""
        if not isinstance(activity, dict) or activity.get("activity_type") not in ("CFEE", "FEE"):
            raise EngineError("Unexpected crypto fee activity.")
        if activity.get("status", "executed") != "executed":
            raise EngineError("Crypto fee activity has not executed.")
        identifier = activity.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", identifier):
            raise EngineError("Crypto fee activity has no valid identifier.")
        raw_symbol = activity.get("symbol")
        symbol = raw_symbol if isinstance(raw_symbol, str) else ""
        if "/" not in symbol and symbol.endswith("USD"):
            symbol = symbol[:-3] + "/USD"
        if not SYMBOL_PATTERN.fullmatch(symbol) or not symbol.endswith("/USD"):
            raise EngineError("Crypto fee cannot be matched to a supported USD pair.")
        with self._write():
            fee_key = agent_id + ':' + identifier
            if fee_key in self._state["crypto_fee_ids"]:
                return False
            agent = self._agent(agent_id)
            if not any(o["agent_id"] == agent_id and o["symbol"] == symbol and o["filled_qty"] > 0 for o in self._records("orders", agent_id)):
                raise EngineError("Crypto fee has no matching filled order in this experiment.")
            if activity["activity_type"] == "CFEE":
                fee_qty = _number(activity.get("qty"), "Crypto fee quantity", minimum=-1e12)
                if fee_qty >= 0:
                    raise EngineError("Crypto fee quantity must reduce holdings.")
                position = agent["positions"].get(symbol)
                if not position or position["qty"] + fee_qty < -CRYPTO_QTY_EPS:
                    raise EngineError("Crypto fee exceeds the owned coin quantity.")
                # The activity's price records the fee when it was charged;
                # a later market move must not rewrite its historical cost.
                fee_price = _number(activity["price"], "Crypto fee price", positive=True) if "price" in activity else position["avg_price"]
                fee_value = -fee_qty * fee_price
                position["qty"] += fee_qty
                if position["qty"] <= CRYPTO_QTY_EPS:
                    agent["positions"].pop(symbol, None)
            else:
                amount = _number(activity.get("net_amount"), "Crypto cash fee", minimum=-1e12)
                if amount >= 0:
                    raise EngineError("Crypto cash fee must reduce cash.")
                agent["cash"] += amount
                fee_value = -amount
            agent["fees"] += fee_value
            self._state["crypto_fee_ids"].append(fee_key)
            self._event(f"{agent_id}: broker crypto fee recorded for {symbol} ({activity['activity_type']}).")
            self._refresh(record=True)
        return True

    def unknown_order(self, order_id, reason, halt=True):
        with self._write():
            order = self._order(order_id)
            if order["status"] in TERMINAL:
                raise EngineError("A terminal order cannot be marked unknown.")
            order["status"] = "unknown"
            order["error"] = _text(reason, "Unknown-order reason", 1000)
            order["updated_at"] = _now()
            if halt:
                self._halt("Order outcome is unknown; reconciliation is required.")
            else:
                control = self._state["agent_controls"][order["agent_id"]]
                if not control.get("paused") or control.get("pause_kind") not in (None, "operator", "integrity"):
                    control.update(paused=True, pause_kind="unknown_order", reason="Order outcome is unknown; reconciliation is required.", updated_at=_now(), paused_at=_now())
                self._event(f"{order['agent_id']}: order outcome is unknown; agent paused.", "error")
        return deepcopy(order)

    def resolve_unknown_rejection(self, *, experiment_id, agent_id, account_id,
                                  expected_order, expected_cash, expected_positions,
                                  broker_confirmation):
        """Commit the operator recovery record and reservation release together.

        The operator-only service workflow performs fresh broker checks before
        calling this primitive. Neither absence nor elapsed time is sufficient.
        Rechecking the exact ledger prevents a delayed verification from clearing
        an order whose state or portfolio changed while broker reads were pending.
        """
        confirmation = _text(broker_confirmation, "Broker confirmation", 1000)
        if len(confirmation) < 8 or not isinstance(expected_order, dict):
            raise EngineError("A recorded broker confirmation and exact original order are required.")
        with self._write():
            exp = self._state["experiment"]
            if exp["id"] != experiment_id or exp["mode"] != "paper":
                raise EngineError("The paper experiment changed during verification; reservation retained.")
            agent = self._agent(agent_id)
            control = self._state["agent_controls"][agent_id]
            order = self._order(expected_order.get("id"))
            if (order != expected_order or order["agent_id"] != agent_id
                    or order["status"] != "unknown" or order.get("broker_id")
                    or order.get("filled_qty", 0) != 0):
                raise EngineError("The unknown order changed during verification; reservation retained.")
            if (not control.get("paused") or agent.get("account_id") != account_id
                    or agent["cash"] != expected_cash or agent["positions"] != expected_positions
                    or any(item["agent_id"] == agent_id and item["id"] != order["id"] for item in self._pending())):
                raise EngineError("The account or portfolio changed during verification; reservation retained.")
            at, resolution_id = _now(), _id()
            order.update(status="rejected", reserved=0.0, updated_at=at)
            order["operator_resolution"] = {
                "id": resolution_id, "at": at, "source": "operator_broker_confirmation",
                "broker_confirmation": confirmation, "confirmed_not_accepted": True,
                "attested_by": "operator", "support_confirmation_independently_verified": False,
                "account_id": account_id, "experiment_id": experiment_id,
                "client_order_id": order["client_order_id"],
                "verification": "Two original-client-ID 404s; authenticated account, order inventory, positions and cash checked.",
            }
            # Successful monitoring must not automatically restart this agent
            # after an operator-attested recovery. Preserve a stronger existing
            # operator/integrity pause and always require fresh reconciliation.
            if control.get("pause_kind") not in ("operator", "integrity"):
                control.update(pause_kind="operator", reason="Unknown order resolved using recorded broker confirmation. Reconcile and resume explicitly.")
            control.update(paused=True, updated_at=at, paused_at=at, last_reconciled_at=None)
            self._state["events"].append({
                "at": at, "level": "warning", "agent_id": agent_id,
                "message": f"{agent_id}: operator recorded broker confirmation that order {order['client_order_id']} was never accepted; reservation released. Manual resume required.",
                "resolution_id": resolution_id, "order_id": order["id"],
            })
            self._refresh(record=True)
        return deepcopy(order)

    def attach_account(self, agent_id, account_id):
        account_id = _text(account_id, "Account id", 200)
        with self._write():
            agent = self._agent(agent_id)
            if agent.get("account_id") and agent["account_id"] != account_id:
                raise EngineError("This agent is bound to a different account.")
            if any(a["id"] != agent_id and a.get("account_id") == account_id for a in self._state["agents"]):
                raise EngineError("Each agent requires a different dedicated paper account.")
            agent["account_id"] = account_id
        return self.snapshot()

    def charge_model(self, agent_id, cost, call_id=None):
        cost = _number(cost, "Model cost")
        if call_id is not None:
            call_id = _text(call_id, "Model call id", 200)
        with self._write():
            agent = self._agent(agent_id)
            charges = agent.setdefault("model_charges", [])
            existing = self._db.execute("SELECT 1 FROM arena_records WHERE experiment_id=? AND kind='model_charges' AND record_key=?",
                                        (self._state["experiment"]["id"], agent_id + ":" + call_id)).fetchone() if call_id is not None else None
            if existing is not None:
                # Recovery may know only the earlier reservation estimate. The
                # first committed charge is authoritative for this call id.
                return self.snapshot()
            agent["model_cost"] += cost
            charges.append({"at": _now(), "cost": cost, "call_id": call_id})
            self._refresh(record=True)
        return self.snapshot()

    def pending_orders(self):
        with self.lock:
            return deepcopy(self._pending())

    def event(self, message, level="info"):
        if level not in ("info", "warning", "error"):
            raise EngineError("Unsupported event level.")
        with self._write():
            self._event(_text(message, "Event message", 1000), level)

    def set_auto_resume(self, enabled):
        if not isinstance(enabled, bool):
            raise EngineError("Automatic restart must be true or false.")
        with self._write():
            exp = self._state["experiment"]
            exp["auto_resume"] = enabled
            exp.pop("legacy_restart_preference_pending", None)
            if not enabled and exp.get("status") == "recovering":
                exp.update(status="paused", autopilot=False, startup_pending=False,
                           halt_reason="Automatic restart disabled by operator.")
            self._event("Automatic restart enabled." if enabled else "Automatic restart disabled.")
        return self.snapshot()

    def set_autopilot(self, enabled):
        if not isinstance(enabled, bool):
            raise EngineError("Autopilot must be true or false.")
        with self._write():
            exp = self._state["experiment"]
            if enabled and (exp["mode"] != "paper" or exp["status"] != "ready"):
                raise EngineError("Resume a paper experiment before enabling autopilot.")
            if enabled and any(not self._state["agent_controls"].get(o["agent_id"], {}).get("paused") for o in self._pending()):
                raise EngineError("Reconcile active agents' pending orders before enabling autopilot.")
            exp["autopilot"] = enabled
        return self.snapshot()

    @staticmethod
    def _synthetic_bars(symbol, count):
        base = {"SPY": 500.0, "QQQ": 450.0, "IWM": 200.0}[symbol]
        phase = {"SPY": 0, "QQQ": 3, "IWM": 7}[symbol]
        day = datetime(2024, 1, 2, 21, tzinfo=timezone.utc)
        bars = []
        for index in range(count):
            while day.weekday() >= 5:
                day += timedelta(days=1)
            close = base * (1 + .0005 * index + .09 * math.sin((index + phase) / 14) + .01 * math.sin(index / 3))
            bars.append({"t": day.isoformat(), "o": close * .998, "h": close * 1.005, "l": close * .995, "c": close, "v": 1000000})
            day += timedelta(days=1)
        return bars

    def demo_step(self, count=1):
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 250:
            raise EngineError("Demo steps must be an integer between 1 and 250.")
        from .adapters import baseline_signal
        with self.lock:
            if self._state["experiment"]["mode"] != "demo":
                raise EngineError("Synthetic replay is available only in demo mode.")
            if self._state["experiment"]["status"] != "ready":
                raise EngineError("Resume the demo experiment before stepping.")
            for _ in range(count):
                if self._state["experiment"]["status"] != "ready":
                    break
                step = self._state["experiment"]["step"] + 1
                bars = {symbol: self._synthetic_bars(symbol, 60 + step) for symbol in SYMBOLS}
                prices = {symbol: series[-1]["c"] for symbol, series in bars.items()}
                at = bars["SPY"][-1]["t"]
                self.mark(prices, at, source="synthetic_demo")
                with self._write():
                    self._state["experiment"]["step"] = step
                    self._state["experiment"]["started"] = True
                for agent_id in [a["id"] for a in self._state["agents"]]:
                    for symbol in SYMBOLS:
                        agent = self._agent(agent_id)
                        if self._state["agent_controls"][agent_id]["paused"]:
                            continue
                        signal = baseline_signal(bars[symbol])
                        action = signal["action"]
                        if agent["provider"] != "rules":
                            self.record_decision(agent_id, {**signal, "action": "hold", "symbol": symbol, "at": at, "status": "disconnected",
                                "reason": "Synthetic demo: this reviewer is disconnected; no provider request or trade occurred.", "source": "synthetic_demo"})
                            continue
                        decision = self.record_decision(agent_id, {**signal, "symbol": symbol, "at": at, "source": "synthetic_demo", "status": "synthetic"})
                        position = agent["positions"].get(symbol)
                        try:
                            if action == "sell" and position and position["qty"] > 0:
                                fill_price = prices[symbol] * (1 - self._state["experiment"]["slippage_bps"] / 10000)
                                order = self.reserve_order(agent_id, symbol, "sell", prices[symbol], qty=position["qty"], decision_id=decision["id"])
                                self.update_order(order["id"], {"id": "demo_" + order["id"], "status": "filled", "filled_qty": order["qty"], "filled_avg_price": fill_price})
                            elif action == "buy" and not position:
                                exp = self._state["experiment"]
                                basis = self._capital_basis(agent)
                                exposure = sum(p["qty"] * prices[s] for s, p in agent["positions"].items())
                                amount = min(basis * exp["position_cap_pct"] / 100,
                                             basis * exp["exposure_cap_pct"] / 100 - exposure,
                                             agent["available_cash"] / (1 + exp["fee_bps"] / 10000))
                                amount = float(Decimal(str(max(0, amount))).quantize(Decimal(".01"), rounding=ROUND_DOWN))
                                if amount >= 1:
                                    order = self.reserve_order(agent_id, symbol, "buy", prices[symbol], notional=amount, decision_id=decision["id"])
                                    fill_price = prices[symbol] * (1 + exp["slippage_bps"] / 10000)
                                    self.update_order(order["id"], {"id": "demo_" + order["id"], "status": "filled", "filled_qty": amount / fill_price, "filled_avg_price": fill_price})
                        except EngineError as exc:
                            self.event(f"Synthetic {agent_id}/{symbol}: {exc}", "warning")
                with self._write():
                    self._refresh(record=True)
        return self.snapshot()
