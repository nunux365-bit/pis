"""Semantic chunking for document ingestion."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Approximate tokens per character (conservative estimate)
CHARS_PER_TOKEN = 4

# Chunking parameters
MIN_CHUNK_TOKENS = 100
MAX_CHUNK_TOKENS = 800
OVERLAP_TOKENS = 50


@dataclass
class DocumentChunk:
    """A chunk of document content."""
    text: str
    section_title: str | None = None
    level: int = 0  # Heading depth
    chunk_index: int = 0
    metadata: dict | None = None


def chunk_document(
    content: str,
    min_tokens: int = MIN_CHUNK_TOKENS,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[DocumentChunk]:
    """
    Chunk document content using section-aware splitting.

    Strategy:
    1. Split by section headers (markdown-style or detected patterns)
    2. Split large sections by paragraphs
    3. Merge small adjacent chunks
    4. Add overlap for context continuity

    Args:
        content: Document text content
        min_tokens: Discard chunks smaller than this
        max_tokens: Split chunks larger than this
        overlap_tokens: Overlap between adjacent chunks

    Returns:
        List of DocumentChunk objects
    """
    if not content.strip():
        return []

    # Split into sections
    sections = _split_into_sections(content)

    # Process each section
    chunks = []
    chunk_index = 0

    for section in sections:
        section_chunks = _process_section(
            section["text"],
            section["title"],
            section["level"],
            max_tokens,
            overlap_tokens,
        )

        for chunk in section_chunks:
            # Skip tiny chunks
            if _estimate_tokens(chunk.text) < min_tokens:
                continue

            chunk.chunk_index = chunk_index
            chunks.append(chunk)
            chunk_index += 1

    log.debug("Created %d chunks from content (%d chars)", len(chunks), len(content))
    return chunks


def _split_into_sections(content: str) -> list[dict]:
    """
    Split content into sections based on headers.

    Detects markdown headers (# ##) and common patterns (CHAPTER, Section, etc.)
    """
    # Patterns for section headers
    header_patterns = [
        # Markdown headers
        (r"^(#{1,6})\s+(.+)$", lambda m: (len(m.group(1)), m.group(2).strip())),
        # Numbered sections (1. Section, 1.1 Subsection)
        (r"^(\d+(?:\.\d+)*)\s+([A-Z][^.!?\n]+)$", lambda m: (m.group(1).count(".") + 1, f"{m.group(1)} {m.group(2).strip()}")),
        # ALL CAPS headers
        (r"^([A-Z][A-Z\s]{5,50})$", lambda m: (1, m.group(1).strip())),
    ]

    lines = content.split("\n")
    sections = []
    current_section = {"title": None, "level": 0, "lines": []}

    for line in lines:
        header_match = None
        header_level = 0
        header_title = None

        for pattern, extractor in header_patterns:
            match = re.match(pattern, line.strip(), re.MULTILINE)
            if match:
                header_level, header_title = extractor(match)
                header_match = match
                break

        if header_match:
            # Save current section if it has content
            if current_section["lines"]:
                current_section["text"] = "\n".join(current_section["lines"]).strip()
                if current_section["text"]:
                    sections.append(current_section)

            # Start new section
            current_section = {
                "title": header_title,
                "level": header_level,
                "lines": [],
            }
        else:
            current_section["lines"].append(line)

    # Don't forget the last section
    if current_section["lines"]:
        current_section["text"] = "\n".join(current_section["lines"]).strip()
        if current_section["text"]:
            sections.append(current_section)

    # If no sections detected, treat entire content as one section
    if not sections:
        sections = [{"title": None, "level": 0, "text": content.strip()}]

    return sections


def _process_section(
    text: str,
    section_title: str | None,
    level: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[DocumentChunk]:
    """
    Process a section into chunks, splitting if too large.
    """
    estimated_tokens = _estimate_tokens(text)

    if estimated_tokens <= max_tokens:
        # Section fits in one chunk
        return [DocumentChunk(text=text, section_title=section_title, level=level)]

    # Split by paragraphs
    paragraphs = _split_paragraphs(text)

    chunks = []
    current_chunk_lines = []
    current_tokens = 0
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    for para in paragraphs:
        para_tokens = _estimate_tokens(para)

        if current_tokens + para_tokens > max_tokens and current_chunk_lines:
            # Flush current chunk
            chunk_text = "\n\n".join(current_chunk_lines)
            chunks.append(DocumentChunk(
                text=chunk_text,
                section_title=section_title,
                level=level,
            ))

            # Start new chunk with overlap
            if overlap_chars > 0 and chunk_text:
                overlap_text = chunk_text[-overlap_chars:]
                current_chunk_lines = [overlap_text]
                current_tokens = _estimate_tokens(overlap_text)
            else:
                current_chunk_lines = []
                current_tokens = 0

        current_chunk_lines.append(para)
        current_tokens += para_tokens

    # Flush remaining
    if current_chunk_lines:
        chunk_text = "\n\n".join(current_chunk_lines)
        chunks.append(DocumentChunk(
            text=chunk_text,
            section_title=section_title,
            level=level,
        ))

    return chunks


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs."""
    # Split on double newlines or multiple newlines
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


def _estimate_tokens(text: str) -> int:
    """Estimate token count from character count."""
    return len(text) // CHARS_PER_TOKEN
