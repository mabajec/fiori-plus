"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255)),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("password_hash", sa.String(255)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pps_element", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "owner_user_id", "pps_element", name="uq_projects_owner_pps"
        ),
    )

    op.create_table(
        "project_shares",
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_number", sa.String(64), nullable=False),
        sa.Column("account_code", sa.String(32), nullable=False),
        sa.Column("account_text", sa.String(255)),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("employee", sa.String(64)),
        sa.Column("text", sa.String(512)),
        sa.Column("source", sa.String(64)),
        sa.Column("year", sa.Integer()),
        sa.UniqueConstraint(
            "project_id",
            "document_number",
            "account_code",
            "amount",
            "posting_date",
            name="uq_transactions_natural_key",
        ),
    )
    op.create_index(
        "ix_transactions_project_id", "transactions", ["project_id"]
    )
    op.create_index(
        "ix_transactions_posting_date", "transactions", ["posting_date"]
    )

    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column(
            "rows_imported", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "rows_skipped", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("import_runs")
    op.drop_index("ix_transactions_posting_date", table_name="transactions")
    op.drop_index("ix_transactions_project_id", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("project_shares")
    op.drop_table("projects")
    op.drop_table("users")
