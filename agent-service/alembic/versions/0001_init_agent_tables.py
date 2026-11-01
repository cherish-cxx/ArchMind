"""R5 初始三表

Revision ID: 0001
Revises:
Create Date: 2026-11-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False, server_default=""),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("graph_rev_at_start", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(3)")),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(3)")),
    )
    op.create_index(
        "idx_agent_conversation_owner",
        "agent_conversation",
        ["user_id", "project_id", "updated_at"],
    )

    op.create_table(
        "agent_turn",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("turn_no", sa.Integer(), nullable=False),

        sa.Column("transcript_json", sa.JSON(), nullable=False),
        sa.Column("llm_calls_json", sa.JSON(), nullable=False),

        sa.Column("question", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("rewritten_query", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column("focus_before", sa.String(512), nullable=True),
        sa.Column("method_uid", sa.String(512), nullable=True),

        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("outcome", sa.String(16), nullable=False, server_default="OK"),

        sa.Column("retrieval_json", sa.JSON(), nullable=True),
        sa.Column("tool_calls_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),

        sa.Column("answer_message", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("headline", sa.String(200), nullable=True),
        sa.Column("sections_json", sa.JSON(), nullable=False),
        sa.Column("citations_json", sa.JSON(), nullable=False),
        sa.Column("ui_commands_json", sa.JSON(), nullable=False),
        sa.Column("graph_patch_json", sa.JSON(), nullable=True),
        sa.Column("steps_json", sa.JSON(), nullable=False),

        sa.Column("graph_rev", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", mysql.DATETIME(fsp=3), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(3)")),

        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("conversation_id", "turn_no", name="uk_agent_turn_conv_turn"),
    )
    op.create_index(
        "idx_agent_turn_owner", "agent_turn", ["user_id", "project_id"]
    )

    op.create_table(
        "agent_context",
        sa.Column("conversation_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("updated_at", mysql.DATETIME(fsp=3), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP(3)")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_context")
    op.drop_index("idx_agent_turn_owner", table_name="agent_turn")
    op.drop_table("agent_turn")
    op.drop_index("idx_agent_conversation_owner", table_name="agent_conversation")
    op.drop_table("agent_conversation")
