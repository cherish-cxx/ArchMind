"""P4.3：零 LLM 导航（`POST /internal/agent/navigate`）。

**这组测试打的是真 app**（`TestClient(app)`），只把 `ContextStore` 换成内存替身 ——
和 `test_viewport.py` 同一个套路。不需要 Java，也不需要真 Redis。

这组测试要钉住的核心是**零 LLM**：前端点按钮时，后端一次模型都不该调。
"""

import pytest
from fastapi.testclient import TestClient

from app.api.agent import get_context_store
from app.config import INTERNAL_TOKEN_HEADER, settings
from app.main import app
from app.models.context import Viewport
from app.models.response import NodeRef, NodeRefType
from fakes import FakeContextStore

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
    # 用 with 才会跑 lifespan —— 见 test_viewport.py 里同样的说明。
    # 注意这里**没有**覆写 get_llm_client：navigate 路由压根不声明 llm 依赖，
    # 这正是「零 LLM」在结构上的保证。
    with TestClient(app) as c:
        yield c


def _body(cid: str, op: str = "BACK", **kw) -> dict:
    return {"projectId": _PROJECT_ID, "conversationId": cid, "op": op, **kw}


# ==================== 出栈（BACK）====================


def test_back_pops_stack_and_focuses_previous(client, store):
    """「退回去」= 焦点栈出栈，并把焦点落到弹出来的那个 uid 上。"""
    cid = store.seed(
        _PROJECT_ID, "c1",
        focus=_cls("a.C"),
        focus_stack=["a.A", "a.B"],
        viewport=Viewport(rev=9),
    ).conversation_id

    body = client.post("/internal/agent/navigate", headers=_H, json=_body(cid)).json()

    assert body["conversationId"] == cid
    assert body["meta"]["intent"] == "NAVIGATE"
    assert [c["op"] for c in body["uiCommands"]] == ["FOCUS_NODE"]
    assert body["uiCommands"][0]["target"] == {"type": "CLASS", "uid": "a.B"}
    # rev 取 viewport.rev —— 前端据此判断这条命令基于哪一版屏幕（旧的就丢）
    assert body["uiCommands"][0]["rev"] == 9

    # ★ 副作用必须被持久化：不 save 的话这次出栈就丢了
    ctx = store.peek(_PROJECT_ID, cid)
    assert ctx.focus.uid == "a.B"
    assert ctx.focus_stack == ["a.A"]


def test_back_with_empty_stack_clears_view(client, store):
    """栈空 = 已经在最开始了，清空视图（而不是报错）。"""
    cid = store.seed(_PROJECT_ID, "c2", focus=_cls("a.A")).conversation_id

    body = client.post("/internal/agent/navigate", headers=_H, json=_body(cid)).json()

    assert [c["op"] for c in body["uiCommands"]] == ["CLEAR_VIEW"]
    assert store.peek(_PROJECT_ID, cid).focus is None


# ==================== 零 LLM（这条接口存在的全部理由）====================


def test_back_has_no_llm_step(client, store):
    """响应里**一步都没有** —— 没有 INTENT（没判意图）、没有 LLM、没有 TOOL。

    `/ask` 那条路要先花 575ms 一次 LLM 才判得出「这是导航」；这条路是前端直接声明的，
    所以 `steps` 为空正是「零 token」的可观测证据。
    """
    cid = store.seed(_PROJECT_ID, "c3", focus_stack=["a.B"]).conversation_id

    body = client.post("/internal/agent/navigate", headers=_H, json=_body(cid)).json()

    assert body["steps"] == []
    assert body["citations"] == []


# ==================== 会话过期（同 P4.2 的语义）====================


def test_expired_session_says_expired_and_creates_nothing(client, store):
    """带了 cid 却找不到 → **说过期，不建空的**。

    导航针对的是一个已存在的会话（栈在它的 Context 里），静默开一个空的
    只会让用户在下一次「退回去」时莫名其妙。
    """
    body = client.post("/internal/agent/navigate", headers=_H, json=_body("c_gone")).json()

    assert body["sessionExpired"] is True
    assert body["conversationId"] == "c_gone"
    assert body["meta"]["intent"] == "SESSION_GONE"
    assert body["uiCommands"] == []
    assert store.peek(_PROJECT_ID, "c_gone") is None      # ★ 没被创建


# ==================== EXPAND 暂不支持 ====================


def test_expand_is_explicitly_unsupported(client, store):
    """EXPAND 的语义在 loop 里从来不存在 —— **如实说没做，不静默当成 BACK**。

    静默降级成 BACK 是最坏的选项：用户以为展开了，屏幕却退回去了。
    """
    cid = store.seed(_PROJECT_ID, "c4", focus_stack=["a.B"]).conversation_id

    body = client.post(
        "/internal/agent/navigate", headers=_H, json=_body(cid, op="EXPAND")
    ).json()

    assert body["meta"]["intent"] == "NAVIGATE_UNSUPPORTED"
    assert body["uiCommands"] == []
    # ★ 状态没被动过 —— 焦点栈还在，用户接着点「退回去」仍然好使
    assert store.peek(_PROJECT_ID, cid).focus_stack == ["a.B"]


def test_unknown_op_rejected_by_model(client, store):
    """枚举外的值在**模型校验层**就被拒（422），不用等路由去判。"""
    cid = store.seed(_PROJECT_ID, "c5", focus_stack=["a.B"]).conversation_id
    resp = client.post("/internal/agent/navigate", headers=_H, json=_body(cid, op="SIDEWAYS"))
    assert resp.status_code == 422


# ==================== 鉴权 ====================


def test_requires_internal_token(client, store):
    """和其余内部接口一样：只认共享密钥，不认 JWT。"""
    cid = store.seed(_PROJECT_ID, "c6", focus_stack=["a.B"]).conversation_id
    resp = client.post("/internal/agent/navigate", json=_body(cid))
    assert resp.status_code == 401
