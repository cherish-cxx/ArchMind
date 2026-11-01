"""R4 评测集自洽性校验。

评测集是**手写标注**，最容易出的错不是指标算错，而是标注和代码对不上：
锚点类被改名/删除、id 重复、负样例混进正样例。这些错误必须在跑评测前就红，
否则算出来的 recall 是假的。
"""

import json
from pathlib import Path

import pytest

DATASET_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "retrieval_dataset.archmind.json"
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = [
    REPO_ROOT / "ArchMind" / "src" / "main" / "java",
    REPO_ROOT / "ArchMind" / "src" / "test" / "java",
]
ALLOWED_CATEGORIES = {"auth", "upload", "project", "analysis", "graph", "task", "overview", "misc"}
ALLOWED_DIFFICULTY = {"easy", "medium", "hard"}


@pytest.fixture(scope="module")
def dataset():
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def test_question_count_in_range(dataset):
    count = len(dataset["questions"])
    assert 30 <= count <= 50, f"R4 约定 30–50 条，实际 {count}"


def test_ids_unique(dataset):
    ids = [q["id"] for q in dataset["questions"]] + [n["id"] for n in dataset["negatives"]]
    assert len(ids) == len(set(ids)), "id 不能重复"


def test_question_fields(dataset):
    for q in dataset["questions"]:
        assert q["question"].strip()
        assert q["category"] in ALLOWED_CATEGORIES, q["id"]
        assert q.get("difficulty") in ALLOWED_DIFFICULTY, q["id"]
        assert isinstance(q["anchors"], list) and q["anchors"], f"{q['id']} 没有锚点"
        assert len(q["anchors"]) == len(set(q["anchors"])), f"{q['id']} 锚点重复"
        assert all("." in uid for uid in q["anchors"]), f"{q['id']} 锚点必须是全限定名"


@pytest.mark.parametrize("root", SOURCE_ROOTS)
def test_source_roots_exist(root):
    assert root.is_dir(), f"源码目录不存在: {root}"


def test_anchor_classes_exist(dataset):
    """锚点必须能在当前源码里找到对应的顶层类型文件。"""
    for q in dataset["questions"]:
        for uid in q["anchors"]:
            relative = Path(*uid.split(".")).with_suffix(".java")
            assert any((root / relative).is_file() for root in SOURCE_ROOTS), \
                f"{q['id']} 的锚点不存在: {uid}"


def test_negatives(dataset):
    for n in dataset["negatives"]:
        assert n["question"].strip()
        assert n.get("note")
