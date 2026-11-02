"""R5 会话结算蒸馏：prompt、解析、扫描流程。

不连 MySQL / Redis / LLM：仓库、记忆服务、锁都用替身，LLM 用 `FakeLLM`。
真库路径仍由 `test_memory_repo.py` 的集成测试覆盖（默认跳过）。
"""

import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.distiller import (
    MAX_MEMORIES_PER_SESSION,
    MemoryDistiller,
    build_distill_messages,
    parse_memories,
)
from app.core.llm import LLMTransportError
from app.models.memory import MemoryType
from fakes import FakeLLM

PAY = "com.hmall.pay.controller.PayNotifyController"


def _turn(no: int, q: str, a: str, evidence: list[str], *, outcome: str = "OK",
          rev: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        id=no, turn_no=no, question=q, answer_message=a,
        evidence_json=evidence, outcome=outcome, graph_rev=rev,
    )


# ==================== prompt ====================


def test_build_prompt_contains_turns_and_evidence():
    messages = build_distill_messages([
        _turn(1, "支付回调从哪进", "从 PayNotifyController 进", [PAY]),
        _turn(2, "那它之后呢", "到 PayService，OrderClient 是跨服务断点", [PAY, "com.x.PayService"]),
    ])

    assert "蒸馏" not in messages[0]["content"], "系统提示是给模型的任务说明"
    user = messages[1]["content"]
    assert "第 1 轮" in user and "第 2 轮" in user
    assert "支付回调从哪进" in user
    assert PAY in user
    assert "graphRev=3" in user


# ==================== 解析 ====================


def test_parse_keeps_valid_and_allows_empty_subjects():
    raw = json.dumps({"memories": [
        {"type": "FACT", "text": "支付回调入口是 PayNotifyController",
         "subjectUids": [PAY], "confidence": 0.9},
        # 空 subject 是允许的（项目级结论）
        {"type": "OPEN_QUESTION", "text": "订单模块还没查完", "subjectUids": []},
    ]})

    drafts = parse_memories(
        raw, allowed_uids={PAY}, graph_rev=5,
        conversation_id="c1", turn_no=2,
    )

    assert [d.type for d in drafts] == [MemoryType.FACT, MemoryType.OPEN_QUESTION]
    assert drafts[0].subject_uids == (PAY,)
    assert drafts[0].graph_rev == 5
    assert drafts[0].source_turn_no == 2
    assert drafts[0].trust.value == "distilled"


def test_parse_rejects_preference_and_out_of_evidence_subjects():
    raw = json.dumps({"memories": [
        # 偏好 v1 不写
        {"type": "PREFERENCE", "text": "用户喜欢先结论", "subjectUids": []},
        # 指向证据外的实体 → 整条丢弃
        {"type": "FACT", "text": "指到证据外的结论", "subjectUids": ["com.x.Ghost"]},
    ]})

    assert parse_memories(
        raw, allowed_uids={PAY}, graph_rev=5,
        conversation_id="c1", turn_no=2,
    ) == []


def test_parse_caps_items_and_accepts_empty():
    items = [{"type": "FACT", "text": f"结论 {i}", "subjectUids": []} for i in range(10)]
    drafts = parse_memories(
        json.dumps({"memories": items}), allowed_uids=set(),
        graph_rev=1, conversation_id="c", turn_no=1,
    )
    assert len(drafts) == MAX_MEMORIES_PER_SESSION

    assert parse_memories(
        '{"memories": []}', allowed_uids=set(),
        graph_rev=1, conversation_id="c", turn_no=1,
    ) == []


def test_parse_rejects_malformed_output():
    common = dict(allowed_uids=set(), graph_rev=1, conversation_id="c", turn_no=1)
    with pytest.raises(ValueError):
        parse_memories("不是 json", **common)
    with pytest.raises(ValueError):
        parse_memories('{"items": []}', **common)


# ==================== 扫描流程 ====================


class FakeTurnRepo:
    def __init__(self, sessions, turns):
        self.sessions = sessions
        self.turns = turns
        self.marked: list[int] = []

    async def idle_conversations_for_distill(self, idle_minutes: int, limit: int = 10,
                                             max_age_days: int = 7):
        return self.sessions

    async def load_undistilled_turns(self, user_id, project_id, conversation_id,
                                     limit: int = 30):
        return self.turns

    async def mark_turns_distilled(self, ids: list[int]) -> int:
        self.marked.extend(ids)
        return len(ids)


class FakeMemoryService:
    enabled = True

    def __init__(self) -> None:
        self.written: list = []
        self.capacity_calls = 0

    async def remember(self, user_id, project_id, draft):
        self.written.append(draft)
        return (None, "inserted")

    async def enforce_capacity(self, user_id, project_id) -> None:
        self.capacity_calls += 1


class FakeLockRedis:
    def __init__(self) -> None:
        self.held: set[str] = set()

    async def set(self, key, value, nx=False, ex=None):
        if key in self.held:
            return None
        self.held.add(key)
        return True

    async def delete(self, key):
        self.held.discard(key)
        return 1

    async def aclose(self) -> None:
        self.held.clear()


def _distiller(monkeypatch, turns, replies, *, sessions=None):
    monkeypatch.setattr(settings, "memory_enabled", True)
    repo = FakeTurnRepo(sessions or [("c1", 7, 9)], turns)
    mem = FakeMemoryService()
    llm = FakeLLM(replies)
    return MemoryDistiller(repo, mem, llm, redis_client=FakeLockRedis()), repo, mem, llm


async def test_distill_writes_memories_and_marks_turns(monkeypatch):
    turns = [
        _turn(1, "支付回调从哪进", "从 PayNotifyController 进", [PAY]),
        _turn(2, "那它之后呢", "到 PayService，OrderClient 跨服务", [PAY]),
    ]
    reply = json.dumps({"memories": [{
        "type": "FACT", "text": "支付回调入口是 PayNotifyController",
        "subjectUids": [PAY], "confidence": 0.8,
    }]})
    distiller, repo, mem, llm = _distiller(monkeypatch, turns, [reply])

    assert await distiller.distill_once() == 1
    assert len(mem.written) == 1
    assert mem.written[0].subject_uids == (PAY,)
    assert repo.marked == [1, 2]
    assert mem.capacity_calls == 1
    assert llm.call_count == 1


async def test_llm_failure_keeps_turns_for_retry(monkeypatch):
    turns = [
        _turn(1, "问一", "答一", [PAY]),
        _turn(2, "问二", "答二", [PAY]),
    ]
    distiller, repo, mem, _ = _distiller(
        monkeypatch, turns, [LLMTransportError("超时")]
    )

    assert await distiller.distill_once() == 0
    assert repo.marked == []
    assert mem.written == []


async def test_single_turn_session_is_marked_without_calling_llm(monkeypatch):
    distiller, repo, mem, llm = _distiller(
        monkeypatch, [_turn(1, "问", "答", [PAY])], []
    )

    assert await distiller.distill_once() == 0
    assert repo.marked == [1]
    assert llm.call_count == 0


async def test_disabled_memory_does_nothing(monkeypatch):
    monkeypatch.setattr(settings, "memory_enabled", False)
    distiller, repo, mem, _ = _distiller(
        monkeypatch, [_turn(1, "问", "答", [PAY])], []
    )
    monkeypatch.setattr(settings, "memory_enabled", False)

    assert await distiller.distill_once() == 0
    assert repo.marked == []


async def test_second_worker_skips_locked_conversation(monkeypatch):
    turns = [
        _turn(1, "问一", "答一", [PAY]),
        _turn(2, "问二", "答二", [PAY]),
    ]
    reply = json.dumps({"memories": []})
    distiller, repo, mem, llm = _distiller(monkeypatch, turns, [reply])

    # 模拟另一个副本已经持有锁
    await distiller._redis.set(f"archmind:memory:distill:c1", "1")

    assert await distiller.distill_once() == 0
    assert llm.call_count == 0
    assert repo.marked == []
