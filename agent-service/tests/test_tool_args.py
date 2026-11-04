"""参数校验与归一化的表驱动测试（R6）。

这些测试只覆盖纯函数，不触达网络、上下文或循环。
"""

import json

import pytest

from app.tools.args import normalize_args
from app.tools.errors import ErrorReason, ToolError, to_tool_message


# ==================== 工具名 ====================


def test_unknown_tool_lists_available():
    with pytest.raises(ToolError) as ei:
        normalize_args("does-not-exist", {})
    assert ei.value.reason == ErrorReason.UNKNOWN_TOOL
    assert "call-chain" in ei.value.hint


# ==================== overview ====================


def test_overview_accepts_empty_args():
    result = normalize_args("overview")
    assert result.args == {}
    assert result.notes == ()


def test_overview_rejects_any_argument():
    with pytest.raises(ToolError) as ei:
        normalize_args("overview", {"limit": 1})
    assert ei.value.reason == ErrorReason.INVALID_ARGUMENTS
    assert ei.value.field == "limit"


# ==================== classes ====================


def test_classes_applies_default_limit():
    assert normalize_args("classes").args == {"limit": 40}


def test_classes_coerces_numeric_string_and_records_note():
    result = normalize_args("classes", {"limit": "10"})
    assert result.args == {"limit": 10}
    assert any("字符串转整数" in note for note in result.notes)


def test_classes_clamps_out_of_range():
    assert normalize_args("classes", {"limit": 999}).args == {"limit": 200}
    assert normalize_args("classes", {"limit": 0}).args == {"limit": 1}


def test_classes_rejects_bool_and_non_numeric():
    with pytest.raises(ToolError):
        normalize_args("classes", {"limit": True})
    with pytest.raises(ToolError):
        normalize_args("classes", {"limit": "many"})


# ==================== class-relations ====================


def test_class_relations_requires_uid():
    with pytest.raises(ToolError) as ei:
        normalize_args("class-relations")
    assert ei.value.field == "uid"


def test_class_relations_defaults_direction_to_both():
    result = normalize_args("class-relations", {"uid": "a.B"})
    assert result.args == {"uid": "a.B", "direction": "BOTH", "limit": 40}


def test_class_relations_normalizes_enum_case():
    result = normalize_args(
        "class-relations", {"uid": "a.B", "direction": "down"}
    )
    assert result.args["direction"] == "DOWN"
    assert any("大小写" in note for note in result.notes)


def test_class_relations_rejects_bad_enum():
    with pytest.raises(ToolError) as ei:
        normalize_args("class-relations", {"uid": "a.B", "direction": "SIDEWAYS"})
    assert ei.value.field == "direction"


def test_class_relations_collapses_method_uid_to_owner():
    result = normalize_args("class-relations", {"uid": "a.B#run()"})
    assert result.args["uid"] == "a.B"
    assert any("收敛" in note for note in result.notes)


def test_class_relations_rejects_empty_uid_and_unknown_key():
    with pytest.raises(ToolError):
        normalize_args("class-relations", {"uid": "   "})
    with pytest.raises(ToolError) as ei:
        normalize_args("class-relations", {"uid": "a.B", "query": "x"})
    assert ei.value.field == "query"


# ==================== call-chain ====================


def test_call_chain_defaults():
    result = normalize_args("call-chain", {"uid": "a.B"})
    assert result.args == {
        "uid": "a.B",
        "direction": "DOWN",
        "maxDepth": 2,
        "maxNodes": 40,
    }


def test_call_chain_rejects_both_direction():
    with pytest.raises(ToolError) as ei:
        normalize_args("call-chain", {"uid": "a.B", "direction": "BOTH"})
    assert ei.value.field == "direction"


def test_call_chain_clamps_depth_and_nodes():
    result = normalize_args(
        "call-chain", {"uid": "a.B", "maxDepth": 99, "maxNodes": 999}
    )
    assert result.args["maxDepth"] == 5
    assert result.args["maxNodes"] == 200


def test_call_chain_method_uid_must_belong_to_uid():
    ok = normalize_args(
        "call-chain", {"uid": "a.B#run()", "methodUid": "a.B#run()"}
    )
    assert ok.args["uid"] == "a.B"

    with pytest.raises(ToolError) as ei:
        normalize_args(
            "call-chain", {"uid": "a.B", "methodUid": "a.C#run()"}
        )
    assert ei.value.field == "methodUid"


# ==================== method-body ====================


def test_method_body_requires_uid_or_method_uid():
    with pytest.raises(ToolError) as ei:
        normalize_args("method-body")
    assert ei.value.field == "uid"


def test_method_body_accepts_uid_only():
    result = normalize_args("method-body", {"uid": "a.B"})
    assert result.args == {"uid": "a.B", "maxMethods": 3, "maxLines": 120}


def test_method_body_accepts_method_uid_only():
    result = normalize_args("method-body", {"methodUid": "a.B#run()"})
    assert result.args["methodUid"] == "a.B#run()"


def test_method_body_rejects_malformed_method_uid():
    with pytest.raises(ToolError) as ei:
        normalize_args("method-body", {"methodUid": "a.B"})
    assert ei.value.field == "methodUid"


def test_method_uid_without_signature_is_rejected_locally():
    """模型会照着检索候选里的裸方法名拼 uid —— 少了参数列表 Java 就找不到方法。

    本地拦住，别让它白打一次 Java 再失败（实测每次都烧掉一次工具预算）。
    """
    with pytest.raises(ToolError) as ei:
        normalize_args("method-body", {"methodUid": "a.B#resolveSourceDir"})
    assert ei.value.field == "methodUid"
    assert "参数类型" in ei.value.hint


def test_method_body_clamps_line_budget():
    result = normalize_args("method-body", {"uid": "a.B", "maxLines": 999})
    assert result.args["maxLines"] == 300


# ==================== 错误渲染 ====================


def test_error_renders_structured_json_for_the_model():
    err = ToolError.invalid_arguments(field="direction", hint="只能是 UP/DOWN")
    payload = json.loads(to_tool_message(err))
    assert payload == {
        "ok": False,
        "reason": "invalid_arguments",
        "hint": "只能是 UP/DOWN",
        "field": "direction",
    }


def test_missing_anchor_has_its_own_reason():
    err = ToolError.missing_anchor("call-chain")
    assert err.reason == ErrorReason.MISSING_ANCHOR
    assert err.field == "uid"


def test_normalization_is_pure_and_repeatable():
    first = normalize_args("class-relations", {"uid": "a.B#run()"})
    second = normalize_args("class-relations", {"uid": "a.B#run()"})
    assert first == second
