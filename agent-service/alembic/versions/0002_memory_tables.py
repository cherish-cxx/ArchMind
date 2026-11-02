"""R5 记忆表

- 新建 `agent_memory`：跨会话派生记忆，软状态迁移
- `agent_turn` 增 `memory_distilled_at`：蒸馏幂等标记

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_memory",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="PROJECT"),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("primary_subject_uid", sa.String(512), nullable=True),
        sa.Column("subject_uids", sa.JSON(), nullable=False),
        sa.Column("graph_rev", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("trust", sa.String(16), nullable=False, server_default="distilled"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("superseded_by", sa.BigInteger(), nullable=True),
        sa.Column("source_conversation_id", sa.String(64), nullable=True),
        sa.Column("source_turn_no", sa.Integer(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("expires_at", mysql.DATETIME(fsp=3), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(3)")),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(3)")),
        sa.UniqueConstraint(
            "user_id", "project_id", "scope", "type", "text_hash",
            name="uk_memory_dedup",
        ),
    )
    op.create_index(
        "idx_memory_owner",
        "agent_memory",
        ["user_id", "project_id", "status", "type"],
    )
    op.create_index(
        "idx_memory_subject",
        "agent_memory",
        ["user_id", "project_id", "primary_subject_uid", "status"],
    )

    op.add_column(
        "agent_turn",
        sa.Column("memory_distilled_at", mysql.DATETIME(fsp=3), nullable=True),
    )
    op.create_index("idx_turn_distill", "agent_turn", ["memory_distilled_at"])


def downgrade() -> None:
    op.drop_index("idx_turn_distill", table_name="agent_turn")
    op.drop_column("agent_turn", "memory_distilled_at")
    op.drop_index("idx_memory_subject", table_name="agent_memory")
    op.drop_index("idx_memory_owner", table_name="agent_memory")
    op.drop_table("agent_memory")
