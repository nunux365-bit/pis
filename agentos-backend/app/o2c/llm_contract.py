"""Multimodal contract extract: OpenAI or Anthropic (settings.llm_provider). PDF + text fallback."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.config.settings import settings
from app.o2c.pdf_text import extract_text_for_llm

log = logging.getLogger(__name__)


class LlmContractEnvelope(BaseModel):
    markdown_document: str = ""
    structured_contract: dict[str, Any] = Field(default_factory=dict)


def _strip_json_fence(s: str) -> str:
    s = s.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", s)
    if m:
        return m.group(1).strip()
    return s


def _build_user_prompt(source_filename: str, page_count: int, pdf_text: str) -> str:
    return (
        f"Source PDF filename: {source_filename}\n"
        f"Page count: {page_count}\n\n"
        "Below is extracted text from the PDF (native and/or OCR). "
        "Produce (1) a faithful markdown_document and (2) structured_contract per AGENOS schema "
        "(required top-level keys: extraction_metadata, client, sites, contract, parties, "
        "payment_terms, rate_lines).\n\n"
        "--- PDF TEXT ---\n"
        f"{pdf_text[:120000]}"
    )


def _sync_load_pdf_for_llm(path: Path) -> tuple[Path, bytes, str, int]:
    """Blocking PDF read + text extract + page count (runs in threadpool)."""
    p = path.expanduser().resolve()
    pdf_bytes = p.read_bytes()
    text, _ing_class = extract_text_for_llm(p)
    import fitz

    doc = fitz.open(p)
    try:
        page_count = doc.page_count
    finally:
        doc.close()
    return p, pdf_bytes, text, page_count


async def extract_markdown_and_structured(
    pdf_path: Path,
    *,
    repair_context: str | None = None,
    previous_json: dict[str, Any] | None = None,
) -> LlmContractEnvelope:
    """
    Calls configured LLM provider. Tries PDF-as-document for Anthropic / OpenAI when possible;
    always has text fallback content from pdf_text module.
    """
    pdf_path_resolved, pdf_bytes, text, page_count = await asyncio.to_thread(
        _sync_load_pdf_for_llm, pdf_path
    )
    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    base = _build_user_prompt(pdf_path_resolved.name, page_count, text)
    if repair_context:
        base = (
            f"VALIDATION / DB ERROR — fix structured_contract only; keep markdown_document updated if needed.\n"
            f"Error:\n{repair_context}\n\n"
            f"Previous JSON (excerpt): {json.dumps(previous_json, default=str)[:8000]}\n\n"
            + base
        )

    sys = (
        "You output a single JSON object with keys markdown_document (string) and "
        "structured_contract (object). No markdown fences around the outer JSON. "
        "structured_contract must satisfy the AGENOS contract ingestion schema."
    )

    if settings.llm_provider == "anthropic":
        return await _anthropic_extract(b64, sys, base)
    return await _openai_extract(b64, sys, base, pdf_path_resolved.name)


async def _anthropic_extract(b64: str, sys: str, user: str) -> LlmContractEnvelope:
    import anthropic

    key = (settings.anthropic_api_key or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing")

    doc_block = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        },
    }
    async with anthropic.AsyncAnthropic(api_key=key) as client:
        try:
            msg = await client.messages.create(
                model=settings.anthropic_chat_model,
                max_tokens=16384,
                system=sys,
                messages=[
                    {"role": "user", "content": [doc_block, {"type": "text", "text": user}]},
                ],
            )
        except Exception as e:
            log.warning("Anthropic PDF document input failed (%s); using text-only prompt", e)
            msg = await client.messages.create(
                model=settings.anthropic_chat_model,
                max_tokens=16384,
                system=sys,
                messages=[{"role": "user", "content": user}],
            )
        raw = ""
        for block in msg.content:
            if hasattr(block, "text"):
                raw += block.text
    return _parse_envelope(raw)


async def _openai_extract(b64: str, sys: str, user: str, filename: str) -> LlmContractEnvelope:
    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing")

    from openai import AsyncOpenAI

    # Try Responses API with PDF (OpenAI file input); fall back to chat + text-only on failure.
    async with AsyncOpenAI(api_key=key) as client:
        try:
            resp = await client.responses.create(
                model=settings.openai_chat_model,
                input=[
                    {"role": "system", "content": sys},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_file",
                                "filename": filename,
                                "file_data": f"data:application/pdf;base64,{b64}",
                            },
                            {"type": "input_text", "text": user},
                        ],
                    },
                ],
                text={"format": {"type": "json_object"}},
            )
            raw = resp.output_text or ""
            return _parse_envelope(raw)
        except Exception as e:
            log.warning("OpenAI PDF responses path failed (%s); falling back to text chat", e)
            completion = await client.chat.completions.create(
                model=settings.openai_chat_model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": user},
                ],
            )
            raw = completion.choices[0].message.content or ""
            return _parse_envelope(raw)


def _parse_envelope(raw: str) -> LlmContractEnvelope:
    raw = _strip_json_fence(raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    md = data.get("markdown_document")
    sc = data.get("structured_contract")
    if not isinstance(md, str):
        md = ""
    if not isinstance(sc, dict):
        sc = {}
    return LlmContractEnvelope(markdown_document=md, structured_contract=sc)
