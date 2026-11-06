"""会话索引 / 历史读取的接口形状。

这组测试只锁一件事：**“一个项目多个会话”对外暴露成什么形状**。
底层不再连真 MySQL —— 替身直接返回行对象，避免环境问题把契约测试变红。
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.agent import get_context_store
from app.config import INTERNAL_TOKEN_HEADER, settings
from app.main import app

_PROJECT_ID = 1001
_USER_ID = 2001
_H = {INTERNAL_TOKEN_HEADER: settings.internal_token}


class FakeConversationStore:
    def __init__(self):
        self.rows = [
            SimpleNamespace(
                id="c_a", project_id=_PROJECT_ID, title="它和订单服务怎么交互",
                turn_count=2, status="ACTIVE",
                created_at=datetime(2026, 11, 6, 10, 0, 0),
                updated_at=datetime(2026, 11, 6, 10, 5, 0),
            ),
            SimpleNamespace(
                id="c_b", project_id=_PROJECT_ID, title="调用链是什么",
                turn_count=1, status="ACTIVE",
                created_at=datetime(2026, 11, 5, 9, 0, 0),
                updated_at=datetime(2026, 11, 5, 9, 1, 0),
            ),
        ]
        self.turns = [
            SimpleNamespace(
                question="它和订单服务怎么交互", answer_message="通过 RPC 调用。",
                headline="跨服务调用", sections_json=[{
                    "title": "调用路径", "body": "Controller → Service",
                    "citedUids": ["a.C"],
                }],
                citations_json=[{"uid": "a.C", "kind": "CLASS"}],
                steps_json=[{"n": 1, "type": "TOOL", "detail": "class-relations", "ms": 12}],
                intent=None, confidence=0.9, graph_rev=3, latency_ms=1200,
                created_at=datetime(2026, 11, 6, 10, 5, 0),
            ),
        ]

    async def list_conversations(self, user_id, project_id=None, limit=50):
        return self.rows

    async def load_turns(self, user_id, project_id, conversation_id):
        return self.turns if conversation_id == "c_a" else []


@pytest.fixture
def store():
    s = FakeConversationStore()
    app.dependency_overrides[get_context_store] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(store):
    with TestClient(app) as c:
        yield c


def test_list_conversations_is_an_index_not_history(client):
    body = client.get(
        "/internal/agent/conversations",
        headers=_H,
        params={"userId": _USER_ID, "projectId": _PROJECT_ID},
    ).json()

    assert [c["id"] for c in body["conversations"]] == ["c_a", "c_b"]
    first = body["conversations"][0]
    assert first["title"] == "它和订单服务怎么交互"
    assert first["turnCount"] == 2
    assert first["updatedAt"].startswith("2026-11-06T10:05:00")
    assert "messages" not in first


def test_history_rebuilds_user_and_agent_turns(client):
    body = client.get(
        "/internal/agent/conversations/c_a/messages",
        headers=_H,
        params={"userId": _USER_ID, "projectId": _PROJECT_ID},
    ).json()

    assert body["exists"] is True
    assert [m["role"] for m in body["messages"]] == ["user", "agent"]
    assert body["messages"][0]["text"] == "它和订单服务怎么交互"
    agent = body["messages"][1]
    assert agent["headline"] == "跨服务调用"
    assert agent["sections"][0]["citedUids"] == ["a.C"]
    assert agent["meta"]["graphRev"] == 3


def test_missing_conversation_reports_expired_not_empty(client):
    body = client.get(
        "/internal/agent/conversations/c_missing/messages",
        headers=_H,
        params={"userId": _USER_ID, "projectId": _PROJECT_ID},
    ).json()

    assert body["exists"] is False
    assert body["reason"] == "SESSION_GONE"
    assert body["messages"] == []
