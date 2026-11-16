"""R7 Task 6：`archmind_list_projects` 与 `archmind_overview`。

全部用替身：Java、上下文客户端、索引状态都不连真实服务。
覆盖三件容易回归的事：list 合并索引状态且保留 camelCase、overview 的 meta、
以及两类预期失败（越权、Java ok=false）。
"""

import pytest

from app.mcp import deps
from app.mcp.deps import McpServices
from app.mcp.principal import McpAuthError, Principal, ProjectContext, principal_scope
from app.mcp.tools import archmind_list_projects, archmind_overview
from app.models.tool import ProjectOverview, ToolEnvelope
from tests.fakes import FakeJava

GOOD = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="good",
)


class FakeContext:
    """最小上下文客户端：项目列表、归属校验、项目上下文都可排队造假。"""

    def __init__(
        self,
        projects: list[dict] | None = None,
        *,
        context_error: McpAuthError | None = None,
        graph_rev: int = 7,
    ) -> None:
        self.projects_result = projects or []
        self.context_error = context_error
        self.graph_rev = graph_rev
        self.project_calls: list[int] = []

    async def authenticate(self, token: str) -> Principal:
        return GOOD

    async def context(self, principal: Principal, project_id: int) -> ProjectContext:
        self.project_calls.append(project_id)
        if self.context_error is not None:
            raise self.context_error
        return ProjectContext(
            user_id=principal.user_id,
            pat_id=principal.pat_id,
            project_id=project_id,
            project_name="hmall",
            analysis_status="done",
            graph_rev=self.graph_rev,
        )

    async def projects(self, user_id: int) -> list[dict]:
        # 返回副本：工具会就地追加 index，别污染测试自己的期望值。
        return [dict(p) for p in self.projects_result]


class FakeIndexStore:
    def __init__(self, states: dict[int, dict | None] | None = None) -> None:
        self.states = states or {}
        self.calls: list[int] = []

    async def get_state(self, project_id: int) -> dict | None:
        self.calls.append(project_id)
        return self.states.get(project_id)


def configure_with_fakes(
    *,
    java=None,
    context=None,
    index_store=None,
) -> None:
    deps.configure(
        McpServices(
            java=java or FakeJava(),
            retriever=None,
            index_store=index_store or FakeIndexStore(),
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
async def test_list_projects_merges_index_state_and_keeps_camel_case():
    context = FakeContext(
        projects=[
            {
                "projectId": 1,
                "name": "hmall",
                "analysisStatus": "done",
                "graphRev": 7,
            }
        ]
    )
    index_store = FakeIndexStore(
        {1: {"status": "ready", "activeRev": 7, "actual": 4120, "expected": 4123}}
    )
    configure_with_fakes(context=context, index_store=index_store)

    with principal_scope(GOOD):
        out = await archmind_list_projects()

    assert out["ok"] is True
    item = out["data"][0]
    assert item["projectId"] == 1 and item["name"] == "hmall"
    assert item["graphRev"] == 7
    assert item["index"] == {
        "status": "ready",
        "rev": 7,
        "coverage": "4120/4123",
    }


@pytest.mark.asyncio
async def test_list_projects_absent_state_is_explicit():
    context = FakeContext(projects=[{"projectId": 2, "name": "empty"}])
    configure_with_fakes(context=context, index_store=FakeIndexStore({}))

    with principal_scope(GOOD):
        out = await archmind_list_projects()

    assert out["data"][0]["index"] == {"status": "absent", "rev": 0}


@pytest.mark.asyncio
async def test_overview_returns_data_and_meta():
    configure_with_fakes(
        java=FakeJava(
            ToolEnvelope(
                ok=True,
                data=ProjectOverview(project_type="微服务", summary="电商交易系统"),
                evidence=[],
                truncated=False,
                graph_rev=7,
            )
        ),
        context=FakeContext(),
    )

    with principal_scope(GOOD):
        out = await archmind_overview(1)

    assert out["ok"] is True
    assert out["data"]["projectType"] == "微服务"
    assert out["meta"]["projectId"] == 1
    assert out["meta"]["graphRev"] == 7
    assert out["meta"]["truncated"] is False


@pytest.mark.asyncio
async def test_overview_unauthorized_project_maps_to_not_authorized():
    configure_with_fakes(
        context=FakeContext(
            context_error=McpAuthError(403, "无权访问该项目"),
        )
    )

    with principal_scope(GOOD):
        out = await archmind_overview(99)

    assert out["ok"] is False
    assert out["error"]["code"] == "not_authorized"
    assert "99" not in str(out)


@pytest.mark.asyncio
async def test_overview_java_failure_maps_to_tool_failed():
    configure_with_fakes(
        java=FakeJava(
            ToolEnvelope(
                ok=False,
                data=None,
                evidence=[],
                truncated=False,
                graph_rev=0,
                note="该项目尚未分析",
            )
        ),
        context=FakeContext(),
    )

    with principal_scope(GOOD):
        out = await archmind_overview(1)

    assert out["ok"] is False
    assert out["error"]["code"] == "tool_failed"
    assert out["error"]["message"] == "该项目尚未分析"


@pytest.mark.asyncio
async def test_overview_propagates_truncated():
    configure_with_fakes(
        java=FakeJava(
            ToolEnvelope(
                ok=True,
                data=ProjectOverview(summary="大项目"),
                evidence=[],
                truncated=True,
                graph_rev=3,
            )
        ),
        context=FakeContext(graph_rev=3),
    )

    with principal_scope(GOOD):
        out = await archmind_overview(1)

    assert out["ok"] is True
    assert out["meta"]["truncated"] is True
