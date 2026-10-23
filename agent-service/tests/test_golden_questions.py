"""R1 golden question 数据集的 schema 校验。

这里只保证「评测集本身是自洽的」：字段齐全、枚举合法、引用的类在 fixture 里真实存在。
真正的意图/工具/延迟打分在 R4 的评测脚本里做。
"""

import json
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).with_name("golden_questions.json")
REPO_ROOT = Path(__file__).resolve().parents[2]

ALLOWED_INTENTS = {
    "EXPLORE_OVERVIEW",
    "EXPLORE_FLOW",
    "EXPLORE_RELATION",
    "NAVIGATE",
    "OUT_OF_SCOPE",
}
ALLOWED_TOOLS = {"overview", "classes", "class-relations", "call-chain", "method-body"}
REQUIRED_FIELDS = {
    "id",
    "question",
    "expectedIntent",
    "expectedToolCalls",
    "expectedAnchorUids",
    "mustDiscloseGap",
}


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_fixture_declared(golden):
    assert golden["version"] == 1
    fixture = REPO_ROOT / golden["projectFixture"]
    assert fixture.is_dir(), f"golden fixture 不存在: {fixture}"


def test_questions_schema(golden):
    questions = golden["questions"]
    assert len(questions) == 5, "R1 约定 5 条 golden question"
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "id 不能重复"

    for q in questions:
        missing = REQUIRED_FIELDS - set(q)
        assert not missing, f"{q.get('id')} 缺少字段: {missing}"
        assert q["expectedIntent"] in ALLOWED_INTENTS
        assert isinstance(q["expectedToolCalls"], list)
        assert set(q["expectedToolCalls"]) <= ALLOWED_TOOLS
        assert isinstance(q["expectedAnchorUids"], list)
        assert isinstance(q["mustDiscloseGap"], bool)
        assert q["question"].strip()


def test_anchor_classes_exist(golden):
    java_root = REPO_ROOT / golden["projectFixture"] / "src" / "main" / "java"
    for q in golden["questions"]:
        for uid in q["expectedAnchorUids"]:
            relative = Path(*uid.split(".")).with_suffix(".java")
            assert (java_root / relative).is_file(), f"{q['id']} 的锚点不存在: {uid}"
