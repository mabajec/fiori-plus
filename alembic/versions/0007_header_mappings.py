"""header_mappings: per-user saved column-name → field mappings

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-17

When a file's header row doesn't auto-resolve (after normalization), the
user can manually map each unrecognized column to a known field. The
resulting mapping is stored here and reused for any future file whose
unrecognized-header *signature* matches.

`signature` is a stable hash of the sorted, normalized unrecognized
headers — so two files with the same column-naming quirk share a single
saved mapping. `mapping` is JSON: {file_header_string: field_name}.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "header_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("mapping", JSONB, nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "user_id", "signature", name="uq_header_mappings_user_signature"
        ),
    )


def downgrade() -> None:
    op.drop_table("header_mappings")
