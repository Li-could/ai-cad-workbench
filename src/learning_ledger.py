"""Append-only hash-chained audit ledger for the CAD learning loop.

Artifacts stay in their existing files. SQLite only records compact,
cross-linked events so every baseline decision can be audited and tampering
with the history can be detected.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


GENESIS_HASH = "GENESIS"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _canonical(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _db_path(root: str | Path) -> Path:
    return Path(root) / "ledger.db"


def _connect(root: str | Path) -> sqlite3.Connection:
    path = _db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            entity_type TEXT,
            entity_id TEXT,
            actor TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type_time "
        "ON events(event_type, created_at)"
    )
    return connection


def _event_hash(
    event_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor: str,
    payload_json: str,
    prev_hash: str,
    created_at: str,
) -> str:
    material = "\n".join(
        (
            prev_hash,
            event_id,
            event_type,
            entity_type,
            entity_id,
            actor,
            payload_json,
            created_at,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def record_event(
    root: str | Path,
    event_type: str,
    payload: Any = None,
    entity_type: str = "",
    entity_id: str = "",
    actor: str = "system",
) -> dict:
    payload_json = _canonical(payload if payload is not None else {})
    created_at = _now()
    event_id = f"ev_{uuid.uuid4().hex[:16]}"
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = str(previous["event_hash"]) if previous else GENESIS_HASH
        event_hash = _event_hash(
            event_id,
            event_type,
            entity_type or "",
            entity_id or "",
            actor or "system",
            payload_json,
            prev_hash,
            created_at,
        )
        cursor = connection.execute(
            """
            INSERT INTO events (
                event_id, event_type, entity_type, entity_id, actor,
                payload_json, prev_hash, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                entity_type or None,
                entity_id or None,
                actor or "system",
                payload_json,
                prev_hash,
                event_hash,
                created_at,
            ),
        )
        connection.commit()
        return {
            "ok": True,
            "seq": cursor.lastrowid,
            "event_id": event_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "prev_hash": prev_hash,
            "event_hash": event_hash,
            "created_at": created_at,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_events(
    root: str | Path,
    limit: int = 50,
    event_type: str | None = None,
) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    connection = _connect(root)
    try:
        if event_type:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE event_type = ?
                ORDER BY seq DESC LIMIT ?
                """,
                (event_type, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        events = []
        for row in rows:
            events.append(
                {
                    "seq": row["seq"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "actor": row["actor"],
                    "payload": json.loads(row["payload_json"]),
                    "prev_hash": row["prev_hash"],
                    "event_hash": row["event_hash"],
                    "created_at": row["created_at"],
                }
            )
        return events
    finally:
        connection.close()


def verify_chain(root: str | Path) -> dict:
    connection = _connect(root)
    try:
        rows = connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
        previous_hash = GENESIS_HASH
        problems = []
        for row in rows:
            expected = _event_hash(
                row["event_id"],
                row["event_type"],
                row["entity_type"] or "",
                row["entity_id"] or "",
                row["actor"],
                row["payload_json"],
                row["prev_hash"],
                row["created_at"],
            )
            if row["prev_hash"] != previous_hash:
                problems.append(
                    {
                        "seq": row["seq"],
                        "event_id": row["event_id"],
                        "reason": "prev_hash_mismatch",
                    }
                )
            if row["event_hash"] != expected:
                problems.append(
                    {
                        "seq": row["seq"],
                        "event_id": row["event_id"],
                        "reason": "event_hash_mismatch",
                    }
                )
            previous_hash = row["event_hash"]
        return {
            "ok": not problems,
            "event_count": len(rows),
            "head_hash": previous_hash,
            "problems": problems,
        }
    finally:
        connection.close()


def status(root: str | Path) -> dict:
    verification = verify_chain(root)
    connection = _connect(root)
    try:
        latest = connection.execute(
            """
            SELECT seq, event_id, event_type, entity_type, entity_id,
                   actor, event_hash, created_at
            FROM events ORDER BY seq DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    path = _db_path(root)
    return {
        "ok": verification["ok"],
        "database": str(path),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "event_count": verification["event_count"],
        "head_hash": verification["head_hash"],
        "verification": verification,
        "latest": dict(latest) if latest else None,
    }
