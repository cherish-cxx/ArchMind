"""R7 Task 9：`archmind_project_brief`。

Java 与项目归属校验都用替身：验的是「工具怎么翻译信封、怎么校验 target」，
不是 Java 的渲染质量。
"""

import pytest

from app.mcp import deps
from app.mcp.deps import McpServices
from app.mcp.principal import Principal, ProjectContext, principal_scope
from app.mcp.tools import archmind_project_brief
from app.models.tool import BriefResponse, ToolEnvelope
from tests.fakes import FakeJava

GOOD = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="good",
)


class FakeContext:
    """最小上下文客户端：只做归属校验并返回固定项目上下文。"""

    def __init__(self, graph_rev: int = 7) -> None:
        self.graph_rev = graph_rev
        self.project_calls: list[int] = []

    async def authenticate(self, token: str) -> Principal:
        return GOOD

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

    async def projects(self, user_id: int) -> list[dict]:
        return []


class FakeIndexStore:
    async def get_state(self, project_id: int) -> dict | None:
        return None


def configure_with_fakes(*, java=None, context=None) -> None:
    deps.configure(
        McpServices(
            java=java or FakeJava(),
            retriever=None,
            index_store=FakeIndexStore(),
            context=context or FakeContext(),
            limiter=None,
        )
    )


def brief_envelope(
    *, ok: bool = True, filename: str = "CLAUDE.md", graph_rev: int = 7,
    note: str | None = None, truncated: bool = False, data: bool = True,
) -> ToolEnvelope:
    return ToolEnvelope(
        ok=ok,
        data=BriefResponse(
            filename=filename,
            content=f"# {filename}\nprojectId: 1\ngraphRev={graph_rev}",
            graph_rev=graph_rev,
        )
        if data
        else None,
        evidence=[],
        truncated=truncated,
        note=note,
        graph_rev=graph_rev,
    )


@pytest.fixture(autouse=True)
def _clean_deps():
    deps.reset()
    yield
    deps.reset()


@pytest.mark.asyncio
async def test_returns_filename_content_and_graph_rev():
    java = FakeJava(brief_envelope=brief_envelope(graph_rev=7))
    configure_with_fakes(java=java, context=FakeContext(graph_rev=7))

    with principal_scope(GOOD):
        out = await archmind_project_brief(1, "claude")

    assert out["ok"] is True
    assert out["data"]["filename"] == "CLAUDE.md"
    assert "projectId: 1" in out["data"]["content"]
    assert out["meta"]["graphRev"] == 7
    assert out["meta"]["projectId"] == 1
    assert out["meta"]["truncated"] is False


@pytest.mark.asyncio
async def test_agents_target_is_accepted_and_forwarded():
    java = FakeJava(brief_envelope=brief_envelope(filename="AGENTS.md"))
    configure_with_fakes(java=java)

    with principal_scope(GOOD):
        out = await archmind_project_brief(1, "agents")

    assert out["ok"] is True
    assert out["data"]["filename"] == "AGENTS.md"
    assert java.calls[-1][0] == "project-brief"
    assert java.kwargs[-1]["target"] == "agents"


@pytest.mark.asyncio
async def test_java_failure_maps_to_tool_failed():
    java = FakeJava(
        brief_envelope=brief_envelope(
            ok=False, data=False, graph_rev=0, note="该项目尚未分析"
        )
    )
    configure_with_fakes(java=java)

    with principal_scope(GOOD):
        out = await archmind_project_brief(1, "claude")

    assert out["ok"] is False
    assert out["error"]["code"] == "tool_failed"
    assert out["error"]["message"] == "该项目尚未分析"


@pytest.mark.asyncio
async def test_missing_data_with_ok_true_still_fails():
    java = FakeJava(
        brief_envelope=brief_envelope(ok=True, data=False, graph_rev=0)
    )
    configure_with_fakes(java=java)

    with principal_scope(GOOD):
        out = await archmind_project_brief(1, "claude")

    assert out["ok"] is False
    assert out["error"]["code"] == "tool_failed"
    assert out["error"]["message"] == "项目说明暂不可用"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["readme", "Claude", "", "AGENTS"])
async def test_invalid_target_is_rejected_before_java(target):
    java = FakeJava()
    context = FakeContext()
    configure_with_fakes(java=java, context=context)

    with principal_scope(GOOD):
        out = await archmind_project_brief(1, target)

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert "claude" in out["error"]["message"]
    assert java.calls == []
    assert context.project_calls == []
