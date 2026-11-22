from __future__ import annotations

import pytest

from intent_parser import _apply_negation_guard, _rules, is_annotation_task, parse_intent


def test_geometry_targets_from_chinese():
    intent = parse_intent("风罩高度443、接线盒伸出294、轴伸加20", use_llm=False)
    assert intent["targets"] == {"H2": 443.0, "D2": 294.0, "shaft": 20.0}
    assert set(intent["enabled"]) == {"H2", "D2", "shaft"}
    assert intent["source"] == "keyword_fallback"


def test_explicit_geometry_does_not_need_reference_image():
    intent = parse_intent("风罩高度443，接线盒伸出294，轴伸加20", use_llm=False)
    assert intent["enabled"] == ["H2", "D2", "shaft"]
    assert intent["targets"] == {"H2": 443.0, "D2": 294.0, "shaft": 20.0}
    assert intent["questions"] == []


def test_reference_image_phrase_alone_cannot_enable_default_changes():
    intent = parse_intent("按参考图改图", use_llm=False)
    assert intent["enabled"] == []
    assert set(intent["targets"].values()) == {None}
    assert intent["questions"]


@pytest.mark.parametrize("text", [
    "H2 443，注意297不是D2",
    "把H2改成443，D2不动",
    "不改D2，只改H2",
])
def test_d2_negation_is_not_a_change_request(text: str):
    intent = parse_intent(text, use_llm=False)
    assert "D2" not in intent["enabled"]
    assert intent["targets"]["D2"] is None


def test_297_can_still_be_rejected_while_d2_is_explicit():
    intent = parse_intent("不要用297，D2=294", use_llm=False)
    assert "D2" in intent["enabled"]
    assert intent["targets"]["D2"] == 294.0


def test_negation_guard_also_filters_llm_output():
    raw = {
        "targets": {"H2": 443.0, "D2": 294.0, "shaft": None},
        "enabled": ["H2", "D2"],
        "confidence": 0.9,
        "summary": "x",
        "questions": [],
        "source": "llm",
    }
    guarded = _apply_negation_guard("H2 443，注意297不是D2", raw, _rules())
    assert guarded["enabled"] == ["H2"]
    assert guarded["targets"]["D2"] is None


def test_annotation_and_real_mm_are_routed():
    annotation = parse_intent("把标注改成1:1实测", use_llm=False)
    assert is_annotation_task("把标注改成1:1实测")
    assert annotation["task_type"] == "annotation"
    assert annotation["mode"] == "1to1"

    real_mm = parse_intent("按真实毫米出图，总长610，总高284", use_llm=False)
    assert real_mm["task_type"] == "real_mm"
    assert real_mm["mode"] == "real_mm"
    assert real_mm["base_value"] == 610.0
