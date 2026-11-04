"""Heading-aware document chunking with embedding-tokenizer measurements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

TOKEN_TARGET = 650
TOKEN_OVERLAP = 100
TOKEN_MINIMUM = 500
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
    # Story 7.5: the page, slide or sheet the chunk comes from ("Page 3",
    # "Slide 2", "Sheet: Cafeteria"), kept apart from the section heading so a
    # citation can name both. None for formats without pages (e.g. Word).
    location: str | None = None


def location_label(marker_line: str) -> str:
    """``"[Page 3 — OCR]"`` -> ``"Page 3"``; ``"[Sheet: Rooms]"`` -> ``"Sheet: Rooms"``."""
    label = marker_line.strip().strip("[]").strip()
    label = re.split(r"\s+[—–-]\s+OCR\b", label, maxsplit=1)[0]
    return label.strip()


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


def _split_sections(text: str) -> list[tuple[str | None, str, str | None]]:
    """Sections as (heading, body, location); location is the page/slide/sheet."""
    sections: list[tuple[str | None, str, str | None]] = []
    heading_levels: list[tuple[int, str]] = []
    current_heading: str | None = None
    current_location: str | None = None
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((current_heading, body, current_location))
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
            if _STRUCTURAL_HEADING.match(line):
                current_location = location_label(line)
            current_heading = line.strip()
            heading_levels.clear()
            continue
        body_lines.append(line)
    flush()

    if not sections and text.strip():
        sections.append((None, text.strip(), None))
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

    def append_chunk(content: str, heading: str | None, location: str | None) -> None:
        token_count = count_tokens(content)
        if token_count > max_tokens:
            raise ValueError("Packed document chunk exceeds the embedding token limit.")
        packed.append(
            DocumentChunkText(
                content=content,
                heading=heading,
                token_count=token_count,
                location=location,
            )
        )

    for chunk in chunks:
        if current is None:
            current = chunk
            current_headings = [chunk.heading] if chunk.heading else []
            continue

        combined = f"{current.content}\n\n{chunk.content}"
        # Chunks from different pages are never merged, so a chunk has one page.
        if count_tokens(combined) <= target_tokens and chunk.location == current.location:
            current = DocumentChunkText(
                content=combined,
                heading=current.heading,
                token_count=count_tokens(combined),
                location=current.location,
            )
            if chunk.heading and (
                not current_headings or chunk.heading != current_headings[-1]
            ):
                current_headings.append(chunk.heading)
            continue

        append_chunk(
            current.content,
            " | ".join(current_headings) if current_headings else None,
            current.location,
        )

        # Chunks split inside one heading already overlap in the section chunker.
        # At a heading boundary, carry context forward when it fits.
        next_content = chunk.content
        previous_heading = current_headings[-1] if current_headings else None
        # Never carry text over a page boundary: a chunk's page must be its own.
        if previous_heading != chunk.heading and chunk.location == current.location:
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
            location=chunk.location,
        )
        current_headings = [chunk.heading] if chunk.heading else []

    if current is not None:
        append_chunk(
            current.content,
            " | ".join(current_headings) if current_headings else None,
            current.location,
        )
    return packed


def _chunk_with_model_tokenizer(
    text: str,
    count_tokens: Callable[[str], int],
    encode_tokens: Callable[[str], tuple[list[int], list[tuple[int, int]]]],
    *,
    target_tokens: int,
    overlap_tokens: int,
    max_tokens: int,
) -> list[DocumentChunkText]:
    sections = _split_sections(text)
    page_groups: list[list[tuple[str | None, str, str | None]]] = []
    for section in sections:
        if not page_groups or page_groups[-1][-1][2] != section[2]:
            page_groups.append([])
        page_groups[-1].append(section)

    chunks: list[DocumentChunkText] = []
    for page_sections in page_groups:
        page_text = ""
        section_boundaries: list[tuple[int, int, str | None]] = []
        for heading, body, _ in page_sections:
            if page_text:
                page_text += "\n\n"
            section_start = len(page_text)
            heading_context = f"{heading}\n\n" if heading else ""
            body_start = section_start + len(heading_context)
            page_text += f"{heading_context}{body}"
            section_boundaries.append((section_start, body_start, heading))

        token_ids, offsets = encode_tokens(page_text)
        if len(token_ids) != len(offsets):
            raise RuntimeError("The embedding tokenizer returned invalid token offsets.")
        if not token_ids:
            continue

        safe_boundaries = {0, len(token_ids)}
        safe_boundaries.update(
            token_index
            for token_index, (character_offset, _) in enumerate(offsets)
            if character_offset == 0 or page_text[character_offset - 1].isspace()
        )

        def section_at(token_index: int) -> tuple[int, int, str | None]:
            character_offset = offsets[token_index][0]
            selected = section_boundaries[0]
            for boundary in section_boundaries[1:]:
                if boundary[0] > character_offset:
                    break
                selected = boundary
            return selected

        def render(start: int, end: int) -> tuple[str, str | None]:
            _, body_start, heading = section_at(start)
            repeated_heading = (
                f"{heading}\n\n"
                if heading and offsets[start][0] >= body_start
                else ""
            )
            start_offset = offsets[start][0]
            end_offset = (
                offsets[end][0]
                if end < len(token_ids)
                else len(page_text)
            )
            content = repeated_heading + page_text[start_offset:end_offset]
            return content, heading

        page_content, _ = render(0, len(token_ids))
        page_token_count = count_tokens(page_content)
        if page_token_count < TOKEN_MINIMUM:
            chunks.append(
                DocumentChunkText(
                    content=page_content,
                    heading=section_at(0)[2],
                    token_count=page_token_count,
                    location=page_sections[0][2],
                )
            )
            continue

        def remaining_chunk_count(token_count: int) -> int | None:
            if token_count <= max_tokens:
                return 1
            capacity_per_additional_chunk = max_tokens - overlap_tokens
            chunk_count = (
                token_count - overlap_tokens + capacity_per_additional_chunk - 1
            ) // capacity_per_additional_chunk
            minimum_page_tokens = (
                chunk_count * (TOKEN_MINIMUM - overlap_tokens) + overlap_tokens
            )
            return chunk_count if token_count >= minimum_page_tokens else None

        start = 0
        while start < len(token_ids):
            remaining_content, remaining_heading = render(start, len(token_ids))
            remaining_count = count_tokens(remaining_content)
            if remaining_count <= max_tokens and remaining_count >= TOKEN_MINIMUM:
                end = len(token_ids)
                content, heading = remaining_content, remaining_heading
            else:
                remaining_tokens = len(token_ids) - start
                chunks_needed = remaining_chunk_count(remaining_tokens)
                if chunks_needed is None or chunks_needed <= 1:
                    raise ValueError(
                        "This page cannot be divided into 500-800 token chunks "
                        "with the requested exact overlap."
                    )
                desired_span = (
                    remaining_tokens + (chunks_needed - 1) * overlap_tokens
                    + chunks_needed - 1
                ) // chunks_needed
                preferred_span = min(max(target_tokens, TOKEN_MINIMUM), max_tokens)
                candidate_range = range(
                    start + overlap_tokens + 1,
                    min(len(token_ids), start + max_tokens) + 1,
                )
                preferred_candidates = sorted(
                    (
                        boundary
                        for boundary in candidate_range
                        if boundary in safe_boundaries
                        and boundary - overlap_tokens in safe_boundaries
                    ),
                    key=lambda boundary: (
                        abs(boundary - start - desired_span),
                        abs(boundary - start - preferred_span),
                    ),
                )
                fallback_candidates = sorted(
                    candidate_range,
                    key=lambda boundary: (
                        abs(boundary - start - desired_span),
                        abs(boundary - start - preferred_span),
                    ),
                )
                selected_end: int | None = None
                for candidates in (preferred_candidates, fallback_candidates):
                    for candidate_end in candidates:
                        candidate_content, _ = render(start, candidate_end)
                        candidate_count = count_tokens(candidate_content)
                        if not TOKEN_MINIMUM <= candidate_count <= max_tokens:
                            continue
                        next_start = candidate_end - overlap_tokens
                        next_token_count = len(token_ids) - next_start
                        if remaining_chunk_count(next_token_count) is None:
                            continue
                        if next_token_count <= max_tokens:
                            next_content, _ = render(next_start, len(token_ids))
                            next_count = count_tokens(next_content)
                            if not TOKEN_MINIMUM <= next_count <= max_tokens:
                                continue
                        selected_end = candidate_end
                        break
                    if selected_end is not None:
                        break
                if selected_end is None:
                    raise ValueError(
                        "The embedding tokenizer could not produce chunks with "
                        "the requested token limits and exact overlap."
                    )
                end = selected_end
                content, heading = render(start, end)

            token_count = count_tokens(content)
            if not TOKEN_MINIMUM <= token_count <= max_tokens:
                raise ValueError("A document chunk is outside the embedding token limits.")
            chunks.append(
                DocumentChunkText(
                    content=content,
                    heading=heading,
                    token_count=token_count,
                    location=page_sections[0][2],
                )
            )
            if end >= len(token_ids):
                break
            if end - start <= overlap_tokens:
                raise ValueError("The requested overlap leaves no room for chunk content.")
            start = end - overlap_tokens

    return chunks


def chunk_document_text(
    text: str,
    count_tokens: Callable[[str], int],
    *,
    target_tokens: int = TOKEN_TARGET,
    overlap_tokens: int = TOKEN_OVERLAP,
    max_tokens: int = TOKEN_LIMIT,
    encode_tokens: Callable[
        [str], tuple[list[int], list[tuple[int, int]]]
    ] | None = None,
) -> list[DocumentChunkText]:
    """Split extracted text with model tokens, preserving headings and page boundaries.

    Short pages stay unpadded and are not merged across page boundaries.
    """
    if not text.strip():
        return []

    if encode_tokens is not None:
        return _chunk_with_model_tokenizer(
            text,
            count_tokens,
            encode_tokens,
            target_tokens=target_tokens,
            overlap_tokens=overlap_tokens,
            max_tokens=max_tokens,
        )

    chunks: list[DocumentChunkText] = []
    for heading, section_text, location in _split_sections(text):
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
                    location=location,
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
