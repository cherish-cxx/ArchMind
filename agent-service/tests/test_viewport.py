"""P4.2：屏幕快照上报 / 会话读回 / 过期即结束。

**这组测试打的是真 app**（`TestClient(app)`），只把 `ContextStore` 换成内存替身 ——
和 `test_contract` 同一个套路，理由也一样：路由形状和 rev 语义必须在
「**真 app 怎么反应**」这一层验，而不是「我以为它会怎么反应」。

不需要 Java，也不需要真 Redis。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.agent import get_context_store, get_llm_client
from app.config import INTERNAL_TOKEN_HEADER, settings
from app.main import app
from app.models.context import VIEWPORT_MAX_ITEMS, Viewport
from app.models.response import NodeRef, NodeRefType
from fakes import FakeContextStore, FakeLLM

_PROJECT_ID = 1001
_H = {INTERNAL_TOKEN_HEADER: settings.internal_token}


def _cls(uid: str) -> NodeRef:
    return NodeRef(type=NodeRefType.CLASS, uid=uid)


@pytest.fixture
def store() -> FakeContextStore:
    s = FakeContextStore()
    app.dependency_overrides[get_context_store] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(store) -> TestClient:
    # 用 with 才会跑 lifespan —— 否则 get_java_client / get_llm_client 那两个依赖
    # 在解析阶段就会撞上没初始化的 app.state。它们建的连接都是懒的，不会真拨号。
    with TestClient(app) as c:
        yield c


def _report(cid: str, rev: int, **kw) -> dict:
    return {"projectId": _PROJECT_ID, "conversationId": cid, "rev": rev, **kw}


# ==================== 纯函数：快照截断 ====================


def test_viewport_truncates_and_flags():
    """超上限截断，且**必须置 truncated** —— 截断可以，假装没截断不行。"""
    v = Viewport.from_report(
        rev=1, visible_nodes=[f"n{i}" for i in range(100)], visible_relations=[],
        focused_node=None,
    )
    assert len(v.visible_nodes) == VIEWPORT_MAX_ITEMS
    assert v.truncated is True


def test_viewport_under_limit_not_truncated():
    v = Viewport.from_report(rev=1, visible_nodes=["a"], visible_relations=[], focused_node=None)
    assert v.truncated is False


def test_viewport_carries_focus_page_fields():
    v = Viewport.from_report(
        rev=2,
        visible_nodes=["a.B", "a.C"],
        visible_relations=[],
        focused_node=_cls("a.B"),
        mode="focus",
        center_uid="a.B",
        hops=1,
        folded_count=3,
    )
    assert v.mode == "focus"
    assert v.center_uid == "a.B"
    assert v.hops == 1
    assert v.folded_count == 3


# ==================== rev 乐观锁（验收项 1）====================


def test_stale_rev_rejected(client, store):
    cid = store.seed(_PROJECT_ID, "c1", viewport=Viewport(rev=5)).conversation_id

    body = client.post("/internal/agent/viewport", headers=_H, json=_report(cid, 5)).json()

    assert body["accepted"] is False
    assert body["reason"] == "STALE_REV"
    # ★ 必须回带当前 rev —— 没有它，前端会一直重发自己那个旧 rev，永远同步不上
    assert body["rev"] == 5


def test_newer_rev_accepted_and_saved(client, store):
    cid = store.seed(_PROJECT_ID, "c2", viewport=Viewport(rev=5)).conversation_id

    body = client.post(
        "/internal/agent/viewport", headers=_H, json=_report(cid, 6, visibleNodes=["a.B"])
    ).json()

    assert body == {"accepted": True, "reason": None, "rev": 6}
    assert store.peek(_PROJECT_ID, cid).viewport.visible_nodes == ["a.B"]


def test_report_truncates_through_the_route(client, store):
    """截断要在**路由走一遍**之后仍然生效（证明路由确实经过了 from_report）。"""
    cid = store.seed(_PROJECT_ID, "c3").conversation_id

    client.post(
        "/internal/agent/viewport", headers=_H,
        json=_report(cid, 1, visibleNodes=[f"n{i}" for i in range(100)]),
    )

    v = store.peek(_PROJECT_ID, cid).viewport
    assert len(v.visible_nodes) == VIEWPORT_MAX_ITEMS
    assert v.truncated is True


def test_focus_page_report_round_trips(client, store):
    cid = store.seed(_PROJECT_ID, "c6").conversation_id

    body = client.post(
        "/internal/agent/viewport", headers=_H,
        json=_report(
            cid, 1, mode="focus", centerUid="a.B", hops=1,
            foldedCount=4, visibleNodes=["a.B"],
        ),
    ).json()

    assert body["accepted"] is True
    v = store.peek(_PROJECT_ID, cid).viewport
    assert v.mode == "focus"
    assert v.center_uid == "a.B"
    assert v.hops == 1
    assert v.folded_count == 4


# ==================== 上报不许创建会话（决策 A）====================


def test_report_does_not_create_conversation(client, store):
    """给不认识的 cid 上报 → 拒绝，且**不许在存储里留下任何东西**。

    这是决策 A 的核心：静默建一个空的，会把「会话已过期」这件事埋掉 ——
    用户下次提问才发现失忆，而且毫无提示。
    """
    body = client.post(
        "/internal/agent/viewport", headers=_H, json=_report("c_ghost", 1)
    ).json()

    assert body["accepted"] is False
    assert body["reason"] == "SESSION_GONE"
    assert body["rev"] is None
    assert store.peek(_PROJECT_ID, "c_ghost") is None      # ★ 没被创建


# ==================== 会话读回（验收项 2）====================


def test_get_conversation_returns_snapshot(client, store):
    cid = store.seed(
        _PROJECT_ID, "c4",
        viewport=Viewport(rev=7, visible_nodes=["a.B"], focused_node=_cls("a.B")),
        focus=_cls("a.B"),
    ).conversation_id

    body = client.get(
        f"/internal/agent/conversations/{cid}", params={"projectId": _PROJECT_ID}, headers=_H
    ).json()

    assert body["exists"] is True
    assert body["reason"] is None
    assert body["viewport"]["rev"] == 7
    assert body["viewport"]["visibleNodes"] == ["a.B"]
    assert body["viewport"]["focusedNode"] == {"type": "CLASS", "uid": "a.B"}
    assert body["viewport"]["mode"] == "cumulative"
    assert body["viewport"]["centerUid"] is None
    assert body["viewport"]["foldedCount"] == 0
    assert body["focus"] == {"type": "CLASS", "uid": "a.B"}
    assert "history" not in body        # ★ 不给 Agent 的内部记忆


def test_get_conversation_missing(client, store):
    body = client.get(
        "/internal/agent/conversations/c_nope", params={"projectId": _PROJECT_ID}, headers=_H
    ).json()
    assert body == {"exists": False, "reason": "SESSION_GONE"}


# ==================== /ask 遇到过期会话（验收项 3）====================


def test_ask_expired_session_says_expired_not_new(client, store):
    """带 cid 但 Context 不在 → **说过期，不回答、也不建新的**。

    「不建」是关键：静默开一个新会话，用户会在上文丢失的情况下
    收到一个看似正常的答案 —— 那正是这次要避免的。
    """
    body = client.post(
        "/internal/agent/ask", headers=_H,
        json={"projectId": _PROJECT_ID, "conversationId": "c_gone", "message": "它的调用链呢"},
    ).json()

    assert body["sessionExpired"] is True
    assert body["conversationId"] == "c_gone"
    assert body["uiCommands"] == []
    assert store.peek(_PROJECT_ID, "c_gone") is None       # ★ 没被创建


def test_ask_without_cid_creates_new(client, store):
    """没带 cid = 新会话 —— **唯一**该走「创建」的路径，且不查 Redis（照旧）。"""
    # 用 OUT_OF_SCOPE 让 run_turn 走「不查图、不调工具」的分支，
    # 这样这条测试不依赖 Java，也只需要一次 LLM 调用。
    fake = FakeLLM([json.dumps({"intent": "OUT_OF_SCOPE", "confidence": 0.95})])
    app.dependency_overrides[get_llm_client] = lambda: fake

    body = client.post(
        "/internal/agent/ask", headers=_H,
        json={"projectId": _PROJECT_ID, "message": "帮我写个登录接口"},
    ).json()

    cid = body["conversationId"]
    assert body["sessionExpired"] is False
    assert store.peek(_PROJECT_ID, cid) is not None        # 建了
    assert store.peek(_PROJECT_ID, cid).viewport.rev == 0  # 新会话的 viewport 是空的
