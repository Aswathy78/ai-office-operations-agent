"""Merge the two migration branches that both followed 20261018_01.

- 20261018_02 -> 20261018_03: meeting cancellation audit, local auth accounts (Story 5.4 / auth)
- 20261019_01: e-mail retry escalation and bounces (Story 4.6)

No schema changes; this only joins the branches so ``alembic upgrade head`` works.

Revision ID: 20261019_02
Revises: 20261018_03, 20261019_01
Create Date: 2026-10-19
"""

from typing import Sequence

revision: str = "20261019_02"
down_revision: tuple[str, str] = ("20261018_03", "20261019_01")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
