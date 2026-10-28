"""Heading-aware document chunking with embedding-tokenizer measurements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

TOKEN_TARGET = 650
TOKEN_OVERLAP = 100
TOKEN_LIMIT = 800

_MARKDOWN_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_STRUCTURAL_HEADING = re.compile(r"^\s*\[(?:Page|Slide|Sheet):?[^\]]*\]\s*$", re.I)
_NUMBERED_HEADING = re.compile(
    r"^\s*(?:(?:section|chapter)\s+)?\d+(?:\.\d+)*(?:[.)])?\s+\S.{0,140}$",
    re.I,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class DocumentChunkText:
    content: str
    heading: str | None
    token_count: int


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _MARKDOWN_HEADING.match(line)
        or _STRUCTURAL_HEADING.match(line)
        or _NUMBERED_HEADING.match(line)
        or (stripped.endswith(":") and len(stripped.split()) <= 18)
        or (
            1 < len(stripped.split()) <= 14
            and stripped == stripped.upper()
            and any(character.isalpha() for character in stripped)
        )
    )


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    heading_levels: list[tuple[int, str]] = []
    current_heading: str | None = None
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((current_heading, body))
        body_lines.clear()

    for line in text.splitlines():
        markdown = _MARKDOWN_HEADING.match(line)
        if markdown:
            flush()
            level = len(markdown.group(1))
            heading_levels[:] = [
                (existing_level, title)
                for existing_level, title in heading_levels
                if existing_level < level
            ]
            heading_levels.append((level, markdown.group(2).strip()))
            current_heading = " > ".join(title for _, title in heading_levels)
            continue
        if _is_heading(line):
            flush()
            current_heading = line.strip()
            heading_levels.clear()
            continue
        body_lines.append(line)
    flush()

    if not sections and text.strip():
        sections.append((None, text.strip()))
    return sections


def _split_long_text(
    text: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[str]:
    words = text.split()
    parts: list[str] = []
    start = 0
    while start < len(words):
        low = start + 1
        high = len(words)
        best = low
        while low <= high:
            middle = (low + high) // 2
            candidate = " ".join(words[start:middle])
            if count_tokens(candidate) <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        parts.append(" ".join(words[start:best]))
        start = best
    return parts


def _trailing_overlap(
    text: str,
    target_tokens: int,
    count_tokens: Callable[[str], int],
) -> str:
    words = text.split()
    low, high = 1, len(words)
    best = len(words)
    while low <= high:
        middle = (low + high) // 2
        candidate = " ".join(words[-middle:])
        if count_tokens(candidate) <= target_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return " ".join(words[-best:]) if words else ""


def _pack_underfilled_chunks(
    chunks: list[DocumentChunkText],
    count_tokens: Callable[[str], int],
    *,
    target_tokens: int,
    overlap_tokens: int,
    max_tokens: int,
) -> list[DocumentChunkText]:
    """Pack short heading sections together without losing their boundaries."""
    packed: list[DocumentChunkText] = []
    current: DocumentChunkText | None = None
    current_headings: list[str] = []

    def append_chunk(content: str, heading: str | None) -> None:
        token_count = count_tokens(content)
        if token_count > max_tokens:
            raise ValueError("Packed document chunk exceeds the embedding token limit.")
        packed.append(
            DocumentChunkText(
                content=content,
                heading=heading,
                token_count=token_count,
            )
        )

    for chunk in chunks:
        if current is None:
            current = chunk
            current_headings = [chunk.heading] if chunk.heading else []
            continue

        combined = f"{current.content}\n\n{chunk.content}"
        if count_tokens(combined) <= target_tokens:
            current = DocumentChunkText(
                content=combined,
                heading=current.heading,
                token_count=count_tokens(combined),
            )
            if chunk.heading and (
                not current_headings or chunk.heading != current_headings[-1]
            ):
                current_headings.append(chunk.heading)
            continue

        append_chunk(
            current.content,
            " | ".join(current_headings) if current_headings else None,
        )

        # Chunks split inside one heading already overlap in the section chunker.
        # At a heading boundary, carry context forward when it fits.
        next_content = chunk.content
        previous_heading = current_headings[-1] if current_headings else None
        if previous_heading != chunk.heading:
            overlap = _trailing_overlap(
                current.content,
                overlap_tokens,
                count_tokens,
            )
            if overlap:
                candidate = f"{overlap}\n\n{chunk.content}"
                if count_tokens(candidate) <= max_tokens:
                    next_content = candidate

        current = DocumentChunkText(
            content=next_content,
            heading=chunk.heading,
            token_count=count_tokens(next_content),
        )
        current_headings = [chunk.heading] if chunk.heading else []

    if current is not None:
        append_chunk(
            current.content,
            " | ".join(current_headings) if current_headings else None,
        )
    return packed


def chunk_document_text(
    text: str,
    count_tokens: Callable[[str], int],
    *,
    target_tokens: int = TOKEN_TARGET,
    overlap_tokens: int = TOKEN_OVERLAP,
    max_tokens: int = TOKEN_LIMIT,
) -> list[DocumentChunkText]:
    """Split extracted text into bounded chunks, repeating heading context."""
    if not text.strip():
        return []

    chunks: list[DocumentChunkText] = []
    for heading, section_text in _split_sections(text):
        heading_context = f"{heading}\n\n" if heading else ""
        heading_tokens = count_tokens(heading_context)
        if heading_tokens >= max_tokens:
            heading_context = " ".join(heading_context.split()[-100:]) + "\n\n"
            heading_tokens = count_tokens(heading_context)
        capacity = max_tokens - heading_tokens
        desired = min(target_tokens - heading_tokens, capacity)
        if capacity <= 0 or desired <= 0:
            raise ValueError("A document heading is too large to chunk safely.")

        atoms: list[str] = []
        for paragraph in re.split(r"\n\s*\n", section_text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            sentences = [
                sentence.strip()
                for sentence in _SENTENCE_BOUNDARY.split(paragraph)
                if sentence.strip()
            ]
            for sentence in sentences:
                if count_tokens(sentence) > desired:
                    atoms.extend(_split_long_text(sentence, desired, count_tokens))
                else:
                    atoms.append(sentence)

        cursor = 0
        overlap = ""
        while cursor < len(atoms):
            body_parts = [overlap] if overlap else []
            used = count_tokens(" ".join(body_parts)) if body_parts else 0
            added = 0
            while cursor < len(atoms):
                candidate = " ".join((*body_parts, atoms[cursor])).strip()
                candidate_tokens = count_tokens(candidate)
                if candidate_tokens > desired and added:
                    break
                if candidate_tokens > capacity:
                    break
                if candidate_tokens > desired and not added and not body_parts:
                    break
                body_parts.append(atoms[cursor])
                used = candidate_tokens
                cursor += 1
                added += 1
                if used >= desired:
                    break

            body = " ".join(part for part in body_parts if part).strip()
            if not body and cursor < len(atoms):
                oversized = atoms[cursor]
                pieces = _split_long_text(oversized, capacity, count_tokens)
                atoms[cursor : cursor + 1] = pieces
                continue

            content = f"{heading_context}{body}".strip()
            count = count_tokens(content)
            chunks.append(
                DocumentChunkText(
                    content=content,
                    heading=heading,
                    token_count=count,
                )
            )
            if cursor >= len(atoms):
                break
            overlap = _trailing_overlap(body, overlap_tokens, count_tokens)
            if overlap and count_tokens(overlap) >= count_tokens(body):
                overlap = ""

    return _pack_underfilled_chunks(
        chunks,
        count_tokens,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        max_tokens=max_tokens,
    )
