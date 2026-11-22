from __future__ import annotations

import sqlite3
from pathlib import Path

import learning_ledger


def test_hash_chain_detects_event_tampering(tmp_path: Path):
    root = tmp_path / "learning"
    first = learning_ledger.record_event(
        root,
        "observe",
        payload={"created": 1},
        entity_type="episode",
    )
    second = learning_ledger.record_event(
        root,
        "feedback",
        payload={"verdict": "accepted"},
        entity_type="task",
        entity_id="task_1",
        actor="pytest",
    )

    assert second["prev_hash"] == first["event_hash"]
    verified = learning_ledger.verify_chain(root)
    assert verified["ok"] is True
    assert verified["event_count"] == 2
    assert verified["head_hash"] == second["event_hash"]

    events = learning_ledger.list_events(root, limit=10)
    assert [row["event_type"] for row in events] == ["feedback", "observe"]

    connection = sqlite3.connect(root / "ledger.db")
    connection.execute(
        "UPDATE events SET payload_json = ? WHERE seq = 1",
        ('{"tampered":true}',),
    )
    connection.commit()
    connection.close()

    broken = learning_ledger.verify_chain(root)
    assert broken["ok"] is False
    assert broken["problems"][0]["seq"] == 1
