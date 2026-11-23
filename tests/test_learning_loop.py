from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import ezdxf

import edit_intent
import learning_loop
import store
import text_edit_engine


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fake_task(tmp: Path, project_root: Path, task_id: str = "task_learn_1") -> Path:
    task_dir = tmp / "tasks" / task_id
    task_dir.mkdir(parents=True)
    source = tmp / "uploads" / "25QYL-2SL.dwg"
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / "input" / "25QYL-2SL.dwg", source)
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0, 0), (10, 0))
    doc.saveas(str(task_dir / "result.dxf"))
    report = {
        "ok": True,
        "engine": "real_mm_engine",
        "checks": {
            "audit": {"passed": True},
            "view_height": {"passed": True, "reference": 284.0, "aligned": 284.0},
            "annotation_consistency": {"passed": True, "count": 2},
        },
        "calibration": {
            "kx": 4.2,
            "ky": 4.0,
            "targets": {"length": 610.0, "height": 284.0},
        },
        "changes": [
            {"kind": "BLOCK", "where": "GS_BlockDim_1", "old": "610", "new": "610.0"},
            {"kind": "BLOCK", "where": "GS_BlockDim_2", "old": "284", "new": "284.0"},
        ],
    }
    report_path = task_dir / "report.json"
    _write(report_path, report)
    task = {
        "task_id": task_id,
        "instruction": "把图改成真实毫米 1:1，总长610，总高284",
        "status": "executed",
        "created_at": "2026-01-01T10:00:00",
        "updated_at": "2026-01-01T10:00:10",
        "task_type": "real_mm",
        "recipe_id": "test-real-mm",
        "files": {
            "source": {
                "name": "25QYL-2SL.dwg",
                "path": str(source),
                "size": source.stat().st_size,
                "sha256": _sha256(source),
            },
            "reference": None,
        },
        "plan": {"intent": {}},
        "intent": {
            "task_type": "real_mm",
            "mode": "real_mm",
            "length_value": 610.0,
            "height_value": 284.0,
            "length_anchor": "610",
            "height_anchor": "284",
            "unit": "mm",
        },
        "versions": [{"version": 1, "note": "test"}],
        "current_version": 1,
        "report": str(report_path),
        "logs": [],
    }
    _write(task_dir / "manifest.json", task)
    return task_dir


def _fake_text_task(tmp: Path, task_id: str = "task_text_1") -> Path:
    task_dir = tmp / "tasks" / task_id
    task_dir.mkdir(parents=True)
    source = tmp / "uploads" / "material.dxf"
    source.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("材料：Q235", dxfattribs={"height": 2.5})
    doc.modelspace().add_line((0, 0), (100, 0))
    doc.saveas(str(source))

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(str(source)))
    intent = edit_intent.parse_text_edit_intent("把Q235改成304", inventory)
    result = text_edit_engine.apply_edits(
        source, task_dir / "result.dxf", intent["edits"]
    )
    report = {
        "ok": result["ok"],
        "engine": "text_edit_engine",
        "checks": {
            "audit": {"passed": result["qa"]["audit_ok"]},
            "geometry_unchanged": {
                "passed": result["stat"]["geometry_identical"]
            },
            "entity_count": {"passed": result["stat"]["entity_count_same"]},
            "text_verified": {"passed": result["qa"]["all_expected"]},
            "untouched_text": {"passed": result["qa"]["only_targets_changed"]},
            "dimension_guard": {
                "passed": result["qa"]["dimension_labels_only"]
            },
            "shared_block_guard": {
                "passed": result["qa"]["shared_blocks_explicit"]
            },
        },
        "text_edit": result,
        "changes": result["changes"],
    }
    report_path = task_dir / "report.json"
    _write(report_path, report)
    task = {
        "task_id": task_id,
        "instruction": "把Q235改成304",
        "status": "executed",
        "created_at": "2026-01-01T10:00:00",
        "updated_at": "2026-01-01T10:00:10",
        "task_type": "text_edit",
        "files": {
            "source": {
                "name": source.name,
                "path": str(source),
                "size": source.stat().st_size,
                "sha256": _sha256(source),
            },
            "reference": None,
        },
        "plan": {"intent": intent},
        "intent": intent,
        "versions": [{"version": 1, "note": "text edit test"}],
        "current_version": 1,
        "report": str(report_path),
        "logs": [],
    }
    _write(task_dir / "manifest.json", task)
    return task_dir


def test_learning_loop_observes_proposes_and_blocks_invalid_case(
    tmp_path: Path, monkeypatch, project_root: Path
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    _fake_task(tmp_path, project_root)

    observed = learning_loop.observe()
    assert observed["episode_count"] == 1
    episodes = learning_loop.list_episodes()
    assert episodes[0]["outcome"] == "passed"
    assert episodes[0]["task_type"] == "real_mm"

    feedback = learning_loop.record_feedback(
        "task_learn_1", "accepted", note="回归基线验收", by="pytest"
    )
    assert feedback["verdict"] == "accepted"
    learning_loop.observe()
    diagnosis = learning_loop.diagnose()
    assert diagnosis["feedback"].get("accepted") == 1

    proposed = learning_loop.propose()
    assert proposed["created"] == 1
    proposal = proposed["proposals"][0]
    assert proposal["kind"] == "regression_case"
    assert proposal["payload"]["expected"]["annotation_texts"] == ["284.0", "610.0"]

    blocked = learning_loop.approve_proposal(
        proposal["proposal_id"], note="dummy case must fail replay", by="pytest"
    )
    assert blocked["status"] == "blocked"
    assert learning_loop.list_cases() == []
    assert (tmp_path / "learning" / "quarantine" / proposal["proposal_id"]).is_dir()

    status = learning_loop.status()
    assert status["episodes"] == 1
    assert status["cases"] == 0
    assert status["proposals"].get("blocked") == 1


def test_user_rejection_invalidates_baseline_and_allows_fresh_candidate(
    tmp_path: Path, monkeypatch, project_root: Path
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    _fake_task(tmp_path, project_root)
    learning_loop.observe()

    episode = learning_loop.list_episodes()[0]
    fingerprint = episode["fingerprint"]
    case_id = "case_test_user_feedback"
    _write(
        tmp_path / "learning" / "cases" / case_id / "case.json",
        {
            "case_id": case_id,
            "status": "active",
            "title": "用户即将否决的基线",
            "task_type": "real_mm",
            "fingerprint": fingerprint,
            "origin_task_id": "task_learn_1",
            "origin_version": 1,
            "expected": {"annotation_texts": ["284.0", "610.0"]},
            "created_at": "2026-01-01T10:01:00",
        },
    )
    _write(
        tmp_path / "learning" / "proposals" / "pr_old.json",
        {
            "proposal_id": "pr_old",
            "kind": "regression_case",
            "status": "promoted",
            "title": "旧基线",
            "fingerprint": fingerprint,
            "task_type": "real_mm",
            "case_id": case_id,
            "created_at": "2026-01-01T10:01:00",
        },
    )
    assert [row["case_id"] for row in learning_loop.list_cases()] == [case_id]

    rejected = learning_loop.record_feedback(
        "task_learn_1", "needs_work", note="右视图比例仍不对", by="pytest"
    )
    assert rejected["invalidated_cases"] == [
        {
            "case_id": case_id,
            "title": "用户即将否决的基线",
            "reason": "用户验收 needs_work",
        }
    ]
    assert learning_loop.list_cases() == []
    invalidated = learning_loop.list_cases(include_invalidated=True)
    assert invalidated[0]["status"] == "invalidated"
    assert invalidated[0]["invalidated_by_feedback"] == rejected["feedback_id"]

    learning_loop.observe()
    assert learning_loop.list_episodes()[0]["outcome"] == "rejected_by_user"
    accepted = learning_loop.record_feedback(
        "task_learn_1", "accepted", note="返工后重新验收", by="pytest"
    )
    assert accepted["invalidated_cases"] == []
    learning_loop.observe()

    proposed = learning_loop.propose()
    assert proposed["created"] == 1
    assert proposed["proposals"][0]["fingerprint"] == fingerprint

    status = learning_loop.status()
    assert status["cases"] == 0
    assert status["invalidated_cases"] == 1


def test_code_fingerprint_tracks_behavior_configuration(tmp_path: Path, monkeypatch):
    profile = tmp_path / "knowledge" / "drawings" / "demo.profile.json"
    _write(profile, {"profile_id": "demo", "version": 1})
    monkeypatch.setattr(learning_loop, "ROOT", tmp_path)

    before = learning_loop._code_fingerprint()
    _write(profile, {"profile_id": "demo", "version": 2})
    after = learning_loop._code_fingerprint()

    assert before != after

    source = tmp_path / "src" / "drawing_measure.py"
    source.parent.mkdir(parents=True)
    source.write_text("TOLERANCE = 0.5\n", encoding="utf-8")
    with_measure = learning_loop._code_fingerprint()
    assert with_measure != after

    source.write_text("TOLERANCE = 1.0\n", encoding="utf-8")
    changed_measure = learning_loop._code_fingerprint()
    assert changed_measure != with_measure


def test_gate_fingerprint_tracks_replay_checks_not_panel_code():
    source_path = Path(learning_loop.__file__)
    source = source_path.read_text(encoding="utf-8")
    baseline = learning_loop._gate_fingerprint(source)

    assert learning_loop._gate_fingerprint(source + "\n# panel-only change\n") == baseline
    original_check = "abs(left_height - right_height) <= 0.05"
    assert original_check in source
    relaxed_check = source.replace(
        original_check,
        "abs(left_height - right_height) <= 0.5",
        1,
    )
    assert learning_loop._gate_fingerprint(relaxed_check) != baseline

    assert "src/learning_loop.py" not in learning_loop.CORE_FILES
    assert "src/learning_ledger.py" not in learning_loop.CORE_FILES


def test_gate_fingerprint_change_invalidates_replay(
    tmp_path: Path, monkeypatch
):
    learning_root = tmp_path / "learning"
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", learning_root)
    monkeypatch.setattr(learning_loop, "list_episodes", lambda: [])
    monkeypatch.setattr(learning_loop, "list_proposals", lambda status=None: [])
    monkeypatch.setattr(
        learning_loop,
        "list_cases_with_integrity",
        lambda include_invalidated=True: [
            {
                "case_id": "case_gate",
                "status": "active",
                "task_type": "real_mm",
                "_integrity": {"status": "sealed"},
            }
        ],
    )
    monkeypatch.setattr(learning_loop, "_code_fingerprint", lambda: "code-v1")
    monkeypatch.setattr(learning_loop, "_gate_fingerprint", lambda: "gate-v1")
    _write(
        learning_root / "replays" / "latest.json",
        {
            "replay_id": "rp_gate",
            "ok": True,
            "code_fingerprint": "code-v1",
            "gate_fingerprint": "gate-v1",
            "results": [{"case_id": "case_gate", "passed": True}],
        },
    )

    ready = learning_loop.status()
    assert ready["baseline_ready"] is True
    assert ready["replay_stale"] is False

    monkeypatch.setattr(learning_loop, "_gate_fingerprint", lambda: "gate-v2")
    stale = learning_loop.status()
    assert stale["baseline_ready"] is False
    assert stale["replay_stale"] is True
    assert stale["next_action"]["code"] == "replay"


def test_case_integrity_seals_and_blocks_tampered_baseline(
    tmp_path: Path, monkeypatch
):
    learning_root = tmp_path / "learning"
    case_dir = learning_root / "cases" / "case_integrity"
    case_dir.mkdir(parents=True)
    (case_dir / "source.dwg").write_bytes(b"source")
    (case_dir / "expected.dxf").write_bytes(b"expected-dxf")
    (case_dir / "expected.dwg").write_bytes(b"expected-dwg")
    (case_dir / "expected_report.json").write_text("{}", encoding="utf-8")
    meta = {
        "case_id": "case_integrity",
        "status": "active",
        "title": "封存完整性测试",
        "task_type": "real_mm",
        "created_at": "2026-01-01T10:00:00",
        "source": {"path": "source.dwg"},
        "artifacts": {
            "expected_dxf": "expected.dxf",
            "expected_dwg": "expected.dwg",
            "expected_report": "expected_report.json",
        },
    }
    _write(case_dir / "case.json", meta)
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", learning_root)

    sealed = learning_loop.seal_cases()
    assert sealed["ok"] is True
    assert sealed["sealed"] == ["case_integrity"]
    sealed_meta = learning_loop.list_cases_with_integrity()[0]
    assert sealed_meta["_integrity"]["status"] == "sealed"

    (case_dir / "expected.dxf").write_bytes(b"tampered")
    broken = learning_loop.list_cases_with_integrity()[0]
    assert broken["_integrity"]["status"] == "broken"
    assert broken["_integrity"]["changed"] == ["expected.dxf"]

    tampered_meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    tampered_meta["expected"] = {"annotation_texts": ["999.0"]}
    _write(case_dir / "case.json", tampered_meta)
    metadata_broken = learning_loop.list_cases_with_integrity()[0]
    assert metadata_broken["_integrity"]["status"] == "broken"
    assert metadata_broken["_integrity"]["metadata_changed"] is True

    replay_result = learning_loop._run_case(sealed_meta, case_dir, with_dwg=False)
    assert replay_result["passed"] is False
    assert "哈希不匹配" in replay_result["error"]

    status = learning_loop.status()
    assert status["case_integrity"]["broken"] == 1
    assert status["baseline_ready"] is False
    assert status["next_action"]["code"] == "baseline_broken"


def test_unknown_legacy_task_is_not_counted_as_failure(
    tmp_path: Path, monkeypatch, project_root: Path
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    explicit = _fake_task(tmp_path, project_root, task_id="task_legacy_explicit")
    manifest_path = explicit / "manifest.json"
    task = json.loads(manifest_path.read_text(encoding="utf-8"))
    task["task_type"] = "unknown"
    _write(manifest_path, task)

    missing = _fake_task(tmp_path, project_root, task_id="task_legacy_missing")
    manifest_path = missing / "manifest.json"
    task = json.loads(manifest_path.read_text(encoding="utf-8"))
    task.pop("task_type", None)
    task.pop("intent", None)
    task.pop("report", None)
    task["instruction"] = "历史任务，无类型与报告元数据"
    _write(manifest_path, task)
    (missing / "report.json").unlink()
    (missing / "result.dxf").unlink()

    observed = learning_loop.observe()
    assert observed["episode_count"] == 2
    episodes = {row["task_id"]: row for row in learning_loop.list_episodes()}
    assert episodes["task_legacy_explicit"]["outcome"] == "legacy"
    assert episodes["task_legacy_missing"]["outcome"] == "legacy"
    assert all(row["task_type"] == "unknown" for row in episodes.values())
    assert learning_loop.diagnose()["failure_patterns"] == []
    assert learning_loop.propose()["created"] == 0


def test_uncalibrated_task_is_expected_gate_not_failure(
    tmp_path: Path, monkeypatch, project_root: Path
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    task_dir = _fake_task(tmp_path, project_root, task_id="task_needs_calibration")
    manifest = task_dir / "manifest.json"
    task = json.loads(manifest.read_text(encoding="utf-8"))
    task["status"] = "needs_calibration"
    task["task_type"] = "geometry"
    task["profile_id"] = None
    task.pop("report", None)
    _write(manifest, task)
    (task_dir / "report.json").unlink()
    (task_dir / "result.dxf").unlink()

    learning_loop.observe()
    episode = learning_loop.list_episodes()[0]
    assert episode["outcome"] == "needs_calibration"
    assert episode["task_type"] == "geometry"
    assert learning_loop.diagnose()["failure_patterns"] == []
    assert learning_loop.propose()["created"] == 0


def test_needs_instruction_is_expected_gate_not_failure(
    tmp_path: Path, monkeypatch, project_root: Path
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    task_dir = _fake_task(tmp_path, project_root, task_id="task_needs_instruction")
    manifest = task_dir / "manifest.json"
    task = json.loads(manifest.read_text(encoding="utf-8"))
    task["status"] = "needs_instruction"
    task["task_type"] = "geometry"
    task["profile_id"] = "td50-40g"
    task["plan"]["known_rules"] = False
    task["plan"]["requested_rules"] = []
    task.pop("report", None)
    _write(manifest, task)
    (task_dir / "report.json").unlink()
    (task_dir / "result.dxf").unlink()

    learning_loop.observe()
    episode = learning_loop.list_episodes()[0]
    assert episode["outcome"] == "needs_instruction"
    assert episode["task_type"] == "geometry"
    assert learning_loop.diagnose()["failure_patterns"] == []
    assert learning_loop.propose()["created"] == 0


def test_learning_operations_append_hash_chain_audit(
    tmp_path: Path, monkeypatch, project_root: Path
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    _fake_task(tmp_path, project_root)

    learning_loop.observe()
    learning_loop.record_feedback(
        "task_learn_1", "accepted", note="审计测试", by="pytest"
    )

    audit = learning_loop.audit(limit=20)
    assert audit["ok"] is True
    assert audit["ledger"]["verification"]["ok"] is True
    event_types = {row["event_type"] for row in audit["events"]}
    assert {"observe", "feedback"} <= event_types


def test_text_edit_baseline_replays_fragment_edits(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", tmp_path / "learning")
    monkeypatch.setattr(store, "TASKS", tmp_path / "tasks")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(
        learning_loop,
        "_dwg_roundtrip",
        lambda dxf, dwg: {"passed": True, "export": {}, "roundtrip": {}},
    )
    _fake_text_task(tmp_path)

    learning_loop.observe()
    episode = learning_loop.list_episodes()[0]
    assert episode["task_type"] == "text_edit"
    assert episode["outcome"] == "passed"

    learning_loop.record_feedback(
        "task_text_1", "accepted", note="文字修改验收", by="pytest"
    )
    learning_loop.observe()
    proposed = learning_loop.propose()
    assert proposed["created"] == 1
    expected = proposed["proposals"][0]["payload"]["expected"]["text_edits"][0]
    assert expected["old"] == "Q235"
    assert expected["new"] == "304"
    assert expected["expected_text"] == "材料：304"
    assert expected["mode"] == "replace"

    approved = learning_loop.approve_proposal(
        proposed["proposals"][0]["proposal_id"],
        note="文字修改链隔离重放",
        by="pytest",
    )
    assert approved["status"] == "promoted"
    case = learning_loop.list_cases()[0]
    replay = learning_loop._run_case(
        case, Path(case["_path"]).parent, with_dwg=True
    )
    assert replay["passed"] is True
    assert replay["checks"]["text_verified"]["passed"] is True
    assert replay["checks"]["dimension_guard"]["passed"] is True
    assert replay["checks"]["shared_block_guard"]["passed"] is True
