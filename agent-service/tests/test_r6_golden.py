"""R6 二十题验收集的 schema 校验。

只保证数据自洽：条数、id 唯一、有锚点、多步题够用。
真正跑分需要 Java + DeepSeek + Qdrant，由 `scripts/eval/run_ab_eval.py` 负责。
"""

import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "r6_golden.json"


def load():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_dataset_shape():
    data = load()
    questions = data["questions"]

    assert data["version"] == 1
    assert data["projectId"] == 2115334472151040002
    assert len(questions) == 20
    assert len({q["id"] for q in questions}) == 20


def test_every_question_has_an_anchor():
    for q in load()["questions"]:
        assert q["question"].strip()
        assert q["expectAnchorUids"], q["id"]


def test_enough_multi_step_questions():
    questions = load()["questions"]
    assert sum(1 for q in questions if q["multiStep"]) >= 8
    assert any(q["mustDiscloseGap"] for q in questions)
