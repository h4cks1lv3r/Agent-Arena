"""Validated, transactional import of the two v0.4 audit storage layouts.

Call only while the application is stopped. Disk databases get a consistent
SQLite backup (including WAL content) before any schema or payload mutation.
Legacy tables remain intact. A failed conversion rolls back the whole database.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

SCHEMAS = {
    "arena_state": ("id", "payload"),
    "arena_archives": ("id", "payload"),
    "arena_records": ("seq", "experiment_id", "kind", "record_key", "agent_id", "client_order_id", "broker_id", "payload"),
    "arena_decisions": ("id", "experiment", "agent_id", "at", "payload"),
    "arena_events": ("seq", "experiment", "at", "level", "message"),
    "arena_chart": ("experiment_id", "level", "bucket", "payload"),
}
# (SQLite declared type, NOT NULL, primary-key order), in column order.
COLUMN_FLAGS = {
    "arena_state": (("INTEGER", 0, 1), ("TEXT", 1, 0)),
    "arena_archives": (("TEXT", 0, 1), ("TEXT", 1, 0)),
    "arena_records": (("INTEGER", 0, 1), ("TEXT", 1, 0), ("TEXT", 1, 0), ("TEXT", 1, 0),
                      ("TEXT", 0, 0), ("TEXT", 0, 0), ("TEXT", 0, 0), ("TEXT", 1, 0)),
    "arena_decisions": (("TEXT", 0, 1), ("TEXT", 1, 0), ("TEXT", 1, 0), ("TEXT", 1, 0), ("TEXT", 1, 0)),
    "arena_events": (("INTEGER", 0, 1), ("TEXT", 1, 0), ("TEXT", 1, 0), ("TEXT", 1, 0), ("TEXT", 1, 0)),
    "arena_chart": (("TEXT", 1, 1), ("INTEGER", 1, 2), ("INTEGER", 1, 3), ("TEXT", 1, 0)),
}
KINDS = ("history", "decisions", "events", "orders", "model_charges")


class MigrationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def inspect_database(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables:
        if table.startswith("arena_") and table not in SCHEMAS:
            raise MigrationError(f"Unknown database table {table}; migration refused.")
        if table in SCHEMAS:
            info = list(db.execute(f"PRAGMA table_info({table})"))
            columns = tuple(r[1] for r in info)
            flags = tuple((r[2].upper(), r[3], r[5]) for r in info)
            if columns != SCHEMAS[table] or flags != COLUMN_FLAGS[table]:
                raise MigrationError(f"Unknown {table} layout; migration refused.")
    row = db.execute("SELECT payload FROM arena_state WHERE id=1").fetchone() if "arena_state" in tables else None
    if row is None:
        if any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in tables if table in SCHEMAS):
            raise MigrationError("Audit tables exist without an active experiment; migration refused.")
        return None, None, tables
    state = json.loads(row[0])
    version = state.get("storage_version")
    if (version is not None and type(version) is not int) or version not in (None, 2, 3):
        raise MigrationError("Unknown storage version; migration refused.")
    if version in (2, 3) and "arena_records" not in tables:
        raise MigrationError("Indexed storage table is missing; migration refused.")
    if version == 3 and "arena_chart" not in tables:
        raise MigrationError("Chart storage table is missing; migration refused.")
    for field, expected in (("experiment", dict), ("agents", list), ("history", list), ("decisions", list), ("events", list), ("orders", list)):
        if not isinstance(state.get(field), expected):
            raise MigrationError(f"Invalid legacy field {field}; migration refused.")
    return state, version, tables


def backup_locked(db_path):
    """Caller owns BEGIN IMMEDIATE; a second read connection sees its old commit."""
    if str(db_path) == ":memory:":
        return None
    path = Path(db_path)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + f".pre-v05-{suffix}-{uuid.uuid4().hex[:8]}.bak")
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        source = sqlite3.connect(str(path))
        target = sqlite3.connect(str(backup))
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("Pre-upgrade backup failed integrity verification.")
        finally:
            source.close()
            target.close()
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return str(backup)


def source_states(db, active, version, tables):
    """Materialize once at upgrade, never on the quote/control path."""
    states = {active["experiment"]["id"]: deepcopy(active)}
    if "arena_archives" in tables:
        for archive_id, payload in db.execute("SELECT id,payload FROM arena_archives ORDER BY rowid"):
            archived = json.loads(payload)
            if archived["experiment"]["id"] != archive_id or archive_id in states:
                raise MigrationError("Archive experiment identity is inconsistent.")
            states[archive_id] = archived
    # v2 indexed records hold full evidence; its active state is only a view.
    if version == 2:
        grouped = defaultdict(lambda: defaultdict(list))
        for experiment, kind, payload in db.execute("SELECT experiment_id,kind,payload FROM arena_records ORDER BY seq"):
            if kind not in KINDS or experiment not in states:
                raise MigrationError("Unknown audit kind or orphan experiment; migration refused.")
            grouped[experiment][kind].append(json.loads(payload))
        for experiment, state in states.items():
            if state.get("audit_counts"):
                for kind, count in state["audit_counts"].items():
                    if kind in KINDS and len(grouped[experiment][kind]) != count:
                        raise MigrationError("Indexed audit counts do not match stored rows; migration refused.")
            for kind in ("history", "decisions", "events", "orders"):
                if kind in grouped[experiment]:
                    state[kind] = grouped[experiment][kind]
            for agent in state["agents"]:
                charges = [item for item in db.execute("SELECT payload FROM arena_records WHERE experiment_id=? AND kind='model_charges' AND agent_id=? ORDER BY seq", (experiment, agent["id"]))]
                if charges:
                    agent["model_charges"] = [json.loads(item[0]) for item in charges]
    if "arena_decisions" in tables:
        full = defaultdict(dict)
        for item_id, experiment, agent_id, at, payload in db.execute("SELECT id,experiment,agent_id,at,payload FROM arena_decisions ORDER BY at,id"):
            item = json.loads(payload)
            if experiment not in states or item.get("id") != item_id or item.get("agent_id") != agent_id or item.get("at") != at:
                raise MigrationError("Legacy decision identity is inconsistent; migration refused.")
            full[experiment][item_id] = item
        for experiment, state in states.items():
            found = full[experiment]
            merged, seen = [], set()
            for item in state["decisions"]:
                key = item["id"]
                if item.get("detail") and key not in found:
                    raise MigrationError("Legacy decision detail is missing; migration refused.")
                if key in found:
                    authoritative = found[key]
                    if any(key not in ("detail",) and key in authoritative and value != authoritative[key] for key, value in item.items()):
                        raise MigrationError("Legacy decision summary conflicts with its full record.")
                    item = authoritative
                merged.append(item)
                seen.add(key)
            merged.extend(item for key, item in found.items() if key not in seen)
            state["decisions"] = merged
    if "arena_events" in tables:
        archived_events = defaultdict(list)
        for seq, experiment, at, level, message in db.execute("SELECT seq,experiment,at,level,message FROM arena_events ORDER BY seq"):
            if experiment not in states:
                raise MigrationError("Orphan legacy events; migration refused.")
            archived_events[experiment].append({"id": f"claude_event_{seq}", "at": at, "level": level, "message": message})
        for experiment, rows in archived_events.items():
            # Claude archives copy only its hot state. SQL events are distinct
            # physical records, even when text and timestamps are identical.
            # Re-imported indexed records can be identified only by stable ID.
            ids = {item["id"] for item in rows}
            tail = [item for item in states[experiment]["events"] if item.get("id") not in ids]
            states[experiment]["events"] = rows + tail
    for state in states.values():
        experiment = state["experiment"]
        experiment.setdefault("loss_limit_includes_model_costs", version != 2)
        experiment.setdefault("auto_resume", True)
        experiment.setdefault("output_token_limit", 8192)
        if "arena_decisions" in tables:
            # Service imports Claude's separately stored opt-out before any
            # recovery. The marker survives crashes between the two DB opens.
            experiment.setdefault("legacy_restart_preference_pending", True)
        state["storage_version"] = 3
    return states


def record_digest(rows):
    """Order-independent content digest, including identity and multiplicity."""
    digest = hashlib.sha256()
    for row in sorted(canonical(row) for row in rows):
        digest.update(row.encode("utf-8") + b"\n")
    return digest.hexdigest()
