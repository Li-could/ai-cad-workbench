from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

import calibration
import drawing_measure
import drawing_profile
import geometry_modifier


def _snapshot(entity):
    dxftype = entity.dxftype()
    if dxftype == "LINE":
        return (dxftype, entity.dxf.start.x, entity.dxf.start.y,
                entity.dxf.end.x, entity.dxf.end.y)
    if dxftype == "CIRCLE":
        return (dxftype, entity.dxf.center.x, entity.dxf.center.y,
                entity.dxf.radius)
    if dxftype == "ARC":
        return (dxftype, entity.dxf.center.x, entity.dxf.center.y,
                entity.dxf.radius, entity.dxf.start_angle, entity.dxf.end_angle)
    if dxftype in ("TEXT", "MTEXT"):
        return (dxftype, entity.dxf.insert.x, entity.dxf.insert.y,
                str(entity.dxf.text))
    if dxftype == "POINT":
        return (dxftype, entity.dxf.location.x, entity.dxf.location.y)
    return (dxftype,)


def _same(a, b, tol: float = 1e-6) -> bool:
    if len(a) != len(b):
        return False
    for left, right in zip(a[1:], b[1:]):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if abs(float(left) - float(right)) > tol:
                return False
        elif left != right:
            return False
    return True


def test_td50_profile_resolves(golden_task: Path):
    doc = ezdxf.readfile(str(golden_task / "clean.dxf"))
    profile, report = drawing_profile.resolve_for(doc, source_name="TD50-40G.dwg")
    assert profile is not None
    assert profile.profile_id == "td50-40g"
    assert report["ok"] is True
    assert all(check["ok"] for check in report["checks"] if check["hard"])
    assert calibration.readiness_problems(profile.data) == []


def test_unknown_drawing_is_rejected_for_geometry():
    doc = ezdxf.new("R2018")
    doc.modelspace().add_line((0, 0), (100, 0))
    profile, report = drawing_profile.resolve_for(doc)
    assert profile is None
    assert report["ok"] is False
    with pytest.raises(drawing_profile.ProfileMismatch):
        drawing_profile.get_profile("td50-40g").require(doc)


def test_new_engine_matches_golden_entities(golden_task: Path, tmp_path: Path):
    profile = drawing_profile.get_profile("td50-40g")
    output = tmp_path / "result.dxf"
    report = geometry_modifier.apply(
        golden_task / "clean.dxf",
        output,
        targets={"H2": 443, "D2": 294, "shaft": 20},
        enabled={"H2", "D2", "shaft"},
        profile=profile,
    )
    assert len(report["changes"]) == 85

    actual = ezdxf.readfile(str(output))
    golden = ezdxf.readfile(str(golden_task / "result.dxf"))
    actual_entities = {entity.dxf.handle: entity for entity in actual.modelspace()}
    golden_entities = {entity.dxf.handle: entity for entity in golden.modelspace()}

    assert set(actual_entities) == set(golden_entities)
    diffs = [
        handle for handle in actual_entities
        if not _same(_snapshot(actual_entities[handle]),
                     _snapshot(golden_entities[handle]))
    ]
    assert diffs == []

    layers = {layer.dxf.name for layer in actual.layers}
    assert not any(name.startswith("变更_") for name in layers), \
        "交付 DXF 不应包含彩色对照标注图层"


def test_independent_measurement_passes(golden_task: Path, tmp_path: Path):
    profile = drawing_profile.get_profile("td50-40g")
    output = tmp_path / "measured.dxf"
    geometry_modifier.apply(
        golden_task / "clean.dxf",
        output,
        targets={"H2": 443, "D2": 294, "shaft": 20},
        enabled={"H2", "D2", "shaft"},
        profile=profile,
    )
    measured = drawing_measure.measure(ezdxf.readfile(str(output)), profile)
    verdict = drawing_measure.verify(
        measured, {"H2": 443, "D2": 294, "shaft": 20},
        {"H2", "D2", "shaft"},
    )
    assert verdict["passed"] is True
    assert abs(measured["H2"]["mm"] - 443) <= 0.5
    assert abs(measured["D2"]["mm"] - 294) <= 0.5
    assert abs(measured["shaft"]["extend_mm"] - 20) <= 0.5


def test_modified_drawing_cannot_be_modified_again(golden_task: Path, tmp_path: Path):
    profile = drawing_profile.get_profile("td50-40g")
    first = tmp_path / "first.dxf"
    geometry_modifier.apply(
        golden_task / "clean.dxf",
        first,
        targets={"H2": 443, "D2": 294, "shaft": 20},
        enabled={"H2", "D2", "shaft"},
        profile=profile,
    )
    second = tmp_path / "second.dxf"
    with pytest.raises(geometry_modifier.SourceNotBaseline):
        geometry_modifier.apply(
            first,
            second,
            targets={"H2": 443, "D2": 294, "shaft": 20},
            enabled={"H2", "D2", "shaft"},
            profile=profile,
        )
    assert not second.exists()
