"""能力检索：故障隔离、payload 映射、prompt 空块、轮首注入。

替身全部本地复制（`FakeEmbed` / `FakeQdrant` / `FakeIndexStore` 与
`tests/test_retriever.py` 同形）—— 不跨测试文件 import，改一处不会连带另一处红。
"""

import json
from types import SimpleNamespace

from app.core.agent_loop import _seed_known, run_agent_turn
from app.core.llm import LLMResult
from app.core.prompt import render_capabilities
from app.core.retriever import Retriever, to_capability
from app.core.turn_support import _retrieve_detail
from app.models.context import AgentContext, MessageRole, ToolCall
from app.models.retrieval import Candidate, CapabilityCandidate, Retrieval
from fakes import FakeJava, FakeLLM, ok_overview


def _class_point(uid="a.OrderService"):
    return SimpleNamespace(payload={"uid": uid, "rev": 7, "kind": "CLASS"}, score=0.03)


def _cap_point(uid="POST /orders", **payload):
    body = {
        "uid": uid, "rev": 7, "name": uid, "description": "下单",
        "areaUid": "order", "areaName": "订单", "entryUid": "a.OrderController",
        "entryKind": "HTTP", "memberCount": 3, **payload,
    }
    return SimpleNamespace(payload=body, score=0.04)


class FakeEmbed:
    def __init__(self, *, raises: Exception | None = None):
        self._raises = raises
        self.calls: list[list[str]] = []

    async def embed(self, texts, *, timeout=None):
        self.calls.append(texts)
        if self._raises is not None:
            raise self._raises
        return [([0.1, 0.2], {5: 0.3}) for _ in texts]


class FakeQdrant:
    def __init__(self, points=None, *, raises: Exception | None = None):
        self._points = points or []
        self._raises = raises
        self.calls: list[dict] = []

    async def query(self, project_id, dense, sparse, *, rev, limit=None, prefetch_limit=None):
        self.calls.append({"project_id": project_id, "rev": rev, "limit": limit})
        if self._raises is not None:
            raise self._raises
        return self._points


class FakeIndexStore:
    def __init__(self, state: dict | None, *, raises: Exception | None = None):
        self.state = state
        self.raises = raises

    async def get_state(self, project_id: int):
        if self.raises is not None:
            raise self.raises
        return self.state


def _capability(uid="POST /orders", entry_uid="a.OrderController"):
    return CapabilityCandidate(
        uid=uid,
        name=uid,
        description="下单并扣减库存",
        area_uid="order",
        area_name="订单",
        entry_uid=entry_uid,
        entry_kind="HTTP",
        member_count=3,
        rev=7,
        score=0.04,
    )


# ==================== 计划里的三条基线 ====================


async def test_capability_failure_keeps_class_candidates():
    embed = FakeEmbed()
    class_qdrant = FakeQdrant([_class_point()])
    cap_qdrant = FakeQdrant(raises=RuntimeError("down"))
    r = Retriever(embed, class_qdrant, FakeIndexStore({"activeRev": 7}),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"activeRev": 7}))
    result = await r.search(1, "怎么下单")
    assert [c.uid for c in result.candidates] == ["a.OrderService"]
    assert result.capabilities == []
    assert result.capability_reason == "qdrant_failed"


def test_capability_payload_maps_and_drops_without_entry():
    assert to_capability(_cap_point()).entry_uid == "a.OrderController"
    assert to_capability(_cap_point(entryUid=None)) is None


def test_render_capabilities_is_empty_without_hits():
    assert render_capabilities([]) == ""


# ==================== 状态闸门与故障隔离 ====================


async def test_capability_not_configured_keeps_reason_none():
    """三参数构造（R4 现状）：能力是「没接线」，不是「没索引」。"""
    r = Retriever(FakeEmbed(), FakeQdrant([_class_point()]),
                  FakeIndexStore({"status": "ready", "activeRev": 7}))
    result = await r.search(1, "怎么下单")

    assert [c.uid for c in result.candidates] == ["a.OrderService"]
    assert result.capabilities == []
    assert result.capability_reason is None


async def test_only_class_indexed_marks_capability_absent():
    """只有类有 activeRev：embed 一次、只查类，能力侧记 absent。"""
    embed = FakeEmbed()
    class_qdrant = FakeQdrant([_class_point()])
    cap_qdrant = FakeQdrant([_cap_point()])
    r = Retriever(embed, class_qdrant, FakeIndexStore({"activeRev": 7}),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"status": "failed"}))
    result = await r.search(1, "怎么下单")

    assert [c.uid for c in result.candidates] == ["a.OrderService"]
    assert result.capabilities == []
    assert result.capability_reason == "absent"
    assert cap_qdrant.calls == []
    assert len(embed.calls) == 1


async def test_only_capability_indexed_searches_only_capability():
    """只有能力有 activeRev：类侧 attempted=False / reason=absent，能力照常命中。"""
    class_qdrant = FakeQdrant([_class_point()])
    cap_qdrant = FakeQdrant([_cap_point()])
    r = Retriever(FakeEmbed(), class_qdrant, FakeIndexStore(None),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"activeRev": 7}))
    result = await r.search(1, "怎么下单")

    assert result.candidates == []
    assert result.attempted is False
    assert result.reason == "absent"
    assert [c.uid for c in result.capabilities] == ["POST /orders"]
    assert result.capability_reason is None
    assert class_qdrant.calls == []
    assert cap_qdrant.calls[0]["rev"] == 7


async def test_both_sides_missing_returns_before_embedding():
    embed = FakeEmbed()
    cap_qdrant = FakeQdrant()
    r = Retriever(embed, FakeQdrant(), FakeIndexStore(None),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"status": "not_ready"}))
    result = await r.search(1, "怎么下单")

    assert result.attempted is False
    assert result.reason == "absent"
    assert result.capability_reason == "not_ready"
    assert embed.calls == [] and cap_qdrant.calls == []


async def test_capability_state_read_failure_only_affects_capability():
    class_qdrant = FakeQdrant([_class_point()])
    cap_qdrant = FakeQdrant([_cap_point()])
    r = Retriever(FakeEmbed(), class_qdrant, FakeIndexStore({"activeRev": 7}),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore(None, raises=RuntimeError("redis 挂了")))
    result = await r.search(1, "怎么下单")

    assert [c.uid for c in result.candidates] == ["a.OrderService"]
    assert result.capabilities == []
    assert result.capability_reason == "state_unavailable"
    assert cap_qdrant.calls == []


async def test_class_state_read_failure_keeps_healthy_capability_path():
    """类状态读不到，但能力索引健康：embed 一次、只查能力。"""
    embed = FakeEmbed()
    class_qdrant = FakeQdrant([_class_point()])
    cap_qdrant = FakeQdrant([_cap_point()])
    r = Retriever(embed, class_qdrant, FakeIndexStore(None, raises=RuntimeError("redis")),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"activeRev": 7}))
    result = await r.search(1, "怎么下单")

    assert result.candidates == []
    assert result.attempted is False
    assert result.reason == "state_unavailable"
    assert [c.uid for c in result.capabilities] == ["POST /orders"]
    assert result.capability_reason is None
    assert len(embed.calls) == 1
    assert class_qdrant.calls == []
    assert cap_qdrant.calls[0]["rev"] == 7


async def test_class_state_read_failure_without_capability_returns_before_embed():
    """三参数构造（能力没接线）：维持 R4 的「state_unavailable、不 embed」。"""
    embed = FakeEmbed()
    r = Retriever(embed, FakeQdrant(), FakeIndexStore(None, raises=RuntimeError("redis")))
    result = await r.search(1, "问题")

    assert result.reason == "state_unavailable"
    assert result.capabilities == []
    assert result.capability_reason is None
    assert embed.calls == []


async def test_class_state_read_failure_with_inactive_capability_returns_before_embed():
    embed = FakeEmbed()
    r = Retriever(embed, FakeQdrant(), FakeIndexStore(None, raises=RuntimeError("redis")),
                  capability_qdrant=FakeQdrant(),
                  capability_store=FakeIndexStore({"status": "failed"}))
    result = await r.search(1, "问题")

    assert result.reason == "state_unavailable"
    assert result.capabilities == []
    assert result.capability_reason == "failed"
    assert embed.calls == []


async def test_class_qdrant_failure_keeps_capability_candidates():
    """反向隔离：类查询挂了，能力命中照常返回。"""
    class_qdrant = FakeQdrant(raises=RuntimeError("down"))
    cap_qdrant = FakeQdrant([_cap_point()])
    r = Retriever(FakeEmbed(), class_qdrant, FakeIndexStore({"activeRev": 7}),
                  capability_qdrant=cap_qdrant,
                  capability_store=FakeIndexStore({"activeRev": 7}))
    result = await r.search(1, "怎么下单")

    assert result.candidates == []
    assert result.attempted is True
    assert result.reason == "qdrant_failed"
    assert [c.uid for c in result.capabilities] == ["POST /orders"]
    assert result.capability_reason is None


async def test_embed_failure_marks_both_sides():
    r = Retriever(FakeEmbed(raises=RuntimeError("embed 挂了")),
                  FakeQdrant(), FakeIndexStore({"activeRev": 7}),
                  capability_qdrant=FakeQdrant(),
                  capability_store=FakeIndexStore({"activeRev": 7}))
    result = await r.search(1, "怎么下单")

    assert result.reason == "embed_failed"
    assert result.capability_reason == "embed_failed"
    assert result.candidates == [] and result.capabilities == []


# ==================== payload 映射 ====================


def test_to_capability_never_raises_on_bad_payload():
    assert to_capability(SimpleNamespace(payload=None, score=0.1)) is None
    assert to_capability(SimpleNamespace(payload={"uid": "x"}, score=0.1)) is None
    # 计数类型坏了不丢点 —— 退回默认值 0
    bad = SimpleNamespace(
        payload={"uid": "x", "entryUid": "a.C", "memberCount": "not-a-number"},
        score=0.1,
    )
    assert to_capability(bad).member_count == 0


def test_to_capability_requires_non_empty_string_entry_uid():
    # description 坏类型 + entryUid 不是字符串：整点丢弃
    assert to_capability(_cap_point(description=123, entryUid=[])) is None
    assert to_capability(_cap_point(entryUid="")) is None


def test_to_capability_ignores_non_string_text_fields():
    c = to_capability(_cap_point(description=123, name=456, areaName=[], areaUid=789))
    assert c is not None
    assert c.description is None
    assert c.name == "POST /orders"
    assert c.area_name == ""
    assert c.area_uid == ""


def test_to_capability_defaults_and_score():
    c = to_capability(_cap_point(description=None, name=None, memberCount=None))
    assert c is not None
    assert c.name == "POST /orders"
    assert c.member_count == 0
    assert c.score == 0.04


# ==================== prompt 渲染 ====================


def test_render_capabilities_shows_fields_and_disclaimer():
    text = render_capabilities([_capability()])

    assert "候选，不是证据" in text
    assert "POST /orders" in text          # name / uid
    assert "订单" in text                  # areaName
    assert "a.OrderController" in text     # entryUid
    assert "3 个类" in text                # memberCount
    assert "下单并扣减库存" in text         # description


# ==================== 步骤摘要 ====================


def test_retrieve_detail_unchanged_without_capabilities():
    plain = Retrieval(candidates=[Candidate(
        uid="a.C", simple_name="C", package_name="a", kind="CLASS",
        description=None, rev=7, score=0.1)], attempted=True)
    assert _retrieve_detail(plain) == "retrieve(1)"
    assert _retrieve_detail(Retrieval(reason="absent")) == "retrieve(skipped:absent)"
    assert _retrieve_detail(Retrieval(attempted=True, reason="qdrant_failed")) == (
        "retrieve(failed:qdrant_failed)")


def test_retrieve_detail_counts_both_kinds():
    result = Retrieval(
        candidates=[Candidate(
            uid="a.C", simple_name="C", package_name="a", kind="CLASS",
            description=None, rev=7, score=0.1)],
        capabilities=[_capability()],
        attempted=True,
    )
    assert _retrieve_detail(result) == "retrieve(1 classes, 1 capabilities)"


def test_retrieve_detail_keeps_class_failure_with_capabilities():
    result = Retrieval(
        capabilities=[_capability(), _capability("GET /orders", "a.OrderController")],
        attempted=True,
        reason="qdrant_failed",
    )
    assert _retrieve_detail(result) == "retrieve(failed:qdrant_failed, 2 capabilities)"


# ==================== 已知实体与轮首注入 ====================


def test_seed_known_includes_capability_entry_uid_only():
    ctx = AgentContext(conversation_id="c1", project_id=7)
    known = _seed_known(ctx, Retrieval(capabilities=[_capability()]))
    assert "a.OrderController" in known
    assert "POST /orders" not in known, "能力 uid 是入口的标签，不是可查询的实体"


class _FixedRetriever:
    def __init__(self, result: Retrieval) -> None:
        self._result = result

    async def search(self, project_id: int, query: str, k: int | None = None) -> Retrieval:
        return self._result


async def test_agent_loop_injects_capability_block_only_when_non_empty():
    answer = {"sections": [{"title": "t", "body": "b", "citedUids": []}]}
    llm = FakeLLM([LLMResult(content=None, tool_calls=(
        ToolCall(id="a1", name="answer", arguments=json.dumps(answer, ensure_ascii=False)),
    ))])
    ctx = AgentContext(conversation_id="c1", project_id=7)
    await run_agent_turn(
        ctx, "这个项目怎么下单", FakeJava(ok_overview()), llm,
        retriever=_FixedRetriever(Retrieval(capabilities=[_capability()], attempted=True)),
    )

    system_texts = [m["content"] for m in llm.calls[0]["messages"] if m["role"] == "system"]
    assert any("候选，不是证据" in text for text in system_texts)

    retrieval_msgs = [m for m in ctx.messages if m.role is MessageRole.RETRIEVAL]
    assert len(retrieval_msgs) == 1
    assert retrieval_msgs[0].meta["uids"] == ["a.OrderController"]


async def test_agent_loop_skips_capability_message_when_empty():
    answer = {"sections": [{"title": "t", "body": "b", "citedUids": []}]}
    llm = FakeLLM([LLMResult(content=None, tool_calls=(
        ToolCall(id="a1", name="answer", arguments=json.dumps(answer, ensure_ascii=False)),
    ))])
    ctx = AgentContext(conversation_id="c1", project_id=7)
    await run_agent_turn(
        ctx, "看看", FakeJava(ok_overview()), llm,
        retriever=_FixedRetriever(Retrieval(attempted=True)),
    )

    assert [m for m in ctx.messages if m.role is MessageRole.RETRIEVAL] == []
