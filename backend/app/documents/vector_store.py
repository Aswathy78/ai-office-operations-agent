"""Persistent SQLite vector storage backed by sqlite-vec."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

import sqlite_vec
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

VECTOR_TABLE = "document_chunk_vectors"
DEFAULT_VECTOR_DIMENSIONS = 512
_DIMENSION_PATTERN = re.compile(r"embedding\s+FLOAT\[(\d+)\]", re.IGNORECASE)


def _load_extension(db: Session) -> None:
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("Document vector storage requires SQLite.")
    raw_connection = connection.connection.driver_connection
    raw_connection.enable_load_extension(True)
    try:
        sqlite_vec.load(raw_connection)
    finally:
        raw_connection.enable_load_extension(False)


def ensure_vector_table(
    db: Session,
    dimensions: int = DEFAULT_VECTOR_DIMENSIONS,
) -> int:
    _load_extension(db)
    table_sql = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": VECTOR_TABLE},
    ).scalar_one_or_none()
    if table_sql is None:
        db.execute(
            text(
                f"CREATE VIRTUAL TABLE {VECTOR_TABLE} "
                f"USING vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[{dimensions}])"
            )
        )
        return dimensions

    match = _DIMENSION_PATTERN.search(table_sql)
    if match is None:
        raise RuntimeError("The document vector table has an unsupported schema.")
    return int(match.group(1))


def persist_vectors(db: Session, vectors: Iterable[tuple[str, list[float]]]) -> None:
    entries = list(vectors)
    if not entries:
        return
    dimensions = ensure_vector_table(db, len(entries[0][1]))
    for chunk_id, vector in entries:
        if len(vector) != dimensions:
            raise ValueError(
                f"Document embedding dimension {len(vector)} does not match "
                f"the vector store dimension {dimensions}."
            )
        db.execute(
            text(
                f"INSERT OR REPLACE INTO {VECTOR_TABLE} (chunk_id, embedding) "
                "VALUES (:chunk_id, :embedding)"
            ),
            {
                "chunk_id": chunk_id,
                "embedding": sqlite_vec.serialize_float32(vector),
            },
        )


def delete_vectors(db: Session, chunk_ids: Iterable[str]) -> None:
    ids = list(chunk_ids)
    if not ids:
        return
    table_exists = db.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": VECTOR_TABLE},
    ).scalar_one_or_none()
    if table_exists is None:
        return
    ensure_vector_table(db)
    statement = text(
        f"DELETE FROM {VECTOR_TABLE} WHERE chunk_id IN :chunk_ids"
    ).bindparams(bindparam("chunk_ids", expanding=True))
    db.execute(statement, {"chunk_ids": ids})


def retrieve_vectors(db: Session, chunk_ids: Iterable[str]) -> dict[str, list[float]]:
    ids = list(chunk_ids)
    if not ids:
        return {}
    ensure_vector_table(db)
    statement = text(
        f"SELECT chunk_id, vec_to_json(embedding) FROM {VECTOR_TABLE} "
        "WHERE chunk_id IN :chunk_ids"
    ).bindparams(bindparam("chunk_ids", expanding=True))
    rows = db.execute(statement, {"chunk_ids": ids}).all()
    return {
        chunk_id: [float(value) for value in json.loads(encoded_vector)]
        for chunk_id, encoded_vector in rows
    }


def backfill_missing_vectors(
    db: Session,
    vectors: Iterable[tuple[str, list[float]]],
    *,
    dimensions: int,
) -> dict[str, list[float]]:
    entries = list(vectors)
    actual_dimensions = ensure_vector_table(db, dimensions)
    existing = retrieve_vectors(db, (chunk_id for chunk_id, _ in entries))
    missing = [
        (chunk_id, vector)
        for chunk_id, vector in entries
        if chunk_id not in existing
    ]
    if missing:
        persist_vectors(db, missing)
        existing.update(retrieve_vectors(db, (chunk_id for chunk_id, _ in missing)))
    if any(len(vector) != actual_dimensions for vector in existing.values()):
        raise ValueError("A stored document vector has an invalid dimension.")
    return existing
