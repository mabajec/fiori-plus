"""import_runs: mode + rows_deleted

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-17

Re-imports can now run in three modes — add (default; skip duplicates),
replace (delete the project's existing rows in the file's date range,
then insert), and analyze (no DB writes, just a diff report). This
migration adds the columns needed to record which mode was used and how
many rows were deleted in the process.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "import_runs",
        sa.Column(
            "mode",
            sa.String(16),
            nullable=False,
            server_default="add",
        ),
    )
    op.add_column(
        "import_runs",
        sa.Column(
            "rows_deleted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("import_runs", "rows_deleted")
    op.drop_column("import_runs", "mode")
