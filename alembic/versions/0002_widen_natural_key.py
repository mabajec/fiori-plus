"""Add employee and text to transactions natural key

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-16

Evidence from real exports showed the initial key dropped legitimately
distinct rows:
  - `employee`: a single payroll document can post the same contribution
    amount to multiple employees on the same day.
  - `text`: the same employee can have multiple postings on the same day
    that differ only by the reporting period embedded in the text
    (e.g. "202601/..." vs "202602/...").

Adding both columns covers every meaningful distinguishing column in the
source data.

"""
from typing import Sequence, Union

from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_transactions_natural_key", "transactions", type_="unique"
    )
    op.create_unique_constraint(
        "uq_transactions_natural_key",
        "transactions",
        [
            "project_id",
            "document_number",
            "account_code",
            "amount",
            "posting_date",
            "employee",
            "text",
        ],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_transactions_natural_key", "transactions", type_="unique"
    )
    op.create_unique_constraint(
        "uq_transactions_natural_key",
        "transactions",
        [
            "project_id",
            "document_number",
            "account_code",
            "amount",
            "posting_date",
        ],
    )
