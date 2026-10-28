"""R4 检索：候选映射、状态闸门、失败降级、entityRef 校验、prompt 注入。

全部用替身，不连 Redis / Qdrant / embed —— 真实排序要等索引可用的环境再验。
"""

from types import SimpleNamespace

import pytest

from app.core.retriever import Retriever, resolve_entity_ref, to_candidate
from app.core.prompt import explain_messages, intent_messages
from app.models.context import AgentContext
from app.models.intent import Intent
from app.models.retrieval import Candidate
from fakes import candidate


def _point(uid: str, *, score: float = 0.03, **payload) -> SimpleNamespace:
    body = {"uid": uid, "rev": 7, "kind": "CLASS", **payload}
    return SimpleNamespace(payload=body, score=score)


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
    def __init__(self, state: dict | None):
        self.state = state
        self.raises: Exception | None = None

    async def get_state(self, project_id: int):
        if self.raises is not None:
            raise self.raises
        return self.state


def _retriever(state, *, points=None, embed_raises=None, qdrant_raises=None):
    embed = FakeEmbed(raises=embed_raises)
    qdrant = FakeQdrant(points, raises=qdrant_raises)
    return Retriever(embed, qdrant, FakeIndexStore(state)), embed, qdrant


# ==================== 状态闸门 ====================


@pytest.mark.asyncio
async def test_no_active_rev_skips_retrieval():
    r, embed, qdrant = _retriever({"status": "failed"})
    result = await r.search(1, "下单扣库存")

    assert result.candidates == []
    assert result.attempted is False
    assert result.reason == "failed"
    assert embed.calls == [] and qdrant.calls == []


@pytest.mark.asyncio
async def test_absent_state_skips_retrieval():
    r, _, _ = _retriever(None)
    result = await r.search(1, "下单扣库存")
    assert result.attempted is False
    assert result.reason == "absent"


@pytest.mark.asyncio
async def test_ready_state_searches_with_active_rev():
    r, _, qdrant = _retriever(
        {"status": "ready", "activeRev": 7},
        points=[_point("com.x.OrderServiceImpl", simpleName="OrderServiceImpl")],
    )
    result = await r.search(1, "下单扣库存")

    assert qdrant.calls == [{"project_id": 1, "rev": 7, "limit": 5}]
    assert [c.uid for c in result.candidates] == ["com.x.OrderServiceImpl"]
    assert result.ok is True


@pytest.mark.asyncio
async def test_failed_state_with_previous_version_still_searches():
    """失败的是新版本，不是整个项目 —— 只要 activeRev 还在就照常检索。"""
    r, _, qdrant = _retriever({"status": "failed", "activeRev": 6})
    await r.search(1, "问题")
    assert qdrant.calls[0]["rev"] == 6


@pytest.mark.asyncio
async def test_partial_index_is_reported():
    r, _, _ = _retriever(
        {"status": "ready", "activeRev": 7, "partial": True, "actual": 103, "expected": 104},
        points=[_point("com.x.A", simpleName="A")],
    )
    result = await r.search(1, "问题")
    assert result.partial == "103/104"


# ==================== 失败降级 ====================


@pytest.mark.asyncio
async def test_embed_failure_returns_no_candidates():
    r, _, qdrant = _retriever({"status": "ready", "activeRev": 7},
                              embed_raises=RuntimeError("embed 挂了"))
    result = await r.search(1, "问题")

    assert result.candidates == []
    assert result.reason == "embed_failed"
    assert qdrant.calls == []


@pytest.mark.asyncio
async def test_qdrant_failure_returns_no_candidates():
    r, _, _ = _retriever({"status": "ready", "activeRev": 7},
                         qdrant_raises=RuntimeError("qdrant 挂了"))
    result = await r.search(1, "问题")

    assert result.candidates == []
    assert result.attempted is True
    assert result.reason == "qdrant_failed"


@pytest.mark.asyncio
async def test_state_read_failure_returns_no_candidates():
    r, _, _ = _retriever(None)
    r._store.raises = RuntimeError("redis 挂了")
    result = await r.search(1, "问题")
    assert result.reason == "state_unavailable"


# ==================== payload → Candidate ====================


def test_to_candidate_reads_payload():
    c = to_candidate(_point(
        "com.x.OrderServiceImpl", simpleName="OrderServiceImpl",
        packageName="com.x", kind="INTERFACE", description="负责下单", rev=9,
    ))
    assert c is not None
    assert (c.uid, c.simple_name, c.package_name) == (
        "com.x.OrderServiceImpl", "OrderServiceImpl", "com.x")
    assert c.kind == "INTERFACE" and c.kind_label == "接口"
    assert c.rev == 9


def test_to_candidate_falls_back_when_package_missing():
    c = to_candidate(_point("com.x.A", simpleName="A"))
    assert c is not None and c.package_name == "com.x"


def test_to_candidate_drops_payload_without_uid():
    assert to_candidate(SimpleNamespace(payload={}, score=0.1)) is None


# ==================== entityRef 校验 ====================


def _cands():
    return [
        candidate("com.moviesys.order.OrderServiceImpl", desc="订单创建"),
        candidate("com.moviesys.inventory.InventoryServiceImpl", desc="库存扣减"),
    ]


def test_entity_ref_exact_uid_accepted():
    ref = "com.moviesys.order.OrderServiceImpl"
    assert resolve_entity_ref(ref, _cands(), "随便问") == ref


def test_entity_ref_simple_name_mapped_to_uid():
    assert resolve_entity_ref("OrderServiceImpl", _cands(), "随便问") == \
        "com.moviesys.order.OrderServiceImpl"


def test_entity_ref_outside_candidates_but_named_by_user_accepted():
    ref = "com.other.PaymentService"
    assert resolve_entity_ref(ref, _cands(), "PaymentService 的调用链") == ref


def test_entity_ref_invented_is_dropped():
    assert resolve_entity_ref("com.ghost.Fake", _cands(), "下单扣库存") is None


def test_entity_ref_unconstrained_when_no_candidates():
    assert resolve_entity_ref("com.x.Any", [], "问题") == "com.x.Any"


def test_entity_ref_ambiguous_simple_name_dropped():
    cands = [
        Candidate("a.A", "A", "a", "CLASS", None, 1, 0.1),
        Candidate("b.A", "A", "b", "CLASS", None, 1, 0.1),
    ]
    assert resolve_entity_ref("A", cands, "问 A") is None


# ==================== prompt 注入 ====================


def _ctx() -> AgentContext:
    return AgentContext(conversation_id="c1", project_id=1)


def test_intent_prompt_has_candidates_when_present():
    msgs = intent_messages(_ctx(), "下单扣库存怎么实现的", _cands())
    system = msgs[0]["content"]
    assert "com.moviesys.order.OrderServiceImpl" in system
    assert "entityRef 只能从" in system


def test_intent_prompt_unchanged_without_candidates():
    """没检索时 prompt 必须与 R4 之前逐字相同 —— 这是回归基准。"""
    assert intent_messages(_ctx(), "问题") == intent_messages(_ctx(), "问题", [])
    assert "候选" not in intent_messages(_ctx(), "问题")[0]["content"]


def test_explain_prompt_marks_candidates_as_unverified():
    msgs = explain_messages(Intent.EXPLORE_FLOW, [], "订单流程", _ctx(), candidates=_cands())
    user = msgs[1]["content"]
    assert "未查证" in user
    assert "同批候选还有" in user
    assert "不是证据" in msgs[0]["content"]


def test_explain_prompt_unchanged_without_candidates():
    assert explain_messages(Intent.EXPLORE_FLOW, [], "订单流程", _ctx()) == \
        explain_messages(Intent.EXPLORE_FLOW, [], "订单流程", _ctx(), candidates=[])
