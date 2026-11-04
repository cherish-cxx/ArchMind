"""工具契约测试（R6 第一阶段）。

只断言 `description` 与 `input_schema` 的形状，不触发任何执行。
这些断言的作用是防止后续改动悄悄改变工具的参数面。
"""

import json

from app.tools.registry import TOOLS

EXPECTED_TOOLS = {"overview", "classes", "class-relations", "call-chain", "method-body"}


def test_tool_names_unchanged():
    assert set(TOOLS) == EXPECTED_TOOLS


def test_every_tool_has_description_and_object_schema():
    for name, spec in TOOLS.items():
        assert spec.description.strip(), f"{name} 缺少给模型看的描述"
        assert spec.input_schema["type"] == "object", f"{name} 的 schema 不是 object"
        json.dumps(spec.input_schema)


def test_overview_and_classes_have_no_required_args():
    assert "required" not in TOOLS["overview"].input_schema
    assert "required" not in TOOLS["classes"].input_schema


def test_relations_and_chain_require_uid():
    assert TOOLS["class-relations"].input_schema["required"] == ["uid"]
    assert TOOLS["call-chain"].input_schema["required"] == ["uid"]


def test_method_body_requires_either_uid_or_method_uid():
    schema = TOOLS["method-body"].input_schema
    assert schema["anyOf"] == [{"required": ["uid"]}, {"required": ["methodUid"]}]


def test_direction_enums_and_defaults_differ_between_tools():
    relations = TOOLS["class-relations"].input_schema["properties"]["direction"]
    chain = TOOLS["call-chain"].input_schema["properties"]["direction"]

    assert relations["enum"] == ["UP", "DOWN", "BOTH"]
    assert relations["default"] == "BOTH"

    assert chain["enum"] == ["DOWN", "UP"]
    assert chain["default"] == "DOWN"


def test_parameter_names_stay_camel_case():
    for name, spec in TOOLS.items():
        for key in spec.input_schema.get("properties", {}):
            assert "_" not in key, f"{name}.{key} 不是 camelCase"


def test_numeric_bounds_match_java_contract():
    assert TOOLS["classes"].input_schema["properties"]["limit"]["maximum"] == 200
    assert TOOLS["class-relations"].input_schema["properties"]["limit"]["maximum"] == 200
    assert TOOLS["call-chain"].input_schema["properties"]["maxNodes"]["maximum"] == 200
    assert TOOLS["method-body"].input_schema["properties"]["maxLines"]["maximum"] == 300
