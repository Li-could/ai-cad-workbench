"""通用文字 / 标注修改链。

这条链不假设图纸型号，只处理用户在图纸里明确指出的 TEXT / MTEXT /
ATTRIB / DIMENSION 内容。几何实体保持逐条不变，找不到唯一目标或存在
多处歧义时由意图层追问，不进入执行。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import runtime

runtime.bootstrap()

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
EDITABLE_TYPES = {"TEXT", "MTEXT", "ATTRIB", "DIMENSION"}
SKIP_BLOCK_PREFIXES = ("*D", "*Model_Space", "*Paper_Space")
DIMENSION_BLOCK_PREFIX = "gs_blockdim"
DIMENSION_LAYER_MARKERS = ("尺寸", "标注", "dim")


def _skip_block(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in SKIP_BLOCK_PREFIXES)


def _is_dimension_block(name: str) -> bool:
    return str(name or "").casefold().startswith(DIMENSION_BLOCK_PREFIX)


def _is_dimension_layer(name: str) -> bool:
    layer = str(name or "").casefold()
    return any(marker in layer for marker in DIMENSION_LAYER_MARKERS)


def _dimension_value(entity) -> float | None:
    try:
        value = float(entity.get_measurement())
    except Exception:  # noqa: BLE001
        return None
    return value if abs(value) > 1e-9 else None


def entity_text(entity) -> str:
    """返回实体在 CAD 中实际显示的文字。"""
    kind = entity.dxftype()
    if kind in ("TEXT", "ATTRIB"):
        return str(entity.dxf.get("text", "") or "")
    if kind == "MTEXT":
        try:
            return str(entity.plain_text())
        except Exception:  # noqa: BLE001
            return str(entity.text or "")
    if kind == "DIMENSION":
        raw = str(entity.dxf.get("text", "") or "").strip()
        if raw and raw != "<>":
            return raw
        value = _dimension_value(entity)
        return f"{value:g}" if value is not None else ""
    return ""


def _raw_editable_text(entity) -> str:
    kind = entity.dxftype()
    if kind in ("TEXT", "ATTRIB"):
        return str(entity.dxf.get("text", "") or "")
    if kind == "MTEXT":
        return str(entity.text or "")
    if kind == "DIMENSION":
        return str(entity.dxf.get("text", "") or "")
    return ""


def _same_text(left: str, right: str) -> bool:
    key = lambda value: re.sub(r"\s+", "", str(value or "")).casefold()
    return key(left) == key(right)


def _iter_entities(doc) -> Iterable[tuple[str, Any, dict]]:
    seen: set[str] = set()
    dimension_blocks: set[str] = set()
    layouts = list(doc.layouts)
    for layout in layouts:
        for entity in layout:
            if entity.dxftype() == "DIMENSION":
                geometry = str(entity.dxf.get("geometry", "") or "")
                if geometry:
                    dimension_blocks.add(geometry)
    _block_by_handle, insert_counts = _block_usage(doc)
    first_insert: dict[str, str] = {}
    for layout in layouts:
        for entity in layout:
            if entity.dxftype() == "INSERT":
                first_insert.setdefault(
                    str(entity.dxf.name), str(entity.dxf.handle)
                )

    def emit(scope: str, entity, **extra):
        if entity.dxftype() not in EDITABLE_TYPES:
            return None
        handle = str(entity.dxf.get("handle", "") or "")
        if not handle or handle in seen:
            return None
        seen.add(handle)
        text = entity_text(entity).strip()
        if not text:
            return None
        layer = str(entity.dxf.get("layer", "") or "")
        layer_dimension = (
            entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB"}
            and _is_dimension_layer(layer)
        )
        row = {
            "handle": handle,
            "kind": entity.dxftype(),
            "text": text,
            "tag": (
                str(entity.dxf.get("tag", "") or "")
                if entity.dxftype() == "ATTRIB"
                else None
            ),
            "layer": layer,
            "scope": scope,
            "measurement": _dimension_value(entity)
            if entity.dxftype() == "DIMENSION"
            else None,
            "is_dimension": entity.dxftype() == "DIMENSION" or layer_dimension,
            "dimension_source": (
                "entity"
                if entity.dxftype() == "DIMENSION"
                else ("layer" if layer_dimension else None)
            ),
            "dimension_block": None,
            "shared": False,
            "insert_count": 0,
        }
        row.update(extra)
        return row

    for layout in layouts:
        scope = "modelspace" if layout.name == "Model" else f"layout:{layout.name}"
        for entity in layout:
            if entity.dxftype() == "INSERT":
                for attrib in entity.attribs:
                    row = emit(
                        f"attrib:{entity.dxf.handle}",
                        attrib,
                        insert_handle=str(entity.dxf.handle),
                    )
                    if row:
                        yield scope, attrib, row
            row = emit(scope, entity)
            if row:
                yield scope, entity, row

    for block in doc.blocks:
        if _skip_block(str(block.name)) or str(block.name) in dimension_blocks:
            continue
        block_name = str(block.name)
        insert_count = insert_counts.get(block_name, 0)
        shared = insert_count > 1
        dimension_block = _is_dimension_block(block_name)
        for entity in block:
            row = emit(
                f"block:{block_name}",
                entity,
                shared=shared,
                insert_count=insert_count,
                insert_handle=first_insert.get(block_name),
                is_dimension=dimension_block,
                dimension_source="block" if dimension_block else None,
                dimension_block=block_name if dimension_block else None,
            )
            if row:
                yield f"block:{block_name}", entity, row


def inspect_doc(doc, limit: int | None = None) -> dict:
    """扫描图纸中所有可直接改文字的位置。"""
    rows = []
    kind_counts = Counter()
    scope_counts = Counter()
    shared_blocks = set()
    for _scope, _entity, row in _iter_entities(doc):
        rows.append(row)
        kind_counts[row["kind"]] += 1
        scope_counts[
            "block" if row["scope"].startswith("block:") else (
                "attrib" if row["scope"].startswith("attrib:") else "layout"
            )
        ] += 1
        if row.get("shared"):
            shared_blocks.add(row["scope"].split(":", 1)[-1])
    items = rows[:limit] if limit is not None else rows
    return {
        "ok": True,
        "editable_count": len(rows),
        "kinds": dict(kind_counts),
        "scopes": dict(scope_counts),
        "dimension_count": sum(1 for row in rows if row.get("is_dimension")),
        "shared_text_count": sum(1 for row in rows if row.get("shared")),
        "shared_blocks": sorted(shared_blocks),
        "items": items,
        "truncated": len(rows) > len(items),
    }


def _set_dimension_text(doc, dimension, new: str) -> None:
    dimension.dxf.text = new
    geometry = str(dimension.dxf.get("geometry", "") or "")
    if geometry and geometry in doc.blocks:
        for entity in doc.blocks[geometry]:
            if entity.dxftype() == "MTEXT":
                entity.text = new
            elif entity.dxftype() == "TEXT":
                entity.dxf.text = new


def _block_usage(doc) -> tuple[dict[str, str], Counter]:
    """句柄→所属块名，以及每个块展开后的可见实例数。"""
    block_by_handle: dict[str, str] = {}
    direct_counts: Counter = Counter()
    parents: dict[str, Counter] = defaultdict(Counter)
    for layout in doc.layouts:
        for entity in layout:
            if entity.dxftype() == "INSERT":
                direct_counts[str(entity.dxf.name)] += 1
    for block in doc.blocks:
        block_name = str(block.name)
        for entity in block:
            handle = str(entity.dxf.get("handle", "") or "")
            if handle:
                block_by_handle[handle] = block_name
            if entity.dxftype() == "INSERT":
                child = str(entity.dxf.name)
                parents[child][block_name] += 1

    memo: dict[str, int] = {}

    def effective(name: str, stack: tuple[str, ...] = ()) -> int:
        if name in memo:
            return memo[name]
        if name in stack:
            return int(direct_counts.get(name, 0))
        total = int(direct_counts.get(name, 0))
        for parent, count in parents.get(name, {}).items():
            total += int(count) * effective(parent, stack + (name,))
        memo[name] = total
        return total

    names = set(direct_counts) | set(block_by_handle.values())
    insert_counts = Counter({name: effective(name) for name in names})
    return block_by_handle, insert_counts


def _dimension_context(
    entity, block_by_handle: dict[str, str]
) -> tuple[bool, str | None, str | None]:
    if entity.dxftype() == "DIMENSION":
        return True, "entity", None
    block_name = block_by_handle.get(str(entity.dxf.get("handle", "") or ""))
    if block_name and _is_dimension_block(block_name):
        return True, "block", block_name
    if (
        entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB"}
        and _is_dimension_layer(entity.dxf.get("layer", ""))
    ):
        return True, "layer", block_name
    return False, None, block_name


def _replace_first(text: str, old: str, new: str) -> str:
    if not old:
        raise ValueError("替换目标为空")
    matches = list(re.finditer(re.escape(old), text, flags=re.IGNORECASE))
    if not matches:
        raise ValueError(f"文字中找不到要替换的片段：{old}")
    match = matches[0]
    return text[:match.start()] + new + text[match.end():]


def _set_entity_text(doc, entity, edit: dict) -> str:
    kind = entity.dxftype()
    new = str(edit.get("new", ""))
    mode = str(edit.get("mode") or "exact")
    old = str(edit.get("old", ""))
    if kind == "DIMENSION":
        if mode != "exact":
            raise ValueError("尺寸标注只支持整条数值替换")
        _set_dimension_text(doc, entity, new)
        return entity_text(entity)
    if mode == "exact":
        if kind == "MTEXT":
            entity.text = new
        else:
            entity.dxf.text = new
        return entity_text(entity)
    if kind == "MTEXT":
        entity.text = _replace_first(str(entity.text or ""), old, new)
    else:
        entity.dxf.text = _replace_first(
            str(entity.dxf.get("text", "") or ""), old, new
        )
    return entity_text(entity)


def _geometry_signature(doc) -> list[tuple]:
    rows = []
    dimension_blocks = {
        str(entity.dxf.get("geometry", "") or "")
        for layout in doc.layouts
        for entity in layout
        if entity.dxftype() == "DIMENSION"
    }

    def add(entity) -> None:
        if entity.dxftype() in EDITABLE_TYPES or entity.dxftype() in {
            "ATTDEF",
            "VIEWPORT",
        }:
            return
        try:
            attrs = tuple(
                sorted(
                    (str(key), str(value))
                    for key, value in entity.dxfattribs().items()
                    if key not in {"handle", "owner"}
                )
            )
        except Exception:  # noqa: BLE001
            attrs = ()
        rows.append((entity.dxftype(), str(entity.dxf.get("handle", "")), attrs))

    for layout in doc.layouts:
        for entity in layout:
            add(entity)
    for block in doc.blocks:
        if _skip_block(str(block.name)) or str(block.name) in dimension_blocks:
            continue
        for entity in block:
            add(entity)
    return rows


def _all_texts(doc) -> dict[str, str]:
    return {
        row["handle"]: row["text"]
        for _scope, _entity, row in _iter_entities(doc)
    }


def _entity_count(doc) -> int:
    """只统计布局中的顶层实体；entitydb 会因保存时的扩展字典变化。"""
    return sum(len(list(layout)) for layout in doc.layouts)


def apply_edits(
    src: str | Path,
    dst: str | Path,
    edits: list[dict],
) -> dict:
    """按句柄执行文字 / 标注替换，并做零几何改动质检。"""
    src, dst = Path(src), Path(dst)
    doc = ezdxf.readfile(str(src))
    before_geometry = _geometry_signature(doc)
    before_texts = _all_texts(doc)
    before_entity_count = _entity_count(doc)
    block_by_handle, insert_counts = _block_usage(doc)

    changes = []
    expected: dict[str, str] = {}
    used_handles: set[str] = set()
    for edit in edits:
        handle = str(edit.get("handle", "") or "")
        if not handle:
            raise ValueError("文字修改缺少实体句柄")
        if handle in used_handles:
            raise ValueError(f"同一个实体不能在一轮里重复修改：{handle}")
        used_handles.add(handle)
        entity = doc.entitydb.get(handle)
        if entity is None:
            raise ValueError(f"找不到实体句柄：{handle}")
        if entity.dxftype() not in EDITABLE_TYPES:
            raise ValueError(f"实体 {handle} 不是可改文字类型：{entity.dxftype()}")

        is_dimension, dimension_source, block_name = _dimension_context(
            entity, block_by_handle
        )
        dimension_effect = str(
            edit.get("dimension_effect") or edit.get("dimension_policy") or ""
        )
        if is_dimension and dimension_effect != "label_only":
            raise ValueError(
                f"实体 {handle} 是尺寸标注。直接改数字只会改标签，不会改几何；"
                "必须明确“只改标注文字”后才能执行。"
            )
        shared_effect = str(edit.get("shared_effect") or "")
        if (
            block_name
            and insert_counts.get(block_name, 0) > 1
            and shared_effect != "all_instances"
        ):
            raise ValueError(
                f"实体 {handle} 位于共享块定义 {block_name}，"
                f"被 {insert_counts[block_name]} 处引用；"
                "必须明确“全部修改”后才能执行。"
            )

        current = entity_text(entity)
        old = str(edit.get("old", "") or "")
        new_fragment = str(edit.get("new", "") or "")
        mode = str(edit.get("mode") or "exact")
        if mode == "exact":
            measurement = _dimension_value(entity) if entity.dxftype() == "DIMENSION" else None
            numeric_match = False
            if measurement is not None and old:
                try:
                    numeric_match = abs(measurement - float(old)) <= 1e-6
                except ValueError:
                    numeric_match = False
            if old and not _same_text(current, old) and not numeric_match:
                raise ValueError(
                    f"实体 {handle} 当前文字为「{current}」，与计划目标「{old}」不一致"
                )
        elif entity.dxftype() == "DIMENSION":
            raise ValueError("尺寸标注只支持整条数值替换")
        elif old.casefold() not in _raw_editable_text(entity).casefold():
            raise ValueError(f"实体 {handle} 中找不到待替换片段「{old}」")

        after = _set_entity_text(doc, entity, edit)
        expected[handle] = after
        changes.append(
            {
                "handle": handle,
                "kind": entity.dxftype(),
                "scope": edit.get("scope") or "",
                "old": current,
                "old_fragment": old,
                "new_fragment": new_fragment,
                "text_before": current,
                "new": after,
                "mode": mode,
                "is_dimension": is_dimension,
                "dimension_source": dimension_source,
                "dimension_effect": "label_only" if is_dimension else None,
                "dimension_block": block_name if is_dimension else None,
                "block": block_name,
                "shared_effect": shared_effect or None,
            }
        )

    doc.saveas(str(dst))
    checked = ezdxf.readfile(str(dst))
    after_geometry = _geometry_signature(checked)
    after_texts = _all_texts(checked)
    after_entity_count = _entity_count(checked)
    audit = checked.audit()

    changed_handles = {
        handle
        for handle in set(before_texts) | set(after_texts)
        if before_texts.get(handle) != after_texts.get(handle)
    }
    only_targets_changed = changed_handles <= used_handles
    all_expected = all(
        _same_text(after_texts.get(handle, ""), text)
        for handle, text in expected.items()
    )
    entity_count_same = before_entity_count == after_entity_count
    geometry_identical = before_geometry == after_geometry
    audit_ok = len(audit.errors) == 0
    dimension_labels_only = all(
        not row.get("is_dimension") or row.get("dimension_effect") == "label_only"
        for row in changes
    )
    shared_blocks_explicit = all(
        not row.get("block")
        or insert_counts.get(str(row.get("block")), 0) <= 1
        or row.get("shared_effect") == "all_instances"
        for row in changes
    )
    passed = bool(
        changes
        and only_targets_changed
        and all_expected
        and entity_count_same
        and geometry_identical
        and audit_ok
        and dimension_labels_only
        and shared_blocks_explicit
    )
    return {
        "engine": "text_edit_engine",
        "source": str(src),
        "output": str(dst),
        "changes": changes,
        "stat": {
            "entity_count_before": before_entity_count,
            "entity_count_after": after_entity_count,
            "entity_count_same": entity_count_same,
            "geometry_entity_count": len(after_geometry),
            "geometry_identical": geometry_identical,
            "changed_text_handles": sorted(changed_handles),
            "target_handles": sorted(used_handles),
        },
        "qa": {
            "audit_errors": len(audit.errors),
            "audit_fixes": len(audit.fixes),
            "audit_ok": audit_ok,
            "all_expected": all_expected,
            "only_targets_changed": only_targets_changed,
            "dimension_labels_only": dimension_labels_only,
            "shared_blocks_explicit": shared_blocks_explicit,
            "passed": passed,
        },
        "ok": passed,
    }


if __name__ == "__main__":
    import json
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "input" / "25QYL-2SL_1to1_best.dwg"
    if target.suffix.lower() == ".dxf":
        inspection = inspect_doc(ezdxf.readfile(str(target)), limit=40)
        print(json.dumps(inspection, ensure_ascii=False, indent=2))
