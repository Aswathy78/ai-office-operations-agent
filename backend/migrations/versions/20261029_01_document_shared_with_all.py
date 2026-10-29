"""Documents shared with all employees (Story 7.3).

Adds ``documents.shared_with_all_employees`` so company-wide documents
(handbooks, SOPs) can be read and asked about by every active employee
without listing each person in the document's ACL.

Revision ID: 20261029_01
Revises: 20261028_01
Create Date: 2026-10-29
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261029_01"
down_revision: str | None = "20261028_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("documents")}
    if "shared_with_all_employees" not in columns:
        op.add_column(
            "documents",
            sa.Column(
                "shared_with_all_employees",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("shared_with_all_employees")
