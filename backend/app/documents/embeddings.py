"""Local CPU embeddings provided by FastEmbed; model downloads on first use."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from fastembed import TextEmbedding

EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-small-en"


@lru_cache(maxsize=1)
def embedding_model() -> TextEmbedding:
    cache_dir = os.environ.get("DOCUMENT_EMBEDDING_CACHE")
    if not cache_dir:
        cache_root = os.environ.get("LOCALAPPDATA")
        if cache_root:
            cache_dir = str(Path(cache_root) / "AIOfficeOperationsAgent" / "fastembed")
        else:
            cache_dir = str(Path.home() / ".cache" / "AIOfficeOperationsAgent" / "fastembed")
    return TextEmbedding(
        model_name=EMBEDDING_MODEL,
        cache_dir=cache_dir,
        threads=2,
        providers=["CPUExecutionProvider"],
    )


def count_tokens(text: str) -> int:
    return embedding_model().token_count(text)


def embed_passages(texts: Iterable[str]) -> list[list[float]]:
    return [
        vector.astype(float).tolist()
        for vector in embedding_model().passage_embed(list(texts))
    ]


def embed_query(text: str) -> list[float]:
    vector = next(embedding_model().query_embed([text]))
    return vector.astype(float).tolist()
