"""启动冒烟：app 到底能不能起来。

**为什么单独一条**：P4.1 引入 `ContextStore` 时踩过一个坑 —— 构造函数参数名
叫 `redis`，遮住了 `redis` 模块，于是 `redis.asyncio.from_url` 取到的是那个参数（None）
→ 每次 `ContextStore()` 都抛 AttributeError，**服务根本起不来**。

而当时 43 个测试**全绿**：离线那批（guard/intent/loop）压根不碰 app；
`test_contract` 在 `require_java` 那步就 skip 了，`TestClient(app)` 从没进去过，
lifespan 也就从没跑过。

所以这条只干一件事：**把 lifespan 跑一遍**。
不需要 Java，也不需要 Redis —— `from_url` 是懒连接，不真的 get/set 就不拨号。
"""

from fastapi.testclient import TestClient

from app.main import app


def test_app_boots_and_health_ok() -> None:
    """lifespan 能跑完（JavaClient / LLMClient / ContextStore 三个都不炸），/health 正常。"""
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
