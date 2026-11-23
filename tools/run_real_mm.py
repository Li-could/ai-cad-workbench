"""Run the verified real-millimeter DWG pipeline from any working directory.

The script is intentionally thin around the proven project engines:

    dwg_bridge -> dxf_cleaner -> real_mm_engine -> ODA DWG export
    -> DWG readback -> independent QA -> preview / report

The workbench engines handle geometry and annotation. This entry point selects
the calibration parameters, verifies the exported drawing, and writes the report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from math import isfinite
from pathlib import Path
from types import SimpleNamespace


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
SPEC_MARKERS = ("G1", "G3/4", "4-Φ11")

PRESETS = {
    "25qyl": {
        "length": 610.0,
        "height": 284.0,
        "length_anchor": "610",
        "height_anchor": "284",
        "align_anchor": "auto",
        "expected_specs": SPEC_MARKERS,
    }
}


def _number(value: float) -> str:
    return f"{value:g}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _numeric(text: str) -> bool:
    return bool(NUMERIC_RE.fullmatch(str(text or "").strip()))


def _resolve_project_root(explicit: Path | None) -> Path:
    configured = os.environ.get("ENGINEERING_DRAWING_AI_ROOT", "").strip()
    default = Path(configured) if configured else DEFAULT_PROJECT_ROOT
    root = (explicit or default).expanduser().resolve()
    if not (root / "src" / "real_mm_engine.py").is_file():
        raise SystemExit(f"找不到真实毫米引擎，请通过 --project-root 指定工作台根目录：{root}")
    return root


def _load_stack(root: Path) -> SimpleNamespace:
    src = root / "src"
    sys.path.insert(0, str(src))

    import runtime

    runtime.bootstrap()

    import dxf_cleaner
    import dwg_bridge
    import dwg_export
    import ezdxf
    import real_mm_engine

    return SimpleNamespace(
        ezdxf=ezdxf,
        dwg_bridge=dwg_bridge,
        dwg_export=dwg_export,
        dxf_cleaner=dxf_cleaner,
        rm=real_mm_engine,
    )


@contextmanager
def _workspace(out_dir: Path, keep: bool):
    if keep:
        work = out_dir / f"_work_{datetime.now():%Y%m%d_%H%M%S}"
        work.mkdir(parents=True, exist_ok=False)
        yield work
        return
    with tempfile.TemporaryDirectory(prefix="_work_", dir=out_dir) as tmp:
        yield Path(tmp)


def _prepare_clean_dxf(stack: SimpleNamespace, source: Path, work: Path) -> dict:
    clean = work / "clean.dxf"
    if source.suffix.lower() == ".dwg":
        raw = work / "source.dxf"
        conversion = stack.dwg_bridge.dwg_to_dxf(source, raw)
        if not conversion.get("ok"):
            raise RuntimeError(f"DWG 转 DXF 失败：{conversion.get('error') or conversion}")
        damage = None
        if conversion.get("needs_cleaning"):
            damage = stack.dxf_cleaner.clean_dxf(raw, clean)
        else:
            shutil.copy2(raw, clean)
        return {
            "converter": conversion.get("converter"),
            "conversion": conversion,
            "cleaning": damage,
            "raw_dxf": str(raw),
        }

    if source.suffix.lower() != ".dxf":
        raise ValueError("输入必须是 .dwg 或 .dxf")

    raw = work / "source.dxf"
    shutil.copy2(source, raw)
    try:
        stack.ezdxf.readfile(str(raw))
        shutil.copy2(raw, clean)
        cleaning = None
    except Exception as first_error:  # noqa: BLE001
        cleaning = stack.dxf_cleaner.clean_dxf(raw, clean)
        try:
            stack.ezdxf.readfile(str(clean))
        except Exception as second_error:  # noqa: BLE001
            raise RuntimeError(
                f"DXF 无法读取；清洗后仍失败：{first_error} / {second_error}"
            ) from second_error
    return {
        "converter": None,
        "conversion": {"ok": True, "converter": "direct-dxf"},
        "cleaning": cleaning,
        "raw_dxf": str(raw),
    }


def _text_of(entity) -> str:
    kind = entity.dxftype()
    try:
        if kind == "TEXT":
            return entity.dxf.text
        if kind == "MTEXT":
            return entity.text
        if kind == "DIMENSION":
            return entity.dxf.text
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _collect_text(doc) -> str:
    values: list[str] = []
    for layout in doc.layouts:
        for entity in layout:
            text = _text_of(entity)
            if text:
                values.append(text)
    for block in doc.blocks:
        for entity in block:
            text = _text_of(entity)
            if text:
                values.append(text)
    return "|".join(values).replace("%%C", "Φ").replace("%%c", "Φ")


def _watermarks(doc) -> dict:
    count = 0
    layers: set[str] = set()
    examples: list[str] = []
    for layout in doc.layouts:
        for entity in layout:
            layer = str(entity.dxf.get("layer", ""))
            text = _text_of(entity)
            if "watermark" in layer.lower() or "evaluation only" in text.lower():
                count += 1
                if layer:
                    layers.add(layer)
                if len(examples) < 5:
                    examples.append(f"{layer}:{text[:60]}")
    return {"entities": count, "layers": sorted(layers), "examples": examples}


def _spec_counts(text: str, markers: tuple[str, ...]) -> dict[str, int]:
    return {marker: text.count(marker) for marker in markers}


def _numeric_rows(stack: SimpleNamespace, doc) -> list[dict]:
    rows = []
    for row in stack.rm.measure_all(doc):
        if _numeric(row.get("old")):
            rows.append(
                {
                    "kind": row["kind"],
                    "where": row["where"],
                    "text": row["old"],
                    "measured": float(row["measured"]),
                }
            )
    return rows


def _annotation_check(rows: list[dict], tolerance: float = 0.051) -> dict:
    comparisons = []
    for row in rows:
        delta = abs(float(row["text"]) - row["measured"])
        comparisons.append(
            {
                **row,
                "delta": round(delta, 6),
                "passed": delta <= tolerance,
            }
        )
    return {
        "passed": bool(comparisons) and all(item["passed"] for item in comparisons),
        "count": len(comparisons),
        "tolerance": tolerance,
        "rows": comparisons,
    }


def _view_check(views: dict, target_height: float, tolerance: float = 0.05) -> dict:
    left = views.get("left") or {}
    right = views.get("right") or {}
    left_height = float(left.get("height") or 0.0)
    right_height = float(right.get("height") or 0.0)
    left_bottom = float(left.get("y0") or 0.0)
    right_bottom = float(right.get("y0") or 0.0)
    left_top = float(left.get("y1") or 0.0)
    right_top = float(right.get("y1") or 0.0)
    checks = {
        "both_views_present": bool(left and right),
        "equal_height": abs(left_height - right_height) <= tolerance,
        "same_baseline": abs(left_bottom - right_bottom) <= tolerance,
        "same_top": abs(left_top - right_top) <= tolerance,
        "left_matches_target": abs(left_height - target_height) <= tolerance,
        "right_matches_target": abs(right_height - target_height) <= tolerance,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "target_height": target_height,
        "tolerance": tolerance,
        "left": left,
        "right": right,
    }


def _handle_check(before_doc, after_doc) -> dict:
    before = {entity.dxf.handle for entity in before_doc.modelspace()}
    after = {entity.dxf.handle for entity in after_doc.modelspace()}
    missing = sorted(before - after)
    extra = sorted(after - before)
    return {
        "passed": not missing and not extra,
        "before": len(before),
        "after": len(after),
        "missing_handles": missing[:20],
        "extra_handles": extra[:20],
    }


def _annotation_signature(rows: list[dict]) -> list[tuple[str, float]]:
    return sorted((str(row["text"]), round(float(row["measured"]), 3)) for row in rows)


def _write_summary(
    path: Path,
    source: Path,
    deliverables: dict[str, Path],
    calibration: dict,
    checks: dict,
    rows: list[dict],
    warnings: list[str],
) -> None:
    view_check = checks["view_geometry"]
    cal = calibration["calibration"]
    normalization = cal.get("view_normalization") or {}
    left = view_check["left"]
    right = view_check["right"]
    lines = [
        f"# {deliverables['dwg'].stem} 真实毫米 1:1 说明",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 源文件：`{source}`",
        f"- 图形单位：mm（`$INSUNITS=4`）",
        f"- 总长目标：{calibration['targets']['length']:g} mm",
        f"- 总高目标：{calibration['targets']['height']:g} mm",
        "",
        "## 几何结果",
        "",
        "| 项目 | 结果 |",
        "|---|---:|",
        f"| 左视图实体高度 | {left.get('height', 0):.6f} |",
        f"| 右视图实体高度 | {right.get('height', 0):.6f} |",
        f"| 左视图底边 | {left.get('y0', 0):.6f} |",
        f"| 右视图底边 | {right.get('y0', 0):.6f} |",
        f"| 左视图顶边 | {left.get('y1', 0):.6f} |",
        f"| 右视图顶边 | {right.get('y1', 0):.6f} |",
        f"| kx | {cal['kx']:.9f} |",
        f"| ky | {cal['ky']:.9f} |",
    ]
    if normalization.get("changed"):
        lines.extend(
            [
                f"| 右视图归一化因子 | {normalization['factor']:.9f} |",
                f"| 归一化前右视图高度 | {normalization['before']['right']['height']:.6f} |",
                f"| 归一化后右视图高度 | {normalization['after']['right']['height']:.6f} |",
            ]
        )

    lines.extend(
        [
            "",
            "## 标注结果",
            "",
            "| 位置 | 标注文字 | 尺寸线实测 | 误差 |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in rows:
        delta = abs(float(row["text"]) - row["measured"])
        lines.append(
            f"| {row['where']} | {row['text']} | {row['measured']:.4f} | {delta:.4f} |"
        )

    lines.extend(
        [
            "",
            "## 质检",
            "",
            "| 检查 | 结果 |",
            "|---|---|",
        ]
    )
    for name, check in checks.items():
        lines.append(f"| `{name}` | {'通过' if check.get('passed') else '失败'} |")

    if warnings:
        lines.extend(["", "## 提示", ""])
        lines.extend(f"- {item}" for item in warnings)

    lines.extend(
        [
            "",
            "## 交付文件",
            "",
        ]
    )
    for name, file in deliverables.items():
        lines.append(f"- {name}：`{file}`")
    lines.extend(
        [
            "",
            "> 预览图只用于检查。最终交付文件是上面的 DWG；请用 eDrawings / SolidWorks / CAXA 打开确认。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _render_preview(root: Path, dxf: Path, png: Path) -> dict:
    tool = root / "tools" / "render_real_mm_preview.py"
    if not tool.is_file():
        return {"ok": False, "warning": f"找不到预览工具：{tool}"}
    completed = subprocess.run(
        [sys.executable, str(tool), str(dxf), str(png)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0 or not png.is_file() or png.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        return {
            "ok": False,
            "warning": f"预览渲染失败，不影响 DWG：{detail}",
            "returncode": completed.returncode,
        }
    svg = png.with_suffix(".svg")
    return {
        "ok": True,
        "png": str(png),
        "svg": str(svg) if svg.is_file() else None,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实毫米 1:1 DWG 标定、两视图等高、标注回写与回读质检"
    )
    parser.add_argument("input", type=Path, help="原始 DWG 或 DXF")
    parser.add_argument(
        "--project-root",
        type=Path,
        help="工作台项目根目录，默认使用脚本所在仓库",
    )
    parser.add_argument(
        "--preset",
        choices=("25qyl", "custom"),
        default="25qyl",
        help="25qyl 使用已定版参数；custom 必须显式给基准",
    )
    parser.add_argument("--length", type=float, help="真实总长 mm")
    parser.add_argument("--height", type=float, help="真实总高 mm")
    parser.add_argument("--length-anchor", help="总长基准标注原文")
    parser.add_argument("--height-anchor", help="总高基准标注原文")
    parser.add_argument(
        "--align-anchor",
        default=None,
        help='右侧总高预对齐基准；默认 auto',
    )
    parser.add_argument("--view-split", type=float, help="两视图分界 X，通常自动")
    parser.add_argument("--out-dir", type=Path, help="交付目录")
    parser.add_argument("--stem", help="交付文件名（不含扩展名）")
    parser.add_argument("--force", action="store_true", help="覆盖同名交付文件")
    parser.add_argument("--skip-preview", action="store_true", help="跳过 PNG/SVG 预览")
    parser.add_argument("--keep-work", action="store_true", help="保留转换中间文件")
    args = parser.parse_args(argv)

    preset = PRESETS.get(args.preset, {})
    args.length = args.length if args.length is not None else preset.get("length")
    args.height = args.height if args.height is not None else preset.get("height")
    args.length_anchor = (
        args.length_anchor
        if args.length_anchor is not None
        else preset.get("length_anchor")
    )
    args.height_anchor = (
        args.height_anchor
        if args.height_anchor is not None
        else preset.get("height_anchor")
    )
    args.align_anchor = (
        args.align_anchor
        if args.align_anchor is not None
        else preset.get("align_anchor", "auto")
    )
    missing = [
        name
        for name in ("length", "height", "length_anchor", "height_anchor")
        if getattr(args, name) in (None, "")
    ]
    if missing:
        parser.error("custom 模式缺少参数：" + ", ".join(missing))
    if any(not isfinite(value) or value <= 0 for value in (args.length, args.height)):
        parser.error("总长和总高必须是有限的正数")
    args.expected_specs = tuple(preset.get("expected_specs", ()))
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    source = args.input.expanduser().resolve()
    if not source.is_file():
        print(json.dumps({"ok": False, "error": f"输入文件不存在：{source}"}, ensure_ascii=False))
        return 1
    if source.suffix.lower() not in (".dwg", ".dxf"):
        print(json.dumps({"ok": False, "error": "只接受 DWG 或 DXF"}, ensure_ascii=False))
        return 1

    root = _resolve_project_root(args.project_root)
    stack = _load_stack(root)
    out_dir = (args.out_dir or (source.parent / f"{source.stem}_真实毫米1比1")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (args.stem or f"{source.stem}_1比1_真实毫米{_number(args.length)}mm").strip()
    stem = Path(stem).name
    if stem.lower().endswith((".dwg", ".dxf")):
        stem = Path(stem).stem
    if not stem or stem in {".", ".."}:
        print(json.dumps({"ok": False, "error": "请提供有效的交付文件名"}, ensure_ascii=False))
        return 1

    deliverables = {
        "dwg": out_dir / f"{stem}.dwg",
        "dxf": out_dir / f"{stem}.dxf",
        "report": out_dir / f"{stem}_质检报告.json",
        "summary": out_dir / f"{stem}_说明.md",
        "preview_png": out_dir / f"{stem}_预览.png",
        "preview_svg": out_dir / f"{stem}_预览.svg",
    }
    if args.skip_preview:
        deliverables.pop("preview_png")
        deliverables.pop("preview_svg")
    if any(
        path.resolve() == source or (path.exists() and path.samefile(source))
        for path in deliverables.values()
    ):
        print(json.dumps({"ok": False, "error": "交付文件路径与原图相同"}, ensure_ascii=False))
        return 1
    conflicts = [str(path) for path in deliverables.values() if path.exists()]
    if conflicts and not args.force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "交付文件已存在；换 --stem / --out-dir，或加 --force",
                    "conflicts": conflicts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    source_sha = _sha256(source)
    warnings: list[str] = []
    try:
        with _workspace(out_dir, args.keep_work) as work:
            staged = {name: work / path.name for name, path in deliverables.items()}
            prepared = _prepare_clean_dxf(stack, source, work)
            clean = work / "clean.dxf"

            source_doc = stack.ezdxf.readfile(str(clean))
            source_rows = _numeric_rows(stack, source_doc)
            source_specs = _spec_counts(_collect_text(source_doc), SPEC_MARKERS)
            expected_specs = args.expected_specs or tuple(
                marker for marker, count in source_specs.items() if count > 0
            )

            for label, anchor in (
                ("总长", args.length_anchor),
                ("总高", args.height_anchor),
            ):
                if not any(row["text"] == anchor for row in source_rows):
                    candidates = sorted(
                        {f"{row['text']} ({row['measured']:.3f})" for row in source_rows}
                    )
                    raise ValueError(
                        f"找不到{label}基准标注 {anchor!r}。现有数值标注："
                        + ", ".join(candidates[:20])
                    )

            calibration = stack.rm.set_real_mm(
                clean,
                staged["dxf"],
                length_value=float(args.length),
                length_anchor=str(args.length_anchor),
                height_value=float(args.height),
                height_anchor=str(args.height_anchor),
                align_anchor=str(args.align_anchor),
                view_split_x=args.view_split,
            )

            dwg_export = stack.rm.export_dwg(
                staged["dxf"], staged["dwg"], version="AC1032"
            )
            if not dwg_export.get("ok"):
                raise RuntimeError(f"DWG 导出失败：{dwg_export}")
            dwg_export["output"] = str(deliverables["dwg"])
            dwg_header = staged["dwg"].read_bytes()[:6].decode(
                "ascii", errors="ignore"
            )

            back_dir = work / "readback"
            back_dir.mkdir()
            back_dxf = back_dir / "readback.dxf"
            back_conversion = stack.dwg_export.dwg_to_dxf(
                staged["dwg"], back_dxf
            )
            if not back_conversion.get("ok"):
                raise RuntimeError(f"DWG 回读转换失败：{back_conversion}")

            dxf_doc = stack.ezdxf.readfile(str(staged["dxf"]))
            back_doc = stack.ezdxf.readfile(str(back_dxf))
            dxf_rows = _numeric_rows(stack, dxf_doc)
            back_rows = _numeric_rows(stack, back_doc)
            dxf_views = _view_check(
                stack.rm.part_view_heights(dxf_doc), float(args.height)
            )
            back_views = _view_check(
                stack.rm.part_view_heights(back_doc), float(args.height)
            )
            final_text = _collect_text(back_doc)
            final_specs = _spec_counts(final_text, expected_specs)
            back_watermarks = _watermarks(back_doc)
            back_units = back_doc.header.get("$INSUNITS")
            back_measurement = back_doc.header.get("$MEASUREMENT")

            source_sha_after = _sha256(source)
            checks = {
                "dwg_header": {
                    "passed": dwg_header == "AC1032",
                    "actual": dwg_header,
                },
                "units": {
                    "passed": back_units == 4 and back_measurement == 1,
                    "INSUNITS": back_units,
                    "MEASUREMENT": back_measurement,
                },
                "dxf_audit": {
                    "passed": calibration["audit"]["final"]["errors"] == 0,
                    **calibration["audit"]["final"],
                },
                "view_geometry": dxf_views,
                "annotations": _annotation_check(dxf_rows),
                "target_length_annotation": {
                    "passed": any(
                        abs(float(row["text"]) - float(args.length)) <= 0.051
                        for row in dxf_rows
                    ),
                    "target": float(args.length),
                },
                "spec_preserved": {
                    "passed": all(
                        final_specs.get(marker, 0) >= count
                        for marker, count in source_specs.items()
                        if count > 0
                    ),
                    "source": source_specs,
                    "after": final_specs,
                },
                "watermark_free": {
                    "passed": back_watermarks["entities"] == 0,
                    **back_watermarks,
                },
                "dwg_readback_conversion": {
                    "passed": bool(back_conversion.get("ok")),
                    "header": back_conversion.get("header"),
                    "size": back_conversion.get("size"),
                },
                "dwg_handle_set": _handle_check(dxf_doc, back_doc),
                "dwg_view_geometry": back_views,
                "dwg_annotations": {
                    "passed": (
                        _annotation_signature(dxf_rows)
                        == _annotation_signature(back_rows)
                    ),
                    "dxf_count": len(dxf_rows),
                    "dwg_count": len(back_rows),
                },
                "source_unchanged": {
                    "passed": source_sha == source_sha_after,
                    "sha256": source_sha,
                },
            }
            overall = all(check["passed"] for check in checks.values())

            preview = {"ok": False, "skipped": True}
            if overall and not args.skip_preview:
                preview = _render_preview(
                    root, staged["dxf"], staged["preview_png"]
                )
                if not preview.get("ok"):
                    warnings.append(preview.get("warning", "预览生成失败"))
                for kind in ("png", "svg"):
                    name = f"preview_{kind}"
                    if staged[name].is_file() and staged[name].stat().st_size > 0:
                        preview[kind] = str(deliverables[name])
                    else:
                        deliverables.pop(name)
                        staged.pop(name)

            if not overall:
                failed_checks = [name for name, check in checks.items() if not check["passed"]]
                raise RuntimeError("质检未通过：" + "、".join(failed_checks))

            report = {
                "ok": overall,
                "skill": "cad-real-mm",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source": str(source),
                "source_sha256": source_sha,
                "project_root": str(root),
                "preset": args.preset,
                "targets": {
                    "length": float(args.length),
                    "height": float(args.height),
                    "length_anchor": str(args.length_anchor),
                    "height_anchor": str(args.height_anchor),
                },
                "prepared": {
                    "converter": prepared.get("converter"),
                    "cleaning": prepared.get("cleaning"),
                    "source_annotation_count": len(source_rows),
                },
                "deliverables": {
                    name: str(path) for name, path in deliverables.items()
                },
                "calibration": calibration,
                "dwg_export": dwg_export,
                "readback_conversion": back_conversion,
                "preview": preview,
                "checks": checks,
                "warnings": warnings,
            }
            staged["report"].write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _write_summary(
                staged["summary"],
                source,
                deliverables,
                calibration,
                checks,
                dxf_rows,
                warnings,
            )
            for name in staged:
                if not args.force and deliverables[name].exists():
                    raise FileExistsError(f"交付文件已存在：{deliverables[name]}")
            for name, path in staged.items():
                if name != "report":
                    path.replace(deliverables[name])
            staged["report"].replace(deliverables["report"])

            print(
                json.dumps(
                    {
                        "ok": overall,
                        "dwg": str(deliverables["dwg"]),
                        "dxf": str(deliverables["dxf"]),
                        "report": str(deliverables["report"]),
                        "summary": str(deliverables["summary"]),
                        "preview_png": (
                            str(deliverables["preview_png"])
                            if preview.get("ok")
                            else None
                        ),
                        "views": {
                            "left_height": dxf_views["left"].get("height"),
                            "right_height": dxf_views["right"].get("height"),
                            "left_bottom": dxf_views["left"].get("y0"),
                            "right_bottom": dxf_views["right"].get("y0"),
                        },
                        "calibration": {
                            "kx": calibration["calibration"]["kx"],
                            "ky": calibration["calibration"]["ky"],
                            "view_normalization": calibration["calibration"].get(
                                "view_normalization"
                            ),
                        },
                        "checks": {
                            name: check["passed"] for name, check in checks.items()
                        },
                        "warnings": warnings,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "source": str(source),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
