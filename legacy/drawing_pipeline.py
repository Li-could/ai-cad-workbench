"""端到端流水线：DWG/DXF → 解析 → 需求匹配知识卡 → 生成变更计划草稿。

用法：
  python src/drawing_pipeline.py tests/sample.dxf "把H2做到443，D2改成294，轴伸加20"
  python src/drawing_pipeline.py path/to/TD50-40G.dwg "风罩高度443"

输出：output/change_plan_draft.json —— 变更计划草稿（未填 delta 真值，需接入原始CAD实测）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from knowledge_base import KnowledgeBase
from dxf_parser import auto_parse
from dwg_converter import convert_dwg_to_dxf, guide

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"


def load_drawing(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".dwg":
        dxf = convert_dwg_to_dxf(path)
        print(f"[转换] DWG -> DXF: {dxf}")
        return auto_parse(dxf)
    return auto_parse(path)


def build_plan(kb: KnowledgeBase, parsed: dict[str, Any], request: str) -> dict[str, Any]:
    matches = kb.match(request)
    changes = []
    for m in matches:
        c = m.card
        changes.append({
            "rule_id": c["rule_id"],
            "matched_terms": m.hit_terms,
            "measure": c["measure"]["definition"],
            "direction": c["direction"]["label"],
            "target": c["target"],
            "op": c["action"]["op"],
            "object": c["action"]["object"],
            "delta": {
                "formula": c["delta"]["formula"],
                "source_of_truth": c["delta"]["source_of_truth"],
                "disallow_pixel_estimate": c["delta"]["disallow_pixel_estimate"],
                "measured_current": None,
                "status": "pending_source_of_truth",
            },
            "invariants": c["invariants"],
            "forbidden": c["forbidden"],
        })

    # 交叉核对图纸中出现的尺寸数字，提示潜在混淆（如 297 非 D2）
    sizes = parsed.get("size_index", {})
    warnings = []
    if "297" in sizes and any(c["rule_id"] == "D2_RADIAL_TERMINAL_BOX" for c in [m.card for m in matches]):
        warnings.append("图纸中发现标注297，但297不是D2；不得用297替换为294（依据 D2.forbidden）。")
    if any(c["rule_id"] == "SHAFT_EXT_PLUS20" for c in [m.card for m in matches]):
        warnings.append("轴伸+20不得加到风罩H2，也不得拉长虚线框内支撑/法兰（依据 免疫约束）。")

    unmatched = [c["rule_id"] for c in kb.cards if c["rule_id"] not in [x["rule_id"] for x in changes]]

    return {
        "task_id": "AUTO-" + Path(parsed["source_file"]).stem,
        "drawing": kb.cards_doc.get("drawing", {}),
        "user_request": request,
        "parser": parsed.get("parser"),
        "matched_rules": [m.card["rule_id"] for m in matches],
        "unmatched_rules": unmatched,
        "changes": changes,
        "warnings": warnings,
        "validation": {
            "need_manual_confirmation": True,
            "reason": "delta 真值需从原始CAD量取；本草稿未填 measured_current",
            "global_rules": kb.cards_doc.get("global_rules", []),
        },
    }


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: python drawing_pipeline.py <DWG或DXF路径> <用户需求描述>")
        print("\n" + guide())
        sys.exit(1)

    drawing_path = Path(sys.argv[1])
    request = sys.argv[2]
    if not drawing_path.exists():
        print(f"文件不存在: {drawing_path}")
        print(guide())
        sys.exit(1)

    kb = KnowledgeBase()
    try:
        parsed = load_drawing(drawing_path)
    except RuntimeError as exc:
        print(f"[无法解析图纸] {exc}")
        print("\n提示：若暂无转换器，可先用 CAD 软件把 DWG 另存为 DXF，再运行：")
        print(f"  python src/drawing_pipeline.py <你的文件.dxf> \"{request}\"")
        sys.exit(2)
    print(f"[解析] 后端={parsed.get('parser')} 实体数={parsed.get('counts', {}).get('entities')}")
    for m in kb.match(request):
        print(f"[匹配] {m.card['rule_id']} (score={m.score}, hits={m.hit_terms})")

    plan = build_plan(kb, parsed, request)
    OUTPUT.mkdir(exist_ok=True)
    out = OUTPUT / "change_plan_draft.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[输出] 变更计划草稿: {out}")
    if plan["warnings"]:
        print("[提示]")
        for w in plan["warnings"]:
            print("  ! " + w)


if __name__ == "__main__":
    main()
