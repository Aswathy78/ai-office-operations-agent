"""Persist document embeddings in sqlite-vec and expose chunk source metadata.

Revision ID: 20261104_01
Revises: 20261103_02
Create Date: 2026-11-04
"""

import json
from typing import Sequence

import sqlite_vec
from alembic import op
import sqlalchemy as sa

revision: str = "20261104_01"
down_revision: str | None = "20261103_02"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

VECTOR_DIMENSIONS = 512


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("document_chunks")
    }
    if "source" not in columns:
        op.add_column(
            "document_chunks",
            sa.Column("source", sa.String(length=512), nullable=True),
        )
    bind.execute(
        sa.text(
            "UPDATE document_chunks "
            "SET source = (SELECT original_filename FROM documents "
            "WHERE documents.id = document_chunks.document_id) "
            "WHERE source IS NULL"
        )
    )

    raw_connection = bind.connection.driver_connection
    raw_connection.enable_load_extension(True)
    try:
        sqlite_vec.load(raw_connection)
    finally:
        raw_connection.enable_load_extension(False)
    bind.exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunk_vectors "
        "USING vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[512])"
    )

    rows = bind.execute(
        sa.text("SELECT id, embedding FROM document_chunks")
    ).all()
    for chunk_id, stored_embedding in rows:
        vector = (
            json.loads(stored_embedding)
            if isinstance(stored_embedding, str)
            else stored_embedding
        )
        if not isinstance(vector, list) or len(vector) != VECTOR_DIMENSIONS:
            raise RuntimeError(
                f"Cannot migrate document chunk {chunk_id}: expected a "
                f"{VECTOR_DIMENSIONS}-dimension embedding."
            )
        bind.exec_driver_sql(
            "INSERT OR REPLACE INTO document_chunk_vectors (chunk_id, embedding) "
            "VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(vector)),
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS document_chunk_vectors")
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("document_chunks")
    }
    if "source" in columns:
        op.drop_column("document_chunks", "source")
