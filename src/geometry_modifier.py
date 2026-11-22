"""受控几何变形引擎（档案驱动）。

规则逻辑（风罩顶/接线盒/轴伸怎么动）是领域算法，留在代码里；
每张图的坐标、定标、handle、排除区、标注位置全部来自
``knowledge/drawings/<profile>.profile.json``。

三条安全边界：
1. 档案指纹不匹配 -> 拒绝执行（绝不把 TD50 常量套到别的图上）；
2. 源图不是原始基线 -> 拒绝执行（防止在已改过的图上二次变形）；
3. 改完重新打开成果文件，独立复测 H2/D2/轴伸并与目标比对。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import runtime

runtime.bootstrap()

import ezdxf

import drawing_measure
from drawing_profile import (
    DrawingProfile,
    DrawingProfileError,
    ProfileMismatch,
    resolve_for,
)


class SourceNotBaseline(DrawingProfileError):
    """源图已经是修改后的版本，继续变形会二次位移。"""


def esc(text: str) -> str:
    return "".join(ch if 32 <= ord(ch) < 127 else f"\\U+{ord(ch):04X}" for ch in text)


def unesc(text: str) -> str:
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text or "")


def point_tuple(entity):
    try:
        if entity.dxftype() == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            return (start.x, start.y, end.x, end.y)
        if entity.dxftype() in ("ARC", "CIRCLE"):
            center = entity.dxf.center
            return (center.x, center.y, entity.dxf.radius,
                    getattr(entity.dxf, "start_angle", 0),
                    getattr(entity.dxf, "end_angle", 0))
    except Exception:  # noqa: BLE001
        pass
    return None


def bbox(entity):
    point = point_tuple(entity)
    if point is None:
        return None
    if entity.dxftype() == "LINE":
        return (min(point[0], point[2]), max(point[0], point[2]),
                min(point[1], point[3]), max(point[1], point[3]))
    radius = point[2]
    return (point[0] - radius, point[0] + radius, point[1] - radius, point[1] + radius)


def shift_entity(entity, dx: float = 0.0, dy: float = 0.0) -> None:
    """通用实体平移（不用 ezdxf.transform，避免代理/扩展数据丢失）。"""
    kind = entity.dxftype()
    if kind == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        entity.dxf.start = (start.x + dx, start.y + dy)
        entity.dxf.end = (end.x + dx, end.y + dy)
    elif kind in ("ARC", "CIRCLE"):
        center = entity.dxf.center
        entity.dxf.center = (center.x + dx, center.y + dy)
    elif kind == "LWPOLYLINE":
        entity.points = [(p[0] + dx, p[1] + dy) + tuple(p[2:])
                         for p in entity.get_points("xyseb")]
    elif kind in ("TEXT", "MTEXT", "INSERT", "ATTDEF"):
        insert = entity.dxf.insert
        entity.dxf.insert = (insert.x + dx, insert.y + dy)
    elif kind == "SPLINE":
        entity.control_points = [(p.x + dx, p.y + dy, p.z) for p in entity.control_points]


def original_text_tokens(doc) -> list[str]:
    return [unesc(str(getattr(entity.dxf, "text", "")))
            for entity in doc.modelspace()
            if entity.dxftype() in ("TEXT", "MTEXT")]


def _resolve_profile(doc, profile, source_sha256, source_name) -> tuple[DrawingProfile, dict]:
    if profile is None:
        profile, report = resolve_for(doc, source_sha256, source_name)
        if profile is None:
            raise ProfileMismatch("<none>", report)
        return profile, report
    return profile, profile.require(doc, source_sha256, source_name)


def _guard_baseline(doc, profile: DrawingProfile) -> dict[str, Any]:
    measured = drawing_measure.measure(doc, profile)
    original = profile.original
    problems = []
    if measured["H2"]["mm"] is not None and abs(measured["H2"]["mm"] - float(original["H2"])) > 1.0:
        problems.append(f"H2 实测 {measured['H2']['mm']}mm，原值 {original['H2']}mm")
    if measured["D2"]["mm"] is not None and abs(measured["D2"]["mm"] - float(original["D2"])) > 1.0:
        problems.append(f"D2 实测 {measured['D2']['mm']}mm，原值 {original['D2']}mm")
    if measured["shaft"]["extend_mm"] is not None and abs(measured["shaft"]["extend_mm"]) > 1.0:
        problems.append(f"轴伸实测已加长 {measured['shaft']['extend_mm']}mm")
    if problems:
        raise SourceNotBaseline(
            "源图不是原始基线（可能已是修改后的文件），继续变形会二次位移："
            + "；".join(problems)
            + "。请用原始 DWG 重新建任务。"
        )
    return measured


def apply(src: str | Path, dst: str | Path, annotate: bool = False,
          targets: dict | None = None, enabled: set | None = None,
          profile: DrawingProfile | None = None,
          source_sha256: str | None = None,
          source_name: str | None = None,
          allow_modified_source: bool = False) -> dict:
    """受控几何变形。

    annotate=False（交付）：只改几何，保持原图黑白样式。
    annotate=True（对照展示）：额外叠加参考图风格彩色标注。
    targets：{"H2":450,"D2":290,"shaft":15}；未提供的键用档案 defaults。
    enabled：本次执行 {"H2","D2","shaft"}；None 表示全做。
    """
    src, dst = Path(src), Path(dst)
    doc = ezdxf.readfile(str(src))
    profile, match_report = _resolve_profile(doc, profile, source_sha256, source_name)
    before_measure = (drawing_measure.measure(doc, profile) if allow_modified_source
                      else _guard_baseline(doc, profile))

    msp = doc.modelspace()
    entities = list(msp)
    before_count = len(entities)
    before_snap = {entity.dxf.handle: point_tuple(entity) for entity in entities}
    before_tokens = original_text_tokens(doc)
    changes: list[dict] = []

    defaults = profile.data.get("defaults", {})
    targets = dict(targets or {})
    target_h2 = float(targets.get("H2", defaults.get("H2", profile.original["H2"])))
    target_d2 = float(targets.get("D2", defaults.get("D2", profile.original["D2"])))
    target_shaft = float(targets.get("shaft", defaults.get("shaft", profile.original["shaft_plus"])))
    enabled = {"H2", "D2", "shaft"} if enabled is None else set(enabled)

    k_v = profile.units_per_mm("y")
    k_h = profile.units_per_mm("x")
    original_h2 = float(profile.original["H2"])
    lower_segments = float(profile.original["lower_segments"])
    base_total = float(profile.original["total_height"])
    hood_top_y = profile.datum("hood_top_y")
    flange_y = profile.datum("flange_y")
    axis_x = profile.datum("axis_x")
    box_outer_x = profile.datum("box_outer_x")
    shaft_bottom_y = profile.datum("shaft_bottom_y")

    delta_h2 = (target_h2 - original_h2) * k_v
    shift_d2 = target_d2 * k_h - (box_outer_x - axis_x)
    extend_shaft = target_shaft * k_v
    new_hood_top = hood_top_y + delta_h2
    new_box_outer = box_outer_x + shift_d2
    new_total_mm = target_h2 + lower_segments
    total_changed = abs(new_total_mm - base_total) > 1e-6

    # ===== ① H2：风罩顶帽整体 + 两侧壁上端 =====
    h2_rule = profile.rule("H2")
    h2_cfg = h2_rule["region"]
    excluded_h2 = set(h2_cfg.get("exclude_handles", []))
    clip_y = float(h2_cfg["clip_threshold_y"])
    hood_count = 0
    for entity in (entities if "H2" in enabled else []):
        if entity.dxftype() not in ("LINE", "ARC", "CIRCLE"):
            continue
        if entity.dxf.handle in excluded_h2:
            continue
        point = point_tuple(entity)
        bb = bbox(entity)
        if bb is None or bb[1] < float(h2_cfg["x"][0]) or bb[0] > float(h2_cfg["x"][1]):
            continue
        if bb[3] < float(h2_cfg["min_ymax"]):
            continue
        if bb[2] > float(h2_cfg["max_ymin"]):
            continue
        if bb[3] - bb[2] > float(h2_cfg["max_yspan"]):
            continue
        if bb[1] - bb[0] > float(h2_cfg["max_xspan"]):
            continue
        old = point
        if entity.dxftype() == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            new_start = (start.x, start.y + delta_h2) if start.y >= clip_y else (start.x, start.y)
            new_end = (end.x, end.y + delta_h2) if end.y >= clip_y else (end.x, end.y)
            entity.dxf.start, entity.dxf.end = new_start, new_end
        else:
            shift_entity(entity, dy=delta_h2)
        hood_count += 1
        changes.append({"rule": "H2", "handle": entity.dxf.handle,
                        "op": "风罩顶线/侧壁整体变换",
                        "before": old, "after": point_tuple(entity)})

    # ---- 顶帽引出线等伴随实体随动 ----
    if "H2" in enabled and abs(delta_h2) > 1e-9:
        for handle in h2_cfg.get("companion_handles", []):
            entity = doc.entitydb.get(handle)
            if entity is None or entity.dxftype() != "LINE":
                continue
            old = point_tuple(entity)
            start, end = entity.dxf.start, entity.dxf.end
            entity.dxf.start = (start.x, start.y + delta_h2)
            entity.dxf.end = (end.x, end.y + delta_h2)
            hood_count += 1
            changes.append({"rule": "H2", "handle": handle,
                            "op": "顶帽引出线随动下移(修复悬空)",
                            "before": old, "after": point_tuple(entity)})

    # ===== ② D2：接线盒外盖 X 外移 + 连接臂右端拉伸 =====
    d2_cfg = profile.rule("D2")["region"]
    excluded_boxes = [tuple(float(v) for v in box) for box in d2_cfg.get("exclude_bboxes", [])]
    endpoint_x_min = float(d2_cfg["endpoint_x_min"])
    cable_cfg = d2_cfg.get("cable_neck", {})
    box_count = 0
    arm_count = 0
    for entity in (entities if "D2" in enabled else []):
        if entity.dxftype() not in ("LINE", "ARC", "CIRCLE"):
            continue
        point = point_tuple(entity)
        bb = bbox(entity)
        if bb is None or bb[3] < float(d2_cfg["y"][0]) or bb[2] > float(d2_cfg["y"][1]):
            continue
        if bb[0] > float(d2_cfg["x_max"]) or bb[1] < float(d2_cfg["x_min"]):
            continue
        if bb[1] - bb[0] > float(d2_cfg["max_xspan"]):
            continue
        if entity.dxftype() == "LINE":
            center_x = (entity.dxf.start.x + entity.dxf.end.x) / 2
            center_y = (entity.dxf.start.y + entity.dxf.end.y) / 2
        else:
            center_x, center_y = entity.dxf.center.x, entity.dxf.center.y
        if any(box[0] <= center_x <= box[1] and box[2] <= center_y <= box[3]
               for box in excluded_boxes):
            continue
        old = point
        cable_neck = (
            bb[0] >= float(cable_cfg.get("bbox_x_min", 1e18))
            and bb[2] >= float(cable_cfg.get("bbox_ymin_min", 1e18))
        )
        if entity.dxftype() == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            move_both = cable_neck or (start.x >= endpoint_x_min and end.x >= endpoint_x_min)
            new_start = ((start.x + shift_d2, start.y)
                         if (move_both or start.x >= endpoint_x_min) else (start.x, start.y))
            new_end = ((end.x + shift_d2, end.y)
                       if (move_both or end.x >= endpoint_x_min) else (end.x, end.y))
            if new_start != (start.x, start.y) or new_end != (end.x, end.y):
                entity.dxf.start, entity.dxf.end = new_start, new_end
                if new_start[0] != start.x and new_end[0] != end.x:
                    box_count += 1
                    op = "外盖/电缆接头整体X平移"
                else:
                    arm_count += 1
                    op = "连接臂右端拉伸"
                changes.append({"rule": "D2", "handle": entity.dxf.handle, "op": op,
                                "before": old, "after": point_tuple(entity)})
        else:
            center = entity.dxf.center
            if center.x >= endpoint_x_min or cable_neck:
                shift_entity(entity, dx=shift_d2)
                box_count += 1
                changes.append({"rule": "D2", "handle": entity.dxf.handle,
                                "op": "电缆接头圆弧X平移",
                                "before": old, "after": point_tuple(entity)})

    # ===== ③ 轴伸：轴端矩形下伸 + 键槽随动 =====
    shaft_rule = profile.rule("shaft")
    shaft_count = 0
    by_handle = {entity.dxf.handle: entity for entity in entities}
    if "shaft" in enabled:
        entity = by_handle.get(shaft_rule["bottom_handle"])
        if entity is not None and entity.dxftype() == "LINE":
            old = point_tuple(entity)
            shift_entity(entity, dy=-extend_shaft)
            shaft_count += 1
            changes.append({"rule": "SHAFT", "handle": entity.dxf.handle,
                            "op": "轴端底边下移", "before": old,
                            "after": point_tuple(entity)})
        for handle in shaft_rule.get("side_handles", []):
            entity = by_handle.get(handle)
            if entity is None or entity.dxftype() != "LINE":
                continue
            old = point_tuple(entity)
            start, end = entity.dxf.start, entity.dxf.end
            new_start = ((start.x, start.y - extend_shaft)
                         if abs(start.y - shaft_bottom_y) < 1 else (start.x, start.y))
            new_end = ((end.x, end.y - extend_shaft)
                       if abs(end.y - shaft_bottom_y) < 1 else (end.x, end.y))
            entity.dxf.start, entity.dxf.end = new_start, new_end
            shaft_count += 1
            changes.append({"rule": "SHAFT", "handle": handle,
                            "op": "轴端侧边下端拉伸", "before": old,
                            "after": point_tuple(entity)})
        keyway = [float(v) for v in shaft_rule["keyway_box"]]
        for entity in entities:
            if entity.dxftype() not in ("LINE", "ARC", "CIRCLE"):
                continue
            bb = bbox(entity)
            if bb is None or bb[0] < keyway[0] or bb[1] > keyway[1]:
                continue
            if bb[2] < keyway[2] or bb[3] > keyway[3]:
                continue
            old = point_tuple(entity)
            shift_entity(entity, dy=-extend_shaft)
            shaft_count += 1
            changes.append({"rule": "SHAFT", "handle": entity.dxf.handle,
                            "op": "键槽随轴端下移", "before": old,
                            "after": point_tuple(entity)})

    # ===== 总高联动：原图黑色尺寸数字随 H2 改写 =====
    total_dim_handle = None
    total_line_handle = None
    if "H2" in enabled:
        link = h2_rule.get("total_height_link", {})
        token = str(link.get("text_contains", ""))
        line_x = float(link.get("line_x", 0.0))
        line_tol = float(link.get("line_x_tolerance", 2.0))
        min_top = float(link.get("line_min_top_y", 0.0))
        if token and total_changed:
            for entity in entities:
                kind = entity.dxftype()
                if kind in ("MTEXT", "TEXT") and token in unesc(str(entity.dxf.text)):
                    old_text = str(entity.dxf.text)
                    entity.dxf.text = re.sub(re.escape(token), f"{new_total_mm:g}", old_text)
                    total_dim_handle = entity.dxf.handle
                    changes.append({"rule": "H2", "handle": entity.dxf.handle,
                                    "op": f"总高尺寸 {token}→{new_total_mm:g}",
                                    "before": old_text, "after": entity.dxf.text})
                if (kind == "LINE" and abs(entity.dxf.start.x - line_x) < line_tol
                        and abs(entity.dxf.end.x - line_x) < line_tol):
                    start, end = entity.dxf.start, entity.dxf.end
                    if max(start.y, end.y) > min_top:
                        hi, lo = (start, end) if start.y > end.y else (end, start)
                        entity.dxf.start = (hi.x, hi.y + delta_h2)
                        entity.dxf.end = (lo.x, lo.y)
                        total_line_handle = entity.dxf.handle
                        changes.append({"rule": "H2", "handle": entity.dxf.handle,
                                        "op": "总高尺寸线顶端随动",
                                        "before": None, "after": point_tuple(entity)})

    # ===== 对照图彩色标注（绝不进交付 DWG）=====
    annotation_handles: list[str] = []
    if annotate:
        annotation_handles = _draw_annotations(doc, msp, profile, enabled,
                                               target_h2, target_d2, target_shaft,
                                               flange_y, new_hood_top, axis_x,
                                               new_box_outer, shaft_bottom_y,
                                               extend_shaft)

    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dst))

    after = ezdxf.readfile(str(dst))
    audit = after.audit()
    after_tokens = original_text_tokens(after)
    kept = {text: (before_tokens.count(text), after_tokens.count(text))
            for text in profile.protected_texts}

    final = dst.with_name(dst.stem + "_最终.dxf")
    removed = _polish_and_save(dst, final)
    final_audit = ezdxf.readfile(str(final)).audit()
    final_doc = ezdxf.readfile(str(final))
    measured_after = drawing_measure.measure(final_doc, profile)
    verification = drawing_measure.verify(
        measured_after,
        {"H2": target_h2, "D2": target_d2, "shaft": target_shaft},
        enabled,
    )

    expected = {change["handle"] for change in changes}
    new_handles = set(annotation_handles)
    after_by_handle = {entity.dxf.handle: entity for entity in after.modelspace()}
    unexpected, deleted = [], []
    for handle, old_point in before_snap.items():
        if handle in expected or handle in new_handles:
            continue
        entity = after_by_handle.get(handle)
        if entity is None:
            deleted.append(handle)
        elif point_tuple(entity) != old_point:
            unexpected.append(handle)

    report = {
        "ok": True,
        "engine": "geometry_modifier/profile",
        "profile_id": profile.profile_id,
        "profile_match": {k: match_report.get(k) for k in ("ok", "score", "problems")},
        "scale": {
            "vertical_unit_per_mm": k_v,
            "horizontal_unit_per_mm": k_h,
            "basis_v": profile.data["calibration"]["vertical"].get("basis", ""),
            "basis_h": profile.data["calibration"]["horizontal"].get("formula", ""),
        },
        "entities_before": before_count,
        "entities_after": len(list(after.modelspace())),
        "audit_errors": len(audit.errors),
        "audit_fixes": len(audit.fixes),
        "final_audit_errors": len(final_audit.errors),
        "final_audit_fixes": len(final_audit.fixes),
        "watermark_removed": removed,
        "targets_used": {"H2": target_h2, "D2": target_d2,
                         "shaft": target_shaft, "enabled": sorted(enabled)},
        "measured_before": {k: before_measure[k] for k in ("H2", "D2", "shaft")},
        "measured_after": {k: measured_after[k] for k in ("H2", "D2", "shaft")},
        "verification": verification,
        "H2": {"target_mm": target_h2, "original_mm": original_h2,
               "flange_y": flange_y, "old_top_y": hood_top_y,
               "new_top_y": round(new_hood_top, 2), "shift_unit": round(delta_h2, 2),
               "entities_moved": hood_count,
               "actual_h2_mm": measured_after["H2"]["mm"]},
        "TOTAL_HEIGHT": {"original_mm": base_total, "new_mm": round(new_total_mm, 2),
                         "formula": f"H2({target_h2:g}) + {lower_segments:g} = {new_total_mm:g}",
                         "dim_text_handle": total_dim_handle,
                         "dim_line_handle": total_line_handle,
                         "changed": bool(total_changed)},
        "D2": {"target_mm": target_d2, "axis_x": axis_x, "old_outer_x": box_outer_x,
               "new_outer_x": round(new_box_outer, 2), "shift_unit": round(shift_d2, 2),
               "cover_entities_moved": box_count, "arm_entities_stretched": arm_count,
               "actual_d2_mm": measured_after["D2"]["mm"]},
        "SHAFT": {"target": f"E+{target_shaft:g}mm",
                  "extend_unit": round(extend_shaft, 2),
                  "old_end_y": shaft_bottom_y,
                  "new_end_y": round(shaft_bottom_y - extend_shaft, 2),
                  "entities_moved": shaft_count,
                  "actual_extend_mm": measured_after["shaft"]["extend_mm"]},
        "original_dimensions_kept": kept,
        "unexpected_modified": len(unexpected),
        "unexpected_handles": unexpected[:30],
        "deleted_entities": len(deleted),
        "deleted_handles": deleted[:30],
        "new_annotation_handles": annotation_handles,
        "changes": changes,
        "note": "几何按档案区域变形；范围外 handle 级零改动；改后已从成果文件独立复测。",
    }
    (dst.parent / "geometry_change_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _polish_and_save(src: Path, final: Path) -> int:
    """去评估水印、修 aspose 遗留的无效 owner/字典键，输出交付 DXF。"""
    doc = ezdxf.readfile(str(src))
    removed = 0
    for entity in list(doc.modelspace()):
        layer = str(entity.dxf.get("layer", "") or "").lower()
        if "watermark" in layer:
            doc.modelspace().delete_entity(entity)
            removed += 1
    if "Annotative" not in doc.styles:
        doc.styles.add("Annotative", font="txt")
    visual_style = doc.rootdict.get("ACAD_VISUALSTYLE")
    if visual_style is not None:
        for key in list(visual_style.keys()):
            visual_style.remove(key)
    for layout in [doc.modelspace()] + list(doc.layouts):
        for entity in list(layout):
            if str(entity.dxf.get("owner", "")) in ("0", "", None):
                layout.delete_entity(entity)
    for entity in list(doc.objects):
        if entity.dxftype() == "axaDraft Document" and str(entity.dxf.get("owner", "")) == "0":
            doc.objects.delete_entity(entity)
    doc.saveas(str(final))
    return removed


def _draw_annotations(doc, msp, profile: DrawingProfile, enabled: set[str],
                      target_h2: float, target_d2: float, target_shaft: float,
                      flange_y: float, new_hood_top: float, axis_x: float,
                      new_box_outer: float, shaft_bottom_y: float,
                      extend_shaft: float) -> list[str]:
    cfg = profile.annotation
    k_v = float(profile.units_per_mm("y"))
    layers = cfg.get("layers", {})
    for item in layers.values():
        if item["name"] not in doc.layers:
            doc.layers.add(item["name"], color=int(item["color"]))

    created: list[str] = []
    line_weight = 50

    def add_text(text: str, x: float, y: float, height: float, layer: str,
                 color: int, rotation: float = 0, align: str | None = None) -> str:
        entity = msp.add_text(esc(text), height=height,
                              dxfattribs={"layer": layer, "color": color,
                                          "rotation": rotation})
        if align == "center":
            entity.set_placement((x, y), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
        else:
            entity.set_placement((x, y))
        return entity.dxf.handle

    def triangle(points, layer: str, color: int) -> str:
        return msp.add_solid(points, dxfattribs={"layer": layer, "color": color}).dxf.handle

    def arrow_up(x: float, y_tip: float, size: float, layer: str, color: int) -> str:
        return triangle([(x - size, y_tip - size * 1.7), (x + size, y_tip - size * 1.7),
                         (x, y_tip)], layer, color)

    def arrow_down(x: float, y_tip: float, size: float, layer: str, color: int) -> str:
        return triangle([(x - size, y_tip + size * 1.7), (x + size, y_tip + size * 1.7),
                         (x, y_tip)], layer, color)

    def arrow_left(x_tip: float, y: float, size: float, layer: str, color: int) -> str:
        return triangle([(x_tip + size * 1.7, y + size), (x_tip + size * 1.7, y - size),
                         (x_tip, y)], layer, color)

    def arrow_right(x_tip: float, y: float, size: float, layer: str, color: int) -> str:
        return triangle([(x_tip - size * 1.7, y + size), (x_tip - size * 1.7, y - size),
                         (x_tip, y)], layer, color)

    def add_line(start, end, layer: str, color: int) -> str:
        return msp.add_line(start, end, dxfattribs={
            "layer": layer, "color": color, "lineweight": line_weight}).dxf.handle

    if "H2" in enabled:
        item = cfg["H2"]
        name, color = layers["H2"]["name"], int(layers["H2"]["color"])
        xb = float(item["dim_x"])
        tick = float(item["tick"])
        h2_ann_top = float(item.get("annotation_top_y") or new_hood_top)
        h2_ann_bottom = float(item.get("annotation_bottom_y")
                              or (h2_ann_top - float(target_h2) * k_v))
        created.append(add_line((xb, h2_ann_bottom), (xb, h2_ann_top), name, color))
        created.append(arrow_up(xb, h2_ann_top, float(item["arrow_size"]), name, color))
        created.append(arrow_down(xb, h2_ann_bottom, float(item["arrow_size"]), name, color))
        for p1, p2 in [((xb, h2_ann_top), (xb + tick, h2_ann_top)),
                       ((xb, h2_ann_bottom), (xb + tick, h2_ann_bottom))]:
            created.append(add_line(p1, p2, name, color))
        created.append(add_text(
            f"① H2 = {target_h2:g} mm",
            xb + float(item["text_dx"]),
            (h2_ann_bottom + h2_ann_top) / 2 + float(item["text_dy"]),
            float(item["text_height"]), name, color, rotation=90))

    if "D2" in enabled:
        item = cfg["D2"]
        name, color = layers["D2"]["name"], int(layers["D2"]["color"])
        h2_ann_top = float(cfg.get("H2", {}).get("annotation_top_y") or new_hood_top)
        ya = float(item.get("y_line")
                   or (h2_ann_top + float(item.get("y_offset", 0.0))))
        created.append(add_line((axis_x, ya), (new_box_outer, ya), name, color))
        created.append(arrow_left(axis_x, ya, float(item["arrow_size"]), name, color))
        created.append(add_line((new_box_outer, ya),
                                (new_box_outer, float(item["box_bottom_y"])), name, color))
        created.append(arrow_up(new_box_outer, ya + 30, float(item["arrow_up_size"]), name, color))
        created.append(add_text(
            f"② D2 = {target_d2:g} mm",
            float(item.get("text_x", new_box_outer + 90.0)),
            float(item.get("text_y", ya + 24.0)),
            float(item["text_height"]), name, color))

    if "shaft" in enabled:
        item = cfg["shaft"]
        name, color = layers["shaft"]["name"], int(layers["shaft"]["color"])
        frame = [tuple(point) for point in item["frame"]]
        for p1, p2 in zip(frame, frame[1:]):
            created.append(add_line(p1, p2, name, color))
        for row_y, row_height, row_text in item["text_rows"]:
            created.append(add_text(row_text.format(target=target_shaft),
                                    float(item["text_x"]), float(row_y),
                                    float(row_height), name, color))
        new_y = shaft_bottom_y - extend_shaft
        start = tuple(item["leader_from"])
        created.append(add_line(start, (float(item["leader_to_x"]),
                                        new_y + float(item["leader_to_dy"])), name, color))
        created.append(arrow_right(float(item["arrow_x"]), new_y + 0.5,
                                   float(item["arrow_size"]), name, color))

    return created


if __name__ == "__main__":
    import sys

    source = sys.argv[1] if len(sys.argv) > 1 else "data/tasks/d663f685b4b9/clean.dxf"
    output = sys.argv[2] if len(sys.argv) > 2 else "output/_engine_profile_check.dxf"
    result = apply(source, output)
    print(json.dumps({key: value for key, value in result.items() if key != "changes"},
                     ensure_ascii=False, indent=2))
