from __future__ import annotations

import hashlib
from pathlib import Path

import ezdxf
from fastapi.testclient import TestClient

import app as app_module
import engine
import intent_parser
import learning_loop
import store


def test_upload_sidecar_survives_without_task(monkeypatch, tmp_path: Path,
                                             project_root: Path):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(store, "TASKS", tasks)
    source = project_root / "input" / "TD50-40G.dwg"
    record = store.save_upload(source, "TD50-40G.dwg")

    recovered = store.get_file(record["file_id"])
    assert recovered is not None
    assert recovered["sha256"] == record["sha256"]
    assert Path(recovered["path"]).exists()


def test_api_creates_geometry_task_without_reference_image(
    monkeypatch, tmp_path: Path, project_root: Path
):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(store, "TASKS", tasks)
    monkeypatch.setattr(engine, "TASKS", tasks)

    def offline_llm(*_args, **_kwargs):
        raise RuntimeError("pytest offline")

    monkeypatch.setattr(intent_parser, "_call_llm", offline_llm)
    client = TestClient(app_module.app)
    source = project_root / "input" / "TD50-40G.dwg"
    with source.open("rb") as stream:
        uploaded = client.post(
            "/api/files",
            files={"file": ("TD50-40G.dwg", stream, "application/octet-stream")},
        )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["file"]["file_id"]

    response = client.post(
        "/api/tasks",
        json={
            "file_id": file_id,
            "instruction": "风罩高度443，接线盒伸出294，轴伸加20",
        },
    )
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["files"]["reference"] is None
    assert task["files"]["source"]["file_id"] == file_id
    assert task["status"] == "planned"
    assert task["profile_id"] == "td50-40g"
    assert task["intent"]["enabled"] == ["H2", "D2", "shaft"]
    assert (task["plan"]["known_rules"]) is True

    blocked = client.post(
        "/api/tasks",
        json={"file_id": file_id, "instruction": "按参考图改图"},
    )
    assert blocked.status_code == 200
    blocked_task = blocked.json()["task"]
    assert blocked_task["files"]["reference"] is None
    assert blocked_task["status"] == "needs_instruction"
    assert blocked_task["plan"]["known_rules"] is False
    assert blocked_task["plan"]["instruction_required"]["questions"]

    approve = client.post(
        f"/api/tasks/{blocked_task['task_id']}/approve", json={}
    )
    assert approve.status_code == 400
    execute = client.post(f"/api/tasks/{blocked_task['task_id']}/execute")
    assert execute.status_code == 409


def test_api_smoke():
    client = TestClient(app_module.app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["service"] == "ai-cad-workbench"

    env = client.get("/api/env")
    assert env.status_code == 200
    assert env.json()["ok"] is True

    profiles = client.get("/api/profiles")
    assert profiles.status_code == 200
    payload = profiles.json()
    assert payload["ok"] is True
    ids = {item["profile_id"] for item in payload["profiles"]}
    assert "td50-40g" in ids

    detail = client.get("/api/profiles/td50-40g")
    assert detail.status_code == 200
    assert detail.json()["profile"]["profile_id"] == "td50-40g"

    missing = client.get("/api/profiles/not-a-profile")
    assert missing.status_code == 404

    draft = {
        "profile_id": "unit-test-draft",
        "model": "TEST",
        "title": "未定标草稿",
        "status": "draft",
        "review": {"status": "draft"},
        "fingerprint": {},
        "calibration": {},
        "datums": {},
        "original": {},
        "rules": {},
    }
    blocked = client.post("/api/profiles", json=draft)
    assert blocked.status_code == 400

    learning_page = client.get("/learning")
    assert learning_page.status_code == 200
    assert "自改进 Loop" in learning_page.text

    learning_status = client.get("/api/learn/status")
    assert learning_status.status_code == 200
    assert learning_status.json()["ok"] is True

    learning_cases = client.get("/api/learn/cases")
    assert learning_cases.status_code == 200
    assert learning_cases.json()["ok"] is True

    learning_proposals = client.get("/api/learn/proposals")
    assert learning_proposals.status_code == 200
    assert learning_proposals.json()["ok"] is True


def test_learning_ledger_and_seal_api_use_isolated_root(
    tmp_path: Path, monkeypatch
):
    learning_root = tmp_path / "learning"
    monkeypatch.setattr(learning_loop, "LEARNING_ROOT", learning_root)
    client = TestClient(app_module.app)

    ledger = client.get("/api/learn/ledger?limit=5")
    assert ledger.status_code == 200
    payload = ledger.json()
    assert payload["ok"] is True
    assert payload["ledger"]["verification"]["ok"] is True
    assert Path(payload["ledger"]["database"]) == learning_root / "ledger.db"

    sealed = client.post("/api/learn/cases/seal")
    assert sealed.status_code == 200
    result = sealed.json()
    assert result["ok"] is True
    assert result["sealed_count"] == 0


def test_upload_inspection_grounds_generic_text_intent(
    tmp_path: Path, monkeypatch
):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(store, "TASKS", tasks)
    app_module._INSPECT_CACHE.clear()

    source = tmp_path / "material.dxf"
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("材料：Q235", dxfattribs={"height": 2.5})
    doc.modelspace().add_line((0, 0), (100, 0))
    doc.saveas(source)
    record = store.save_upload(source, "material.dxf")

    client = TestClient(app_module.app)
    inspection = client.get(f"/api/files/{record['file_id']}/inspect")
    assert inspection.status_code == 200
    payload = inspection.json()
    assert payload["ok"] is True
    assert payload["inspection"]["editable_count"] == 1
    assert payload["inspection"]["sample"][0]["text"] == "材料：Q235"
    assert payload["inspection"]["suggestions"][0]["instruction"] == "把材料改成"

    parsed = client.post(
        "/api/parse-intent",
        json={
            "file_id": record["file_id"],
            "instruction": "把Q235改成304",
            "use_llm": False,
        },
    )
    assert parsed.status_code == 200
    intent = parsed.json()["intent"]
    assert intent["task_type"] == "text_edit"
    assert len(intent["edits"]) == 1
    assert intent["edits"][0]["expected_text"] == "材料：304"

    field_parsed = client.post(
        "/api/parse-intent",
        json={
            "file_id": record["file_id"],
            "instruction": "把材料改成304",
            "use_llm": False,
        },
    )
    assert field_parsed.status_code == 200
    field_edit = field_parsed.json()["intent"]["edits"][0]
    assert field_edit["expected_text"] == "材料：304"
    assert field_edit["field"] == "材料"


def test_api_dimension_edit_needs_explicit_label_semantics(
    tmp_path: Path, monkeypatch
):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(store, "TASKS", tasks)
    monkeypatch.setattr(engine, "TASKS", tasks)
    app_module._INSPECT_CACHE.clear()

    source = tmp_path / "dimension.dxf"
    doc = ezdxf.new("R2018")
    block = doc.blocks.new("GS_BlockDim_100")
    block.add_text("610")
    doc.modelspace().add_blockref("GS_BlockDim_100", (0, 0))
    doc.modelspace().add_line((0, 0), (610, 0))
    doc.saveas(source)
    record = store.save_upload(source, "dimension.dxf")

    client = TestClient(app_module.app)
    inspection = client.get(f"/api/files/{record['file_id']}/inspect")
    assert inspection.status_code == 200
    inspection_data = inspection.json()["inspection"]
    assert inspection_data["dimension_count"] == 1
    assert inspection_data["sample"][0]["is_dimension"] is True
    assert "只改标注文字" in inspection_data["suggestions"][0]["hint"]

    parsed = client.post(
        "/api/parse-intent",
        json={
            "file_id": record["file_id"],
            "instruction": "把610改成620",
            "use_llm": False,
        },
    )
    assert parsed.status_code == 200
    intent = parsed.json()["intent"]
    assert intent["edits"] == []
    assert intent["questions"]

    created = client.post(
        "/api/tasks",
        json={
            "file_id": record["file_id"],
            "instruction": "把610改成620",
        },
    )
    assert created.status_code == 200
    task = created.json()["task"]
    assert task["status"] == "needs_instruction"
    assert task["plan"]["operations"] == []

    approve = client.post(f"/api/tasks/{task['task_id']}/approve", json={})
    assert approve.status_code == 400
    assert "尺寸标注" in approve.json()["detail"]
    execute = client.post(f"/api/tasks/{task['task_id']}/execute")
    assert execute.status_code == 409
    assert "尺寸标注" in execute.json()["detail"]


def test_api_handle_disambiguation_plans_single_target(
    tmp_path: Path, monkeypatch
):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(store, "TASKS", tasks)
    monkeypatch.setattr(engine, "TASKS", tasks)
    app_module._INSPECT_CACHE.clear()

    source = tmp_path / "repeated.dxf"
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("610")
    doc.modelspace().add_text("610")
    doc.modelspace().add_line((0, 0), (610, 0))
    doc.saveas(source)
    record = store.save_upload(source, "repeated.dxf")

    client = TestClient(app_module.app)
    inspection = client.get(f"/api/files/{record['file_id']}/inspect")
    handles = [
        row["handle"]
        for row in inspection.json()["inspection"]["sample"]
        if row["text"] == "610"
    ]
    assert len(handles) == 2

    ambiguous = client.post(
        "/api/tasks",
        json={"file_id": record["file_id"], "instruction": "把610改成620"},
    )
    assert ambiguous.status_code == 200
    blocked = ambiguous.json()["task"]
    assert blocked["status"] == "needs_instruction"
    assert blocked["plan"]["operations"] == []
    assert "句柄" in " ".join(
        blocked["plan"]["instruction_required"]["questions"]
    )

    locked = client.post(
        "/api/tasks",
        json={
            "file_id": record["file_id"],
            "instruction": f"把句柄{handles[1]}的610改成620",
        },
    )
    assert locked.status_code == 200
    task = locked.json()["task"]
    assert task["status"] == "planned"
    assert task["task_type"] == "text_edit"
    assert len(task["plan"]["operations"]) == 1
    assert task["plan"]["operations"][0]["target"] == handles[1]
    assert task["intent"]["edits"][0]["expected_text"] == "620"


def test_locate_api_returns_full_and_zoom_pngs(tmp_path: Path, monkeypatch):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(store, "TASKS", tasks)
    app_module._INSPECT_CACHE.clear()
    app_module._LOCATE_CACHE.clear()

    source = tmp_path / "locate.dxf"
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_text("材料：Q235", dxfattribs={"height": 2.5, "insert": (10, 10)})
    msp.add_text("图号 A-001", dxfattribs={"height": 2.5, "insert": (30, 10)})
    msp.add_line((0, 0), (100, 0))
    doc.saveas(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    record = store.save_upload(source, "locate.dxf")

    client = TestClient(app_module.app)
    inspection = client.get(f"/api/files/{record['file_id']}/inspect?limit=20")
    assert inspection.status_code == 200
    handles = [
        item["handle"]
        for item in inspection.json()["inspection"]["items"]
    ]
    assert len(handles) == 2

    located = client.get(
        f"/api/files/{record['file_id']}/locate",
        params={"handles": ",".join(handles)},
    )
    assert located.status_code == 200
    payload = located.json()
    assert payload["count"] == 2
    assert payload["missing"] == []
    assert all(item["boxes"] for item in payload["items"])
    assert payload["full_url"] != payload["zoom_url"]
    assert payload["full_svg_url"] != payload["zoom_svg_url"]

    for url in (payload["full_url"], payload["zoom_url"]):
        image = client.get(url)
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(image.content) > 1000

    for url in (payload["full_svg_url"], payload["zoom_svg_url"]):
        vector = client.get(url)
        assert vector.status_code == 200
        assert vector.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in vector.content[:500]
        assert len(vector.content) > 1000

    after = hashlib.sha256(source.read_bytes()).hexdigest()
    assert after == before


def test_task_svg_is_generated_on_demand_without_touching_result(
    tmp_path: Path, monkeypatch
):
    tasks = tmp_path / "tasks"
    task_dir = tasks / "vector-task"
    task_dir.mkdir(parents=True)
    result = task_dir / "result.dxf"
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text(
        "总长 610", dxfattribs={"height": 2.5, "insert": (10, 10)}
    )
    doc.modelspace().add_line((0, 0), (610, 0))
    doc.saveas(result)
    before = hashlib.sha256(result.read_bytes()).hexdigest()

    monkeypatch.setattr(store, "TASKS", tasks)
    monkeypatch.setattr(engine, "TASKS", tasks)
    monkeypatch.setattr(app_module, "TASKS", tasks)
    client = TestClient(app_module.app)
    response = client.get(
        "/api/tasks/vector-task/download", params={"kind": "after.svg"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content[:500]
    assert (task_dir / "after.svg").exists()
    assert hashlib.sha256(result.read_bytes()).hexdigest() == before
