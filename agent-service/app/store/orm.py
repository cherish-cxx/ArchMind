"""四张表的 ORM 映射（R5）。

- `agent_conversation`：会话索引 + 序列计数器
- `agent_turn`：回合流水（只追加）
- `agent_context`：状态快照（覆盖写）
- `agent_memory`：跨会话的派生记忆（软状态，只追加 + 状态迁移）

**列名是数据库契约**，迁移脚本按这里生成；改字段必须同时改迁移。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.store.db import Base


class Conversation(Base):
    __tablename__ = "agent_conversation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: 序列计数器：发号用。只能 +1，永不回退。
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_rev_at_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=func.now(3)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=func.now(3), onupdate=func.now(3)
    )

    __table_args__ = (
        Index("idx_agent_conversation_owner", "user_id", "project_id", "updated_at"),
    )


class Turn(Base):
    __tablename__ = "agent_turn"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 会话内序号，1 开始。由 `conversation.turn_count` 分配，永不修改。
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # 完整转录：本轮的 delta messages（含 retrieval / tool）
    transcript_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    # 每次 LLM 调用的元信息 + 原始返回
    llm_calls_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)

    # 输入
    question: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False, default="")
    rewritten_query: Mapped[str | None] = mapped_column(MEDIUMTEXT, nullable=True)
    focus_before: Mapped[str | None] = mapped_column(String(512), nullable=True)
    method_uid: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 判定（R6 去掉六选一后可空）
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    degraded: Mapped[bool] = mapped_column(nullable=False, default=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="OK")

    # 结构化产物
    retrieval_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_calls_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)

    # 输出
    answer_message: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False, default="")
    headline: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sections_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    citations_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    ui_commands_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    graph_patch_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    steps_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)

    # 运维
    graph_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    #: 这一轮是否已被记忆蒸馏消费。NULL = 还没蒸过；时间戳只用于排查。
    #: 幂等靠它，不靠 Redis 记批次 —— 重跑、崩溃续跑都不会重复写。
    memory_distilled_at: Mapped[datetime | None] = mapped_column(
        DATETIME(fsp=3), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=func.now(3)
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_no", name="uk_agent_turn_conv_turn"),
        Index("idx_agent_turn_owner", "user_id", "project_id"),
        Index("idx_turn_distill", "memory_distilled_at"),
    )


class ContextSnapshot(Base):
    __tablename__ = "agent_context"

    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_conversation.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=func.now(3), onupdate=func.now(3)
    )


class Memory(Base):
    """跨会话的派生记忆。一条一行，**只追加 + 状态迁移，不物理删**。

    失效（stale / expired / superseded / rejected）只改 `status`：
    MySQL 行保留是为了审计、复活和评测；Qdrant 那边由仓库的调用方删点。
    """

    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="PROJECT")
    #: v1 只写 FACT / OPEN_QUESTION，其余取值保留在 schema 里
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: 归一化文本的 sha256。唯一键用它而不是 text 本身 —— TEXT 不能直接做唯一键，
    #: 而且"文本一字不差"本来就是合并的判据。
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: `subject_uids` 是 JSON，按 subject 精确查（纠正/未决续接）走这一列
    primary_subject_uid: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subject_uids: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    graph_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    trust: Mapped[str] = mapped_column(String(16), nullable=False, default="distilled")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_turn_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=func.now(3)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=3), nullable=False, server_default=func.now(3), onupdate=func.now(3)
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", "scope", "type", "text_hash",
            name="uk_memory_dedup",
        ),
        Index("idx_memory_owner", "user_id", "project_id", "status", "type"),
        Index(
            "idx_memory_subject", "user_id", "project_id",
            "primary_subject_uid", "status",
        ),
    )
