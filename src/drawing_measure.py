"""从 DXF 独立复测关键尺寸。

"数值不靠目测"的闭环：改完后重新打开成果文件，按档案定义的基准重新量一遍
H2 / D2 / 轴伸，再与目标值比对。引擎报告只能说明它"想改多少"，这里说明
文件里"实际变成了多少"。
"""
from __future__ import annotations

from typing import Any

import runtime

runtime.bootstrap()

import ezdxf


def entity_bbox(entity) -> tuple[float, float, float, float] | None:
    kind = entity.dxftype()
    try:
        if kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            return (min(start.x, end.x), max(start.x, end.x),
                    min(start.y, end.y), max(start.y, end.y))
        if kind in ("ARC", "CIRCLE"):
            center, radius = entity.dxf.center, float(entity.dxf.radius)
            return (center.x - radius, center.x + radius, center.y - radius, center.y + radius)
        if kind in ("TEXT", "MTEXT", "INSERT", "ATTDEF"):
            point = entity.dxf.insert
            return (point.x, point.x, point.y, point.y)
    except Exception:  # noqa: BLE001
        return None
    return None


def select_h2_region(doc, rule: dict[str, Any], profile) -> list:
    """H2 区域选点，与变形引擎同一套几何条件。"""
    cfg = rule["region"]
    x0, x1 = float(cfg["x"][0]), float(cfg["x"][1])
    excluded = set(cfg.get("exclude_handles", []))
    selected = []
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LINE", "ARC", "CIRCLE"):
            continue
        if entity.dxf.handle in excluded:
            continue
        bb = entity_bbox(entity)
        if bb is None:
            continue
        if bb[1] < x0 or bb[0] > x1:
            continue
        if bb[3] < float(cfg["min_ymax"]):
            continue
        if bb[2] > float(cfg["max_ymin"]):
            continue
        if bb[3] - bb[2] > float(cfg["max_yspan"]):
            continue
        if bb[1] - bb[0] > float(cfg["max_xspan"]):
            continue
        selected.append(entity)
    return selected


def select_d2_region(doc, rule: dict[str, Any], profile) -> list:
    cfg = rule["region"]
    y0, y1 = float(cfg["y"][0]), float(cfg["y"][1])
    x_max, x_min = float(cfg["x_max"]), float(cfg["x_min"])
    excludes = [tuple(float(v) for v in box) for box in cfg.get("exclude_bboxes", [])]
    selected = []
    for entity in doc.modelspace():
        if entity.dxftype() not in ("LINE", "ARC", "CIRCLE"):
            continue
        bb = entity_bbox(entity)
        if bb is None:
            continue
        if bb[3] < y0 or bb[2] > y1:
            continue
        if bb[0] > x_max or bb[1] < x_min:
            continue
        if bb[1] - bb[0] > float(cfg["max_xspan"]):
            continue
        if entity.dxftype() == "LINE":
            cx = (entity.dxf.start.x + entity.dxf.end.x) / 2
            cy = (entity.dxf.start.y + entity.dxf.end.y) / 2
        else:
            cx, cy = entity.dxf.center.x, entity.dxf.center.y
        if any(box[0] <= cx <= box[1] and box[2] <= cy <= box[3] for box in excludes):
            continue
        selected.append(entity)
    return selected


def measure(doc, profile) -> dict[str, Any]:
    """返回当前文件里的几何实测值（mm）。"""
    k_v = profile.units_per_mm("y")
    k_h = profile.units_per_mm("x")
    flange_y = profile.datum("flange_y")
    axis_x = profile.datum("axis_x")

    h2_rule = profile.rule("H2")
    hood_entities = select_h2_region(doc, h2_rule, profile)
    hood_top_y = max((entity_bbox(e)[3] for e in hood_entities), default=None)
    if h2_rule.get("measure_handle"):
        entity = doc.entitydb.get(h2_rule["measure_handle"])
        bb = entity_bbox(entity) if entity is not None else None
        if bb is not None:
            hood_top_y = bb[3]

    d2_rule = profile.rule("D2")
    box_entities = select_d2_region(doc, d2_rule, profile)
    box_outer_x = max((entity_bbox(e)[1] for e in box_entities), default=None)
    if d2_rule.get("measure_handle"):
        entity = doc.entitydb.get(d2_rule["measure_handle"])
        bb = entity_bbox(entity) if entity is not None else None
        if bb is not None:
            box_outer_x = bb[1]

    shaft_rule = profile.rule("shaft")
    bottom_y = None
    for entity in doc.modelspace():
        if entity.dxf.handle == shaft_rule["bottom_handle"] and entity.dxftype() == "LINE":
            bottom_y = max(entity.dxf.start.y, entity.dxf.end.y)
            break

    texts = []
    for entity in doc.modelspace():
        if entity.dxftype() in ("TEXT", "MTEXT"):
            texts.append(str(getattr(entity.dxf, "text", "")))

    h2_mm = (hood_top_y - flange_y) / k_v if hood_top_y is not None else None
    d2_mm = (box_outer_x - axis_x) / k_h if box_outer_x is not None else None
    shaft_ext = (float(profile.datum("shaft_bottom_y")) - bottom_y) / k_v if bottom_y is not None else None

    return {
        "H2": {
            "top_y": round(hood_top_y, 3) if hood_top_y is not None else None,
            "flange_y": flange_y,
            "mm": round(h2_mm, 2) if h2_mm is not None else None,
            "selected_entities": len(hood_entities),
        },
        "D2": {
            "outer_x": round(box_outer_x, 3) if box_outer_x is not None else None,
            "axis_x": axis_x,
            "mm": round(d2_mm, 2) if d2_mm is not None else None,
            "selected_entities": len(box_entities),
        },
        "shaft": {
            "bottom_y": round(bottom_y, 3) if bottom_y is not None else None,
            "extend_mm": round(shaft_ext, 2) if shaft_ext is not None else None,
        },
        "texts": texts,
    }


def verify(measured: dict[str, Any], targets: dict[str, Any], enabled: set[str],
           tolerance_mm: float = 0.5) -> dict[str, Any]:
    """把实测值与目标值比对；只检查本次启用的规则。"""
    checks = []
    if "H2" in enabled and targets.get("H2") is not None:
        actual = measured["H2"]["mm"]
        expected = float(targets["H2"])
        ok = actual is not None and abs(actual - expected) <= tolerance_mm
        checks.append({"rule": "H2", "expected": expected, "actual": actual,
                       "tolerance_mm": tolerance_mm, "passed": ok})
    if "D2" in enabled and targets.get("D2") is not None:
        actual = measured["D2"]["mm"]
        expected = float(targets["D2"])
        ok = actual is not None and abs(actual - expected) <= tolerance_mm
        checks.append({"rule": "D2", "expected": expected, "actual": actual,
                       "tolerance_mm": tolerance_mm, "passed": ok})
    if "shaft" in enabled and targets.get("shaft") is not None:
        actual = measured["shaft"]["extend_mm"]
        expected = float(targets["shaft"])
        ok = actual is not None and abs(actual - expected) <= tolerance_mm
        checks.append({"rule": "shaft", "expected": expected, "actual": actual,
                       "tolerance_mm": tolerance_mm, "passed": ok})
    return {
        "passed": all(check["passed"] for check in checks) if checks else True,
        "checks": checks,
    }


if __name__ == "__main__":
    import json
    import sys

    from drawing_profile import resolve_for

    path = sys.argv[1]
    doc = ezdxf.readfile(path)
    profile, report = resolve_for(doc)
    if profile is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    print(json.dumps(measure(doc, profile), ensure_ascii=False, indent=2))
