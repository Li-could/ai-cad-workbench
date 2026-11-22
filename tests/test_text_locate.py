from __future__ import annotations

from pathlib import Path

import ezdxf

import text_edit_engine
import text_locate


def test_scan_keeps_insert_handle_and_locate_returns_all_instances(
    tmp_path: Path,
):
    doc = ezdxf.new("R2018")
    block = doc.blocks.new("TITLE")
    block.add_text("A-001", dxfattribs={"height": 2.5, "insert": (5, 5)})
    msp = doc.modelspace()
    first = msp.add_blockref("TITLE", (10, 10))
    second = msp.add_blockref("TITLE", (210, 10))
    msp.add_line((0, 0), (300, 0))
    source = tmp_path / "shared-block.dxf"
    doc.saveas(source)

    doc = ezdxf.readfile(source)
    inventory = text_edit_engine.inspect_doc(doc)
    row = next(item for item in inventory["items"] if item["text"] == "A-001")
    assert row["insert_handle"] == str(first.dxf.handle)
    assert row["insert_count"] == 2

    located = text_locate.locate_handles(doc, [row["handle"]], inventory)
    assert located["ok"] is True
    assert located["missing"] == []
    item = located["items"][0]
    assert item["instances"] == 2
    assert len(item["boxes"]) == 2
    assert abs((item["boxes"][1][0] - item["boxes"][0][0]) - 200) < 1e-6
    assert item["handle"] == row["handle"]


def test_locate_composes_nested_block_transforms(tmp_path: Path):
    doc = ezdxf.new("R2018")
    part = doc.blocks.new("PART")
    part.add_text("610", dxfattribs={"height": 2.5, "insert": (1, 2)})
    wrapper = doc.blocks.new("WRAPPER")
    wrapper.add_blockref("PART", (10, 20))
    msp = doc.modelspace()
    msp.add_blockref("WRAPPER", (100, 50))
    msp.add_blockref("WRAPPER", (300, 50))
    source = tmp_path / "nested-block.dxf"
    doc.saveas(source)

    doc = ezdxf.readfile(source)
    inventory = text_edit_engine.inspect_doc(doc)
    row = next(item for item in inventory["items"] if item["text"] == "610")
    located = text_locate.locate_handles(doc, [row["handle"]], inventory)

    item = located["items"][0]
    assert item["instances"] == 2
    first_box, second_box = item["boxes"]
    assert first_box[0] > 100
    assert first_box[1] > 50
    assert second_box[0] > 300
    assert abs((second_box[0] - first_box[0]) - 200) < 1e-6
    assert all("INSERT" in path for path in item["paths"])


def test_locate_reports_missing_handle_without_raising(tmp_path: Path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("610")
    source = tmp_path / "missing-handle.dxf"
    doc.saveas(source)

    located = text_locate.locate_file(source, ["ZZZZZZ"])
    assert located["ok"] is True
    assert located["missing"] == ["ZZZZZZ"]
    assert located["items"] == []
    assert any("ZZZZZZ" in warning for warning in located["warnings"])
