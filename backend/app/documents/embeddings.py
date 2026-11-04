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


def _model_and_tokenizer():
    model = embedding_model().model
    tokenizer = model.tokenizer
    if tokenizer is None:
        raise RuntimeError("The embedding model tokenizer is unavailable.")
    tokenizer.no_truncation()
    return model, tokenizer


def count_tokens(text: str) -> int:
    model, _ = _model_and_tokenizer()
    return model.token_count(text)


def encode_tokens(text: str) -> tuple[list[int], list[tuple[int, int]]]:
    model, tokenizer = _model_and_tokenizer()
    model.token_count(text)
    encoded = tokenizer.encode(text, add_special_tokens=False)
    return encoded.ids, encoded.offsets


def embed_passages(texts: Iterable[str]) -> list[list[float]]:
    return [
        vector.astype(float).tolist()
        for vector in embedding_model().passage_embed(list(texts))
    ]


def embed_query(text: str) -> list[float]:
    vector = next(embedding_model().query_embed([text]))
    return vector.astype(float).tolist()
