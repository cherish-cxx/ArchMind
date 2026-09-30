"""T5 契约测试的公共夹具。

**这组测试是集成测试**：验的是「Java 实际怎么反应」，所以必须打真的 Java 后端。
换成 mock 就变成「我以为 Java 会怎么反应」—— 那恰好是 A1 契约漂移要防的东西。

由此有一条铁律：**Java 不可达时 skip，不是 fail。**
红只应该代表一件事：契约漂移了。环境问题不该制造假警报。
"""

import os

import httpx
import pytest
import pytest_asyncio

from app.config import INTERNAL_TOKEN_HEADER, settings
from app.tools.java_client import JavaClient

# 测试参数走环境变量 —— 换项目不用改测试代码
TEST_PROJECT_ID = int(os.getenv("TEST_PROJECT_ID", "2101954967776358402"))
TEST_CALL_CHAIN_START = os.getenv(
    "TEST_CALL_CHAIN_START", "com.hmall.trade.controller.OrderController"
)
# 未落图项目（overview 有数据、graphRev=0）。没配就是没有这个前置条件，测试 5 会 skip
TEST_UNMAPPED_PROJECT_ID = os.getenv("TEST_UNMAPPED_PROJECT_ID")

_JAVA_DOWN = (
    f"Java 后端不可达（{settings.java_base_url}）—— 环境问题，不是契约问题，跳过集成测试。"
    "需要：与服务器同一局域网 + 后端已起（cd ArchMind && ./mvnw spring-boot:run）"
)


@pytest.fixture(scope="session")
def java_alive() -> bool:
    """探测 Java 是否活着。

    **只要拿到 HTTP 响应就算活着**（哪怕 401 / 500）—— 我们只想知道
    「有没有东西在那儿监听」，不是「它答得对不对」。
    """
    try:
        httpx.post(
            f"{settings.java_base_url}/internal/tools/overview",
            json={"projectId": TEST_PROJECT_ID, "args": {}},
            headers={INTERNAL_TOKEN_HEADER: settings.internal_token},
            timeout=3.0,
            trust_env=False,
        )
        return True
    except httpx.TransportError:
        return False


@pytest.fixture
def require_java(java_alive: bool) -> None:
    """Java 不通就 skip —— 让「环境问题」和「契约漂移」在报告里长得不一样。"""
    if not java_alive:
        pytest.skip(_JAVA_DOWN)


@pytest_asyncio.fixture
async def client(require_java):
    """真的 JavaClient（走真 HTTP 到 localhost:8080），用完关掉。"""
    c = JavaClient()
    try:
        yield c
    finally:
        await c.aclose()


# ── 参数以夹具形式暴露：测试文件不必跨模块 import 常量 ──────────────────────


@pytest.fixture(scope="session")
def project_id() -> int:
    return TEST_PROJECT_ID


@pytest.fixture(scope="session")
def chain_start() -> str:
    return TEST_CALL_CHAIN_START


@pytest.fixture(scope="session")
def unmapped_project_id() -> int | None:
    return int(TEST_UNMAPPED_PROJECT_ID) if TEST_UNMAPPED_PROJECT_ID else None
