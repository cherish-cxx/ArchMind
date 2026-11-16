"""回归：同一个进程里建两个 ASGI app，先后启动都必须成功。

`build_mcp()` 以前缓存单个 FastMCP；两个 app 在任一 lifespan 跑之前就建好时，
会共享同一个 StreamableHTTPSessionManager，第二个 app 启动即抛
`RuntimeError: ... can only be called once per instance`。
"""

from starlette.testclient import TestClient

from app.mcp import deps
from app.mcp.asgi import create_app
from app.mcp.principal import Principal

GOOD = Principal(
    pat_id=1,
    user_id=7,
    bound_project_id=None,
    expires_at=None,
    raw_token="good",
)


class FakeContext:
    async def authenticate(self, token: str) -> Principal:
        return GOOD


class FakeLimiter:
    async def allow(self, pat_id: int) -> bool:
        return True


class _Services:
    """create_app 只用到 context 与 limiter；其余组件这里不需要。"""

    def __init__(self) -> None:
        self.context = FakeContext()
        self.limiter = FakeLimiter()


def test_two_apps_start_sequentially_without_sharing_session_manager():
    first = create_app(_Services())
    second = create_app(_Services())  # 第二个 app 在第一个 lifespan 之前建好

    try:
        with TestClient(first) as client:
            assert client.get("/health").status_code == 200
        with TestClient(second) as client:
            assert client.get("/health").status_code == 200
    finally:
        deps.reset()
