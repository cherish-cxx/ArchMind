"""讲解结构化输出（R2）—— 解析、逐段校验、message 拼装。

不碰网络：`_parse_and_ground` 是纯函数，和 guard 一样能测到最细。
"""

import json

import pytest

from app.core.explain import ExplainParseError, _parse_and_ground, explain
from app.core.prompt import explain_messages
from app.models.intent import Intent
from fakes import FakeLLM, ok_chain

A = "com.demo.A"
B = "com.demo.B"
GHOST = "com.demo.Ghost"


def _payload(**overrides) -> str:
    raw = {
        "headline": "A 只调用了 B",
        "sections": [
            {"title": "调用路径", "body": "`A` 调用 `B`。", "citedUids": [A, B]},
            {"title": "边界", "body": "`B` 之后没有下游。", "citedUids": []},
        ],
    }
    raw.update(overrides)
    return json.dumps(raw, ensure_ascii=False)


def test_new_shape_parsed_and_message_assembled():
    r = _parse_and_ground(_payload(), {A, B})

    assert r.headline == "A 只调用了 B"
    assert [s.title for s in r.sections] == ["调用路径", "边界"]
    assert [c.uid for c in r.citations] == [A, B], "顶层 citations 应是各段并集且去重"
    assert r.message == (
        "## A 只调用了 B\n\n### 调用路径\n\n`A` 调用 `B`。"
        "\n\n### 边界\n\n`B` 之后没有下游。"
    )
    assert r.degraded is False


def test_ghost_uid_dropped_only_from_its_section(caplog):
    text = json.dumps({
        "headline": "h",
        "sections": [
            {"title": "有脏引用", "body": "a", "citedUids": [A, GHOST]},
            {"title": "干净", "body": "b", "citedUids": [B]},
        ],
    })

    with caplog.at_level("ERROR"):
        r = _parse_and_ground(text, {A, B})

    assert r.sections[0].cited_uids == [A]
    assert r.sections[1].cited_uids == [B], "别的段不该被牵连"
    assert any("有脏引用" in rec.getMessage() for rec in caplog.records), \
        "日志必须能定位到是哪一段编的"


def test_empty_section_dropped_but_others_survive():
    text = json.dumps({"sections": [
        {"title": "空", "body": "   ", "citedUids": [A]},
        {"title": "有内容", "body": "x", "citedUids": [B]},
    ]})

    r = _parse_and_ground(text, {A, B})

    assert [s.title for s in r.sections] == ["有内容"]
    assert [c.uid for c in r.citations] == [B]


def test_legacy_shape_still_parses():
    """旧形状退化成单个无标题段落 —— 不因为它老就让整轮降级到模板。"""
    r = _parse_and_ground(
        json.dumps({"message": "一段旧格式的话。", "citedUids": [A]}), {A}
    )

    assert len(r.sections) == 1
    assert r.sections[0].title == ""
    assert r.sections[0].body == "一段旧格式的话。"
    assert r.message == "一段旧格式的话。", "没有标题时不该凭空加 #"
    assert [c.uid for c in r.citations] == [A]


@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    '{"sections": []}',
    '{"sections": [{"body": "   "}]}',
    '{"message": ""}',
    '{"sections": ["not-an-object"]}',
])
def test_unusable_output_raises(text):
    with pytest.raises(ExplainParseError):
        _parse_and_ground(text, set())


async def test_garbage_falls_back_to_template():
    """LLM 吐垃圾 → 模板降级，sections 为空（前端据此回落到 message）。"""
    r = await explain(Intent.EXPLORE_FLOW, [ok_chain()], {A, B}, "问", FakeLLM(["oops"]))

    assert r.degraded is True
    assert r.sections == []
    assert r.message, "降级也必须给出一句人话"


def test_prompt_carries_the_new_contract():
    system = explain_messages(Intent.EXPLORE_FLOW, [], "问")[0]["content"]

    for token in ("headline", "sections", "citedUids", "json"):
        assert token in system

    assert "不要输出 markdown 标题" not in system, "旧规则会与 Markdown 正文冲突"
    assert "反引号" in system, "List<String> 不包反引号会被前端当 HTML 标签吞掉"
    # 受众与结构：R2 的核心约束，删掉就会退回「罗列类名」的旧形态
    assert "刚接手这个项目" in system
    assert "先结论，再路径" in system
    assert "禁止只罗列类名" in system
