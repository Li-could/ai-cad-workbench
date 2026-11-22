"""自然语言意图解析：把用户一句话转成受控几何修改的 targets/enabled。

优先调用阿里云 MaaS(qwen-plus，OpenAI 兼容接口)，把 skill/rules.json 的领域知识
注入 system prompt；模型不可用时回退到确定性的关键词+正则解析，保证工作台离线可用。

输出严格白名单校验：只接受 H2/D2/shaft 三个 key，数值必须在合理区间，
未给数值的规则沿用 rules.json 的 default_target_mm。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import runtime

runtime.bootstrap()

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "skill" / "rules.json"

# 合理区间（单位 mm），超出视为解析错误
RANGES = {"H2": (100.0, 2000.0), "D2": (50.0, 2000.0), "shaft": (0.0, 200.0)}
KEYS = ("H2", "D2", "shaft")


def _rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _llm_config() -> tuple[str, str, str]:
    key = runtime.llm_key()
    url = os.environ.get("DASHSCOPE_BASE_URL") or os.environ.get("ALIYUN_MAAS_URL",
                         "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
    model = os.environ.get("DASHSCOPE_MODEL") or os.environ.get("ALIYUN_MAAS_MODEL", "qwen-plus")
    return key, url, model


def _build_system_prompt(rules: dict) -> str:
    rule_lines = []
    for r in rules["rules"]:
        rule_lines.append(
            f"- {r['key']}（{r['rule_id']}）：{r['meaning']}；别名：{', '.join(r['aliases'])}；"
            f"默认目标 {r['default_target_mm']}{r['unit']}"
        )
    policy = rules["parsing_policy"]
    return (
        "你是工程图修改意图解析器。把用户一句话解析为严格 JSON，不要输出任何解释文字。\n"
        "只输出如下结构：\n"
        '{"targets":{"H2":数值或null,"D2":数值或null,"shaft":数值或null},'
        '"enabled":["H2"或"D2"或"shaft"...],"confidence":0到1,"summary":"一句话复述",'
        '"questions":["需追问的问题"]}\n'
        "规则：\n"
        "1. targets 的 key 只能是 H2/D2/shaft；用户没提到的填 null，不要臆造。\n"
        "2. shaft 指'在标准轴伸 E 基础上再加多少 mm'，用户说'轴伸加20'则 shaft=20，不是轴伸总长。\n"
        "3. 用户提到某规则但没给数值：enabled 包含它，targets 该 key 填 null（工作台将用默认目标值）。\n"
        "4. 只有用户明确点名“三处/全部/三色”并给出目标值时才可批量启用；"
        "“按参考图”不是数值来源，参考图是可选的，不能据此猜测目标值。\n"
        "5. 完全无法判断要改哪个或改多少：enabled=[]，questions 写清要追问什么。\n"
        "6. 原图297不是D2。若用户说'把297改成294'，这是误解，questions 里提示297与D2不是同一尺寸。\n"
        "7. 数值单位都是毫米(mm)，只填纯数字。\n\n"
        "可用规则：\n" + "\n".join(rule_lines) + "\n"
        f"数值合理区间：H2 {RANGES['H2']}，D2 {RANGES['D2']}，shaft {RANGES['shaft']}。\n"
        f"歧义策略：{policy['ambiguous']}\n"
    )


def _validate(raw: dict, rules: dict) -> dict:
    """白名单校验 + 默认值回填，返回规范化的意图对象。"""
    defaults = {r["key"]: r["default_target_mm"] for r in rules["rules"]}
    if not isinstance(raw, dict):
        raise ValueError("模型输出不是对象")

    targets_in = raw.get("targets") if isinstance(raw.get("targets"), dict) else {}
    enabled_raw = raw.get("enabled") if isinstance(raw.get("enabled"), list) else []

    enabled = [k for k in enabled_raw if k in KEYS]
    # 去重保序
    enabled = list(dict.fromkeys(enabled))

    targets: dict[str, float | None] = {}
    questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
    questions = [str(q) for q in questions][:4]

    for k in KEYS:
        v = targets_in.get(k, None)
        if v is None or v == "":
            targets[k] = None
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            targets[k] = None
            questions.append(f"{k} 的数值无法识别，请确认")
            continue
        lo, hi = RANGES[k]
        if not (lo <= num <= hi):
            targets[k] = None
            questions.append(f"{k}={num:g} 超出合理区间 {lo}~{hi}mm，请确认")
        else:
            targets[k] = num

    # enabled 但没数值 -> 用默认目标值
    for k in enabled:
        if targets.get(k) is None:
            targets[k] = defaults[k]

    try:
        confidence = float(raw.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))

    summary = str(raw.get("summary", "")).strip() or _fallback_summary(enabled, targets)
    if not enabled and not questions:
        questions.append(
            "请说明要修改的对象和数值，例如“风罩高度443、接线盒伸出294、轴伸加20”；"
            "参考图可选。"
        )

    return {
        "targets": {k: targets.get(k) for k in KEYS},
        "enabled": enabled,
        "confidence": round(confidence, 2),
        "summary": summary,
        "questions": questions,
        "source": "llm",
    }


def _fallback_summary(enabled: list, targets: dict) -> str:
    label = {"H2": "风罩总高", "D2": "接线盒径向伸出", "shaft": "轴伸加长"}
    parts = []
    for k in enabled:
        v = targets.get(k)
        parts.append(f"{label[k]}" + (f"={v:g}mm" if v is not None else ""))
    return "将修改：" + "、".join(parts) if parts else "未识别到明确修改"


def _call_llm(text: str, rules: dict) -> dict:
    key, url, model = _llm_config()
    if not key:
        raise RuntimeError("未配置 ALIYUN_MAAS_API_KEY")
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt(rules)},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    # 去掉可能的代码围栏
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return _validate(json.loads(content), rules)


_NEGATION_BEFORE = re.compile(
    r"(?:不是|并非|非|不改|不动|不要改|无需改|不得改|保持|维持)\s*[“\"']?\s*$")
_NEGATION_AFTER = re.compile(
    r"^\s*(?:不是|不动|不改|保持不变|保持|不变|无需改|不用改|不能改)")


def _alias_enabled(low: str, alias: str) -> bool:
    """别名出现在否定语境里时不算启用（例如“297 不是 D2”“D2 不动”）。"""
    key = str(alias).lower()
    if not key:
        return False
    for match in re.finditer(re.escape(key), low):
        left = low[max(0, match.start() - 8):match.start()]
        right = low[match.end():match.end() + 8]
        if _NEGATION_BEFORE.search(left) or _NEGATION_AFTER.search(right):
            continue
        return True
    return False


def _apply_negation_guard(text: str, result: dict, rules: dict) -> dict:
    """把“297 不是 D2 / D2 不动”等否定语境应用到 LLM 结果。"""
    low = (text or "").lower()
    enabled = list(result.get("enabled") or [])
    targets = dict(result.get("targets") or {})
    for rule in rules.get("rules", []):
        key = rule["key"]
        if key not in enabled:
            continue
        if not any(_alias_enabled(low, alias) for alias in rule.get("aliases", [])):
            enabled.remove(key)
            targets[key] = None
    result["enabled"] = enabled
    result["targets"] = targets
    questions = list(result.get("questions") or [])
    if not enabled and not questions:
        questions.append(
            "请说明要修改的对象和数值，例如“风罩高度443、接线盒伸出294、轴伸加20”；"
            "参考图可选。"
        )
    result["questions"] = questions
    return result


def _fallback_keywords(text: str, rules: dict) -> dict:
    """离线兜底：关键词判规则 + 正则抽数值，行为确定可预测。"""
    defaults = {r["key"]: r["default_target_mm"] for r in rules["rules"]}
    aliases = {r["key"]: r["aliases"] for r in rules["rules"]}
    low = text.lower()

    enabled: list[str] = []
    for k in KEYS:
        if any(_alias_enabled(low, a) for a in aliases[k]):
            enabled.append(k)
    if any(kw in low for kw in ("全部", "三处", "三色")) and len(enabled) >= 2:
        enabled = list(KEYS)

    targets: dict[str, float | None] = {k: None for k in KEYS}

    def number_after(pattern: str) -> float | None:
        match = re.search(pattern, low)
        return float(match.group(1)) if match else None

    # 轴伸加 N：优先 "加/伸出/E+" 后面的数字
    targets["shaft"] = number_after(
        r"(?:轴伸|轴端|轴|e)\s*(?:加|增加|伸出|\+|＋)?\s*(\d+(?:\.\d+)?)")
    # H2：显式 H2，其次"风罩/罩壳/总高"附近数字
    targets["H2"] = (number_after(r"h2\s*(?:改到|做到|改成|为|=|:|：)?\s*(\d+(?:\.\d+)?)")
                     or number_after(
                         r"(?:风罩|罩壳|总高|整体高度)[^0-9]{0,8}(\d+(?:\.\d+)?)"))
    # D2：显式 D2，其次"接线盒伸出"附近数字
    targets["D2"] = (number_after(r"d2\s*(?:改到|做到|改成|为|=|:|：)?\s*(\d+(?:\.\d+)?)")
                     or number_after(
                         r"(?:接线盒|径向伸出|伸出)[^0-9]{0,8}(\d+(?:\.\d+)?)"))

    # 轴伸句式里若同时出现"轴伸"与"加20"，上面的正则已覆盖；
    # 但"轴伸加 20"会被"轴"分支先命中 20，同样正确。

    questions: list[str] = []
    if not enabled:
        questions.append(
            "请说明要修改的对象和数值，例如“风罩高度443、接线盒伸出294、轴伸加20”；"
            "参考图可选。"
        )
    for k in enabled:
        if targets[k] is None:
            targets[k] = defaults[k]
    for k in KEYS:
        v = targets[k]
        if v is not None and not (RANGES[k][0] <= v <= RANGES[k][1]):
            targets[k] = None
            questions.append(f"{k} 数值超出合理区间，请确认")

    return {
        "targets": targets,
        "enabled": enabled,
        "confidence": 0.6 if enabled else 0.0,
        "summary": _fallback_summary(enabled, targets),
        "questions": questions,
        "source": "keyword_fallback",
    }


# ---------------------------------------------------------------- 标注换算类意图
# 几何变形类（H2/D2/shaft）由 rules.json 驱动；标注换算类（比例/单位/竖排修复）
# 是第二条执行链（annotation_engine），这里用确定性规则识别，离线可用。
ANNOTATION_TRIGGERS = (
    "1:1", "1比1", "1：1", "一比一", "1:4", "1比4", "一比四",
    "实测", "量出来", "比例", "换算", "标注改", "标注统一", "单位", "cm",
    "厘米", "毫米", "标在蓝线", "竖排", "重叠", "看不清", "小数点", "字符",
    "基准", "以610", "按610", "610为", "改成cm", "全cm",
    "真实毫米", "毫米版", "实物尺寸", "实际尺寸",
)
REAL_MM_TRIGGERS = (
    "真实毫米", "毫米版", "实物尺寸", "实际尺寸", "总长",
    "1图形单位=1mm", "1单位=1mm",
)


def is_annotation_task(text: str) -> bool:
    """判断是不是标注换算/修复类需求（而非几何变形）。"""
    low = (text or "").lower()
    # 明确的几何规则词优先：出现 H2/D2/轴伸 且没有标注换算词 -> 几何类
    geo_hit = any(k in low for k in ("h2", "d2", "轴伸", "轴端", "风罩", "接线盒"))
    ann_hit = any(k in low for k in ANNOTATION_TRIGGERS)
    return ann_hit and not geo_hit


def parse_annotation_intent(text: str) -> dict:
    """标注换算/修复意图解析（确定性，无需大模型）。

    输出：
      {"task_type":"annotation", "mode":..., "base_value":..., "base_where":...,
       "fix_vertical":bool, "unit":..., "confidence":..., "summary":..., "questions":[]}
    """
    low = (text or "").lower()
    if any(key in low for key in REAL_MM_TRIGGERS):
        m_len = re.search(
            r"(?:总长|外形长|长度)\D{0,8}(\d+(?:\.\d+)?)\s*(?:mm|毫米)?",
            text,
        )
        m_h = re.search(
            r"(?:总高|外形高|高度)\D{0,8}(\d+(?:\.\d+)?)\s*(?:mm|毫米)?",
            text,
        )
        length_value = float(m_len.group(1)) if m_len else None
        height_value = float(m_h.group(1)) if m_h else None
        questions = []
        if length_value is None:
            questions.append("请给出真实毫米总长，例如“总长610mm”")
        summary = "把图形几何标定到真实毫米"
        if length_value is not None:
            summary += f"，总长{length_value:g}mm"
        if height_value is not None:
            summary += f"，总高{height_value:g}mm"
        return {
            "task_type": "real_mm",
            "mode": "real_mm",
            "length_value": length_value,
            "height_value": height_value,
            "length_anchor": "610",
            "height_anchor": "284",
            "base_value": length_value,
            "base_where": None,
            "fix_vertical": True,
            "unit": "mm",
            "confidence": 0.9 if length_value is not None else 0.55,
            "summary": summary,
            "questions": questions,
            "source": "annotation_rule",
        }

    mode = None
    base_value = None
    base_where = None
    questions: list[str] = []

    # 模式判定（优先级：cm > base > 1to4 > 1to1）
    if any(k in low for k in ("cm", "厘米", "÷10", "除以10", "除10")) and "mm" not in low:
        mode = "cm"
    elif any(k in low for k in ("基准", "以610", "按610", "610为", "以某个", "基准值")):
        mode = "base"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|毫米|cm|厘米)?", text)
        # 取提到"基准/610"语境里的数：先找 基准/以 后面的数，再退回全句最大数
        mb = re.search(r"(?:基准|以|按)\s*(\d+(?:\.\d+)?)", text)
        if mb:
            base_value = float(mb.group(1))
        elif m:
            base_value = float(m.group(1))
        if base_value is None:
            questions.append("请告诉我以哪条标注、多少毫米为基准")
    elif any(k in low for k in ("1:4", "1比4", "一比四", "1：4")):
        mode = "1to4"
    elif any(k in low for k in ("1:1", "1比1", "一比一", "1：1", "实测", "量出来", "图形一致")):
        mode = "1to1"

    # 基准值兜底：文字含具体大数且是 base 模式
    if mode == "base" and base_value is None:
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
        if nums:
            base_value = max(nums)

    # 竖排修复判定：提到重叠/看不清/小数点/字符/标在蓝线/竖排 -> 修复
    fix_kw = ("重叠", "看不清", "小数点", "字符", "标在蓝线", "贴蓝线", "竖排", "数字之间", "间距", "修复")
    fix_vertical = any(k in low for k in fix_kw)

    # 若既没识别出模式也没识别出修复 -> 需要追问
    if mode is None and not fix_vertical:
        questions.append("请说明换算方式（1:1实测 / 1:4 / 以某值为基准 / 改成cm），或要修复哪处标注显示")

    summary_map = {
        "1to1": "按严格 1:1 把长度标注改为图形实测跨度",
        "1to4": "按 1:4 标准比例换算长度标注",
        "base": f"以 {base_value:g} 为基准统一换算全图标注" if base_value else "以指定值为基准换算标注",
        "cm": "把长度标注换算成 cm（÷10）",
    }
    if mode and fix_vertical:
        summary = summary_map.get(mode, "") + "，并修复竖向标注字符挤压"
    elif mode:
        summary = summary_map.get(mode, "标注换算")
    else:
        summary = "修复竖向标注显示（字符/小数点挤压）"

    return {
        "task_type": "annotation",
        "mode": mode,
        "base_value": base_value,
        "base_where": base_where,
        "fix_vertical": fix_vertical,
        "unit": "cm" if mode == "cm" else "mm",
        "confidence": 0.85 if mode else 0.6,
        "summary": summary,
        "questions": questions,
        "source": "annotation_rule",
    }


def parse_intent(text: str, use_llm: bool = True) -> dict:
    """主入口：自然语言 -> 意图对象。

    自动分流：标注换算/修复类走确定性规则；几何变形类走大模型+关键词兜底。
    模型失败自动回退关键词解析。
    """
    if is_annotation_task(text):
        return parse_annotation_intent(text)
    rules = _rules()
    if use_llm:
        try:
            return _apply_negation_guard(text, _call_llm(text, rules), rules)
        except Exception as ex:
            fb = _fallback_keywords(text, rules)
            fb["llm_error"] = str(ex)[:160]
            fb["source"] = "keyword_fallback"
            return fb
    return _fallback_keywords(text, rules)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "风罩高度443、接线盒伸出294、轴伸加20"
    print(json.dumps(parse_intent(q), ensure_ascii=False, indent=2))
