"""McpContextClient：Java 内部 MCP 接口的 HTTP 映射、错误码与缓存。

用 httpx.MockTransport，不发真请求 —— 这一层只该关心
「HTTP 状态码怎么变成 McpAuthError」和「camelCase 怎么变成领域对象」。
"""

import httpx
import pytest

from app.mcp.context_client import McpContextClient
from app.mcp.principal import McpAuthError, Principal


def _client(handler, cache_seconds: int | None = None) -> McpContextClient:
    return McpContextClient(
        base_url="http://java.test",
        internal_token="test-internal-token",
        transport=httpx.MockTransport(handler),
        cache_seconds=cache_seconds,
    )


PRINCIPAL = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="am_pat_token",
)


async def test_authenticate_parses_camel_case_and_sends_internal_header():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "patId": 11,
                "userId": 7,
                "boundProjectId": 99,
                "expiresAt": "2030-01-02T03:04:05",
            },
        )

    client = _client(handler)
    try:
        principal = await client.authenticate("am_pat_token")
    finally:
        await client.aclose()

    assert principal.pat_id == 11
    assert principal.user_id == 7
    assert principal.bound_project_id == 99
    assert principal.expires_at is not None
    assert principal.raw_token == "am_pat_token"
    assert len(requests) == 1
    req = requests[0]
    assert req.url.path == "/internal/mcp/authenticate"
    assert req.headers["X-Internal-Token"] == "test-internal-token"
    assert b"am_pat_token" in req.content


async def test_authenticate_401_raises_without_token_in_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "invalid"})

    client = _client(handler)
    try:
        with pytest.raises(McpAuthError) as excinfo:
            await client.authenticate("super-secret-token")
    finally:
        await client.aclose()

    assert excinfo.value.status == 401
    assert "super-secret-token" not in str(excinfo.value)


async def test_context_parses_camel_case():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/mcp/context"
        return httpx.Response(
            200,
            json={
                "userId": 7,
                "patId": 1,
                "projectId": 99,
                "projectName": "hmall",
                "analysisStatus": "COMPLETED",
                "graphRev": 7,
            },
        )

    client = _client(handler)
    try:
        ctx = await client.context(PRINCIPAL, 99)
    finally:
        await client.aclose()

    assert ctx.user_id == 7
    assert ctx.pat_id == 1
    assert ctx.project_id == 99
    assert ctx.project_name == "hmall"
    assert ctx.analysis_status == "COMPLETED"
    assert ctx.graph_rev == 7


async def test_context_403_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": 403, "message": "无权访问该项目"})

    client = _client(handler)
    try:
        with pytest.raises(McpAuthError) as excinfo:
            await client.context(PRINCIPAL, 99)
    finally:
        await client.aclose()

    assert excinfo.value.status == 403


async def test_authenticate_cache_avoids_second_http_call():
    counter = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["calls"] += 1
        return httpx.Response(200, json={"patId": 1, "userId": 7})

    # 显式开启缓存：默认 TTL=0（关闭），见下一个用例。
    client = _client(handler, cache_seconds=60)
    try:
        first = await client.authenticate("am_pat_token")
        second = await client.authenticate("am_pat_token")
    finally:
        await client.aclose()

    assert counter["calls"] == 1
    assert first.user_id == 7
    assert second.user_id == 7


async def test_default_cache_disabled_makes_two_http_calls():
    """默认 TTL=0：吊销/过期必须立即 401，不能让缓存兜住鉴权结论。"""
    counter = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["calls"] += 1
        return httpx.Response(200, json={"patId": 1, "userId": 7})

    client = _client(handler)
    try:
        await client.authenticate("am_pat_token")
        await client.authenticate("am_pat_token")
    finally:
        await client.aclose()

    assert counter["calls"] == 2


async def test_projects_returns_data_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/mcp/projects"
        assert request.url.params.get("userId") == "7"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "ok",
                "data": [
                    {
                        "projectId": 1,
                        "name": "hmall",
                        "analysisStatus": "COMPLETED",
                        "graphRev": 7,
                    }
                ],
            },
        )

    client = _client(handler)
    try:
        projects = await client.projects(7)
    finally:
        await client.aclose()

    assert projects == [
        {
            "projectId": 1,
            "name": "hmall",
            "analysisStatus": "COMPLETED",
            "graphRev": 7,
        }
    ]
