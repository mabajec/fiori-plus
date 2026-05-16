"""Project metadata: description, annual data, budget lines

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-16

Adds the data tables that back the per-project settings page:
  - projects.description: free-text description of the project
  - project_annual_data: one row per (project, year) with optional total
    budget and starting balance
  - project_budget_lines: zero or more category lines per annual row,
    each with a label, optional account-prefix mapping, and amount
All metadata is optional; the rest of the app must work without it.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("description", sa.Text(), nullable=True))

    op.create_table(
        "project_annual_data",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("total_budget", sa.Numeric(15, 2), nullable=True),
        sa.Column("starting_balance", sa.Numeric(15, 2), nullable=True),
        sa.UniqueConstraint("project_id", "year", name="uq_project_annual_data"),
    )

    op.create_table(
        "project_budget_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_annual_data_id",
            sa.Integer(),
            sa.ForeignKey("project_annual_data.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("account_prefix", sa.String(32), nullable=True),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False),
        sa.Column(
            "position", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_table("project_budget_lines")
    op.drop_table("project_annual_data")
    op.drop_column("projects", "description")
