"""Project start / end dates

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-17

Adds two optional DATE columns to projects so the projection logic can
clamp the "period" to the actual project lifetime instead of always
assuming the calendar year. A project ending in July should project to
July, not to December.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("projects", sa.Column("end_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "end_date")
    op.drop_column("projects", "start_date")
