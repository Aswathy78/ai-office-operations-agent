"""Support safe approval decisions and signed email links (Story 9.6).

Revision ID: 20261108_02
Revises: 20261108_01
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20261108_02"
down_revision: str | None = "20261108_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _recover_stale_approval_batch_table() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    original_name = "assistant_action_approvals"
    temporary_name = "_alembic_tmp_assistant_action_approvals"
    if not inspector.has_table(temporary_name):
        return
    if not inspector.has_table(original_name):
        raise RuntimeError(
            f"Found {temporary_name!r} without {original_name!r}; refusing to alter "
            "approval data automatically."
        )

    original_columns = inspector.get_columns(original_name)
    temporary_columns = inspector.get_columns(temporary_name)
    original_signature = [(column["name"], str(column["type"])) for column in original_columns]
    temporary_signature = [(column["name"], str(column["type"])) for column in temporary_columns]
    if original_signature != temporary_signature or not original_signature:
        raise RuntimeError(
            f"Found incompatible {temporary_name!r}; refusing to discard or merge it."
        )

    metadata = sa.MetaData()
    original = sa.Table(original_name, metadata, autoload_with=connection)
    temporary = sa.Table(temporary_name, metadata, autoload_with=connection)
    primary_key = [column.name for column in original.primary_key.columns]
    if primary_key != ["id"] or [column.name for column in temporary.primary_key.columns] != ["id"]:
        raise RuntimeError(
            f"Found unexpected primary keys on {temporary_name!r}; refusing recovery."
        )

    original_rows = {
        row["id"]: tuple(row[column.name] for column in original.columns)
        for row in connection.execute(sa.select(original)).mappings()
    }
    staged_rows = list(connection.execute(sa.select(temporary)).mappings())
    old_statuses = {"pending", "rejected", "cancelled", "expired", "executed", "failed"}
    for row in staged_rows:
        existing = original_rows.get(row["id"])
        staged_values = tuple(row[column.name] for column in temporary.columns)
        if existing is not None:
            if existing != staged_values:
                raise RuntimeError(
                    f"Approval {row['id']!r} differs between the original and temporary "
                    "tables; refusing recovery."
                )
            continue
        if row["status"] not in old_statuses:
            raise RuntimeError(
                f"Approval {row['id']!r} has status {row['status']!r}, which the current "
                "original table cannot safely store; refusing recovery."
            )
        connection.execute(original.insert().values(**dict(row)))
        original_rows[row["id"]] = staged_values

    verified_rows = {
        row["id"]: tuple(row[column.name] for column in original.columns)
        for row in connection.execute(sa.select(original)).mappings()
    }
    if any(
        verified_rows.get(row["id"])
        != tuple(row[column.name] for column in temporary.columns)
        for row in staged_rows
    ):
        raise RuntimeError(
            f"Could not verify every staged approval in {temporary_name!r}; refusing recovery."
        )
    op.drop_table(temporary_name)


def _ensure_email_links_table() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    table_name = "assistant_approval_email_links"
    index_name = "ix_assistant_approval_email_links_approval_recipient"
    if not inspector.has_table(table_name):
        op.create_table(
            table_name,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("approval_id", sa.String(length=36), nullable=False),
            sa.Column("recipient_id", sa.String(length=36), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["approval_id"], ["assistant_action_approvals.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["recipient_id"], ["employees.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_assistant_approval_email_links_token_hash"),
        )
        inspector = sa.inspect(connection)
    else:
        expected_columns = [
            "id",
            "approval_id",
            "recipient_id",
            "token_hash",
            "expires_at",
            "used_at",
            "created_at",
        ]
        existing_columns = inspector.get_columns(table_name)
        if [column["name"] for column in existing_columns] != expected_columns:
            raise RuntimeError(
                f"Existing {table_name!r} has an unexpected schema; refusing to alter it."
            )
        unique_constraints = inspector.get_unique_constraints(table_name)
        if not any(
            constraint.get("name") == "uq_assistant_approval_email_links_token_hash"
            and constraint.get("column_names") == ["token_hash"]
            for constraint in unique_constraints
        ):
            raise RuntimeError(
                f"Existing {table_name!r} lacks its token uniqueness constraint."
            )
        foreign_keys = {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (foreign_key.get("options") or {}).get("ondelete", "").upper(),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        expected_foreign_keys = {
            (("approval_id",), "assistant_action_approvals", ("id",), "CASCADE"),
            (("recipient_id",), "employees", ("id",), "CASCADE"),
        }
        if foreign_keys != expected_foreign_keys:
            raise RuntimeError(
                f"Existing {table_name!r} has unexpected foreign keys; refusing to alter it."
            )

    indexes = {index["name"]: index for index in inspector.get_indexes(table_name)}
    existing_index = indexes.get(index_name)
    if existing_index is None:
        op.create_index(
            index_name,
            table_name,
            ["approval_id", "recipient_id"],
        )
    elif existing_index.get("column_names") != ["approval_id", "recipient_id"]:
        raise RuntimeError(
            f"Existing index {index_name!r} has unexpected columns; refusing to alter it."
        )


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("assistant_action_approvals"):
        _recover_stale_approval_batch_table()
        with op.batch_alter_table("assistant_action_approvals") as batch:
            batch.drop_constraint("ck_assistant_action_approvals_status", type_="check")
            batch.create_check_constraint(
                "ck_assistant_action_approvals_status",
                "status IN ('pending', 'rejected', 'cancelled', 'expired', "
                "'executing', 'executed', 'failed')",
            )
    elif sa.inspect(op.get_bind()).has_table("_alembic_tmp_assistant_action_approvals"):
        raise RuntimeError(
            "Found an approval staging table without assistant_action_approvals; "
            "refusing to alter approval data automatically."
        )

    _ensure_email_links_table()


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_approval_email_links_approval_recipient",
        table_name="assistant_approval_email_links",
    )
    op.drop_table("assistant_approval_email_links")
    with op.batch_alter_table("assistant_action_approvals") as batch:
        batch.drop_constraint("ck_assistant_action_approvals_status", type_="check")
        batch.create_check_constraint(
            "ck_assistant_action_approvals_status",
            "status IN ('pending', 'rejected', 'cancelled', 'expired', 'executed', 'failed')",
        )
