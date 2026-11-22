"""新图纸定标：扫描候选基准并生成档案骨架。

这里刻意不做"自动猜坐标"：几何语义（哪条线是法兰面、哪个 handle 是风罩顶）
必须由人确认。脚本负责把候选尺寸文字、DIMENSION、长线、圆/弧和 bbox 摆出来，
避免工程师从头翻 CAD 找 handle。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import runtime

runtime.bootstrap()

import ezdxf


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_text(text: str) -> str:
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})",
                  lambda match: chr(int(match.group(1), 16)), text or "")
    text = text.replace("\\P", " ")
    text = re.sub(r"\\[A-Za-z][^;\\{}]{0,30};", "", text)
    text = re.sub(r"\\[{}]", "", text)
    return text.strip()


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(float(value), digits) if value is not None else None


def scan_drawing(doc, limit: int = 80) -> dict[str, Any]:
    msp = doc.modelspace()
    from ezdxf import bbox as _bbox

    ext = _bbox.extents(msp)
    types: dict[str, int] = {}
    text_candidates = []
    block_text_candidates = []
    dimensions = []
    lines = []
    circles = []

    for entity in msp:
        dxftype = entity.dxftype()
        types[dxftype] = types.get(dxftype, 0) + 1
        if dxftype in ("TEXT", "MTEXT"):
            raw = str(entity.dxf.get("text", ""))
            plain = _plain_text(raw)
            numbers = re.findall(r"\d+(?:\.\d+)?", plain)
            if not numbers:
                continue
            insert = entity.dxf.insert
            text_candidates.append({
                "handle": entity.dxf.handle,
                "type": dxftype,
                "layer": entity.dxf.layer,
                "text": plain,
                "raw": raw,
                "numbers": numbers,
                "insert": [_round(insert.x), _round(insert.y)],
                "height": _round(
                    entity.dxf.get("height", 0) if dxftype == "TEXT"
                    else entity.dxf.get("char_height", 0)),
                "rotation": _round(getattr(entity.dxf, "rotation", 0)),
            })
        elif dxftype == "DIMENSION":
            try:
                measurement = entity.get_measurement()
            except Exception:  # noqa: BLE001
                measurement = None
            dimensions.append({
                "handle": entity.dxf.handle,
                "layer": entity.dxf.layer,
                "text": _plain_text(str(entity.dxf.get("text", ""))),
                "measurement": _round(measurement),
                "defpoint": [_round(entity.dxf.defpoint.x),
                             _round(entity.dxf.defpoint.y)],
            })
        elif dxftype == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            length = ((end.x - start.x) ** 2 + (end.y - start.y) ** 2) ** 0.5
            if length < 1e-6:
                continue
            dx, dy = end.x - start.x, end.y - start.y
            if abs(dy) <= abs(dx) * 1e-3:
                orientation = "horizontal"
            elif abs(dx) <= abs(dy) * 1e-3:
                orientation = "vertical"
            else:
                orientation = "oblique"
            lines.append({
                "handle": entity.dxf.handle,
                "layer": entity.dxf.layer,
                "orientation": orientation,
                "length": _round(length),
                "start": [_round(start.x), _round(start.y)],
                "end": [_round(end.x), _round(end.y)],
            })
        elif dxftype in ("CIRCLE", "ARC"):
            center = entity.dxf.center
            circles.append({
                "handle": entity.dxf.handle,
                "type": dxftype,
                "layer": entity.dxf.layer,
                "center": [_round(center.x), _round(center.y)],
                "radius": _round(entity.dxf.radius),
            })

    lines.sort(key=lambda item: item["length"] or 0, reverse=True)
    circles.sort(key=lambda item: item["radius"] or 0, reverse=True)
    dimensions.sort(key=lambda item: item["measurement"] or 0, reverse=True)
    def first_number(item) -> float:
        try:
            return abs(float(item["numbers"][0]))
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    text_candidates.sort(key=first_number, reverse=True)

    # 很多国产 CAD 图把尺寸文字放在块里；模型空间直接扫不到，必须展开 INSERT。
    def collect_block_texts(insert, path: str, depth: int = 0) -> None:
        if depth > 5 or len(block_text_candidates) >= limit:
            return
        try:
            virtual = list(insert.virtual_entities())
        except Exception:  # noqa: BLE001
            return
        for entity in virtual:
            if len(block_text_candidates) >= limit:
                return
            dxftype = entity.dxftype()
            if dxftype in ("TEXT", "MTEXT"):
                raw = str(entity.dxf.get("text", ""))
                plain = _plain_text(raw)
                numbers = re.findall(r"\d+(?:\.\d+)?", plain)
                if not numbers:
                    continue
                insert_point = entity.dxf.insert
                block_text_candidates.append({
                    "handle": entity.dxf.handle,
                    "insert_handle": insert.dxf.handle,
                    "path": path,
                    "type": dxftype,
                    "layer": entity.dxf.layer,
                    "text": plain,
                    "raw": raw,
                    "numbers": numbers,
                    "insert": [_round(insert_point.x), _round(insert_point.y)],
                    "height": _round(
                        entity.dxf.get("height", 0) if dxftype == "TEXT"
                        else entity.dxf.get("char_height", 0)),
                    "rotation": _round(getattr(entity.dxf, "rotation", 0)),
                })
            elif dxftype == "INSERT":
                collect_block_texts(entity, f"{path}/{entity.dxf.name}", depth + 1)

    for insert in msp.query("INSERT"):
        collect_block_texts(insert, insert.dxf.name)

    return {
        "entity_count": len(list(msp)),
        "entity_types": types,
        "bbox": {
            "min": [_round(ext.extmin.x, 1), _round(ext.extmin.y, 1)],
            "max": [_round(ext.extmax.x, 1), _round(ext.extmax.y, 1)],
        },
        "text_candidates": text_candidates[:limit],
        "block_text_candidates": block_text_candidates,
        "dimensions": dimensions[:limit],
        "longest_lines": lines[:limit],
        "largest_circles": circles[:limit],
    }


def build_skeleton(doc, *, profile_id: str, model: str, title: str,
                   source_file: str, source_sha256: str,
                   scan: dict[str, Any] | None = None) -> dict[str, Any]:
    scan = scan or scan_drawing(doc)
    bbox = scan["bbox"]
    count = int(scan["entity_count"])
    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "model": model,
        "title": title,
        "source_file": source_file,
        "status": "draft",
        "notes": "由 calibration.py 生成的骨架。必须人工确认锚点与定标后才能 install。",
        "review": {
            "status": "draft",
            "confirmed_by": None,
            "confirmed_at": None,
        },
        "fingerprint": {
            "sha256": source_sha256,
            "filenames": [source_file],
            "required_handles": [],
            "markers": [],
            "entity_count": [max(0, count - 30), count + 30],
            "bbox": {
                "min": bbox["min"],
                "max": bbox["max"],
                "tolerance": 5.0,
            },
        },
        "calibration": {
            "vertical": {
                "units_per_mm": None,
                "formula": "TODO",
                "datum_from": "hood_top_y",
                "datum_to": "foundation_top_y",
                "reference_mm": None,
                "basis": "TODO：用两条已知尺寸线反算，禁止用外廓目测",
            },
            "horizontal": {
                "units_per_mm": None,
                "formula": "TODO",
                "cross_check": {
                    "datum_from": "axis_x",
                    "datum_to": "box_outer_x",
                    "reference_mm": None,
                    "note": "TODO",
                },
            },
        },
        "datums": {
            "hood_top_y": None,
            "flange_y": None,
            "foundation_top_y": None,
            "axis_x": None,
            "box_outer_x": None,
            "box_bottom_y": None,
            "shaft_bottom_y": None,
            "shaft_top_y": None,
            "keyway_box": None,
        },
        "original": {
            "H2": None,
            "D2": None,
            "shaft_plus": None,
            "total_height": None,
            "lower_segments": None,
        },
        "defaults": {
            "H2": None,
            "D2": None,
            "shaft": None,
        },
        "protected_texts": [],
        "rules": {
            "H2": {
                "label": "风罩总高",
                "region": {
                    "x": [None, None],
                    "min_ymax": None,
                    "max_ymin": None,
                    "max_yspan": None,
                    "max_xspan": None,
                    "exclude_handles": [],
                    "companion_handles": [],
                },
                "measure_handle": None,
            },
            "D2": {
                "label": "接线盒径向伸出",
                "region": {
                    "y": [None, None],
                    "x_max": None,
                    "x_min": None,
                    "max_xspan": None,
                    "endpoint_x_min": None,
                    "exclude_bboxes": [],
                },
                "measure_handle": None,
            },
            "shaft": {
                "label": "轴伸加长",
                "bottom_handle": None,
                "side_handles": [],
                "keyway_box": None,
            },
        },
        "annotation": {},
        "compare_view": {
            "zoom_viewport": None,
        },
        "evidence": {
            "text_candidates": scan["text_candidates"],
            "block_text_candidates": scan.get("block_text_candidates", []),
            "dimensions": scan["dimensions"],
            "longest_lines": scan["longest_lines"],
            "largest_circles": scan["largest_circles"],
        },
        "_instructions": [
            "从 evidence.text_candidates / dimensions 中确认 H2、D2、轴伸对应的尺寸线与 handle。",
            "用两条已知尺寸反算 calibration.vertical/horizontal.units_per_mm，不要用外廓目测。",
            "填写 datums 与 original 真值，再填 rules 的区域/排除 handle/measure_handle。",
            "填写 fingerprint.required_handles（至少 3 个）和 markers，防止套错图纸。",
            "把 review.status 改为 confirmed 并填写确认人，然后执行 `python cli.py profiles install <file>`。",
        ],
    }


_REQUIRED_DATUMS = (
    "hood_top_y", "flange_y", "axis_x", "box_outer_x", "shaft_bottom_y",
)


def readiness_problems(data: dict[str, Any]) -> list[str]:
    """检查档案是否已达到可安装状态；返回问题列表。"""
    problems = []
    review = data.get("review", {})
    if review.get("status") != "confirmed":
        problems.append("review.status 必须为 confirmed")
    if not review.get("confirmed_by"):
        problems.append("review.confirmed_by 必须填写")
    fingerprint = data.get("fingerprint", {})
    if len(fingerprint.get("required_handles") or []) < 3:
        problems.append("fingerprint.required_handles 至少 3 个")
    if not fingerprint.get("markers"):
        problems.append("fingerprint.markers 至少 1 个")
    for axis in ("vertical", "horizontal"):
        value = (data.get("calibration", {}).get(axis) or {}).get("units_per_mm")
        if not isinstance(value, (int, float)) or value <= 0:
            problems.append(f"calibration.{axis}.units_per_mm 必须是正数")
    for name in _REQUIRED_DATUMS:
        value = (data.get("datums") or {}).get(name)
        if not isinstance(value, (int, float)):
            problems.append(f"datums.{name} 必须是数字")
    for name in ("H2", "D2", "total_height", "lower_segments"):
        value = (data.get("original") or {}).get(name)
        if not isinstance(value, (int, float)):
            problems.append(f"original.{name} 必须是数字")
    h2_region = ((data.get("rules") or {}).get("H2") or {}).get("region") or {}
    if not all(isinstance(v, (int, float)) for v in h2_region.get("x") or []):
        problems.append("rules.H2.region.x 必须是两个数字")
    d2_region = ((data.get("rules") or {}).get("D2") or {}).get("region") or {}
    if not all(isinstance(v, (int, float)) for v in d2_region.get("y") or []):
        problems.append("rules.D2.region.y 必须是两个数字")
    keyway = ((data.get("rules") or {}).get("shaft") or {}).get("keyway_box")
    if not (isinstance(keyway, list) and len(keyway) == 4
            and all(isinstance(v, (int, float)) for v in keyway)):
        problems.append("rules.shaft.keyway_box 必须是 4 个数字")
    return problems


def dump_json(data: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
