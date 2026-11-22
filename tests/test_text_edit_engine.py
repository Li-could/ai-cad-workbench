from __future__ import annotations

import hashlib
from pathlib import Path

import ezdxf
import pytest

import edit_intent
import engine as workbench_engine
import store
import text_edit_engine


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_text_drawing(path: Path) -> Path:
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_text("材料：Q235", dxfattribs={"height": 2.5})
    msp.add_text("图号 A-001", dxfattribs={"height": 2.5})
    msp.add_line((0, 0), (100, 0))
    msp.add_circle((50, 20), radius=8)
    title = doc.blocks.new("TITLE")
    title.add_text("公司名称", dxfattribs={"height": 2.5})
    msp.add_blockref("TITLE", (0, 30))
    doc.saveas(str(path))
    return path


def test_inventory_matches_text_and_supports_substring_replacement(tmp_path: Path):
    source = _make_text_drawing(tmp_path / "text.dxf")
    doc = ezdxf.readfile(source)
    inventory = text_edit_engine.inspect_doc(doc)

    texts = {row["text"] for row in inventory["items"]}
    assert {"材料：Q235", "图号 A-001", "公司名称"} <= texts
    assert inventory["scopes"]["block"] == 1

    intent = edit_intent.parse_text_edit_intent("把材料里的Q235改成304", inventory)
    assert intent["requested"] is True
    assert len(intent["edits"]) == 1
    edit = intent["edits"][0]
    assert edit["old"] == "Q235"
    assert edit["expected_text"] == "材料：304"

    result = text_edit_engine.apply_edits(source, tmp_path / "result.dxf", intent["edits"])
    assert result["ok"] is True
    assert result["qa"]["only_targets_changed"] is True
    assert result["stat"]["geometry_identical"] is True

    changed = ezdxf.readfile(tmp_path / "result.dxf")
    values = {
        text_edit_engine.entity_text(entity)
        for entity in changed.modelspace()
        if entity.dxftype() == "TEXT"
    }
    assert "材料：304" in values
    assert "图号 A-001" in values


def test_ambiguous_numeric_target_requires_confirmation(tmp_path: Path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_text("610")
    msp.add_text("610")
    source = tmp_path / "ambiguous.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    intent = edit_intent.parse_text_edit_intent("把610改成620", inventory)
    assert intent["edits"] == []
    assert len(intent["ambiguous"]) == 1
    assert len(intent["ambiguous"][0]["candidates"]) == 2

    replace_all = edit_intent.parse_text_edit_intent("把全部610改成620", inventory)
    assert len(replace_all["edits"]) == 2
    assert all(row["expected_text"] == "620" for row in replace_all["edits"])


def test_ambiguous_numeric_target_can_be_locked_by_handle(tmp_path: Path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("610")
    doc.modelspace().add_text("610")
    source = tmp_path / "handle-lock.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    handles = [
        row["handle"] for row in inventory["items"] if row["text"] == "610"
    ]
    assert len(handles) == 2

    intent = edit_intent.parse_text_edit_intent(
        f"把句柄{handles[1]}的610改成620", inventory
    )
    assert len(intent["edits"]) == 1
    assert intent["edits"][0]["handle"] == handles[1]
    assert intent["edits"][0]["expected_text"] == "620"

    handle_only = edit_intent.parse_text_edit_intent(
        f"把句柄{handles[0]}改成621", inventory
    )
    assert len(handle_only["edits"]) == 1
    assert handle_only["edits"][0]["handle"] == handles[0]
    assert handle_only["edits"][0]["expected_text"] == "621"


def test_ordinal_can_disambiguate_repeated_text(tmp_path: Path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("610")
    doc.modelspace().add_text("610")
    source = tmp_path / "ordinal.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    second = inventory["items"][1]["handle"]
    intent = edit_intent.parse_text_edit_intent("把第2个610改成620", inventory)
    assert len(intent["edits"]) == 1
    assert intent["edits"][0]["handle"] == second
    assert intent["edits"][0]["expected_text"] == "620"

    missing = edit_intent.parse_text_edit_intent("把第9个610改成620", inventory)
    assert missing["edits"] == []
    assert any("没有第 9 个" in question for question in missing["questions"])


def test_layer_qualifier_filters_same_text(tmp_path: Path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("610", dxfattribs={"layer": "A层"})
    doc.modelspace().add_text("610", dxfattribs={"layer": "B层"})
    source = tmp_path / "layer-filter.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    intent = edit_intent.parse_text_edit_intent(
        "把图层B层里的610改成620", inventory
    )
    assert len(intent["edits"]) == 1
    assert intent["edits"][0]["layer"] == "B层"
    assert intent["edits"][0]["expected_text"] == "620"


def test_field_only_instruction_replaces_value_not_field_label(tmp_path: Path):
    source = _make_text_drawing(tmp_path / "fields.dxf")
    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))

    material = edit_intent.parse_text_edit_intent(
        "把材料改成304", inventory
    )
    assert len(material["edits"]) == 1
    assert material["edits"][0]["field"] == "材料"
    assert material["edits"][0]["expected_text"] == "材料：304"

    drawing_no = edit_intent.parse_text_edit_intent(
        "把图号改成A-002", inventory
    )
    assert len(drawing_no["edits"]) == 1
    assert drawing_no["edits"][0]["expected_text"] == "图号 A-002"

    both = edit_intent.parse_text_edit_intent(
        "把材料改成304，把图号改成A-002", inventory
    )
    assert len(both["edits"]) == 2
    assert {row["expected_text"] for row in both["edits"]} == {
        "材料：304",
        "图号 A-002",
    }

    result = text_edit_engine.apply_edits(
        source, tmp_path / "fields-result.dxf", material["edits"]
    )
    assert result["ok"] is True
    changed = ezdxf.readfile(tmp_path / "fields-result.dxf")
    values = {
        text_edit_engine.entity_text(entity)
        for entity in changed.modelspace()
        if entity.dxftype() == "TEXT"
    }
    assert "材料：304" in values
    assert "304：Q235" not in values

    no_change = edit_intent.parse_text_edit_intent(
        "把材料改成Q235", inventory
    )
    assert no_change["edits"] == []
    assert any("没有可执行的修改" in question for question in no_change["questions"])


def test_title_block_attrib_field_matches_hidden_tag(tmp_path: Path):
    doc = ezdxf.new("R2018")
    block = doc.blocks.new("TITLE")
    block.add_attdef("图号", (0, 0), dxfattribs={"height": 2.5})
    block.add_attdef("REV", (0, 5), dxfattribs={"height": 2.5})
    insert = doc.modelspace().add_blockref("TITLE", (0, 0))
    insert.add_auto_attribs({"图号": "A-001", "REV": "A"})
    doc.modelspace().add_line((0, 0), (100, 0))
    source = tmp_path / "attributes.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    attribute = inventory["items"][0]
    assert attribute["kind"] == "ATTRIB"
    assert attribute["tag"] == "图号"
    assert (
        edit_intent.suggest_instruction(attribute)["instruction"]
        == "把图号改成"
    )

    intent = edit_intent.parse_text_edit_intent(
        "把图号改成A-002", inventory
    )
    assert len(intent["edits"]) == 1
    assert intent["edits"][0]["expected_text"] == "A-002"

    revision = edit_intent.parse_text_edit_intent(
        "把REV改成B", inventory
    )
    assert len(revision["edits"]) == 1
    assert revision["edits"][0]["field"] == "REV"
    assert revision["edits"][0]["expected_text"] == "B"

    result = text_edit_engine.apply_edits(
        source, tmp_path / "attributes-result.dxf", intent["edits"]
    )
    assert result["ok"] is True
    changed = ezdxf.readfile(tmp_path / "attributes-result.dxf")
    insert = next(
        entity for entity in changed.modelspace() if entity.dxftype() == "INSERT"
    )
    attributes = {row.dxf.tag: row.dxf.text for row in insert.attribs}
    assert attributes["图号"] == "A-002"
    assert attributes["REV"] == "A"


def test_field_value_without_separator_is_replaced_as_value(tmp_path: Path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("材料Q235", dxfattribs={"height": 2.5})
    source = tmp_path / "compact-field.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    intent = edit_intent.parse_text_edit_intent("把材料改成304", inventory)
    assert len(intent["edits"]) == 1
    edit = intent["edits"][0]
    assert edit["field"] == "材料"
    assert edit["old"] == "Q235"
    assert edit["new"] == "304"
    assert edit["expected_text"] == "材料304"

    result = text_edit_engine.apply_edits(
        source, tmp_path / "compact-field-result.dxf", intent["edits"]
    )
    assert result["ok"] is True
    changed = ezdxf.readfile(tmp_path / "compact-field-result.dxf")
    values = {
        text_edit_engine.entity_text(entity)
        for entity in changed.modelspace()
        if entity.dxftype() == "TEXT"
    }
    assert "材料304" in values


def test_generic_intent_keeps_specialized_geometry_chain():
    inventory = {
        "editable_count": 1,
        "items": [
            {"handle": "A1", "kind": "TEXT", "text": "H2 443", "scope": "modelspace"}
        ],
    }
    generic = edit_intent.parse_with_drawing("把Q235改成304", inventory, use_llm=False)
    assert generic["task_type"] == "text_edit"
    assert generic["requested"] is True

    specialized = edit_intent.parse_with_drawing("风罩高度443", inventory, use_llm=False)
    assert specialized["enabled"] == ["H2"]
    assert specialized["targets"]["H2"] == 443.0


def test_workbench_plans_and_executes_text_edit_dxf(
    tmp_path: Path, monkeypatch
):
    tasks = tmp_path / "tasks"
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(store, "TASKS", tasks)
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(workbench_engine, "TASKS", tasks)

    source = _make_text_drawing(tmp_path / "source.dxf")
    record = {
        "file_id": "testfile",
        "name": source.name,
        "path": str(source),
        "size": source.stat().st_size,
        "sha256": _sha(source),
    }
    task = store.create_task("把Q235改成304", record, None)
    workbench_engine.parse_and_plan(task, use_llm=False)

    assert task["task_type"] == "text_edit"
    assert task["status"] == "planned"
    assert task["plan"]["requested_rules"] == ["TEXT_EDIT"]
    assert task["parse"]["editable_text"]["count"] >= 3

    monkeypatch.setattr(workbench_engine, "_render_png", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        workbench_engine,
        "_roundtrip",
        lambda *args, **kwargs: {"ok": True, "passed": True},
    )
    import dwg_export

    def fake_export(dxf, dwg, version="AC1032"):
        Path(dwg).write_bytes(b"AC1032-test")
        return {"ok": True, "header": "AC1032"}

    monkeypatch.setattr(dwg_export, "export", fake_export)
    report = workbench_engine.execute(task)

    assert report["ok"] is True
    assert report["checks"]["text_verified"]["passed"] is True
    changed = ezdxf.readfile(tasks / task["task_id"] / "result.dxf")
    values = {
        text_edit_engine.entity_text(entity)
        for entity in changed.modelspace()
        if entity.dxftype() == "TEXT"
    }
    assert "材料：304" in values


def _make_guarded_drawing(path: Path) -> Path:
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    dimension = doc.blocks.new("GS_BlockDim_100")
    dimension.add_text("610")
    msp.add_blockref("GS_BlockDim_100", (0, 0))
    part_no = doc.blocks.new("PART_NO")
    part_no.add_text("A-001")
    msp.add_blockref("PART_NO", (0, 20))
    msp.add_blockref("PART_NO", (0, 40))
    msp.add_line((0, 0), (610, 0))
    doc.saveas(str(path))
    return path


def test_dimension_number_requires_explicit_label_only_policy(tmp_path: Path):
    source = _make_guarded_drawing(tmp_path / "guarded.dxf")
    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    dimension = next(row for row in inventory["items"] if row["text"] == "610")
    assert dimension["is_dimension"] is True
    assert dimension["dimension_source"] == "block"

    intent = edit_intent.parse_text_edit_intent("把610改成620", inventory)
    assert intent["edits"] == []
    assert intent["blocked"]
    assert any("尺寸标注" in question for question in intent["questions"])

    unsafe = {
        "handle": dimension["handle"],
        "old": "610",
        "new": "620",
        "mode": "exact",
    }
    with pytest.raises(ValueError, match="尺寸标注"):
        text_edit_engine.apply_edits(
            source, tmp_path / "unsafe.dxf", [unsafe]
        )

    label_only = edit_intent.parse_text_edit_intent(
        "把610改成620，只改标注文字", inventory
    )
    assert len(label_only["edits"]) == 1
    assert label_only["edits"][0]["dimension_effect"] == "label_only"
    result = text_edit_engine.apply_edits(
        source, tmp_path / "label-only.dxf", label_only["edits"]
    )
    assert result["ok"] is True
    assert result["qa"]["dimension_labels_only"] is True
    assert result["stat"]["geometry_identical"] is True


def test_numeric_text_on_dimension_layer_is_not_silently_edited(tmp_path: Path):
    doc = ezdxf.new("R2018")
    doc.layers.add("尺寸线层")
    msp = doc.modelspace()
    msp.add_text("255", dxfattribs={"layer": "尺寸线层"})
    msp.add_line((0, 0), (100, 0))
    source = tmp_path / "layer-dimension.dxf"
    doc.saveas(source)

    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    item = inventory["items"][0]
    assert item["is_dimension"] is True
    assert item["dimension_source"] == "layer"

    intent = edit_intent.parse_text_edit_intent("把255改成256", inventory)
    assert intent["edits"] == []
    assert any("尺寸标注" in question for question in intent["questions"])


def test_shared_block_text_requires_explicit_all_instances(tmp_path: Path):
    source = _make_guarded_drawing(tmp_path / "shared.dxf")
    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    shared = next(row for row in inventory["items"] if row["text"] == "A-001")
    assert shared["shared"] is True
    assert shared["insert_count"] == 2

    intent = edit_intent.parse_text_edit_intent("把A-001改成A-002", inventory)
    assert intent["edits"] == []
    assert any("共享块" in question for question in intent["questions"])

    unsafe = {
        "handle": shared["handle"],
        "old": "A-001",
        "new": "A-002",
        "mode": "exact",
    }
    with pytest.raises(ValueError, match="共享块"):
        text_edit_engine.apply_edits(
            source, tmp_path / "unsafe-shared.dxf", [unsafe]
        )

    replace_all = edit_intent.parse_text_edit_intent(
        "把全部A-001改成A-002", inventory
    )
    assert len(replace_all["edits"]) == 1
    assert replace_all["edits"][0]["shared_effect"] == "all_instances"
    result = text_edit_engine.apply_edits(
        source, tmp_path / "all-shared.dxf", replace_all["edits"]
    )
    assert result["ok"] is True
    assert result["qa"]["shared_blocks_explicit"] is True


def test_batch_handle_instruction_keeps_per_item_semantics(tmp_path: Path):
    source = _make_guarded_drawing(tmp_path / "batch-guarded.dxf")
    inventory = text_edit_engine.inspect_doc(ezdxf.readfile(source))
    dimension = next(row for row in inventory["items"] if row["text"] == "610")
    shared = next(row for row in inventory["items"] if row["text"] == "A-001")
    instruction = (
        f"把句柄{dimension['handle']}改成“620”，只改标注文字；"
        f"把句柄{shared['handle']}改成A-002，全部修改"
    )

    intent = edit_intent.parse_text_edit_intent(instruction, inventory)
    assert intent["questions"] == []
    assert len(intent["edits"]) == 2
    by_handle = {edit["handle"]: edit for edit in intent["edits"]}
    dimension_edit = by_handle[dimension["handle"]]
    shared_edit = by_handle[shared["handle"]]
    assert dimension_edit["expected_text"] == "620"
    assert dimension_edit["dimension_effect"] == "label_only"
    assert shared_edit["expected_text"] == "A-002"
    assert shared_edit["shared_effect"] == "all_instances"

    result = text_edit_engine.apply_edits(
        source,
        tmp_path / "batch-result.dxf",
        intent["edits"],
    )
    assert result["ok"] is True
    assert result["qa"]["dimension_labels_only"] is True
    assert result["qa"]["shared_blocks_explicit"] is True
    assert result["stat"]["geometry_identical"] is True


def test_workbench_dimension_plan_waits_for_semantic_choice(
    tmp_path: Path, monkeypatch
):
    tasks = tmp_path / "tasks"
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(store, "TASKS", tasks)
    monkeypatch.setattr(store, "UPLOADS", uploads)
    monkeypatch.setattr(workbench_engine, "TASKS", tasks)

    source = _make_guarded_drawing(tmp_path / "source.dxf")
    record = {
        "file_id": "guarded",
        "name": source.name,
        "path": str(source),
        "size": source.stat().st_size,
        "sha256": _sha(source),
    }
    blocked = store.create_task("把610改成620", record, None)
    workbench_engine.parse_and_plan(blocked, use_llm=False)
    assert blocked["status"] == "needs_instruction"
    assert blocked["plan"]["operations"] == []
    assert blocked["plan"]["instruction_required"]["questions"]

    planned = store.create_task("把610改成620，只改标注文字", record, None)
    workbench_engine.parse_and_plan(planned, use_llm=False)
    assert planned["status"] == "planned"
    assert planned["plan"]["operations"][0]["dimension_effect"] == "label_only"
    assert "仅改标注显示" in planned["plan"]["operations"][0]["action"]
