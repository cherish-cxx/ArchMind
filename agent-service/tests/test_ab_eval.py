"""A/B 脚本里唯一有逻辑的部分：从**原始模型输出**里数引用越界。

脚本本身要连 Java / DeepSeek / Qdrant，但这几个纯函数能脱网测死 ——
数错了会让「多步到底有没有变好」这个结论直接反过来。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "eval"))

import run_ab_eval as ab  # noqa: E402

from app.models.context import AgentContext  # noqa: E402


def turn(calls: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(llm_calls=calls)


# ==================== 解析 ====================


def test_extracts_cited_uids_from_sections():
    text = '{"sections":[{"body":"b","citedUids":["a.A","b.B"]},{"body":"c"}]}'
    assert ab._cited_uids(text) == ["a.A", "b.B"]


def test_supports_legacy_flat_shape():
    assert ab._cited_uids('{"message":"m","citedUids":["a.A"]}') == ["a.A"]


def test_malformed_output_is_empty():
    assert ab._cited_uids("not json") == []
    assert ab._cited_uids("[]") == []
    assert ab._cited_uids('{"sections": "nope"}') == []


# ==================== 取原始引用 ====================


def test_tools_mode_reads_answer_arguments():
    calls = [{
        "purpose": "agent",
        "rawResponse": "",
        "toolCalls": [
            {"name": "call-chain", "arguments": '{"uid":"a.A"}'},
            {"name": "answer",
             "arguments": '{"sections":[{"body":"b","citedUids":["x.Y"]}]}'},
        ],
    }]
    assert ab._offered_citations("tools", turn(calls)) == ["x.Y"]


def test_tools_mode_returns_empty_without_answer():
    calls = [{"purpose": "agent", "toolCalls": [
        {"name": "overview", "arguments": "{}"},
    ]}]
    assert ab._offered_citations("tools", turn(calls)) == []


def test_legacy_mode_reads_explain_response():
    calls = [
        {"purpose": "intent", "rawResponse": '{"intent":"EXPLORE_FLOW"}'},
        {"purpose": "explain",
         "rawResponse": '{"sections":[{"body":"b","citedUids":["a.A"]}]}'},
    ]
    assert ab._offered_citations("legacy", turn(calls)) == ["a.A"]


# ==================== 指标 ====================


def test_metrics_counts_only_what_backend_actually_dropped():
    """越界看的是「提出但被丢掉」，不是「不在图证据里」——

    循环的白名单已经放宽到源码里出现过的类，拿 `ctx.turn.evidence` 比会误判。
    """
    ctx = AgentContext(conversation_id="c", project_id=1)
    ctx.turn.evidence = ["a.A"]
    ctx.turn.llm_calls = [{
        "purpose": "agent",
        "tokensIn": 5,
        "tokensOut": 6,
        "toolCalls": [{
            "name": "answer",
            "arguments": '{"sections":[{"body":"b","citedUids":["a.A","z.Z"]}]}',
        }],
    }]
    resp = SimpleNamespace(
        meta=SimpleNamespace(latency_ms=12),
        # 后端保留了 a.A，丢掉了 z.Z
        sections=[SimpleNamespace(cited_uids=["a.A"])],
        citations=[], ui_commands=[], message="正文",
    )

    metrics = ab._metrics("tools", {"id": "q"}, ctx, resp)

    assert metrics["citationsOffered"] == 2
    assert metrics["citationsUngrounded"] == 1
    assert metrics["citationsUngroundedUids"] == ["z.Z"]
    assert metrics["tokensIn"] == 5
    assert metrics["latencyMs"] == 12


def test_gap_disclosure_is_only_measured_when_required():
    ctx = AgentContext(conversation_id="c", project_id=1)
    resp = SimpleNamespace(
        meta=SimpleNamespace(latency_ms=1),
        sections=[], citations=[], ui_commands=[], message="这里有个断点，跨服务调用",
    )

    assert ab._metrics("tools", {"id": "q"}, ctx, resp)["disclosedGap"] is None
    assert ab._metrics("tools", {"id": "q", "mustDiscloseGap": True}, ctx, resp)["disclosedGap"] is True
