"""把“把 A 改成 B”落到图纸中真实存在的文字 / 标注句柄。

这一层不猜几何，只做三件事：
1. 从自然语言抽取替换对；
2. 用上传图纸的文本清单做唯一性匹配；
3. 多义时返回候选和追问，不生成可执行修改。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

CONNECTORS = (
    "修改为", "更新为", "变更为", "改成", "改为", "换成", "换为",
    "设成", "设为", "变为", "写成", "标成",
)
_CONNECTOR_RE = re.compile(
    "(" + "|".join(re.escape(item) for item in CONNECTORS) + ")"
)
_CLAUSE_SPLIT = re.compile(r"[，,；;\n]+")
_QUOTED = re.compile(r"[“\"'‘]([^”\"'’]+)[”\"'’]")
FIELD_NAMES = (
    "材料", "材质", "型号", "图号", "编号", "代号", "名称", "零件号",
    "比例", "重量", "日期", "公司名称", "公司", "厂家", "零件材料",
    "总长", "总高", "长度", "高度", "宽度", "厚度", "直径", "半径",
    "尺寸", "轴伸",
)
_FIELD_PATTERN = "|".join(
    re.escape(item) for item in sorted(FIELD_NAMES, key=len, reverse=True)
)
_PREFIX_RE = re.compile(
    r"^(?:请|麻烦|帮我|帮忙|把|将|图纸上|图纸里|图纸中|图中|图里|"
    r"标题栏里的|标题栏中的|标题栏里|标题栏中|这个|该|所有|全部|每一处)"
)
_FIELD_RE = re.compile(r"^(?:" + _FIELD_PATTERN + r")")
_TRAILING_RE = re.compile(r"(?:这个|这一项|这一处|这一条|标注|文字|内容)$")
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_HANDLE_RE = re.compile(
    r"(?:句柄|handle)\s*[:：=]?\s*([0-9A-Za-z]+)\s*(?:里的|中的|里|中|的)?",
    flags=re.IGNORECASE,
)
_LAYER_RE = re.compile(
    r"(?:图层|layer)\s*[:：=]?\s*"
    r"(?:(?P<name>[^\s，,；;。]+?)\s*(?:里的|中的|里|中|的)|"
    r"(?P<bare>[^\s，,；;。]+))",
    flags=re.IGNORECASE,
)
_ORDINAL_RE = re.compile(
    r"第\s*(\d+)\s*(?:个|处|条|项)\s*(?:里的|中的|里|中|的)?"
)
_LABEL_ONLY_RE = re.compile(
    r"(?:只|仅)(?:改|修改|动|调整|更新)?"
    r"(?:这处|这个|该处|该|标注|尺寸|数字|文字|标签|显示)|"
    r"(?:不要|别|不用|无需)(?:改|修改|动)(?:图形|几何|实体|尺寸线)|"
    r"(?:图形|几何|实体)(?:保持|不动|不变|别动|不用动|不要动|不修改)"
)
_GEOMETRY_CHANGE_RE = re.compile(
    r"(?:连|把)?(?:图形|几何|实体)(?:也|一起|同时|都)+(?:改|修改|动|变|更新)|"
    r"(?:图形|几何|实体)(?:要|需要|得)(?:改|修改|更新)|"
    r"真实尺寸|实际尺寸|实物尺寸|真实毫米|按实物|按实际|"
    r"1\s*[:：]\s*1|1\s*比\s*1|一比一|"
    r"(?:标注|尺寸).{0,8}(?:图形|几何).{0,8}(?:一致|一起|同时)"
)


def _display(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _key(value: str) -> str:
    text = _display(value).casefold()
    return re.sub(r"[\s:：,，.。;；'\"“”‘’、_\-—–/\\()（）\[\]【】<>《》]+", "", text)


def _field_only(value: str) -> str | None:
    key = _key(value)
    for field in sorted(FIELD_NAMES, key=len, reverse=True):
        if _key(field) == key:
            return field
    return None


def _field_target(field: str, text: str) -> dict | None:
    """把“材料：Q235”拆成前缀和字段值，避免替换字段名本身。"""
    raw = str(text or "")
    if _key(raw) == _key(field):
        return {
            "prefix": "",
            "value": "",
            "suffix": "",
            "has_value": False,
        }
    pattern = re.compile(
        rf"^\s*(?:零件|产品|部件|设备|机器|整机)?"
        rf"{re.escape(field)}\s*(?:[:：=]\s*|(?=[A-Za-z0-9#ΦφØ\-]))",
        flags=re.IGNORECASE,
    )
    match = pattern.match(raw)
    if not match:
        return None
    value = raw[match.end():]
    return {
        "prefix": raw[:match.end()],
        "value": value,
        "suffix": "",
        "has_value": bool(value.strip()),
    }


def _clean_field_value(field: str, value: str) -> str:
    text = _display(value)
    match = re.match(
        rf"^{re.escape(field)}\s*(?:[:：=]\s*)?(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else text.strip()


def _field_matches(field: str, items: list[dict]) -> list[dict]:
    return [
        item
        for item in items
        if (
            _key(item.get("tag", "")) == _key(field)
            or _field_target(field, str(item.get("text", "") or ""))
        )
    ]


def _tag_matches(value: str, items: list[dict]) -> list[dict]:
    key = _key(value)
    if not key:
        return []
    return [item for item in items if _key(item.get("tag", "")) == key]


def _extract_qualifiers(text: str) -> dict:
    """抽取句柄 / 图层 / 第 N 个等定位条件，用于消歧。"""
    handle = None
    layer = None
    ordinal = None
    match = _HANDLE_RE.search(text)
    if match:
        handle = match.group(1)
    match = _LAYER_RE.search(text)
    if match:
        layer = (match.group("name") or match.group("bare") or "").strip()
    match = _ORDINAL_RE.search(text)
    if match:
        ordinal = int(match.group(1))
    return {"handle": handle, "layer": layer, "ordinal": ordinal}


def _strip_qualifiers(text: str) -> str:
    text = _HANDLE_RE.sub("", str(text or ""))
    text = _LAYER_RE.sub("", text)
    return _ORDINAL_RE.sub("", text).strip()


def _clean_old(value: str) -> str:
    text = _display(value)
    text = text.strip("，,；;。.:：")
    changed = True
    while changed and text:
        changed = False
        match = _PREFIX_RE.match(text)
        if match:
            text = text[match.end():].strip()
            changed = True
        match = _FIELD_RE.match(text)
        if match and len(text) > match.end():
            text = text[match.end():].strip(" ：:")
            changed = True
    while text:
        match = _TRAILING_RE.search(text)
        if not match:
            break
        text = text[:match.start()].strip()
    return text


def _clean_new(value: str) -> str:
    text = _display(value)
    text = text.strip("，,；;。.:：")
    text = re.sub(r"^(?:为|是|成)\s*", "", text)
    quote_pairs = {
        "“": "”",
        "‘": "’",
        '"': '"',
        "'": "'",
        "「": "」",
        "『": "』",
    }
    if (
        len(text) >= 2
        and text[0] in quote_pairs
        and text[-1] == quote_pairs[text[0]]
    ):
        text = text[1:-1].strip()
    return text.strip()


def _after_field_word(value: str) -> list[str]:
    out = []
    text = _display(value)
    for marker in ("里的", "中的"):
        if marker in text:
            out.append(text.split(marker, 1)[1].strip())
    for separator in ("：", ":"):
        if separator in text:
            out.append(text.split(separator, 1)[1].strip())
    return out


def _variants(value: str) -> list[str]:
    raw = _display(value)
    out = [raw, _clean_old(raw)]
    out.extend(_after_field_word(raw))
    for part in re.split(r"[\s,，;；]+", raw):
        if part:
            out.append(_clean_old(part))
    out.extend(_QUOTED.findall(raw))
    if _NUMERIC_RE.fullmatch(raw.strip()):
        out.append(raw.strip())
    result = []
    seen = set()
    for item in out:
        item = item.strip("，,；;。.:：")
        key = _key(item)
        if len(key) < 1 or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def extract_replacements(text: str) -> list[dict]:
    """抽取用户明确说出的替换对。"""
    pairs = []
    for clause in _CLAUSE_SPLIT.split(str(text or "")):
        clause = clause.strip()
        if not clause:
            continue
        match = _CONNECTOR_RE.search(clause)
        if not match:
            continue
        qualifiers = _extract_qualifiers(clause[:match.start()])
        old_raw = _strip_qualifiers(clause[:match.start()].strip())
        new_raw = clause[match.end():].strip()
        quoted = _QUOTED.findall(clause)
        if len(quoted) >= 2:
            old_raw, new_raw = quoted[0], quoted[1]
        old = _clean_old(old_raw)
        new = _clean_new(new_raw)
        if (not old and not qualifiers["handle"]) or not new:
            continue
        pairs.append(
            {
                "old": old,
                "old_raw": old_raw,
                "new": new,
                "all": bool(re.search(r"所有|全部|每一处|全部替换", text)),
                **qualifiers,
            }
        )
    return pairs


def _numeric_text_match(item: dict, number: float) -> bool:
    measurement = item.get("measurement")
    if measurement is not None and abs(float(measurement) - number) <= 1e-6:
        return True
    text = str(item.get("text", "") or "")
    return any(
        abs(float(token) - number) <= 1e-6
        for token in re.findall(r"(?<!\d)-?\d+(?:\.\d+)?(?!\d)", text)
    )


def _find_matches(variant: str, items: list[dict]) -> tuple[list[dict], str]:
    key = _key(variant)
    if not key:
        return [], "none"
    exact = [item for item in items if _key(item.get("text", "")) == key]
    if exact:
        return exact, "exact"
    if _NUMERIC_RE.fullmatch(variant.strip()):
        number = float(variant)
        numeric = [item for item in items if _numeric_text_match(item, number)]
        if numeric:
            return numeric, "numeric"
    substring = [item for item in items if key in _key(item.get("text", ""))]
    if substring:
        return substring, "substring"
    return [], "none"


def _actual_fragment(item_text: str, variant: str) -> str:
    match = re.search(re.escape(variant), item_text, flags=re.IGNORECASE)
    return match.group(0) if match else variant


def _expected_text(item_text: str, fragment: str, new: str, mode: str) -> str:
    if mode == "exact":
        return new
    return _replace_first(item_text, fragment, new)


def _replace_first(text: str, old: str, new: str) -> str:
    match = re.search(re.escape(old), text, flags=re.IGNORECASE)
    if not match:
        return text
    return text[:match.start()] + new + text[match.end():]


def _candidate_payload(item: dict) -> dict:
    return {
        "handle": item.get("handle"),
        "kind": item.get("kind"),
        "text": item.get("text"),
        "tag": item.get("tag"),
        "scope": item.get("scope"),
        "layer": item.get("layer"),
        "measurement": item.get("measurement"),
        "shared": bool(item.get("shared")),
        "insert_count": item.get("insert_count"),
        "is_dimension": bool(item.get("is_dimension")),
        "dimension_source": item.get("dimension_source"),
        "dimension_block": item.get("dimension_block"),
    }


def suggest_instruction(item: dict) -> dict:
    """给扫描结果生成一句可直接补全的改图指令。"""
    text = str(item.get("text", "") or "").strip()
    tag = str(item.get("tag", "") or "").strip()
    hints = []
    if tag:
        label = f"{tag}：{text}" if text else tag
        instruction = f"把{tag}改成"
    else:
        field = None
        for candidate in sorted(FIELD_NAMES, key=len, reverse=True):
            if _field_target(candidate, text):
                field = candidate
                break
        if field:
            label = text
            instruction = f"把{field}改成"
        elif item.get("is_dimension"):
            label = f"尺寸 {text}"
            instruction = f"把「{text}」改成"
        else:
            label = text
            instruction = f"把「{text}」改成"
    if item.get("is_dimension"):
        hints.append("只改显示请补“只改标注文字”")
    if item.get("shared"):
        hints.append("共享块需补“全部修改”")
    hint = "；".join(hints)
    return {
        "handle": item.get("handle"),
        "label": label or str(item.get("handle") or ""),
        "instruction": instruction,
        "hint": hint,
        "is_dimension": bool(item.get("is_dimension")),
        "shared": bool(item.get("shared")),
    }


def _dimension_policy(text: str) -> str | None:
    low = _display(text).casefold()
    label_only = bool(_LABEL_ONLY_RE.search(low))
    geometry_change = bool(_GEOMETRY_CHANGE_RE.search(low))
    if label_only and geometry_change:
        return "conflict"
    if label_only:
        return "label_only"
    if geometry_change:
        return "geometry"
    return None


def parse_text_edit_intent(text: str, inventory: dict | None = None) -> dict:
    """解析通用文字 / 标注替换；只返回唯一命中的可执行操作。"""
    items = list((inventory or {}).get("items") or [])
    pairs = extract_replacements(text)
    policy = _dimension_policy(text)
    if not pairs:
        return {
            "task_type": "text_edit",
            "mode": "text_edit",
            "requested": False,
            "edits": [],
            "ambiguous": [],
            "blocked": [],
            "questions": [],
            "confidence": 0.0,
            "summary": "",
            "source": "drawing_text_match",
            "dimension_policy": policy,
        }

    edits = []
    ambiguous = []
    blocked = []
    questions = []
    used_handles = set()
    for pair in pairs:
        matches = []
        match_mode = "none"
        selected_variant = pair["old"]
        handle = str(pair.get("handle") or "").strip()
        handle_locked = bool(handle)
        pool = items
        if handle:
            pool = [
                item
                for item in items
                if str(item.get("handle") or "").strip().casefold()
                == handle.casefold()
            ]
            if not pool:
                questions.append(
                    f"图纸里没有找到句柄 {handle}。请从扫描结果中重新选择目标。"
                )
                continue
        field = _field_only(pair["old"])
        if not pair["old"]:
            matches = list(pool)
            match_mode = "whole"
        elif field:
            matches = _field_matches(field, pool)
            if matches:
                match_mode = "field"
                selected_variant = field
        elif _tag_matches(pair["old"], pool):
            matches = _tag_matches(pair["old"], pool)
            match_mode = "tag"
            selected_variant = pair["old"]
        else:
            for variant in _variants(pair["old"]):
                found, found_mode = _find_matches(variant, pool)
                if found:
                    matches, match_mode, selected_variant = found, found_mode, variant
                    break
        if matches and pair.get("layer"):
            layer_key = _key(pair["layer"])
            matches = [
                item
                for item in matches
                if _key(item.get("layer", "")) == layer_key
            ]
            if not matches:
                questions.append(
                    f"「{pair['old'] or handle}」在图层「{pair['layer']}」中没有找到。"
                )
                continue
        if matches and pair.get("ordinal"):
            index = int(pair["ordinal"])
            if 1 <= index <= len(matches):
                matches = [matches[index - 1]]
            else:
                questions.append(f"只匹配到 {len(matches)} 处，没有第 {index} 个。")
                continue
        if not matches:
            if handle:
                questions.append(
                    f"句柄 {handle} 的当前文字与「{pair['old']}」不一致。"
                )
            else:
                questions.append(
                    f"图纸里没有找到「{pair['old']}」。请给出图纸中实际显示的文字或数值。"
                )
            continue
        if len(matches) > 1 and not pair.get("all"):
            ambiguous.append(
                {
                    "old": pair["old"],
                    "new": pair["new"],
                    "reason": f"「{pair['old']}」匹配到 {len(matches)} 处",
                    "candidates": [_candidate_payload(row) for row in matches[:8]],
                }
            )
            questions.append(
                f"「{pair['old']}」在图纸里出现 {len(matches)} 次；"
                "请用“句柄XXXX”锁定某一处、说“第2个”，或说“全部改成…”"
            )
            continue
        for item in matches:
            item_handle = str(item.get("handle") or "")
            if not item_handle or item_handle in used_handles:
                continue
            item_text = str(item.get("text", "") or "")
            field_info = None
            replacement = pair["new"]
            if match_mode == "whole":
                fragment = item_text
                mode = "exact"
                expected_text = replacement
            elif match_mode == "tag":
                field = str(item.get("tag") or "")
                field_info = {
                    "prefix": "",
                    "value": "",
                    "suffix": "",
                    "has_value": False,
                }
                fragment = item_text
                mode = "exact"
                expected_text = replacement
            elif match_mode == "field" and field:
                field_info = _field_target(field, item_text)
                if not field_info and _key(item.get("tag", "")) == _key(field):
                    field_info = {
                        "prefix": "",
                        "value": "",
                        "suffix": "",
                        "has_value": False,
                    }
                if not field_info:
                    continue
                replacement = _clean_field_value(field, pair["new"])
                if field_info["has_value"]:
                    fragment = field_info["value"]
                    mode = "replace"
                else:
                    fragment = item_text
                    mode = "exact"
                expected_text = (
                    field_info["prefix"] + replacement + field_info["suffix"]
                )
            elif match_mode == "exact":
                fragment = item_text
                mode = "exact"
            else:
                fragment = _actual_fragment(item_text, selected_variant)
                mode = "replace"
                if _NUMERIC_RE.fullmatch(item_text.strip()):
                    fragment = item_text
                    mode = "exact"
                if pair["new"] == fragment or not fragment:
                    mode = "exact"
                    fragment = item_text
            if not field_info:
                expected_text = _expected_text(
                    item_text, fragment, replacement, mode
                )
            if expected_text == item_text:
                questions.append(
                    f"「{item_text}」的新值与当前显示相同，没有可执行的修改。"
                )
                continue

            if item.get("is_dimension"):
                if policy == "conflict":
                    reason = "尺寸标注修改同时出现“只改标注”和“连图形改”，需要二选一"
                elif policy == "geometry":
                    reason = (
                        "这是尺寸标注；通用链只能改显示文字，不能自动改几何。"
                        "请改用真实毫米链并提供总长/总高，或明确“只改标注文字”"
                    )
                elif policy != "label_only":
                    reason = (
                        "这是尺寸标注；直接改数字只会改标签，不会改几何。"
                        "请明确“只改标注文字”，或说明要连图形一起改"
                    )
                else:
                    reason = None
                if reason:
                    blocked.append(
                        {
                            "old": pair["old"],
                            "new": pair["new"],
                            "reason": reason,
                            "candidate": _candidate_payload(item),
                        }
                    )
                    questions.append(f"「{item_text}」：{reason}。")
                    continue

            if item.get("shared") and not pair.get("all"):
                reason = (
                    f"位于共享块定义 {str(item.get('scope') or '').split(':', 1)[-1]}，"
                    f"被 {item.get('insert_count') or '多'} 处引用；"
                    "改一处会一起变。请明确说“全部修改”"
                )
                blocked.append(
                    {
                        "old": pair["old"],
                        "new": pair["new"],
                        "reason": reason,
                        "candidate": _candidate_payload(item),
                    }
                )
                questions.append(f"「{item_text}」{reason}。")
                continue

            used_handles.add(item_handle)
            edit = {
                "handle": item_handle,
                "kind": item.get("kind"),
                "scope": item.get("scope"),
                "layer": item.get("layer"),
                "old": fragment,
                "new": replacement,
                "mode": mode,
                "text_before": item_text,
                "expected_text": expected_text,
                "shared": bool(item.get("shared")),
                "insert_count": item.get("insert_count"),
                "is_dimension": bool(item.get("is_dimension")),
                "dimension_source": item.get("dimension_source"),
                "dimension_block": item.get("dimension_block"),
                "confidence": (
                    0.98
                    if handle_locked
                    else 0.97
                    if match_mode in {"field", "tag"}
                    else (0.96 if match_mode == "exact" else 0.86)
                ),
            }
            if field_info is not None:
                edit["field_aware"] = True
                edit["field"] = field
            if item.get("is_dimension"):
                edit["dimension_policy"] = "label_only"
                edit["dimension_effect"] = "label_only"
                edit["geometry_effect"] = "none"
            if item.get("shared"):
                edit["shared_effect"] = "all_instances"
            edits.append(edit)

    questions = list(dict.fromkeys(questions))
    if edits and not ambiguous and not questions:
        summary = "；".join(
            f"把「{row['text_before']}」改成「{row['expected_text']}」"
            for row in edits[:5]
        )
        if len(edits) > 5:
            summary += f" 等 {len(edits)} 处"
        confidence = 0.95
    elif edits:
        summary = f"已匹配 {len(edits)} 处，但还有其他修改需要确认"
        confidence = 0.6
    elif questions:
        summary = f"找到 {len(blocked)} 处需要先确认语义的目标"
        confidence = 0.45
    elif ambiguous:
        summary = f"找到 {sum(len(row['candidates']) for row in ambiguous)} 个候选，需要确认具体目标"
        confidence = 0.4
    else:
        summary = "图纸里没有找到明确的待改文字"
        confidence = 0.25

    kinds = Counter(row.get("kind") for row in items)
    return {
        "task_type": "text_edit",
        "mode": "text_edit",
        "requested": True,
        "edits": edits,
        "ambiguous": ambiguous,
        "blocked": blocked,
        "questions": questions,
        "confidence": confidence,
        "summary": summary,
        "source": "drawing_text_match",
        "dimension_policy": policy,
        "inventory": {
            "editable_count": int((inventory or {}).get("editable_count") or len(items)),
            "kinds": dict(kinds),
        },
        "candidates": [
            _candidate_payload(row)
            for row in items[:8]
        ],
    }


def _specialized_ready(intent: dict) -> bool:
    task_type = intent.get("task_type")
    if task_type == "real_mm":
        return intent.get("length_value") is not None
    if task_type == "annotation":
        return bool(intent.get("mode") or intent.get("fix_vertical"))
    return bool(intent.get("enabled"))


def parse_with_drawing(
    text: str,
    inventory: dict | None,
    use_llm: bool = True,
) -> dict:
    """优先使用专业链；专业链不成立时，用真实图纸文字清单兜底。"""
    from intent_parser import parse_intent

    specialized = parse_intent(text, use_llm=use_llm)
    generic = parse_text_edit_intent(text, inventory)
    if _specialized_ready(specialized):
        specialized.setdefault("source", "specialized")
        return specialized
    if generic.get("requested"):
        return generic
    return specialized
