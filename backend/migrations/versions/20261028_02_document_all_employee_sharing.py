"""Bridge the historical document-sharing revision.

Revision ID: 20261028_02
Revises: 20261028_01
"""

from typing import Sequence

revision: str = "20261028_02"
down_revision: str | None = "20261028_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
