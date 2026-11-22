"""需求 -> 结构化修改计划（项目建设方案第五、六、七章）。

输入：用户自然语言 + 参考图要求 + 图纸解析结果（含真实 handle）。
输出：受控的修改指令对象（JSON），白名单操作，每条带目标/参数/风险/验证/禁止项。

知识来源：knowledge/change_cards.json —— 字段严格按该文件实际结构解析：
  rule_id / aliases / measure{datum_from,datum_to,definition,plain} /
  target{value,unit,operator,tolerance_mm} / action{op,object,detail,geometry_hint} /
  invariants / forbidden[{rule,reason}] / direction / delta / dependencies / evidence
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CARDS_PATH = ROOT / "knowledge" / "change_cards.json"

# action.op -> 白名单操作映射
OP_MAP = {
    "deform": "stretch_entity",
    "move": "move_entity",
    "extend": "stretch_entity",
    "update_text": "update_text",
    "update_dimension": "update_dimension",
}


def _load_knowledge() -> tuple[dict[str, dict], list[str]]:
    data = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    cards = {c["rule_id"]: c for c in data.get("cards", [])}
    global_rules = [r.get("rule", "") if isinstance(r, dict) else str(r)
                    for r in data.get("global_rules", [])]
    return cards, global_rules


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def detect_requested(text: str, cards: dict[str, dict]) -> list[str]:
    """识别用户要改哪几条规则（基于知识卡 aliases + 兜底关键词）。"""
    low = text.lower()
    hit = []
    for rid, card in cards.items():
        aliases = list(card.get("aliases", []))
        if any(a.lower() in low for a in aliases):
            hit.append(rid)
    # 只有明确说“全部/三处/三色”才批量启用；参考图本身不是数值来源。
    if any(k in low for k in ("全部", "三处", "三色")) and len(hit) >= 2:
        hit = list(cards.keys())
    return list(dict.fromkeys(hit))


def _find_target(parsed: dict, card: dict) -> tuple[str, float, str, str]:
    """在解析结果里定位实体：返回 (handle, confidence, evidence, matched_text)。"""
    idx = parsed.get("size_index", {})
    entities = parsed.get("entities", [])
    rid = card.get("rule_id", "")

    # 各规则的候选标注文字（原图/参考图中可能出现的写法）
    candidates = {
        "H2_AXIAL_HEIGHT": ["H2", "443", "风罩"],
        "D2_RADIAL_TERMINAL_BOX": ["D2", "294", "接线盒"],
        "SHAFT_EXT_PLUS20": ["E+20", "轴伸", "轴端", "E"],
    }.get(rid, list(card.get("aliases", []))[:3])

    # 1) 实体文字精确/包含匹配
    for e in entities:
        txt = str(e.get("text", ""))
        for c in candidates:
            if c and c in txt:
                h = str(e.get("handle", e.get("id", "PENDING")))
                return h, 0.85, f"实体文字匹配:{txt}", txt
    # 2) 尺寸图谱匹配
    for k, hits in idx.items():
        for c in candidates:
            if c and c.lower() in str(k).lower():
                h = str(hits[0].get("handle", "PENDING")) if hits else "PENDING"
                return h, (0.75 if h != "PENDING" else 0.5), f"尺寸图谱匹配:{k}", str(k)
    # 3) 图层名匹配（罩壳/接线盒/轴）
    layer_hint = {"H2_AXIAL_HEIGHT": ["罩", "风罩", "H2"],
                  "D2_RADIAL_TERMINAL_BOX": ["接线盒", "D2"],
                  "SHAFT_EXT_PLUS20": ["轴", "轴伸"]}.get(rid, [])
    for e in entities:
        lyr = str(e.get("layer", ""))
        if any(l in lyr for l in layer_hint):
            h = str(e.get("handle", e.get("id", "PENDING")))
            return h, 0.55, f"图层线索:{lyr}", str(e.get("text", ""))
    return "PENDING", 0.3, "未定位到实体，需人工点选确认", ""


def _forbidden_texts(card: dict) -> list[str]:
    out = []
    for f in card.get("forbidden", []):
        if isinstance(f, dict):
            out.append(f"{f.get('rule','')}（{f.get('reason','')}）")
        else:
            out.append(str(f))
    return out


def build_plan(text: str, parsed: dict, task_id: str, source_name: str) -> dict:
    cards, global_rules = _load_knowledge()
    requested = detect_requested(text, cards)
    ops: list[dict[str, Any]] = []
    items = []

    for i, rid in enumerate(requested, 1):
        card = cards[rid]
        measure = card.get("measure", {})
        target = card.get("target", {})
        action = card.get("action", {})
        tv = target.get("value")
        unit = target.get("unit", "mm")
        tol = target.get("tolerance_mm", 0.5)

        handle, conf, evidence, matched = _find_target(parsed, card)
        need_confirm = (handle == "PENDING") or conf < 0.8 or target.get("operator") == "formula"

        # 目标文字：保留原标注格式习惯，仅把目标值写清
        if rid == "H2_AXIAL_HEIGHT":
            new_text = f"H2 = {tv}".strip()
        elif rid == "D2_RADIAL_TERMINAL_BOX":
            new_text = f"D2 = {tv}".strip()
        elif rid == "SHAFT_EXT_PLUS20":
            new_text = f"轴伸 {target.get('base','E')}+{target.get('add',20)}"
        else:
            new_text = f"{tv} {unit}".strip()

        items.append({
            "id": f"REQ-{i:02d}",
            "text": card.get("color_marker", "") + " " + measure.get("definition", rid),
            "confidence": conf, "source": "user_text",
            "evidence": evidence,
        })
        ops.append({
            "op_id": f"OP-{i:02d}",
            "operation": OP_MAP.get(action.get("op", ""), "update_text"),
            "target": {
                "handle": handle, "entity_type": "DIMENSION",
                "text_match": matched or None,
                "near": measure.get("definition", ""),
                "locate_confidence": conf,
                "layer_hint": card.get("direction", ""),
            },
            "params": {
                "new_text": new_text,
                "target_value": tv if isinstance(tv, (int, float)) else None,
                "unit": unit, "tolerance_mm": tol,
                "object": action.get("object", ""),
                "geometry_hint": action.get("geometry_hint", ""),
                "keep": card.get("invariants", []),
                "direction": card.get("direction", ""),
            },
            "reason": f"{card.get('color_marker','')}：{measure.get('plain','')}",
            "forbidden": _forbidden_texts(card),
            "risk": "high" if need_confirm else "medium",
            "requires_confirmation": need_confirm,
            "verification": f"重新打开核对 {measure.get('definition','')} 目标 {tv}{unit}；检查 invariants 与 forbidden 未触发",
        })

    if not ops:
        items.append({"id": "REQ-01", "text": text or "（空需求）", "confidence": 0.0,
                      "source": "user_text", "evidence": "未能映射到已知变更规则"})
        ops.append({
            "op_id": "OP-01", "operation": "update_text",
            "target": {"handle": "PENDING", "locate_confidence": 0.0},
            "params": {}, "reason": "需求无法自动映射",
            "forbidden": ["禁止在未明确定位与数值时修改图纸"],
            "risk": "high", "requires_confirmation": True,
            "verification": "需用户补充说明要改哪个对象、改成什么",
        })

    return {
        "task_id": task_id,
        "source_file": source_name,
        "plan_status": "awaiting_confirmation" if any(o["requires_confirmation"] for o in ops) else "draft",
        "created_at": _now(),
        "requirement": {"user_text": text, "items": items},
        "operations": ops,
        "global_invariants": global_rules + [
            "原始 DWG 只读保存，所有修改在副本执行",
            "只修改明确要求涉及的实体，范围外对象保持不变",
        ],
        "verification": {
            "reopen_test": True, "entity_count_check": True,
            "dimension_checks": [
                {"dimension": "H2", "expected": 443},
                {"dimension": "D2", "expected": 294},
            ],
            "unchanged_region_check": True,
        },
    }


if __name__ == "__main__":
    import sys
    txt = sys.argv[1] if len(sys.argv) > 1 else "按参考图修改：H2做到443，D2改成294，轴伸加20"
    parsed = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else {}
    print(json.dumps(build_plan(txt, parsed, "demo", "TD50-40G.dwg"), ensure_ascii=False, indent=2))
