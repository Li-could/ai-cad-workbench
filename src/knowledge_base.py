"""知识库加载与检索模块（纯标准库，无第三方依赖）。

职责：
1. 加载 knowledge/*.json 并做基本结构校验（不依赖 jsonschema）。
2. 按用户口语需求检索匹配的变更知识卡（别名 + 领域词典 + 负面词）。
3. 生成可嵌入系统提示词的知识文本片段。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "knowledge"


@dataclass
class MatchResult:
    card: dict[str, Any]
    score: float
    hit_terms: list[str]


class KnowledgeBase:
    def __init__(self, kb_dir: Path = KB_DIR):
        self.kb_dir = kb_dir
        self.cards_doc = self._load("change_cards.json")
        self.lexicon = self._load("domain_lexicon.json")
        self.invariants = self._load("invariants.json")
        self.cards = self.cards_doc.get("cards", [])
        self.validate()

    def _load(self, name: str) -> dict[str, Any]:
        path = self.kb_dir / name
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def validate(self) -> None:
        """轻量结构校验：确保每张卡的关键字段齐备。"""
        required = ["rule_id", "measure", "direction", "target", "delta", "action",
                    "invariants", "forbidden", "evidence"]
        ids: set[str] = set()
        for i, card in enumerate(self.cards):
            missing = [k for k in required if k not in card]
            if missing:
                raise ValueError(f"卡片 #{i} 缺少字段: {missing}")
            if card["rule_id"] in ids:
                raise ValueError(f"重复 rule_id: {card['rule_id']}")
            ids.add(card["rule_id"])
            if not card.get("aliases"):
                raise ValueError(f"{card['rule_id']} 缺少 aliases")

    # ---------- 检索 ----------
    def _synonym_terms(self) -> dict[str, list[str]]:
        """把领域词典里的同义词展开，映射到 canonical rule_id。"""
        mapping: dict[str, list[str]] = {}
        for dim, info in self.lexicon.get("dimensions", {}).items():
            canonical = info.get("canonical")
            if canonical:
                mapping.setdefault(canonical, [])
                mapping[canonical].extend(info.get("synonyms", []))
        return mapping

    def match(self, user_text: str, min_score: float = 1.0) -> list[MatchResult]:
        """按用户口语文本匹配知识卡。返回按得分降序的结果。"""
        text = user_text.strip()
        synonym_map = self._synonym_terms()
        results: list[MatchResult] = []

        for card in self.cards:
            if not card.get("enabled", True):
                continue
            rid = card["rule_id"]
            terms: set[str] = set(card.get("aliases", []))
            terms.update(synonym_map.get(rid, []))

            hits = [t for t in terms if t and t in text]
            score = float(len(hits))
            # 命中"测量定义"里的基准词额外加权
            datum_terms = [card["measure"].get("datum_from", ""),
                           card["measure"].get("datum_to", "")]
            for d in datum_terms:
                if d and d in text:
                    score += 0.5
                    hits.append(d)
            if score >= min_score:
                results.append(MatchResult(card=card, score=score, hit_terms=hits))

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def build_system_prompt(self, matched: Iterable[MatchResult] | None = None) -> str:
        """生成可嵌入 LLM 系统提示词的知识文本（严格约束输出为变更计划 JSON）。"""
        cards = [m.card for m in matched] if matched is not None else self.cards
        card_json = json.dumps(cards, ensure_ascii=False, indent=2)
        return (
            "你是「工程图变更规划助手」。你的任务不是作图，而是把用户要求转成变更计划 JSON。\n"
            "规则：\n"
            "1. 仅依据下方知识卡判断，不得凭图片像素估算尺寸。\n"
            "2. 先识别用户提到的尺寸符号/别名，匹配对应知识卡；需求缺失时主动追问。\n"
            "3. 目标值与改变量必须来自 source_of_truth（原始CAD或电机图），禁止直接替换图上数字。\n"
            "4. 严格遵守每张卡的 invariants（免疫）与 forbidden（禁止）清单。\n"
            "5. 只输出变更计划 JSON，不输出解释性文字。\n\n"
            f"知识卡：\n{card_json}\n"
        )

    def summary(self) -> str:
        lines = [f"知识库版本: {self.cards_doc.get('version')}",
                 f"图纸型号: {self.cards_doc.get('drawing', {}).get('model')}",
                 f"知识卡数量: {len(self.cards)}"]
        for c in self.cards:
            lines.append(f"  - {c['rule_id']}: {c['measure']['definition']} -> 目标 {c['target']['value']}{c['target']['unit']}")
        return "\n".join(lines)


if __name__ == "__main__":
    kb = KnowledgeBase()
    print(kb.summary())
    print("\n--- 检索测试 ---")
    for q in ["风罩高度做到443，轴伸加20", "把接线盒伸出改成294", "H2改成443"]:
        print(f"\n需求: {q}")
        for m in kb.match(q):
            print(f"  -> {m.card['rule_id']} (score={m.score}, hits={m.hit_terms})")
