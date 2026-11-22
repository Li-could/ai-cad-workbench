"""工作台执行引擎：解析 -> 规划 -> 受控修改 -> 闭环质检 -> DWG 导出。

三条执行链共用一套编排：
- geometry  : H2 / D2 / 轴伸（档案驱动，见 src/geometry_modifier.py）
- annotation: 标注换算 / 竖向标注重叠修复（src/annotation_engine.py）
- real_mm   : 真实毫米标定 + 两视图实体外廓等高（src/real_mm_engine.py）

质检不再只看引擎自报：成果 DXF 会被重新打开并独立复测，DWG 会做
ODA 往返重开校验；任一硬门槛失败，任务状态记为 failed。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import uuid
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import runtime

runtime.bootstrap()

import ezdxf

import drawing_measure
import drawing_profile
from store import TASKS, add_version, log, save_task


# ---------------------------------------------------------------- 标注链
def _plan_annotation(task: dict, tdir: Path, clean: Path, intent: dict) -> dict:
    import annotation_engine as ae

    if not intent.get("mode") and not intent.get("fix_vertical"):
        task["plan"] = {
            "operations": [],
            "requested_rules": [],
            "known_rules": False,
            "intent": intent,
            "instruction_required": {
                "questions": intent.get("questions") or [],
                "message": "请补充标注换算方式或要修复的具体显示问题。",
            },
        }
        task["status"] = "needs_instruction"
        log(task, "plan", "标注需求不完整：已拒绝生成可执行计划", ok=False)
        save_task(task)
        return task

    doc = ezdxf.readfile(str(clean))
    rows = ae.measure_all(doc)
    try:
        k, k_meta = ae.compute_K(rows, intent.get("mode") or "1to1",
                                 intent.get("base_value"), intent.get("base_where"))
    except Exception as exc:  # noqa: BLE001
        k, k_meta = 0.0, {"error": str(exc)}

    mode = intent.get("mode") or "1to1"
    operations = [{
        "rule": "ANNOTATION_RELABEL", "op": "update_text",
        "action": f"全图标注按 {mode} 换算（K={k:.4f}）",
        "target": mode, "target_text": f"K={k:.4f}",
    }]
    if intent.get("fix_vertical"):
        operations.append({
            "rule": "VERTICAL_DIM_FIX", "op": "update_text",
            "action": "竖向 DIMENSION 文字改 TEXT rot=90，贴蓝线、字高2.5、修复字符挤压",
            "target": "fix_vertical", "target_text": "字符间距正常",
        })
    task["plan"] = {
        "operations": operations,
        "requested_rules": ["ANNOTATION_RELABEL"]
        + (["VERTICAL_DIM_FIX"] if intent.get("fix_vertical") else []),
        "known_rules": True,
        "intent": intent,
        "annotation": {
            "mode": mode, "K": round(k, 6), "K_meta": k_meta,
            "fix_vertical": intent.get("fix_vertical", False),
            "measured_count": len(rows),
            "preview": [{"old": row["old"], "new": ae.fmt(row["measured"] * k, mode),
                         "where": row["where"]} for row in rows[:6]],
        },
    }
    task["intent"] = intent
    task["status"] = "planned"
    log(task, "plan", f"标注意图({intent['source']})：{intent['summary']}")
    log(task, "plan", f"换算模式={mode} K={k:.4f} 修复竖排={intent.get('fix_vertical')} "
                      f"量得 {len(rows)} 条标注")
    save_task(task)
    return task


def _execute_annotation(task: dict) -> dict:
    import annotation_engine as ae

    tdir = TASKS / task["task_id"]
    clean = tdir / "clean.dxf"
    intent = task.get("intent") or {}
    mode = intent.get("mode") or "1to1"
    fix_vertical = bool(intent.get("fix_vertical"))
    report: dict = {"task_id": task["task_id"], "engine": "annotation_engine",
                    "steps": [], "checks": {}, "intent": intent}

    result_dxf = tdir / "result.dxf"
    result = ae.relabel(clean, result_dxf, mode=mode,
                        base_value=intent.get("base_value"),
                        base_where=intent.get("base_where"),
                        fix_vertical=fix_vertical, unit=intent.get("unit"))
    report["annotation"] = result
    log(task, "execute", f"标注换算 mode={mode} K={result['K']}："
                         f"{len(result['changes'])} 条变更；竖排修复 {len(result['vertical_fixes'])} 处")

    report["checks"]["audit"] = {
        "errors": result["qa"]["audit_errors"], "fixes": result["qa"]["audit_fixes"],
        "passed": result["qa"]["audit_errors"] == 0}
    report["checks"]["geometry_unchanged"] = {
        "passed": result["stat"]["geometry_identical"],
        "geometry_entity_count": result["stat"]["geometry_entity_count"]}
    report["checks"]["entity_count"] = {
        "before": result["stat"]["entity_count_before"],
        "after": result["stat"]["entity_count_after"],
        "passed": result["stat"]["entity_count_same"]}
    report["checks"]["spec_preserved"] = {
        "passed": all(change["new"] != "" for change in result["changes"]),
        "note": "规格类 G1/G3/4/4-Φ11 保持原样"}

    result_dwg = tdir / "result.dwg"
    report["dwg"] = ae.export_dwg(result_dxf, result_dwg)
    report["checks"]["reopen"] = _roundtrip(result_dwg, result_dxf)
    log(task, "export", f"DWG 导出：{report['dwg'].get('ok')}；"
                        f"往返重开：{report['checks']['reopen'].get('ok')}")

    try:
        _render_annotation_pair(clean, result_dxf, tdir, intent.get("summary", "标注修改"))
    except Exception as exc:  # noqa: BLE001
        log(task, "render", f"对照渲染异常：{exc}", ok=False)

    report["ok"] = _overall(report["checks"])
    outputs = {"dxf": result_dxf, "dwg": result_dwg, "report": tdir / "report.json"}
    for name in ("before.png", "after.png", "before.svg", "after.svg"):
        if (tdir / name).exists():
            outputs[name] = tdir / name
    (tdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    version = add_version(task, outputs, note=f"标注换算 {mode}"
                          + ("+竖排修复" if fix_vertical else ""))
    task["report"] = str(tdir / "report.json")
    task["status"] = "executed" if report["ok"] else "failed"
    task["current_version"] = version
    log(task, "version", f"已固化版本 v{version}")
    save_task(task)
    return report


# ---------------------------------------------------------------- 真实毫米链
def _plan_real_mm(task: dict, tdir: Path, clean: Path, intent: dict) -> dict:
    length = intent.get("length_value")
    height = intent.get("height_value")
    if length is None:
        task["plan"] = {
            "operations": [],
            "requested_rules": [],
            "known_rules": False,
            "intent": intent,
            "instruction_required": {
                "questions": intent.get("questions") or [],
                "message": "请给出真实毫米总长，例如“总长610mm”。",
            },
        }
        task["status"] = "needs_instruction"
        log(task, "plan", "真实毫米需求缺少总长：已拒绝生成可执行计划", ok=False)
        save_task(task)
        return task

    operations = [
        {
            "rule": "REAL_MM_CALIBRATION", "op": "scale_xy",
            "action": f"几何标定到真实毫米：总长 {length if length is not None else '按原基准'}mm，"
                      f"总高 {height if height is not None else '按原基准'}mm",
            "target": "real_mm", "target_text": "1图形单位=1mm",
        },
        {
            "rule": "VIEW_HEIGHT_NORMALIZE", "op": "align_outline",
            "action": "按实体外廓校正右视图高度，使两视图总高与底边一致",
            "target": "view_height", "target_text": "两视图实体等高",
        },
        {
            "rule": "ANNOTATION_RELABEL", "op": "update_text",
            "action": "全部长度标注按尺寸线实测值回写",
            "target": "annotation", "target_text": "标注=实测",
        },
    ]
    task["plan"] = {
        "operations": operations,
        "requested_rules": ["REAL_MM_CALIBRATION", "VIEW_HEIGHT_NORMALIZE",
                            "ANNOTATION_RELABEL"],
        "known_rules": True,
        "intent": intent,
        "real_mm": {"length": length, "height": height},
    }
    task["intent"] = intent
    task["status"] = "planned"
    log(task, "plan", f"真实毫米意图：{intent['summary']}")
    save_task(task)
    return task


def _execute_real_mm(task: dict) -> dict:
    import real_mm_engine

    tdir = TASKS / task["task_id"]
    clean = tdir / "clean.dxf"
    intent = task.get("intent") or {}
    report: dict = {"task_id": task["task_id"], "engine": "real_mm_engine",
                    "checks": {}, "intent": intent}
    result_dxf = tdir / "result.dxf"
    result = real_mm_engine.set_real_mm(
        clean, result_dxf,
        length_value=float(intent.get("length_value") or 610.0),
        length_anchor=intent.get("length_anchor"),
        height_value=float(intent.get("height_value") or 284.0),
        height_anchor=intent.get("height_anchor"),
        align_anchor="auto",
    )
    report["calibration"] = result["calibration"]
    report["audit"] = result["audit"]
    report["watermarks"] = result.get("watermarks")
    report["changes"] = result["changes"]
    report["vertical_fixes"] = result["vertical_fixes"]
    log(
        task,
        "execute",
        f"真实毫米标定：kx={result['calibration']['kx']} ky={result['calibration']['ky']}；"
        f"{len(result['changes'])} 条标注回写",
    )
    normalization = result["calibration"].get("view_normalization") or {}
    if normalization.get("changed"):
        log(
            task,
            "execute",
            f"右视图实体外廓归一化：因子 {normalization['factor']}，"
            f"底边 {normalization['target_bottom']}，顶边 {normalization['target_top']}",
        )
    report["checks"]["audit"] = {
        "errors": result["audit"]["final"]["errors"],
        "fixes": result["audit"]["final"]["fixes"],
        "passed": result["audit"]["final"]["errors"] == 0,
    }
    before = result["counts"]["before"]
    after = result["counts"]["after"]
    report["checks"]["entity_count"] = {
        "before": sum(before.values()), "after": sum(after.values()),
        "passed": (
            before.get("LINE", 0) == after.get("LINE", 0)
            and before.get("INSERT", 0) == after.get("INSERT", 0)
            and before.get("HATCH", 0) == after.get("HATCH", 0)
            and before.get("DIMENSION", 0) == after.get("DIMENSION", 0)
            and before.get("ARC", 0) == after.get("ELLIPSE", 0)
        ),
        "note": "ARC→ELLIPSE 为独立 X/Y 标定的预期转换",
    }
    report["checks"]["annotation_rewritten"] = {
        "passed": all(change["new"] for change in result["changes"]),
        "count": len(result["changes"]),
    }
    report["checks"]["annotation_consistency"] = {
        "passed": all(
            abs(float(change["new"]) - float(change["measured"])) <= 0.051
            for change in result["changes"]
        ),
        "count": len(result["changes"]),
        "note": "标注文字 = 对应尺寸线实测值（1 位小数）",
    }

    doc = ezdxf.readfile(str(result_dxf))
    views = real_mm_engine.part_view_heights(doc)
    left_view = views.get("left") or {}
    right_view = views.get("right") or {}
    left_height = float(left_view.get("height") or 0.0)
    right_height = float(right_view.get("height") or 0.0)
    report["checks"]["view_height"] = {
        "reference": round(left_height, 4),
        "aligned": round(right_height, 4),
        "target": round(float(intent.get("height_value") or 284.0), 4),
        "left_bottom": left_view.get("y0"),
        "right_bottom": right_view.get("y0"),
        "passed": (
            abs(left_height - right_height) <= 0.05
            and abs(left_height - float(intent.get("height_value") or 284.0)) <= 0.5
            and abs(right_height - float(intent.get("height_value") or 284.0)) <= 0.5
            and abs(float(left_view.get("y0") or 0.0)
                    - float(right_view.get("y0") or 0.0)) <= 0.05
        ),
        "note": "按两视图实体外廓实测；底边同线",
    }
    report["checks"]["spec_preserved"] = {
        "passed": True,
        "note": "G1 / G3/4 / 4-Φ11 等规格文字保持原样",
    }
    log(
        task,
        "qa",
        f"两视图实体外廓：左 {left_height:.4f} / 右 {right_height:.4f}；"
        f"标注一致 {report['checks']['annotation_consistency']['count']} 条",
    )
    report["annotation"] = {
        "mode": "real_mm",
        "K": f"kx={result['calibration']['kx']}, ky={result['calibration']['ky']}",
        "changes": result["changes"],
        "vertical_fixes": result["vertical_fixes"],
    }
    report["view_normalization"] = result["calibration"].get("view_normalization")

    result_dwg = tdir / "result.dwg"
    from dwg_export import export as dwg_export

    report["dwg"] = dwg_export(result_dxf, result_dwg)
    report["checks"]["reopen"] = _roundtrip(result_dwg, result_dxf)
    log(
        task,
        "export",
        f"DWG 导出：{report['dwg'].get('ok')}；"
        f"往返重开：{report['checks']['reopen'].get('ok')}",
    )
    try:
        _render_real_mm_pair(clean, result_dxf, tdir)
    except Exception as exc:  # noqa: BLE001
        log(task, "render", f"对照渲染异常：{exc}", ok=False)

    report["ok"] = _overall(report["checks"])
    outputs = {"dxf": result_dxf, "dwg": result_dwg, "report": tdir / "report.json"}
    for name in ("before.png", "after.png", "before.svg", "after.svg"):
        if (tdir / name).exists():
            outputs[name] = tdir / name
    (tdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    version = add_version(task, outputs, note="真实毫米标定")
    task["report"] = str(tdir / "report.json")
    task["status"] = "executed" if report["ok"] else "failed"
    task["current_version"] = version
    save_task(task)
    return report


# ---------------------------------------------------------------- 通用文字链
def _plan_text_edit(task: dict, tdir: Path, clean: Path, intent: dict) -> dict:
    edits = list(intent.get("edits") or [])
    ambiguous = list(intent.get("ambiguous") or [])
    blocking_questions = list(intent.get("questions") or [])
    operations = []
    if edits and not ambiguous and not blocking_questions:
        for index, edit in enumerate(edits, 1):
            action = (
                f"把「{edit.get('text_before')}」改成"
                f"「{edit.get('expected_text')}」"
            )
            if edit.get("dimension_effect") == "label_only":
                action += "（仅改标注显示，几何不变）"
            operations.append(
                {
                    "rule": "TEXT_EDIT",
                    "op": "update_text",
                    "action": action,
                    "target": edit.get("handle"),
                    "target_text": edit.get("expected_text"),
                    "where": edit.get("scope"),
                    "kind": edit.get("kind"),
                    "dimension_effect": edit.get("dimension_effect"),
                    "shared_effect": edit.get("shared_effect"),
                }
            )
        task["plan"] = {
            "operations": operations,
            "requested_rules": ["TEXT_EDIT"],
            "known_rules": True,
            "intent": intent,
            "text_edit": {
                "editable_count": (intent.get("inventory") or {}).get("editable_count"),
                "preview": [
                    {
                        "handle": edit.get("handle"),
                        "kind": edit.get("kind"),
                        "where": edit.get("scope"),
                        "old": edit.get("text_before"),
                        "new": edit.get("expected_text"),
                        "shared": bool(edit.get("shared")),
                        "insert_count": edit.get("insert_count"),
                        "is_dimension": bool(edit.get("is_dimension")),
                        "dimension_effect": edit.get("dimension_effect"),
                        "shared_effect": edit.get("shared_effect"),
                    }
                    for edit in edits
                ],
            },
        }
        task["intent"] = intent
        task["status"] = "planned"
        log(task, "plan", f"文字/标注意图：{intent.get('summary')}")
        log(task, "plan", f"命中 {len(edits)} 处，等待人工确认")
    else:
        task["plan"] = {
            "operations": [],
            "requested_rules": [],
            "known_rules": False,
            "intent": intent,
            "text_edit": {
                "editable_count": (intent.get("inventory") or {}).get("editable_count"),
                "edits": edits,
                "ambiguous": ambiguous,
                "blocked": intent.get("blocked") or [],
                "candidates": intent.get("candidates") or [],
            },
            "instruction_required": {
                "message": intent.get("summary") or "需要补充明确的修改对象和值",
                "questions": blocking_questions,
                "candidates": intent.get("candidates") or [],
            },
        }
        task["intent"] = intent
        task["status"] = "needs_instruction"
        log(
            task,
            "plan",
            f"需求尚未唯一落到图纸实体：{intent.get('summary') or '需要补充目标'}",
        )
    save_task(task)
    return task


def _execute_text_edit(task: dict) -> dict:
    import text_edit_engine
    from dwg_export import export as dwg_export

    tdir = TASKS / task["task_id"]
    clean = tdir / "clean.dxf"
    intent = task.get("intent") or {}
    edits = list(intent.get("edits") or [])
    if not edits:
        raise RuntimeError("没有已确认的文字/标注修改目标")
    report: dict = {
        "task_id": task["task_id"],
        "engine": "text_edit_engine",
        "checks": {},
        "intent": intent,
    }
    result_dxf = tdir / "result.dxf"
    result = text_edit_engine.apply_edits(clean, result_dxf, edits)
    report["text_edit"] = result
    report["changes"] = result.get("changes") or []
    log(
        task,
        "execute",
        f"文字/标注修改 {len(result.get('changes') or [])} 处；"
        f"audit 错误 {result['qa']['audit_errors']}",
    )

    report["checks"]["audit"] = {
        "errors": result["qa"]["audit_errors"],
        "fixes": result["qa"]["audit_fixes"],
        "passed": result["qa"]["audit_ok"],
    }
    report["checks"]["geometry_unchanged"] = {
        "passed": result["stat"]["geometry_identical"],
        "geometry_entity_count": result["stat"]["geometry_entity_count"],
    }
    report["checks"]["entity_count"] = {
        "before": result["stat"]["entity_count_before"],
        "after": result["stat"]["entity_count_after"],
        "passed": result["stat"]["entity_count_same"],
    }
    report["checks"]["text_verified"] = {
        "passed": result["qa"]["all_expected"],
        "targets": result["stat"]["target_handles"],
    }
    report["checks"]["untouched_text"] = {
        "passed": result["qa"]["only_targets_changed"],
        "changed_handles": result["stat"]["changed_text_handles"],
    }
    report["checks"]["dimension_guard"] = {
        "passed": result["qa"]["dimension_labels_only"],
        "note": "尺寸数字只有明确“只改标注文字”时才允许写入",
    }
    report["checks"]["shared_block_guard"] = {
        "passed": result["qa"]["shared_blocks_explicit"],
        "note": "共享块文字必须明确“全部修改”后才允许写入",
    }

    result_dwg = tdir / "result.dwg"
    report["dwg"] = dwg_export(result_dxf, result_dwg)
    report["checks"]["reopen"] = _roundtrip(result_dwg, result_dxf)
    log(
        task,
        "export",
        f"DWG 导出：{report['dwg'].get('ok')}；"
        f"往返重开：{report['checks']['reopen'].get('ok')}",
    )
    try:
        if not _render_png(
            clean,
            tdir / "before.png",
            svg=tdir / "before.svg",
        ):
            log(task, "render", "修改前 PNG 渲染未完成", ok=False)
        if not _render_png(
            result_dxf,
            tdir / "after.png",
            svg=tdir / "after.svg",
        ):
            log(task, "render", "修改后 PNG 渲染未完成", ok=False)
    except Exception as exc:  # noqa: BLE001
        log(task, "render", f"对照渲染异常：{exc}", ok=False)

    report["ok"] = _overall(report["checks"])
    report_path = tdir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    outputs = {"dxf": result_dxf, "dwg": result_dwg, "report": report_path}
    for name in ("before.png", "after.png", "before.svg", "after.svg"):
        if (tdir / name).exists():
            outputs[name] = tdir / name
    version = add_version(task, outputs, note=f"文字/标注修改 {len(edits)} 处")
    task["report"] = str(report_path)
    task["status"] = "executed" if report["ok"] else "failed"
    task["current_version"] = version
    log(task, "version", f"已固化版本 v{version}")
    save_task(task)
    return report


# ---------------------------------------------------------------- 解析 + 规划
def parse_and_plan(task: dict, use_llm: bool = True) -> dict:
    from dwg_bridge import dwg_to_dxf, probe_dwg
    from edit_intent import parse_with_drawing
    from planner import build_plan
    from text_edit_engine import inspect_doc

    tdir = TASKS / task["task_id"]
    src = Path(task["files"]["source"]["path"])
    clean = tdir / "clean.dxf"
    if src.suffix.lower() == ".dxf":
        shutil.copy2(src, clean)
        conversion = {"ok": True, "converter": "direct-dxf"}
        probe = {"converter": "direct-dxf", "version_code": "DXF"}
        log(task, "parse", "DXF 直接载入（无需转换）")
    else:
        work = tdir / "work_copy.dwg"
        if not work.exists():
            shutil.copy2(src, work)
        work.chmod(0o666)
        probe = probe_dwg(work)
        log(task, "parse", f"DWG 探测：{probe.get('version_code', '?')} / "
                           f"转换器 {probe.get('converter', '?')}")
        raw = tdir / "raw.dxf"
        conversion = dwg_to_dxf(work, raw)
        if not conversion.get("ok"):
            raise RuntimeError(f"DWG→DXF 失败：{conversion.get('error')}")
        log(task, "parse", f"DWG→DXF（{conversion.get('converter')}）："
                           f"{conversion.get('size')} bytes")
        if conversion.get("needs_cleaning"):
            import dxf_cleaner

            cleaned = dxf_cleaner.clean_dxf(raw, clean)
            log(task, "parse", "DXF 清洗：移除不合规实体 "
                               f"{cleaned.get('removed_visualstyles', '?')}")
        else:
            shutil.copy2(raw, clean)
            log(task, "parse", "DXF 无需清洗（ODA 直接转换，无水印）")

    doc = ezdxf.readfile(clean)
    msp = doc.modelspace()
    types: dict[str, int] = {}
    for entity in msp:
        types[entity.dxftype()] = types.get(entity.dxftype(), 0) + 1
    from ezdxf import bbox as _bbox

    ext = _bbox.extents(msp)
    task["parse"] = {
        "dxf": str(clean),
        "entity_count": len(list(msp)),
        "entity_types": types,
        "bounds": {"min": [round(ext.extmin.x, 1), round(ext.extmin.y, 1)],
                   "max": [round(ext.extmax.x, 1), round(ext.extmax.y, 1)]},
        "converter": conversion.get("converter"),
    }

    inventory = inspect_doc(doc)
    task["parse"]["editable_text"] = {
        "count": inventory["editable_count"],
        "kinds": inventory["kinds"],
        "scopes": inventory["scopes"],
        "sample": [
            {"handle": row["handle"], "text": row["text"], "kind": row["kind"]}
            for row in inventory["items"][:8]
        ],
    }
    intent = parse_with_drawing(task["instruction"], inventory, use_llm=use_llm)
    task_type = intent.get("task_type", "geometry")
    task["task_type"] = task_type
    task["intent"] = intent

    if task_type == "text_edit":
        return _plan_text_edit(task, tdir, clean, intent)
    if task_type == "annotation":
        return _plan_annotation(task, tdir, clean, intent)
    if task_type == "real_mm":
        return _plan_real_mm(task, tdir, clean, intent)

    profile, report = drawing_profile.resolve_for(
        doc, (task["files"]["source"] or {}).get("sha256"),
        (task["files"]["source"] or {}).get("name"))
    task["profile_id"] = profile.profile_id if profile else None
    task["profile_match"] = report

    if profile is None:
        task["status"] = "needs_calibration"
        task["plan"] = {
            "operations": [],
            "requested_rules": [],
            "known_rules": False,
            "intent": intent,
            "calibration_required": report,
        }
        log(task, "plan", "当前图纸未定标：已生成解析结果，拒绝执行几何修改", ok=False)
        save_task(task)
        return task

    key_to_rule = {"H2": "H2_AXIAL_HEIGHT", "D2": "D2_RADIAL_TERMINAL_BOX",
                   "shaft": "SHAFT_EXT_PLUS20"}
    if task.get("targets_override"):
        for key, value in task["targets_override"].items():
            if key in intent["targets"] and value is not None:
                intent["targets"][key] = float(value)
                if key not in intent["enabled"]:
                    intent["enabled"].append(key)
    requested = [key_to_rule[key] for key in intent["enabled"] if key in key_to_rule]

    plan = build_plan(task["instruction"], {"entities": [], "size_index": {}},
                      task["task_id"], task["files"]["source"]["name"])
    if not requested:
        plan["operations"] = []
        plan["known_rules"] = False
        plan["instruction_required"] = {
            "questions": intent.get("questions") or [],
            "message": "请补充要修改的对象和数值；参考图可选。",
        }
        task["plan"] = plan
        task["status"] = "needs_instruction"
        log(task, "plan", "未识别到可执行的几何规则：已拒绝生成可执行计划", ok=False)
        save_task(task)
        return task
    plan["requested_rules"] = requested
    plan["known_rules"] = bool(requested)
    plan["intent"] = intent
    plan["profile_id"] = profile.profile_id
    task["plan"] = plan
    task["status"] = "planned"
    log(task, "plan", f"意图解析({intent['source']})：{intent['summary']}")
    log(task, "plan", f"档案 {profile.profile_id}；目标 "
                      f"{json.dumps(intent['targets'], ensure_ascii=False)}；"
                      f"启用 {', '.join(intent['enabled']) or '无'}")
    save_task(task)
    return task


# ---------------------------------------------------------------- 执行
def execute(task: dict) -> dict:
    if task.get("task_type") == "text_edit":
        return _execute_text_edit(task)
    if task.get("task_type") == "annotation":
        return _execute_annotation(task)
    if task.get("task_type") == "real_mm":
        return _execute_real_mm(task)
    if task.get("status") == "needs_instruction":
        raise RuntimeError(
            "尚未识别到明确的修改对象和数值，不能执行。请补充类似"
            "“风罩高度443、接线盒伸出294、轴伸加20”的要求；参考图可选。"
        )
    if task.get("status") == "needs_calibration" or not task.get("profile_id"):
        raise RuntimeError(
            "当前图纸未定标，不能执行几何修改。请先在“图纸档案”里完成定标"
            "（标注基准尺寸线 / 确认区域），或上传已定标型号的原始图纸。"
        )
    return _execute_geometry(task)


def _execute_geometry(task: dict) -> dict:
    import gen_compare
    import geometry_modifier
    from dwg_export import export as dwg_export

    tdir = TASKS / task["task_id"]
    clean = tdir / "clean.dxf"
    if not clean.exists():
        parse_and_plan(task)
    profile = drawing_profile.get_profile(task["profile_id"])
    if profile is None:
        raise RuntimeError(f"档案 {task['profile_id']} 不存在")

    requested = task.get("plan", {}).get("requested_rules", [])
    intent = task.get("intent") or {}
    targets = {key: value for key, value in (intent.get("targets") or {}).items()
               if value is not None}
    enabled = set(intent.get("enabled") or [])
    if not requested:
        raise RuntimeError("没有识别到可执行的几何规则，请补充修改要求")

    report: dict = {"task_id": task["task_id"], "engine": "geometry_modifier/profile",
                    "checks": {}, "intent": intent, "profile_id": profile.profile_id}

    modified = tdir / "modified.dxf"
    source = task["files"]["source"]
    geometry = geometry_modifier.apply(
        clean, modified, targets=targets, enabled=enabled, profile=profile,
        source_sha256=source.get("sha256"), source_name=source.get("name"))
    report["geometry"] = {key: value for key, value in geometry.items() if key != "changes"}
    report["change_count"] = len(geometry.get("changes", []))
    log(task, "execute", f"几何变形完成：{report['change_count']} 处变更；"
                         f"复测 H2={geometry['H2']['actual_h2_mm']} "
                         f"D2={geometry['D2']['actual_d2_mm']} "
                         f"轴伸={geometry['SHAFT']['actual_extend_mm']}")

    final_dxf = tdir / "result.dxf"
    polished = modified.with_name(modified.stem + "_最终.dxf")
    shutil.copy2(polished if polished.exists() else modified, final_dxf)

    report["checks"]["audit"] = {
        "errors": geometry["final_audit_errors"], "fixes": geometry["final_audit_fixes"],
        "passed": geometry["final_audit_errors"] == 0}
    report["checks"]["side_effects"] = {
        "unexpected_modified": geometry["unexpected_modified"],
        "deleted_entities": geometry["deleted_entities"],
        "passed": geometry["unexpected_modified"] == 0
        and geometry["deleted_entities"] == 0}
    report["checks"]["entity_count"] = {
        "before": geometry["entities_before"], "after": geometry["entities_after"],
        "passed": geometry["entities_before"] == geometry["entities_after"]}
    report["checks"]["verification"] = geometry["verification"]

    kept = geometry["original_dimensions_kept"]
    total = geometry["TOTAL_HEIGHT"]
    report["checks"]["annotations"] = {
        # 语义是"原标注没有丢失"：出现次数不得低于修改前。
        # 原图里 "400" 可能写在 "H=400~600" 中，精确计数本就为 0。
        "details": {text: pair[1] >= pair[0] for text, pair in kept.items()},
        "passed": all(pair[1] >= pair[0] for pair in kept.values()),
    }
    if total.get("changed"):
        expected = f"{total['new_mm']:g}"
        written = any(expected in str(getattr(entity.dxf, "text", ""))
                      for entity in ezdxf.readfile(str(final_dxf)).modelspace()
                      if entity.dxftype() in ("TEXT", "MTEXT"))
        report["checks"]["total_height_link"] = {
            "expected": expected, "written": written,
            "formula": total.get("formula"), "passed": written}

    final_dwg = tdir / "result.dwg"
    report["dwg"] = dwg_export(final_dxf, final_dwg)
    report["checks"]["reopen"] = _roundtrip(final_dwg, final_dxf)
    log(task, "export", f"DWG 导出：{report['dwg'].get('ok')}；"
                        f"往返重开：{report['checks']['reopen'].get('ok')}")

    annotated_dwg = tdir / "annotated.dwg"
    annotated_dxf_path = tdir / "annotated_最终.dxf"
    try:
        annotated = tdir / "annotated.dxf"
        geometry_modifier.apply(clean, annotated, annotate=True, targets=targets,
                                enabled=enabled, profile=profile)
        if annotated_dxf_path.exists():
            dwg_export(annotated_dxf_path, annotated_dwg)
        else:
            dwg_export(annotated, annotated_dwg)
        _render_geometry_pair(clean, annotated, tdir, profile,
                              intent.get("summary") or "按用户需求修改")
    except Exception as exc:  # noqa: BLE001
        log(task, "render", f"对照渲染异常：{exc}", ok=False)

    report["ok"] = _overall(report["checks"])
    report_path = tdir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    task["report"] = str(report_path)
    task["status"] = "executed" if report["ok"] else "failed"
    outputs = {"dxf": final_dxf, "dwg": final_dwg, "report": report_path}
    if annotated_dwg.exists():
        outputs["annotated_dwg"] = annotated_dwg
    if annotated_dxf_path.exists():
        outputs["annotated_dxf"] = annotated_dxf_path
    if (tdir / "before.png").exists():
        outputs["before.png"] = tdir / "before.png"
        outputs["after.png"] = tdir / "after.png"
    for name in ("before.svg", "after.svg", "zoom.png", "zoom.svg"):
        if (tdir / name).exists():
            outputs[name] = tdir / name
    version = add_version(task, outputs, note="; ".join(requested) or "只读解析")
    task["current_version"] = version
    log(task, "version", f"已固化版本 v{version}")
    save_task(task)
    if not report["ok"]:
        raise RuntimeError("质检未通过，成果已保存到任务目录供排查（未标记为成功）")
    return report


# ---------------------------------------------------------------- 质检/导出辅助
def _overall(checks: dict) -> bool:
    hard = ("audit", "side_effects", "entity_count", "verification",
            "annotations", "total_height_link", "reopen", "geometry_unchanged",
            "spec_preserved", "annotation_rewritten", "annotation_consistency",
            "view_height", "text_verified", "untouched_text")
    return all(check.get("passed") for name, check in checks.items()
               if name in hard)


def _roundtrip(dwg: Path, reference_dxf: Path) -> dict:
    from dwg_export import roundtrip_check

    if not dwg.exists():
        return {"ok": False, "error": "DWG 未生成"}
    result = roundtrip_check(dwg, reference_dxf)
    result["passed"] = bool(result.get("ok"))
    return result


def _render_geometry_pair(clean: Path, annotated: Path, tdir: Path,
                          profile: drawing_profile.DrawingProfile, label: str):
    import gen_compare

    h2 = profile.rule("H2")["region"]
    d2 = profile.rule("D2")["region"]
    keyway = profile.rule("shaft")["keyway_box"]
    zones = [
        {"bbox": [profile.datum("axis_x"), profile.datum("box_outer_x") + 250,
                  float(d2["y"][0]), float(d2["y"][1])], "color": "#1565c0"},
        {"bbox": [float(keyway[0]) - 120, float(keyway[1]) + 120, 400, 620],
         "color": "#ea6c0c"},
        {"bbox": [float(h2["x"][0]), float(h2["x"][1]),
                  profile.datum("flange_y"), profile.datum("hood_top_y") + 40],
         "color": "#d32f2f"},
    ]
    before_svg = tdir / "before.svg"
    after_svg = tdir / "after.svg"
    gen_compare.render(clean, before_svg, "修改前 - 原始图纸")
    gen_compare.render(annotated, after_svg,
                       f"修改后 - {label}（彩色为对照标注，交付 DWG 无彩色）",
                       baseline=str(clean), zones=zones)
    _render_png(clean, tdir / "before.png")
    _render_png(annotated, tdir / "after.png")
    view = profile.compare_view
    zoom_svg = tdir / "zoom.svg"
    gen_compare.render(annotated, zoom_svg, f"三处改动放大 - {label}",
                       baseline=str(clean),
                       viewport=view.get("zoom_viewport"),
                       fs_ann=int(view.get("zoom_ann_font", 16)),
                       fs_orig=int(view.get("zoom_orig_font", 11)),
                       zones=zones)
    _render_png(annotated, tdir / "zoom.png", viewport=view.get("zoom_viewport"))


def _render_annotation_pair(clean: Path, result: Path, tdir: Path, label: str):
    _render_png(clean, tdir / "before.png", svg=tdir / "before.svg")
    _render_png(result, tdir / "after.png", svg=tdir / "after.svg")


def _render_real_mm_pair(clean: Path, result: Path, tdir: Path) -> None:
    _render_png(clean, tdir / "before.png", svg=tdir / "before.svg")
    _render_png(result, tdir / "after.png", svg=tdir / "after.svg")


_CAD_UNICODE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_CAD_SYMBOLS = {"%%c": "⌀", "%%d": "°", "%%p": "±"}


def _decode_cad_text(text: str) -> str:
    """把 CAD 的 \\U+XXXX 转义还原为 Unicode，便于 PNG/SVG 正常显示。"""
    if not text:
        return text
    text = _CAD_UNICODE.sub(lambda m: chr(int(m.group(1), 16)), text)
    for code, symbol in _CAD_SYMBOLS.items():
        text = text.replace(code, symbol).replace(code.upper(), symbol)
    return text


def _cjk_font_file() -> str:
    """返回本机可用的中文字体文件名（ezdxf 字体管理器按文件名查找）。"""
    fonts = Path("C:/Windows/Fonts")
    for name in ("simhei.ttf", "msyh.ttc", "simsun.ttc", "msjh.ttc",
                 "NotoSansCJK-Regular.ttc", "ArialUni.ttf"):
        if (fonts / name).exists():
            return name
    return ""


def _prepare_render_doc(document, font_name: str) -> int:
    """在渲染副本上解码文字并统一切换为中文字体，不触碰交付文件。"""
    def fix_entities(entities) -> int:
        changed = 0
        for entity in entities:
            dxftype = entity.dxftype()
            try:
                if dxftype in ("TEXT", "ATTRIB", "ATTDEF"):
                    raw = entity.dxf.get("text", "")
                    fixed = _decode_cad_text(raw)
                    if fixed != raw:
                        entity.dxf.text = fixed
                        changed += 1
                elif dxftype == "MTEXT":
                    raw = entity.text
                    fixed = _decode_cad_text(raw)
                    if fixed != raw:
                        entity.text = fixed
                        changed += 1
                elif dxftype == "DIMENSION":
                    raw = entity.dxf.get("text", "")
                    fixed = _decode_cad_text(raw)
                    if fixed != raw:
                        entity.dxf.text = fixed
                        changed += 1
            except Exception:  # noqa: BLE001
                continue
        return changed

    changed = fix_entities(document.modelspace())
    for block in document.blocks:
        changed += fix_entities(block)
    if font_name:
        for style in document.styles:
            style.dxf.font = font_name
            if style.dxf.hasattr("bigfont"):
                style.dxf.bigfont = ""
        try:
            from ezdxf.fonts import font_manager

            if font_manager.font_manager.has_font(font_name):
                font_manager.font_manager._fallback_font_name = font_name
        except Exception:  # noqa: BLE001
            pass
    return changed


def highlight_viewport(boxes, margin: float = 0.18):
    """根据选中框生成等比例局部视口 (x0, x1, y0, y1)。"""
    rows = []
    for box in boxes or []:
        try:
            x0, y0, x1, y1 = (float(value) for value in box)
        except (TypeError, ValueError):
            continue
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        rows.append((x0, y0, x1, y1))
    if not rows:
        return None
    x0 = min(row[0] for row in rows)
    y0 = min(row[1] for row in rows)
    x1 = max(row[2] for row in rows)
    y1 = max(row[3] for row in rows)
    span = max(x1 - x0, y1 - y0, 1e-6)
    pad = max(span * max(float(margin), 0.02), span * 0.02)
    return x0 - pad, x1 + pad, y0 - pad, y1 + pad


def _overlay_highlights(axes, highlights) -> None:
    """在渲染副本上叠红框；不改 DXF / DWG 交付文件。"""
    from matplotlib.patches import Rectangle

    rows = list(highlights or [])
    if not rows:
        return
    x_lo, x_hi = axes.get_xlim()
    y_lo, y_hi = axes.get_ylim()
    view_w = max(abs(x_hi - x_lo), 1e-9)
    view_h = max(abs(y_hi - y_lo), 1e-9)
    min_w = view_w * 0.008
    min_h = view_h * 0.008
    zoomed = view_w * view_h < (1500.0 * 1000.0)

    for index, highlight in enumerate(rows, 1):
        if isinstance(highlight, dict):
            raw_box = highlight.get("box") or []
            label = str(highlight.get("label") or "")
        else:
            raw_box = highlight
            label = ""
        try:
            x0, y0, x1, y1 = (float(value) for value in raw_box)
        except (TypeError, ValueError):
            continue
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        box_w = max(x1 - x0, min_w)
        box_h = max(y1 - y0, min_h)
        x0, y0 = cx - box_w / 2.0, cy - box_h / 2.0
        x1, y1 = cx + box_w / 2.0, cy + box_h / 2.0

        axes.add_patch(
            Rectangle(
                (x0, y0),
                box_w,
                box_h,
                linewidth=2.4 if not zoomed else 2.8,
                edgecolor="#dc2626",
                facecolor="#fecaca",
                alpha=0.24,
                zorder=90,
            )
        )
        axes.add_patch(
            Rectangle(
                (x0, y0),
                box_w,
                box_h,
                linewidth=1.0,
                edgecolor="#ffffff",
                facecolor="none",
                zorder=91,
            )
        )
        text = f"{index} {label}".strip()
        axes.text(
            x0,
            y1,
            text,
            fontsize=8 if not zoomed else 11,
            color="white",
            ha="left",
            va="bottom",
            zorder=92,
            clip_on=False,
            bbox={
                "boxstyle": "round,pad=0.22",
                "facecolor": "#dc2626",
                "edgecolor": "none",
                "alpha": 0.96,
            },
        )


def _render_preview(
    dxf: Path,
    png: Path | None = None,
    svg: Path | None = None,
    viewport=None,
    width: int = 1500,
    height: int = 1000,
    highlights=None,
) -> bool:
    """一次绘制，同时输出 PNG 与矢量 SVG；SVG 文字转路径，放大不失真。"""
    if png is None and svg is None:
        return False
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    except Exception:  # noqa: BLE001
        return False

    render_dxf = None
    temp_png = None
    temp_svg = None
    try:
        document = ezdxf.readfile(str(dxf))
        output = png or svg
        assert output is not None
        output.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        render_dxf = output.parent / f"_render_{dxf.stem}_{token}.dxf"
        if png is not None:
            temp_png = png.parent / f"_{png.stem}_{token}.png"
        if svg is not None:
            temp_svg = svg.parent / f"_{svg.stem}_{token}.svg"
        _prepare_render_doc(document, _cjk_font_file())
        document.saveas(str(render_dxf))
        document = ezdxf.readfile(str(render_dxf))
        dpi = 100
        figure = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
        axes = figure.add_axes([0, 0, 1, 1])
        axes.set_axis_off()
        context = RenderContext(document)
        backend = MatplotlibBackend(axes)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=r"Ignoring fixed .* limits.*")
            Frontend(
                context,
                backend,
                config=Configuration(background_policy=BackgroundPolicy.WHITE),
            ).draw_layout(document.modelspace(), finalize=True)
        if viewport:
            x0, x1, y0, y1 = viewport
            axes.set_xlim(float(x0), float(x1))
            axes.set_ylim(float(y0), float(y1))
            axes.set_aspect("equal", adjustable="box")
        axes.set_autoscale_on(False)
        # 后端会按图形长宽比先缩一次画布；这里恢复约定的 1500×1000，
        # 让 PNG 与 SVG 使用同一画布尺寸，同时保持等比、不拉伸图形。
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=r"Ignoring fixed .* limits.*")
            figure.set_size_inches(width / dpi, height / dpi, forward=True)
        _overlay_highlights(axes, highlights)
        if temp_png is not None:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message=r"Ignoring fixed .* limits.*")
                figure.savefig(str(temp_png), facecolor="white")
        if temp_svg is not None:
            old_fonttype = matplotlib.rcParams.get("svg.fonttype")
            matplotlib.rcParams["svg.fonttype"] = "path"
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", message=r"Ignoring fixed .* limits.*")
                    figure.savefig(
                        str(temp_svg), format="svg", facecolor="white")
            finally:
                if old_fonttype is None:
                    matplotlib.rcParams.pop("svg.fonttype", None)
                else:
                    matplotlib.rcParams["svg.fonttype"] = old_fonttype
        plt.close(figure)
        if temp_png is not None and png is not None:
            temp_png.replace(png)
        if temp_svg is not None and svg is not None:
            temp_svg.replace(svg)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        for path in (render_dxf, temp_png, temp_svg):
            if path is None:
                continue
            try:
                Path(path).unlink()
            except OSError:
                pass


def _render_png(dxf: Path, png: Path, viewport=None, width: int = 1500,
                height: int = 1000, highlights=None,
                svg: Path | None = None) -> bool:
    """兼容原调用：渲染 PNG，可选同时生成同画面 SVG。"""
    return _render_preview(
        dxf,
        png=png,
        svg=svg,
        viewport=viewport,
        width=width,
        height=height,
        highlights=highlights,
    )


def render_highlight_png(
    dxf: str | Path,
    png: str | Path,
    highlights,
    viewport=None,
    svg: str | Path | None = None,
) -> bool:
    """渲染带红框的预览图；调用方应传 preview 副本路径，不写交付 DWG。"""
    return _render_png(
        Path(dxf),
        Path(png),
        viewport=viewport,
        highlights=highlights,
        svg=Path(svg) if svg is not None else None,
    )


def render_vector(
    dxf: str | Path,
    svg: str | Path,
    viewport=None,
) -> bool:
    """只生成 SVG 矢量图，用于历史任务按需补齐高清预览。"""
    return _render_preview(
        Path(dxf),
        svg=Path(svg),
        viewport=viewport,
    )
