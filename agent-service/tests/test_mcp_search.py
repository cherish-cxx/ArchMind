"""R7 Task 7：`archmind_search_entities`。

用 test_retriever 的替身模式（FakeEmbed / FakeQdrant / FakeIndexStore）驱动
**真实的 Retriever**，这样测到的是「工具如何翻译检索结果」，而不是替身自己。
索引状态与检索共用同一个 store —— 和 deps 里的约束一致。
"""

from types import SimpleNamespace

import pytest

from app.core.retriever import Retriever
from app.mcp import deps
from app.mcp.deps import McpServices
from app.mcp.principal import Principal, ProjectContext, principal_scope
from app.mcp.tools import archmind_search_entities

GOOD = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="good",
)


class FakeEmbed:
    async def embed(self, texts, *, timeout=None):
        return [([0.1, 0.2], {5: 0.3}) for _ in texts]


class FakeQdrant:
    def __init__(self, points=None):
        self._points = points or []
        self.calls: list[dict] = []

    async def query(self, project_id, dense, sparse, *, rev, limit=None, prefetch_limit=None):
        self.calls.append({"project_id": project_id, "rev": rev, "limit": limit})
        return self._points


class FakeIndexStore:
    """可选地在第 N 次读取时抛错，用来验证工具层读状态的降级。"""

    def __init__(self, state: dict | None, *, raises_on_call: int | None = None):
        self.state = state
        self.raises_on_call = raises_on_call
        self.calls = 0

    async def get_state(self, project_id: int):
        self.calls += 1
        if self.raises_on_call is not None and self.calls >= self.raises_on_call:
            raise RuntimeError("redis 挂了")
        return self.state


class FakeContext:
    """只实现工具用到的方法：归属校验 + 项目上下文。"""

    def __init__(self, *, graph_rev: int = 7):
        self.graph_rev = graph_rev
        self.project_calls: list[int] = []

    async def context(self, principal: Principal, project_id: int) -> ProjectContext:
        self.project_calls.append(project_id)
        return ProjectContext(
            user_id=principal.user_id,
            pat_id=principal.pat_id,
            project_id=project_id,
            project_name="hmall",
            analysis_status="done",
            graph_rev=self.graph_rev,
        )


def _point(uid: str, *, score: float = 0.8, **payload) -> SimpleNamespace:
    body = {"uid": uid, "rev": 7, "kind": "CLASS", **payload}
    return SimpleNamespace(payload=body, score=score)


def _fake_retriever(
    state: dict | None,
    *,
    points=None,
    raises_on_call: int | None = None,
) -> tuple[Retriever, FakeIndexStore]:
    store = FakeIndexStore(state, raises_on_call=raises_on_call)
    retriever = Retriever(FakeEmbed(), FakeQdrant(points), store)
    return retriever, store


def configure_with_fakes(
    *, retriever, index_store, context=None
) -> None:
    deps.configure(
        McpServices(
            java=None,
            retriever=retriever,
            index_store=index_store,
            context=context or FakeContext(),
            limiter=None,
        )
    )


@pytest.fixture(autouse=True)
def _clean_deps():
    deps.reset()
    yield
    deps.reset()


@pytest.mark.asyncio
async def test_returns_ranked_candidates():
    retriever, store = _fake_retriever(
        {"status": "ready", "activeRev": 7, "actual": 2, "expected": 2},
        points=[
            _point(
                "com.hmall.controller.OrderController",
                score=0.81,
                simpleName="OrderController",
                packageName="com.hmall.controller",
                description="下单入口",
                methods=[{"name": "create"}, {"name": "cancel"}],
            ),
            _point(
                "com.hmall.service.impl.OrderServiceImpl",
                score=0.42,
                simpleName="OrderServiceImpl",
                packageName="com.hmall.service.impl",
                description="下单实现",
            ),
        ],
    )
    configure_with_fakes(retriever=retriever, index_store=store)

    with principal_scope(GOOD):
        out = await archmind_search_entities(1, "下单扣库存", 10)

    assert out["ok"] is True
    assert [c["uid"] for c in out["data"]] == [
        "com.hmall.controller.OrderController",
        "com.hmall.service.impl.OrderServiceImpl",
    ]
    assert out["data"][0] == {
        "uid": "com.hmall.controller.OrderController",
        "simpleName": "OrderController",
        "packageName": "com.hmall.controller",
        "kind": "CLASS",
        "description": "下单入口",
        "score": 0.81,
        "methods": ["create", "cancel"],
    }
    assert out["data"][1]["score"] == 0.42
    assert out["meta"] == {
        "projectId": 1,
        "graphRev": 7,
        "index": {"status": "ready", "rev": 7, "coverage": "2/2"},
        "truncated": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state, reason",
    [
        (None, "absent"),
        ({"status": "failed"}, "failed"),
    ],
)
async def test_absent_or_failed_index_is_explicit_error(state, reason):
    retriever, store = _fake_retriever(state)
    configure_with_fakes(retriever=retriever, index_store=store)

    with principal_scope(GOOD):
        out = await archmind_search_entities(1, "下单扣库存", 10)

    assert out["ok"] is False
    assert out["error"]["code"] == "not_indexed"
    assert out["error"]["message"] == reason
    assert "分析" in out["error"]["hint"]


@pytest.mark.asyncio
async def test_attempted_but_empty_is_not_an_error():
    retriever, store = _fake_retriever(
        {"status": "ready", "activeRev": 7}, points=[]
    )
    configure_with_fakes(retriever=retriever, index_store=store)

    with principal_scope(GOOD):
        out = await archmind_search_entities(1, "下单扣库存", 10)

    assert out["ok"] is True
    assert out["data"] == []
    assert out["meta"]["index"] == {"status": "ready", "rev": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, k",
    [
        ("", 10),
        ("   ", 10),
        ("下单", 0),
        ("下单", 51),
    ],
)
async def test_blank_query_and_out_of_range_k_are_invalid(query, k):
    retriever, store = _fake_retriever({"status": "ready", "activeRev": 7})
    context = FakeContext()
    configure_with_fakes(retriever=retriever, index_store=store, context=context)

    with principal_scope(GOOD):
        out = await archmind_search_entities(1, query, k)

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    # 参数校验必须先于归属校验，避免拿非法输入去打 Java。
    assert context.project_calls == []


@pytest.mark.asyncio
async def test_partial_coverage_appears_in_meta():
    retriever, store = _fake_retriever(
        {
            "status": "ready",
            "activeRev": 7,
            "partial": True,
            "actual": 103,
            "expected": 104,
        },
        points=[
            _point(
                "com.hmall.service.OrderService",
                simpleName="OrderService",
                packageName="com.hmall.service",
            )
        ],
    )
    configure_with_fakes(retriever=retriever, index_store=store)

    with principal_scope(GOOD):
        out = await archmind_search_entities(1, "下单", 10)

    assert out["ok"] is True
    assert out["meta"]["index"]["coverage"] == "103/104"


@pytest.mark.asyncio
async def test_index_state_read_failure_falls_back_to_absent():
    """检索成功但 meta 读状态失败时，结果照常返回，索引状态退化成 absent。"""
    retriever, store = _fake_retriever(
        {"status": "ready", "activeRev": 7},
        points=[_point("com.hmall.service.OrderService", simpleName="OrderService")],
        raises_on_call=2,  # 第 1 次给 Retriever，第 2 次给工具层 meta
    )
    configure_with_fakes(retriever=retriever, index_store=store)

    with principal_scope(GOOD):
        out = await archmind_search_entities(1, "下单", 10)

    assert out["ok"] is True
    assert out["data"][0]["uid"] == "com.hmall.service.OrderService"
    assert out["meta"]["index"] == {"status": "absent", "rev": 0}
