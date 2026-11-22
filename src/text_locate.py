"""把扫描到的文字 / 标注句柄定位到图纸世界坐标。

块定义里的文字可能被多个 INSERT 引用。这里不假设“第一个实例”，而是从
每个布局出发递归展开块引用，用 ``parent @ child`` 组合矩阵把块内包围盒
转换到世界坐标，供工作台高亮和局部放大使用。
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import runtime

runtime.bootstrap()

import ezdxf
from ezdxf import bbox
from ezdxf.math import Matrix44, Vec3

from text_edit_engine import entity_text

MAX_BLOCK_DEPTH = 12


def _round_box(box: tuple[float, float, float, float]) -> list[float]:
    return [round(float(value), 6) for value in box]


def _entity_box(entity) -> tuple[float, float, float, float] | None:
    """返回实体自身的轴对齐包围盒；失败时用关键点兜底。"""
    try:
        extents = bbox.extents([entity])
        if extents.has_data:
            return (
                float(extents.extmin.x),
                float(extents.extmin.y),
                float(extents.extmax.x),
                float(extents.extmax.y),
            )
    except Exception:  # noqa: BLE001
        pass

    points: list[tuple[float, float]] = []
    for name in (
        "insert",
        "defpoint",
        "defpoint2",
        "defpoint3",
        "defpoint4",
        "text_midpoint",
    ):
        point = entity.dxf.get(name, None) if entity.dxf.is_supported(name) else None
        if point is not None:
            try:
                points.append((float(point.x), float(point.y)))
            except Exception:  # noqa: BLE001
                continue
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _transform_box(
    matrix: Matrix44,
    box: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    corners = [
        matrix.transform(Vec3(x0, y0, 0.0)),
        matrix.transform(Vec3(x0, y1, 0.0)),
        matrix.transform(Vec3(x1, y0, 0.0)),
        matrix.transform(Vec3(x1, y1, 0.0)),
    ]
    xs = [point.x for point in corners]
    ys = [point.y for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _union_box(
    boxes: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    rows = list(boxes)
    if not rows:
        return None
    return (
        min(row[0] for row in rows),
        min(row[1] for row in rows),
        max(row[2] for row in rows),
        max(row[3] for row in rows),
    )


def _normalise_handles(handles: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in handles:
        handle = str(value or "").strip()
        key = handle.casefold()
        if not handle or key in seen:
            continue
        seen.add(key)
        out.append(handle)
    return out


def _inventory_map(inventory: dict | None) -> dict[str, dict]:
    rows = (inventory or {}).get("items") or []
    return {
        str(row.get("handle") or "").casefold(): row
        for row in rows
        if row.get("handle")
    }


def locate_handles(
    doc,
    handles: Iterable[str],
    inventory: dict | None = None,
) -> dict:
    """返回句柄在全部可见实例上的世界坐标包围盒。

    ``box`` 是全部实例的合并包围盒，``boxes`` 保留每个实例，便于共享块在图上
    逐处画框。找不到可见实例时只返回 warning，不抛异常。
    """
    requested = _normalise_handles(handles)
    if not requested:
        return {
            "ok": True,
            "requested": [],
            "count": 0,
            "missing": [],
            "items": [],
            "warnings": ["没有提供要定位的句柄"],
        }

    wanted = {handle.casefold(): handle for handle in requested}
    metadata = _inventory_map(inventory)
    instances: dict[str, list[dict]] = {handle.casefold(): [] for handle in requested}
    entity_by_handle: dict[str, Any] = {}
    warnings: list[str] = []

    def add_instance(
        target_handle: str,
        entity,
        matrix: Matrix44 | None,
        path: tuple[str, ...],
    ) -> None:
        key = target_handle.casefold()
        if key not in wanted:
            return
        local_box = _entity_box(entity)
        if local_box is None:
            warnings.append(f"句柄 {target_handle} 的实体无法计算包围盒")
            return
        world_box = (
            _transform_box(matrix, local_box)
            if matrix is not None
            else local_box
        )
        entity_by_handle[key] = entity
        instances[key].append(
            {
                "box": _round_box(world_box),
                "path": " / ".join(path) if path else "modelspace",
            }
        )

    def walk_block(
        block_name: str,
        matrix: Matrix44,
        path: tuple[str, ...],
        stack: tuple[str, ...],
    ) -> None:
        if len(stack) >= MAX_BLOCK_DEPTH:
            warnings.append(f"块引用超过 {MAX_BLOCK_DEPTH} 层，已停止递归：{block_name}")
            return
        if block_name in stack:
            warnings.append(f"检测到循环块引用，已停止：{block_name}")
            return
        block = doc.blocks.get(block_name)
        if block is None:
            warnings.append(f"找不到块定义：{block_name}")
            return
        next_stack = stack + (block_name,)
        for entity in block:
            handle = str(entity.dxf.get("handle", "") or "")
            if handle.casefold() in wanted:
                add_instance(handle, entity, matrix, path)
            if entity.dxftype() == "INSERT":
                child_name = str(entity.dxf.name)
                child_matrix = matrix @ entity.matrix44()
                walk_block(
                    child_name,
                    child_matrix,
                    path + (f"INSERT {entity.dxf.handle} {child_name}",),
                    next_stack,
                )

    for layout in doc.layouts:
        layout_scope = "modelspace" if layout.name == "Model" else f"layout:{layout.name}"
        for entity in layout:
            entity_handle = str(entity.dxf.get("handle", "") or "")
            if entity_handle.casefold() in wanted:
                add_instance(entity_handle, entity, None, (layout_scope,))
            if entity.dxftype() == "INSERT":
                for attrib in entity.attribs:
                    attrib_handle = str(attrib.dxf.get("handle", "") or "")
                    if attrib_handle.casefold() in wanted:
                        # 附加 ATTRIB 的坐标已由 INSERT 放置到世界坐标。
                        add_instance(
                            attrib_handle,
                            attrib,
                            None,
                            (layout_scope, f"ATTRIB of {entity.dxf.handle}"),
                        )
                block_name = str(entity.dxf.name)
                walk_block(
                    block_name,
                    entity.matrix44(),
                    (layout_scope, f"INSERT {entity.dxf.handle} {block_name}"),
                    (),
                )

    items = []
    missing = []
    for handle in requested:
        key = handle.casefold()
        found = instances[key]
        if not found:
            missing.append(handle)
            continue
        entity = entity_by_handle.get(key)
        row = metadata.get(key) or {}
        boxes = [tuple(instance["box"]) for instance in found]
        union = _union_box(boxes)
        assert union is not None
        items.append(
            {
                "handle": handle,
                "text": str(row.get("text") or (entity_text(entity) if entity else "")),
                "kind": str(row.get("kind") or (entity.dxftype() if entity else "")),
                "scope": row.get("scope") or (found[0]["path"] if found else ""),
                "layer": row.get("layer"),
                "tag": row.get("tag"),
                "is_dimension": bool(row.get("is_dimension")),
                "shared": bool(row.get("shared")),
                "box": _round_box(union),
                "boxes": [_round_box(box) for box in boxes],
                "instances": len(boxes),
                "paths": [instance["path"] for instance in found],
            }
        )

    if missing:
        warnings.append(
            "以下句柄没有在当前布局的可见实例中找到："
            + "、".join(missing)
        )
    return {
        "ok": True,
        "requested": requested,
        "count": len(items),
        "missing": missing,
        "items": items,
        "warnings": list(dict.fromkeys(warnings)),
    }


def locate_file(
    dxf_path: str | Path,
    handles: Iterable[str],
    inventory: dict | None = None,
) -> dict:
    """读取 DXF 并定位句柄，方便 CLI / API 共用。"""
    doc = ezdxf.readfile(str(dxf_path))
    return locate_handles(doc, handles, inventory)
