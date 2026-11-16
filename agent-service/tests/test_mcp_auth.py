"""MCP PAT ASGI 中间件：401 / 429 / principal 上下文 / 非 HTTP 作用域透传。

这里全部用替身，不连 Java / Redis —— 中间件的职责只有三件事：
取 Bearer、换 Principal（失败给状态码）、限流。
"""

import httpx

from app.mcp.auth import PatAuthMiddleware
from app.mcp.principal import McpAuthError, Principal, current_principal

GOOD = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="good",
)


class FakeContext:
    """只认 "good" 的最小鉴权客户端。"""

    def __init__(self, principal: Principal = GOOD) -> None:
        self.principal = principal
        self.calls: list[str] = []

    async def authenticate(self, token: str) -> Principal:
        self.calls.append(token)
        if token == "good":
            return self.principal
        raise McpAuthError(401, "无效或过期的 token")


class FakeLimiter:
    def __init__(self, allow: bool = True) -> None:
        self.allowed = allow
        self.calls: list[int] = []

    async def allow(self, pat_id: int) -> bool:
        self.calls.append(pat_id)
        return self.allowed


def _inner(seen: dict | None = None, status: int = 200):
    async def inner(scope, receive, send):
        if seen is not None:
            seen["principal"] = current_principal()
            seen["scope"] = scope
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return inner


def build_test_app(context=None, limiter=None, seen=None):
    return PatAuthMiddleware(_inner(seen), context or FakeContext(), limiter or FakeLimiter())


async def _post(app, path="/", headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        return await c.post(path, headers=headers, json={})


async def test_missing_token_returns_401():
    app = build_test_app()
    r = await _post(app)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_invalid_token_returns_401_and_does_not_echo_token():
    app = build_test_app()
    r = await _post(app, headers={"Authorization": "Bearer secret-value"})
    assert r.status_code == 401
    assert "secret-value" not in r.text


async def test_valid_token_sets_principal():
    seen: dict = {}
    context = FakeContext()
    limiter = FakeLimiter()
    app = build_test_app(context, limiter, seen)

    r = await _post(app, headers={"Authorization": "Bearer good"})

    assert r.status_code == 200
    assert seen["principal"] is not None
    assert seen["principal"].user_id == 7
    assert context.calls == ["good"]
    # 限流按 pat_id 统计，不是按 token 字符串
    assert limiter.calls == [1]


async def test_rate_limited_returns_429():
    app = build_test_app(limiter=FakeLimiter(allow=False))
    r = await _post(app, headers={"Authorization": "Bearer good"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"


async def test_lifespan_scope_is_forwarded_without_auth():
    seen: dict = {}

    async def inner(scope, receive, send):
        seen["type"] = scope["type"]

    app = PatAuthMiddleware(inner, FakeContext(), FakeLimiter(allow=False))
    await app({"type": "lifespan"}, None, None)

    assert seen["type"] == "lifespan"
