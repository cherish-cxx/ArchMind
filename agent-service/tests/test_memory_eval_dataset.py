"""R5 跨会话记忆评测集的 schema 校验。

和 R1 的 `test_golden_questions.py` 同一个思路：这里只保证**评测集本身自洽**，
真正的召回打分在 `scripts/eval/run_memory_recall_eval.py`。

有一条额外约束：问题里**不能出现种子实体的类名**。出现了就变成关键词匹配，
测不出"语义召回"这件事——而这正是 R5 要走向量而不是查表的原因。
"""

import json
from pathlib import Path

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "memory_cases.json"


def _load() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_dataset_shape():
    data = _load()
    assert data["version"] == 2
    assert len(data["seed"]) >= 15
    assert len(data["cases"]) >= 15
    assert len({s["key"] for s in data["seed"]}) == len(data["seed"])


def test_rev_cases_are_well_formed():
    data = _load()
    keys = {s["key"] for s in data["seed"]}
    rev_cases = data.get("revCases") or []
    assert len(rev_cases) >= 2, "跨 rev 至少要有一条失效 + 一条复活"

    ids = [c["id"] for c in rev_cases]
    assert len(ids) == len(set(ids)), "rev case id 不能重复"
    for case in rev_cases:
        assert case["seedKey"] in keys
        assert case["staleRev"] < case["reviveRev"], "复活用的 rev 必须比失效时更高"
        assert case["question"].strip()


def test_every_case_points_at_a_seed():
    data = _load()
    keys = {s["key"] for s in data["seed"]}
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids)), "case id 不能重复"

    for case in data["cases"]:
        assert case["question"].strip()
        assert case["expectKey"] in keys, f"{case['id']} 指向不存在的种子"
        for key in case.get("mustNotRecall") or []:
            assert key in keys, f"{case['id']} 的干扰项不存在: {key}"
            assert key != case["expectKey"]


def test_seed_memories_are_well_formed():
    data = _load()
    for seed in data["seed"]:
        assert seed["type"] in {"FACT", "OPEN_QUESTION"}
        assert seed["text"].strip()
        assert len(seed["text"]) <= 120
        assert seed["subjectUids"], f"{seed['key']} 缺少 subjectUids"
        for uid in seed["subjectUids"]:
            assert uid.count(".") >= 1, f"{seed['key']} 的 uid 不像全限定名: {uid}"


def test_questions_do_not_leak_class_names():
    """问题里出现类名就不是语义召回测试，是关键词匹配测试。"""
    data = _load()
    seeds = {s["key"]: s for s in data["seed"]}

    for case in data["cases"]:
        seed = seeds[case["expectKey"]]
        for uid in seed["subjectUids"]:
            simple = uid.rsplit(".", 1)[-1]
            assert simple not in case["question"], (
                f"{case['id']} 的问句直接包含了类名 {simple}，测不出语义召回"
            )
