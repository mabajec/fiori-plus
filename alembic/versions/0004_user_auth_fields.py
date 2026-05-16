"""User auth fields: totp_secret + force_password_change

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-16

Adds the two columns needed for Phase 4 authentication:
  - totp_secret: NULL when the user hasn't enrolled in 2FA yet. The
    enrolment flow generates a secret and stores it once the user
    successfully enters their first TOTP code.
  - force_password_change: set to TRUE when an admin creates a user
    or resets their password via CLI. The user is taken to the
    change-password page immediately after their first successful
    password login, before 2FA is reached.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.String(64), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "force_password_change",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "force_password_change")
    op.drop_column("users", "totp_secret")
