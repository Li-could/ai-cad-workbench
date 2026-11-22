"""Real-millimeter 1:1 calibration for multi-view mechanical drawings.

The engine starts from a cleaned DXF baseline and performs three deterministic
steps:

1. Align the second view's total-height dimension to the reference view.
2. Apply independent X/Y calibration so the reference length and height become
   the requested real millimeter values.
3. Rewrite every length annotation from its measured geometry.

The source file is never modified. Output geometry, annotations, audit status,
and every applied calibration factor are returned as a report.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import runtime

runtime.bootstrap()

import ezdxf
import ezdxf.transform
from ezdxf import bbox
from ezdxf.math import Matrix44, Vec2

import annotation_engine as ae

SPEC_TEXTS = ae.SPEC_TEXTS


def _line_angle(line) -> float:
    delta = Vec2(line.dxf.end) - Vec2(line.dxf.start)
    return math.degrees(math.atan2(delta.y, delta.x)) % 180.0


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def measure_block(block) -> tuple[str, float]:
    texts = [entity for entity in block if entity.dxftype() == "TEXT"]
    if not texts:
        mtexts = [entity for entity in block if entity.dxftype() == "MTEXT"]
        if not mtexts:
            return "", 0.0
        old = mtexts[0].text
    else:
        old = texts[0].dxf.text

    lines = [entity for entity in block if entity.dxftype() == "LINE"]
    if len(lines) == 3:
        angles = [_line_angle(line) for line in lines]
        for index, angle in enumerate(angles):
            other = [angles[i] for i in range(3) if i != index]
            if all(_angle_diff(angle, value) > 1.0 for value in other):
                delta = Vec2(lines[index].dxf.end) - Vec2(lines[index].dxf.start)
                return old, delta.magnitude

    hspan, vspan = ae.block_span(block)
    rotation = texts[0].dxf.get("rotation", 0) if texts else 0
    measured = vspan if abs(rotation - 90) < 1 else hspan
    return old, measured if measured > 0 else max(hspan, vspan)


def measure_all(doc) -> list[dict]:
    rows = []
    for block in doc.blocks:
        if not block.name.startswith("GS_BlockDim"):
            continue
        old, measured = measure_block(block)
        if not old or measured <= 0 or old in SPEC_TEXTS:
            continue
        rows.append(
            {
                "kind": "BLOCK",
                "where": block.name,
                "old": old,
                "measured": round(measured, 6),
            }
        )
    for dimension in doc.modelspace().query("DIMENSION"):
        old = dimension.dxf.text
        if not old or old in SPEC_TEXTS:
            continue
        measured = float(dimension.get_measurement())
        rows.append(
            {
                "kind": "DIM",
                "where": dimension.dxf.handle,
                "old": old,
                "measured": round(measured, 6),
                "geometry_block": dimension.dxf.get("geometry", ""),
            }
        )
    return rows


def _find_anchor(rows: list[dict], value: float | str, where: str | None) -> dict:
    anchor_text = str(value)
    for row in rows:
        if where and (where == row["where"] or where in row["where"]):
            return row
        if not where and row["old"] == anchor_text:
            return row
    raise ValueError(f"找不到基准标注: value={value!r} where={where!r}")


def detect_view_split(doc) -> float:
    xs: list[float] = []
    for entity in list(doc.modelspace()):
        if entity.dxftype() not in {"LINE", "ARC", "CIRCLE"}:
            continue
        if entity.dxf.get("layer") == "0":
            continue
        if entity.dxftype() == "LINE":
            xs.extend((entity.dxf.start.x, entity.dxf.end.x))
        else:
            xs.extend(
                (
                    entity.dxf.center.x - entity.dxf.radius,
                    entity.dxf.center.x + entity.dxf.radius,
                )
            )
    if not xs:
        raise ValueError("图中没有可识别的视图几何")

    step = 2.0
    start = int(min(xs) // step)
    stop = int(max(xs) // step) + 1
    occupied = [0] * (stop - start)
    for value in xs:
        index = int(value // step) - start
        if 0 <= index < len(occupied):
            occupied[index] = 1

    runs: list[tuple[float, float]] = []
    run_start = None
    for index, value in enumerate(occupied + [1]):
        if value and run_start is None:
            run_start = index
        elif not value and run_start is not None:
            runs.append(((start + run_start) * step, (start + index) * step))
            run_start = None

    gaps = [
        (runs[i][0] - runs[i - 1][1], runs[i - 1][1], runs[i][0])
        for i in range(1, len(runs))
    ]
    if not gaps:
        raise ValueError("无法自动识别两视图分界，请指定 view_split_x")
    _, left_end, right_start = max(gaps)
    return (left_end + right_start) / 2.0


def _entity_center_x(entity) -> float | None:
    try:
        box = bbox.extents([entity])
    except Exception:
        return None
    if not box.has_data:
        return None
    return (box.extmin.x + box.extmax.x) / 2.0


def _block_center_x(block) -> float | None:
    entities = list(block)
    if not entities:
        return None
    try:
        box = bbox.extents(entities)
    except Exception:
        return None
    if not box.has_data:
        return None
    return (box.extmin.x + box.extmax.x) / 2.0


def _scale_entity_y_about(entity, base_y: float, factor: float) -> None:
    kind = entity.dxftype()
    if kind == "LINE":
        for attr in ("start", "end"):
            point = entity.dxf.get(attr)
            entity.dxf.set(
                attr,
                (point.x, base_y + (point.y - base_y) * factor, point.z),
            )
    elif kind == "ARC":
        center = entity.dxf.center
        entity.dxf.center = (center.x, base_y + (center.y - base_y) * factor, center.z)
        entity.dxf.radius *= factor
    elif kind == "TEXT":
        insert = entity.dxf.insert
        entity.dxf.insert = (
            insert.x,
            base_y + (insert.y - base_y) * factor,
            insert.z,
        )
    elif kind == "MTEXT":
        insert = entity.dxf.insert
        entity.dxf.insert = (
            insert.x,
            base_y + (insert.y - base_y) * factor,
            insert.z,
        )
    else:
        matrix = Matrix44.translate(0.0, -base_y, 0.0) @ Matrix44.scale(
            1.0, factor, 1.0
        ) @ Matrix44.translate(0.0, base_y, 0.0)
        entity.transform(matrix)


def _scale_dimension_y_about(dimension, base_y: float, factor: float) -> None:
    for attr in ("defpoint", "defpoint2", "defpoint3", "text_midpoint"):
        point = dimension.dxf.get(attr)
        if point is not None:
            dimension.dxf.set(
                attr,
                (point.x, base_y + (point.y - base_y) * factor, point.z),
            )


def _transform_all_y_about(entity, base_y: float, factor: float) -> None:
    _scale_entity_y_about(entity, base_y, factor)


def _transform_text_global(entity, kx: float, ky: float) -> None:
    insert = entity.dxf.insert
    entity.dxf.insert = (insert.x * kx, insert.y * ky, insert.z)
    height = entity.dxf.get("height")
    if height is not None:
        entity.dxf.height = height * ky
    align_point = entity.dxf.get("align_point")
    if align_point is not None:
        entity.dxf.align_point = (
            align_point.x * kx,
            align_point.y * ky,
            align_point.z,
        )


def _transform_mtext_global(entity, kx: float, ky: float) -> None:
    insert = entity.dxf.insert
    entity.dxf.insert = (insert.x * kx, insert.y * ky, insert.z)
    height = entity.dxf.get("char_height")
    if height is not None:
        entity.dxf.char_height = height * ky


def _transform_all_global(entity, matrix: Matrix44, kx: float, ky: float) -> None:
    kind = entity.dxftype()
    if kind == "TEXT":
        _transform_text_global(entity, kx=kx, ky=ky)
    elif kind == "MTEXT":
        _transform_mtext_global(entity, kx=kx, ky=ky)
    else:
        log = ezdxf.transform.inplace([entity], matrix)
        if len(log):
            raise ValueError(f"实体无法缩放: {entity.dxftype()} {log.messages()}")


def _scale_insert_global(insert, kx: float, ky: float) -> None:
    point = insert.dxf.insert
    insert.dxf.insert = (point.x * kx, point.y * ky, point.z)


GEOMETRY_TYPES = {
    "LINE", "ARC", "ELLIPSE", "CIRCLE",
    "LWPOLYLINE", "POLYLINE", "SPLINE", "HATCH",
}
ANNOTATION_LAYER_KEYS = ("尺寸", "标注", "中心", "轴线", "文字", "标题", "图框")


def _is_annotation_layer(name: str) -> bool:
    return any(key in name for key in ANNOTATION_LAYER_KEYS)


def strip_watermarks(doc) -> dict:
    """Remove third-party SDK evaluation watermarks from every layout."""
    removed = 0
    layers: set[str] = set()
    for layout in doc.layouts:
        for entity in list(layout):
            layer = str(entity.dxf.get("layer", ""))
            text = ""
            if entity.dxftype() == "TEXT":
                text = entity.dxf.text
            elif entity.dxftype() == "MTEXT":
                text = entity.text
            if "watermark" in layer.lower() or "evaluation only" in text.lower():
                layers.add(layer)
                layout.delete_entity(entity)
                removed += 1
    removed_layers = []
    for name in layers:
        if not name or name not in doc.layers:
            continue
        try:
            doc.layers.remove(name)
            removed_layers.append(name)
        except Exception:
            pass
    return {"entities": removed, "layers": removed_layers}


def part_view_heights(doc, split_x: float | None = None) -> dict:
    """Measure the real part outline of each view, ignoring paper and annotations."""
    if split_x is None:
        split_x = detect_view_split(doc)
    boxes: dict[str, list] = {"left": [], "right": []}
    for entity in doc.modelspace():
        if entity.dxftype() not in GEOMETRY_TYPES:
            continue
        layer = str(entity.dxf.get("layer", ""))
        if not layer or layer == "0" or _is_annotation_layer(layer):
            continue
        try:
            box = bbox.extents([entity])
        except Exception:
            continue
        values = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
        if not all(math.isfinite(value) for value in values):
            continue
        center_x = (box.extmin.x + box.extmax.x) / 2
        side = "left" if center_x < split_x else "right"
        boxes[side].append(box)

    result: dict[str, dict | float | None] = {"split_x": round(split_x, 6)}
    for side, view_boxes in boxes.items():
        if not view_boxes:
            result[side] = None
            continue
        x0 = min(box.extmin.x for box in view_boxes)
        y0 = min(box.extmin.y for box in view_boxes)
        x1 = max(box.extmax.x for box in view_boxes)
        y1 = max(box.extmax.y for box in view_boxes)
        result[side] = {
            "x0": round(x0, 6),
            "y0": round(y0, 6),
            "x1": round(x1, 6),
            "y1": round(y1, 6),
            "width": round(x1 - x0, 6),
            "height": round(y1 - y0, 6),
        }
    return result


def _map_y(value: float, source_bottom: float, target_bottom: float, factor: float) -> float:
    return target_bottom + (value - source_bottom) * factor


def _bake_insert_y(doc, insert, source_bottom: float, target_bottom: float,
                   factor: float, processed: set[str]) -> None:
    """Apply a Y scale to a block definition, keeping text height readable."""
    name = insert.dxf.name
    if name in processed:
        return
    block = doc.blocks.get(name)
    if block is None:
        return
    insert_y = insert.dxf.insert.y
    xscale = float(insert.dxf.get("xscale", 1.0))
    yscale = float(insert.dxf.get("yscale", 1.0))
    rotation = float(insert.dxf.get("rotation", 0.0))
    if abs(xscale - 1.0) > 1e-9 or abs(yscale - 1.0) > 1e-9 or abs(rotation) > 1e-9:
        insert.dxf.insert = (
            insert.dxf.insert.x,
            _map_y(insert_y, source_bottom, target_bottom, factor),
            insert.dxf.insert.z,
        )
        insert.dxf.yscale = yscale * factor
        processed.add(name)
        return

    offset = target_bottom + factor * (insert_y - source_bottom) - insert_y
    matrix = Matrix44.chain(
        Matrix44.scale(1.0, factor, 1.0),
        Matrix44.translate(0.0, offset, 0.0),
    )
    for entity in list(block):
        kind = entity.dxftype()
        if kind == "TEXT":
            point = entity.dxf.insert
            entity.dxf.insert = (point.x, factor * point.y + offset, point.z)
            align_point = entity.dxf.get("align_point")
            if align_point is not None:
                entity.dxf.align_point = (
                    align_point.x,
                    factor * align_point.y + offset,
                    align_point.z,
                )
        elif kind == "MTEXT":
            point = entity.dxf.insert
            entity.dxf.insert = (point.x, factor * point.y + offset, point.z)
        else:
            ezdxf.transform.inplace([entity], matrix)
    processed.add(name)


def _normalize_view_geometry_height(doc, split_x: float | None = None,
                                    tolerance: float = 0.05) -> dict:
    """Scale the right view so both part outlines have exactly the same height.

    The first alignment pass follows the height dimension text.  In drawings
    where that dimension floats above the real outline, the two views can still
    differ.  This pass anchors the right view to the left outline's bottom and
    top, then re-measures every annotation from the corrected geometry.
    """
    if split_x is None:
        split_x = detect_view_split(doc)
    before = part_view_heights(doc, split_x)
    left, right = before.get("left"), before.get("right")
    if not left or not right:
        return {"changed": False, "reason": "缺少一侧视图实体", "before": before}
    if abs(left["height"] - right["height"]) <= tolerance:
        return {"changed": False, "before": before, "after": before}

    source_bottom = right["y0"]
    target_bottom = left["y0"]
    target_top = left["y1"]
    factor = left["height"] / right["height"]
    matrix = Matrix44.chain(
        Matrix44.translate(0.0, -source_bottom, 0.0),
        Matrix44.scale(1.0, factor, 1.0),
        Matrix44.translate(0.0, target_bottom, 0.0),
    )
    msp = doc.modelspace()

    dimensions = [
        dimension
        for dimension in msp.query("DIMENSION")
        if abs(float(dimension.dxf.get("angle", 0))) % 180 == 90
        and (_entity_center_x(dimension) or 0.0) > split_x
    ]
    dimension = max(dimensions, key=lambda item: float(item.get_measurement())) if dimensions else None
    old_top = None
    bracket_handles: set[str] = set()
    if dimension is not None:
        points = (
            dimension.dxf.defpoint,
            dimension.dxf.defpoint2,
            dimension.dxf.defpoint3,
        )
        old_top = max(point.y for point in points)
        for line in msp.query("LINE"):
            layer = str(line.dxf.get("layer", ""))
            if not _is_annotation_layer(layer):
                continue
            high = max(line.dxf.start.y, line.dxf.end.y)
            if abs(high - old_top) <= 0.5:
                bracket_handles.add(line.dxf.handle)

    processed_blocks: set[str] = set()
    for entity in list(msp):
        if entity.dxftype() == "DIMENSION":
            continue
        if entity.dxf.handle in bracket_handles:
            continue
        layer = str(entity.dxf.get("layer", ""))
        if not layer or layer == "0":
            continue
        try:
            box = bbox.extents([entity])
        except Exception:
            continue
        values = (box.extmin.x, box.extmax.x)
        if not all(math.isfinite(value) for value in values):
            continue
        center_x = (box.extmin.x + box.extmax.x) / 2
        if center_x < split_x:
            continue
        kind = entity.dxftype()
        if kind == "INSERT":
            _bake_insert_y(doc, entity, source_bottom, target_bottom, factor, processed_blocks)
        elif kind == "TEXT":
            point = entity.dxf.insert
            entity.dxf.insert = (
                point.x,
                _map_y(point.y, source_bottom, target_bottom, factor),
                point.z,
            )
        elif kind == "MTEXT":
            point = entity.dxf.insert
            entity.dxf.insert = (
                point.x,
                _map_y(point.y, source_bottom, target_bottom, factor),
                point.z,
            )
        else:
            ezdxf.transform.inplace([entity], matrix)

    if dimension is not None:
        p1, p2, p3 = (
            dimension.dxf.defpoint,
            dimension.dxf.defpoint2,
            dimension.dxf.defpoint3,
        )
        dimension.dxf.defpoint = (p1.x, target_top, p1.z)
        dimension.dxf.defpoint2 = (p2.x, target_bottom, p2.z)
        dimension.dxf.defpoint3 = (p3.x, target_top, p3.z)
        midpoint = dimension.dxf.get("text_midpoint")
        if midpoint is not None:
            dimension.dxf.text_midpoint = (
                midpoint.x,
                (target_bottom + target_top) / 2,
                midpoint.z,
            )
        geometry_name = dimension.dxf.get("geometry", "")
        if geometry_name in doc.blocks:
            shift = target_top - old_top
            translation = Matrix44.translate(0.0, shift, 0.0)
            for entity in list(doc.blocks[geometry_name]):
                if entity.dxftype() in {"TEXT", "MTEXT"}:
                    point = entity.dxf.insert
                    entity.dxf.insert = (point.x, point.y + shift, point.z)
                    if entity.dxftype() == "TEXT":
                        align_point = entity.dxf.get("align_point")
                        if align_point is not None:
                            entity.dxf.align_point = (
                                align_point.x,
                                align_point.y + shift,
                                align_point.z,
                            )
                else:
                    ezdxf.transform.inplace([entity], translation)

    for line in list(msp.query("LINE")):
        if line.dxf.handle not in bracket_handles:
            continue
        start, end = line.dxf.start, line.dxf.end
        if abs(start.x - end.x) < 1e-6:
            low = min(start.y, end.y)
            high = max(start.y, end.y)
            new_low = _map_y(low, source_bottom, target_bottom, factor)
            new_high = target_top if abs(high - old_top) <= 0.5 else _map_y(
                high, source_bottom, target_bottom, factor
            )
            line.dxf.start = (start.x, new_low, start.z)
            line.dxf.end = (end.x, new_high, end.z)
        elif abs(start.y - end.y) < 1e-6:
            new_y = target_top if abs(start.y - old_top) <= 0.5 else _map_y(
                start.y, source_bottom, target_bottom, factor
            )
            line.dxf.start = (start.x, new_y, start.z)
            line.dxf.end = (end.x, new_y, end.z)

    after = part_view_heights(doc, split_x)
    return {
        "changed": True,
        "factor": round(factor, 9),
        "target_bottom": round(target_bottom, 6),
        "target_top": round(target_top, 6),
        "before": before,
        "after": after,
    }


def _rewrite_annotations(doc) -> list[dict]:
    changes = []
    for block in doc.blocks:
        if not block.name.startswith("GS_BlockDim"):
            continue
        old, measured = measure_block(block)
        if not old or old in SPEC_TEXTS or measured <= 0:
            continue
        new = f"{measured:.1f}"
        for entity in block:
            if entity.dxftype() in {"TEXT", "MTEXT"}:
                if entity.dxftype() == "TEXT":
                    entity.dxf.text = new
                else:
                    entity.text = new
        changes.append(
            {
                "kind": "BLOCK",
                "where": block.name,
                "old": old,
                "new": new,
                "measured": round(measured, 6),
            }
        )

    for dimension in doc.modelspace().query("DIMENSION"):
        old = dimension.dxf.text
        if not old or old in SPEC_TEXTS:
            continue
        measured = float(dimension.get_measurement())
        if measured <= 0:
            continue
        new = f"{measured:.1f}"
        ae._set_dim_text(doc, dimension, new)
        changes.append(
            {
                "kind": "DIM",
                "where": dimension.dxf.handle,
                "old": old,
                "new": new,
                "measured": round(measured, 6),
            }
        )
    return changes


def set_real_mm(
    src: str | Path,
    dst: str | Path,
    length_value: float = 610.0,
    length_anchor: str | None = "610",
    height_value: float = 284.0,
    height_anchor: str | None = "284",
    align_anchor: str | None = "auto",
    view_split_x: float | None = None,
) -> dict:
    src, dst = Path(src), Path(dst)
    doc = ezdxf.readfile(str(src))
    watermarks = strip_watermarks(doc)
    # A real-millimeter drawing must advertise millimeter units to downstream
    # CAD viewers. Relying on the source header made the result depend on the
    # imported baseline and could silently deliver unitless geometry.
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    initial_counts = Counter(entity.dxftype() for entity in doc.modelspace())
    rows = measure_all(doc)
    length_row = _find_anchor(rows, length_anchor, None)
    height_row = _find_anchor(rows, height_anchor, None)
    align_row = None
    if align_anchor and align_anchor != "auto":
        try:
            align_row = _find_anchor(rows, align_anchor, None)
        except ValueError:
            align_row = None

    if view_split_x is not None:
        split_x = float(view_split_x)
    else:
        split_x = detect_view_split(doc)

    factor = 1.0
    base_y = None
    if align_row is None:
        right_vertical = []
        for dimension in doc.modelspace().query("DIMENSION"):
            if abs(float(dimension.dxf.get("angle", 0))) % 180 != 90:
                continue
            center_x = _entity_center_x(dimension)
            if center_x is None or center_x <= split_x:
                continue
            measured = float(dimension.get_measurement())
            right_vertical.append(
                {
                    "kind": "DIM",
                    "where": dimension.dxf.handle,
                    "old": dimension.dxf.text,
                    "measured": round(measured, 6),
                }
            )
        if right_vertical:
            align_row = max(right_vertical, key=lambda row: row["measured"])

    if align_row is not None:
        align_dimension = next(
            (
                dimension
                for dimension in doc.modelspace().query("DIMENSION")
                if dimension.dxf.handle == align_row["where"]
            ),
            None,
        )
        if align_dimension is None:
            raise ValueError("对齐基准必须是 DIMENSION")
        base_y = min(
            point.y
            for point in (
                align_dimension.dxf.defpoint,
                align_dimension.dxf.defpoint2,
                align_dimension.dxf.defpoint3,
            )
        )
        factor = height_row["measured"] / align_row["measured"]
        for entity in list(doc.modelspace()):
            if entity.dxftype() in {"INSERT", "DIMENSION"}:
                continue
            center_x = _entity_center_x(entity)
            if center_x is not None and center_x > split_x:
                _transform_all_y_about(entity, base_y, factor)

        _scale_dimension_y_about(align_dimension, base_y, factor)
        geometry_name = align_dimension.dxf.get("geometry", "")
        if geometry_name in doc.blocks:
            for entity in list(doc.blocks[geometry_name]):
                _transform_all_y_about(entity, base_y, factor)
        for block in doc.blocks:
            if not block.name.startswith("GS_BlockDim"):
                continue
            center_x = _block_center_x(block)
            if center_x is not None and center_x > split_x:
                for entity in list(block):
                    _transform_all_y_about(entity, base_y, factor)

    rows_after_alignment = measure_all(doc)
    length_row = _find_anchor(rows_after_alignment, length_anchor, None)
    height_row = _find_anchor(rows_after_alignment, height_anchor, None)

    kx = length_value / length_row["measured"]
    ky = height_value / height_row["measured"]
    matrix = Matrix44.scale(kx, ky, 1.0)
    for entity in list(doc.modelspace()):
        kind = entity.dxftype()
        if kind == "INSERT":
            _scale_insert_global(entity, kx, ky)
        elif kind == "DIMENSION":
            continue
        else:
            _transform_all_global(entity, matrix, kx, ky)
    for dimension in doc.modelspace().query("DIMENSION"):
        for attr in ("defpoint", "defpoint2", "defpoint3", "text_midpoint"):
            point = dimension.dxf.get(attr)
            if point is not None:
                dimension.dxf.set(attr, (point.x * kx, point.y * ky, point.z))
    for block in doc.blocks:
        if block.name in {"*Model_Space"} or block.name.startswith("*Paper_Space"):
            continue
        for entity in list(block):
            _transform_all_global(entity, matrix, kx, ky)

    view_normalization = _normalize_view_geometry_height(doc, split_x * kx)
    vertical_fixes = ae.fix_vertical_dims(doc, height=2.5 * ky, gap=1.5 * kx)
    changes = _rewrite_annotations(doc)

    auditor = doc.audit()
    first_audit = {"errors": len(auditor.errors), "fixes": len(auditor.fixes)}
    doc.saveas(str(dst))
    check = ezdxf.readfile(str(dst))
    auditor = check.audit()
    second_audit = {"errors": len(auditor.errors), "fixes": len(auditor.fixes)}
    if second_audit["errors"] or second_audit["fixes"]:
        check.saveas(str(dst))
        check = ezdxf.readfile(str(dst))
        auditor = check.audit()
        second_audit = {"errors": len(auditor.errors), "fixes": len(auditor.fixes)}

    counts_after = Counter(entity.dxftype() for entity in check.modelspace())
    report = {
        "engine": "real_mm_engine",
        "source": str(src),
        "output": str(dst),
        "unit": "mm",
        "unit_metadata": {"INSUNITS": 4, "MEASUREMENT": 1},
        "watermarks": watermarks,
        "targets": {
            "length": length_value,
            "height": height_value,
        },
        "calibration": {
            "kx": round(kx, 9),
            "ky": round(ky, 9),
            "length_anchor": length_row,
            "height_anchor": height_row,
            "view_alignment": {
                "split_x": round(split_x, 6),
                "factor": round(factor, 9),
                "base_y": round(base_y, 6) if base_y is not None else None,
                "aligned_dimension": align_row["where"] if align_row else None,
                "before": align_row["measured"] if align_row else None,
                "after": round(align_row["measured"] * factor, 6) if align_row else None,
            },
            "view_normalization": view_normalization,
        },
        "audit": {"first": first_audit, "final": second_audit},
        "counts": {
            "before": dict(initial_counts),
            "after": dict(counts_after),
        },
        "vertical_fixes": vertical_fixes,
        "changes": changes,
    }
    return report


def export_dwg(dxf: str | Path, dwg: str | Path, version: str = "AC1032") -> dict:
    from dwg_export import export as _export

    return _export(str(dxf), str(dwg), version=version)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "output" / "25QYL-2SL_clean.dxf")
    dst = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "output" / "_real_mm_from_clean.dxf")
    report = set_real_mm(src, dst)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"changes", "vertical_fixes"}},
            ensure_ascii=False,
            indent=2,
        )
    )
