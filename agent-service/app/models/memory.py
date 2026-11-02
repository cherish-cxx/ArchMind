"""记忆的领域模型（R5）。

和 `models/context.py` 的分工：那边是**会话状态**，这边是**跨会话的派生事实**。
共同点是一律不持有图对象，只存 uid 字符串。

## v1 只写两类

`FACT` / `OPEN_QUESTION`。`PREFERENCE` / `CORRECTION` 留在枚举里是因为它们是
schema 的一部分，但写入路径不使用 —— 见方案 §1.1 D9。**不在仓库层拦截**：
拦截属于写入方，仓库保持通用，否则以后恢复这两类还要改数据层。

## 为什么哈希不折叠大小写

`OrderService` 和 `orderservice` 是两个东西。归一化只折叠空白，不动大小写。
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

#: 单条记忆的字符上限。超过说明不是一条原子断言，应该拆开或降级成会话摘要。
MAX_TEXT_CHARS = 120


class MemoryType(StrEnum):
    FACT = "FACT"
    OPEN_QUESTION = "OPEN_QUESTION"
    #: v1 不写，恢复时需要先定作用域（方案 §1.3 P2）
    PREFERENCE = "PREFERENCE"
    CORRECTION = "CORRECTION"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    #: 图谱 rev 变了，等重新验证或由新的蒸馏复活
    STALE = "stale"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class MemoryScope(StrEnum):
    PROJECT = "PROJECT"
    USER = "USER"


class MemoryTrust(StrEnum):
    EXPLICIT = "explicit"
    DISTILLED = "distilled"


def normalize_text(text: str) -> str:
    """折叠空白（含换行）。**不做大小写折叠** —— 类名的大小写有语义。"""
    return " ".join(text.split())


def hash_text(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryDraft:
    """写入一条记忆的入参。

    `subject_uids` 必须已经是当轮 `evidence_json` 的子集 —— 仓库不校验它，
    因为它看不到 evidence；这是写入方（显式入口 / 蒸馏器）的责任。
    """

    type: MemoryType
    text: str
    subject_uids: tuple[str, ...] = ()
    graph_rev: int = 0
    scope: MemoryScope = MemoryScope.PROJECT
    confidence: float = 0.5
    trust: MemoryTrust = MemoryTrust.DISTILLED
    source_conversation_id: str | None = None
    source_turn_no: int | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        normalized = normalize_text(self.text)
        if not normalized:
            raise ValueError("记忆文本不能为空")
        if len(normalized) > MAX_TEXT_CHARS:
            raise ValueError(
                f"记忆文本 {len(normalized)} 字，超过单条上限 {MAX_TEXT_CHARS}"
            )

    @property
    def text_hash(self) -> str:
        return hash_text(self.text)

    @property
    def primary_subject_uid(self) -> str | None:
        return self.subject_uids[0] if self.subject_uids else None


@dataclass(frozen=True)
class MemoryHit:
    """一次召回的结果。

    只用于 prompt 注入与日志，**不进 Context 快照**（`TurnArtifacts` 标了
    `exclude=True`）。它和检索候选一样：是"上次说过"，不是"本轮证据"。
    """

    id: int
    text: str
    type: MemoryType
    subject_uids: tuple[str, ...] = ()
    score: float = 0.0

    @property
    def label(self) -> str:
        return "事实" if self.type is MemoryType.FACT else "未决"
