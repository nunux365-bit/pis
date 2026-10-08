"""Index contract markdown in Qdrant with robust collection handling.

**Retrieval design (common RAG practice)**

- **Related-chunk discovery** is driven by the **embedded string** (``text``): markdown slice plus a
  short **structural prefix** (document title + chapter/section) from ``_add_contract_context_prefix``.
  That aligns neighboring chunks in embedding space and matches natural-language queries.

- **Structured commercial data** (payment terms, rate lines, parties, TDS, linked PDFs) lives in
  **Postgres / ``contract_payload``**. After a vector hit, load details by ``metadata.contract_document_id``
  (and ``contract_terms_version_id``). Duplicating those fields on every chunk inflates payloads and
  does not improve semantic similarity between chunks.

- **Per-chunk JSON-derived metadata** includes **contract identity filters** (kind, ref, dates,
  notice days, site keys, categories, ingestion class). **Key dates** also appear once as a short
  **prose summary line** inside ``text`` (before the markdown slice) so vector search matches
  queries about term length, notice period, etc., even when the current slice is about another topic.

Chunking: MarkdownHeaderTextSplitter on H1/H2, header carry-over, optional flat fallback.
Each markdown section is indexed as a single chunk (no recursive split). One vector per point;
payload ``text`` equals the embedded string.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter
from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import PayloadSchemaType

from app.config.settings import settings

log = logging.getLogger(__name__)


def _embedding_transient(exc: BaseException) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    if isinstance(exc, APIError):
        code = getattr(exc, "status_code", None)
        return code is None or code >= 500 or code == 429
    return False


def _qdrant_transient(exc: BaseException) -> bool:
    if isinstance(exc, UnexpectedResponse):
        code = exc.status_code
        return code is None or code >= 500 or code == 429
    return isinstance(exc, (TimeoutError, OSError))


def contract_chunk_filter_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Small set of **contract-level** fields copied onto every chunk for **Qdrant pre-filters** only
    (narrow the corpus before similarity). Not a substitute for DB JSON: no payment terms, rates,
    parties, or linked-document lists — those are retrieved via ``contract_document_id`` after search.

    Typical filters: ``contract_kind``, ISO date strings, ``termination_notice_days``, ``site_keys``,
    ``service_categories``, ``ingestion_class``. Range semantics on dates are for the application
    or Qdrant datetime indexes if you add them later (strings are stored as in the payload).
    """
    em = payload.get("extraction_metadata") if isinstance(payload.get("extraction_metadata"), dict) else {}
    ct = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    sites = payload.get("sites") if isinstance(payload.get("sites"), list) else []

    out: dict[str, Any] = {}
    if isinstance(em.get("ingestion_class"), str):
        out["ingestion_class"] = em["ingestion_class"]

    for key in ("contract_kind", "effective_from", "effective_to", "ref_number"):
        v = ct.get(key)
        if v is not None and v != "":
            out[key] = v

    exd = ct.get("execution_date")
    if isinstance(exd, str) and exd.strip():
        out["execution_date"] = exd.strip()

    if isinstance(ct.get("termination_notice_days"), int):
        out["termination_notice_days"] = ct["termination_notice_days"]

    ns = ct.get("non_solicitation_months")
    if isinstance(ns, int) and ns > 0:
        out["non_solicitation_months"] = ns

    site_keys: list[str] = []
    categories: set[str] = set()
    for s in sites:
        if not isinstance(s, dict):
            continue
        sk = s.get("site_key")
        if isinstance(sk, str) and sk.strip():
            site_keys.append(sk.strip())
        cat = s.get("service_category")
        if isinstance(cat, str) and cat.strip():
            categories.add(cat.strip())
    if site_keys:
        out["site_keys"] = sorted(set(site_keys))
    if categories:
        out["service_categories"] = sorted(categories)

    return out


# Backward-compatible name (same behaviour: filter-only metadata).
contract_vector_metadata_from_payload = contract_chunk_filter_metadata_from_payload


def contract_terms_summary_for_embedding(contract: dict[str, Any]) -> str | None:
    """
    One short prose line derived from ``contract`` (dates, notice, non-solicitation).

    Prepended to every chunk's ``text`` so dense retrieval aligns with natural questions about
    term and notice even when the chunk body is about rates, scope, etc.
    """
    if not contract:
        return None
    parts: list[str] = []
    ef = contract.get("effective_from")
    if isinstance(ef, str) and ef.strip():
        parts.append(f"Effective from {ef.strip()}.")
    et = contract.get("effective_to")
    if isinstance(et, str) and et.strip():
        parts.append(f"Term ends {et.strip()}.")
    elif isinstance(ef, str) and ef.strip():
        parts.append("No fixed end date (open-ended or auto-renewal).")
    ex = contract.get("execution_date")
    if isinstance(ex, str) and ex.strip():
        parts.append(f"Execution date {ex.strip()}.")
    tn = contract.get("termination_notice_days")
    if isinstance(tn, int) and tn >= 0:
        parts.append(f"Termination notice {tn} days.")
    ns = contract.get("non_solicitation_months")
    if isinstance(ns, int) and ns > 0:
        parts.append(f"Non-solicitation {ns} months post-termination.")
    if not parts:
        return None
    s = " ".join(parts)
    max_len = 400
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def _markdown_splitter() -> MarkdownHeaderTextSplitter:
    return MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "Chapter"),
            ("##", "Section"),
        ],
        strip_headers=False,
    )


def _add_contract_context_prefix(
    body: str,
    *,
    document_title: str | None,
    chapter: str | None,
    section: str | None,
    contract_terms_summary: str | None = None,
    client_summary: str | None = None,
    counterparties_summary: str | None = None,
) -> str:
    """
    Prepends **structural context** and optional **contract term facts** before embedding.

    Document/section lines help same-contract, same-section chunks cluster. The optional
    ``contract_terms_summary`` repeats key dates and notice period on every chunk so vector
    search can match term/notice queries without relying on that clause appearing in the slice.
    """
    lines: list[str] = []
    if document_title:
        t = document_title[:60] + "..." if len(document_title) > 60 else document_title
        lines.append(f"Document: {t}")
    hierarchy: list[str] = []
    if chapter:
        hierarchy.append(chapter)
    if section:
        hierarchy.append(section)
    if hierarchy:
        lines.append(f"Section: {' - '.join(hierarchy)}")
    if client_summary:
        lines.append(client_summary.strip())
    if counterparties_summary:
        lines.append(counterparties_summary.strip())
    if contract_terms_summary:
        lines.append(contract_terms_summary.strip())
    if not lines:
        return body
    return "\n".join(lines) + "\n\n" + body


def _parent_context(chapter: str | None, section: str | None) -> str:
    parts: list[str] = []
    if chapter:
        parts.append(f"Chapter: {chapter}")
    if section:
        parts.append(f"Section: {section}")
    return " | ".join(parts)


def _normalize_header_value(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        v = str(v)
    s = v.strip()
    return s or None


def _apply_chapter_section_carryover(docs: list[Document]) -> list[tuple[str, str | None, str | None]]:
    """
    Continuation chunks may omit headers; carry last seen Chapter/Section. A new H1 without ``##``
    clears the carried section until the next ``##``.
    """
    out: list[tuple[str, str | None, str | None]] = []
    last_ch: str | None = None
    last_se: str | None = None
    for d in docs:
        m = d.metadata or {}
        ch = _normalize_header_value(m.get("Chapter"))
        se = _normalize_header_value(m.get("Section"))
        if ch:
            last_ch = ch
            last_se = se
        elif se:
            last_se = se
        eff_ch = ch or last_ch
        eff_se = se if se else last_se
        text = (d.page_content or "").strip()
        if text:
            out.append((text, eff_ch, eff_se))
    return out


def _append_chunk_records(
    records: list[dict[str, Any]],
    *,
    body: str,
    chapter: str | None,
    section: str | None,
    document_title: str | None,
    structure_source: str,
    contract_terms_summary: str | None = None,
    client_summary: str | None = None,
    counterparties_summary: str | None = None,
) -> None:
    """One section/body equals one chunk, then add compact prefix context."""
    p = body.strip()
    if len(p) < settings.o2c_markdown_min_chunk_chars:
        return
    text = _add_contract_context_prefix(
        p,
        document_title=document_title,
        chapter=chapter,
        section=section,
        contract_terms_summary=contract_terms_summary,
        client_summary=client_summary,
        counterparties_summary=counterparties_summary,
    )
    records.append(
        {
            "text": text,
            "chapter": chapter,
            "section": section,
            "parent_context": _parent_context(chapter, section),
            "structure_source": structure_source,
        }
    )


def _chunk_from_markdown_pipeline(
    text: str,
    *,
    document_title: str | None,
    contract_terms_summary: str | None = None,
    client_summary: str | None = None,
    counterparties_summary: str | None = None,
) -> list[dict[str, Any]]:
    try:
        splitter_md = _markdown_splitter()
        sections = splitter_md.split_text(text)
    except Exception as e:
        log.warning("Markdown structure split failed; using flat recursive chunking: %s", e)
        return []
    if not sections:
        sections = [Document(page_content=text, metadata={})]
    labeled = _apply_chapter_section_carryover(sections)
    records: list[dict[str, Any]] = []
    for body, chapter, section in labeled:
        if not body:
            continue
        _append_chunk_records(
            records,
            body=body,
            chapter=chapter,
            section=section,
            document_title=document_title,
            structure_source="markdown_hierarchy",
            contract_terms_summary=contract_terms_summary,
            client_summary=client_summary,
            counterparties_summary=counterparties_summary,
        )
    return records


def _chunk_recursive_only(
    text: str,
    *,
    document_title: str | None,
    contract_terms_summary: str | None = None,
    client_summary: str | None = None,
    counterparties_summary: str | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    _append_chunk_records(
        records,
        body=text.strip(),
        chapter=None,
        section=None,
        document_title=document_title,
        structure_source="flat",
        contract_terms_summary=contract_terms_summary,
        client_summary=client_summary,
        counterparties_summary=counterparties_summary,
    )
    return records


def split_contract_markdown(
    markdown: str,
    *,
    document_title: str | None = None,
    contract_terms_summary: str | None = None,
    client_summary: str | None = None,
    counterparties_summary: str | None = None,
) -> dict[str, Any]:
    """
    Returns ``{"chunks": [...], "used_recursive_fallback": bool}``.

    Each chunk is ``{text, chapter, section, parent_context, structure_source}`` where ``text`` is
    the exact string to embed (optional term summary + document/section prefix + slice).
    If markdown-based splitting produces no chunks
    (e.g. all slices below min length), one recursive pass over the full document is used — no
    duplicate broad try/except around the whole pipeline.
    """
    text = (markdown or "").strip()
    if not text:
        return {"chunks": [], "used_recursive_fallback": False}

    records = _chunk_from_markdown_pipeline(
        text,
        document_title=document_title,
        contract_terms_summary=contract_terms_summary,
        client_summary=client_summary,
        counterparties_summary=counterparties_summary,
    )
    used_fallback = False
    if not records:
        records = _chunk_recursive_only(
            text,
            document_title=document_title,
            contract_terms_summary=contract_terms_summary,
            client_summary=client_summary,
            counterparties_summary=counterparties_summary,
        )
        used_fallback = True

    return {"chunks": records, "used_recursive_fallback": used_fallback}


async def _sleep_backoff_async(attempt: int) -> None:
    base = max(0.05, float(settings.o2c_vector_index_retry_base_seconds))
    await asyncio.sleep(base * (2**attempt))


async def _embed_text_batch_with_retry_async(
    client: AsyncOpenAI,
    texts: list[str],
) -> list[list[float]]:
    kwargs: dict[str, Any] = {
        "model": settings.o2c_embedding_model,
        "input": texts,
    }
    if settings.o2c_embedding_dimensions > 0:
        kwargs["dimensions"] = settings.o2c_embedding_dimensions

    last_exc: BaseException | None = None
    attempts = max(1, int(settings.o2c_vector_index_retries))
    for attempt in range(attempts):
        try:
            resp = await client.embeddings.create(**kwargs)
            return [list(d.embedding) for d in resp.data]
        except Exception as e:
            last_exc = e
            if not _embedding_transient(e) or attempt == attempts - 1:
                raise
            log.warning(
                "Embedding batch failed (attempt %s/%s), retrying: %s",
                attempt + 1,
                attempts,
                e,
            )
            await _sleep_backoff_async(attempt)
    assert last_exc is not None
    raise last_exc


async def _upsert_with_retry_async(
    qdrant: AsyncQdrantClient,
    *,
    collection_name: str,
    points: list[qm.PointStruct],
) -> None:
    attempts = max(1, int(settings.o2c_vector_index_retries))
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            await qdrant.upsert(collection_name=collection_name, points=points, wait=True)
            return
        except Exception as e:
            last_exc = e
            if not _qdrant_transient(e) or attempt == attempts - 1:
                raise
            log.warning(
                "Qdrant upsert failed (attempt %s/%s), retrying: %s",
                attempt + 1,
                attempts,
                e,
            )
            await _sleep_backoff_async(attempt)
    assert last_exc is not None
    raise last_exc


async def ensure_agentos_collection_exists_async(client: AsyncQdrantClient) -> None:
    if not settings.qdrant_enabled:
        return
    name = settings.qdrant_collection_agentos
    try:
        await client.get_collection(name)
        return
    except UnexpectedResponse as e:
        if e.status_code != 404:
            raise

    log.info("Qdrant collection %s not found; creating it", name)
    await client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(
            size=settings.o2c_embedding_dimensions,
            distance=qm.Distance.COSINE,
        ),
    )

    for field in (
        "metadata.contract_document_id",
        "metadata.contract_terms_version_id",
        "metadata.client_slug",
        "metadata.relative_path",
        "metadata.contract_kind",
        "metadata.ingestion_class",
    ):
        try:
            await client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except UnexpectedResponse as e:
            msg = str(e).lower()
            if "already exists" in msg or e.status_code == 409:
                continue
            log.warning("Qdrant payload index creation skipped for %s: %s", field, e)
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "already exist" in msg:
                continue
            log.warning("Qdrant payload index creation skipped for %s: %s", field, e)


async def index_contract_markdown_async(
    markdown: str,
    *,
    contract_document_id: str,
    contract_terms_version_id: str,
    relative_path: str,
    source_filename: str,
    client_slug: str | None,
    document_title: str | None = None,
    contract_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Same behaviour as ``index_contract_markdown`` using AsyncOpenAI embeddings and AsyncQdrantClient.
    """
    if not settings.qdrant_enabled:
        return {"status": "skipped", "reason": "qdrant_disabled", "chunks": 0}

    cl = (
        contract_payload.get("client")
        if isinstance(contract_payload, dict) and isinstance(contract_payload.get("client"), dict)
        else {}
    )
    parties = (
        contract_payload.get("parties")
        if isinstance(contract_payload, dict) and isinstance(contract_payload.get("parties"), list)
        else []
    )
    ct = (
        contract_payload.get("contract")
        if isinstance(contract_payload, dict) and isinstance(contract_payload.get("contract"), dict)
        else {}
    )
    client_line: str | None = None
    cn = cl.get("short_name") or cl.get("legal_name")
    if isinstance(cn, str) and cn.strip():
        client_line = f"Client: {cn.strip()[:120]}"
    cps: list[str] = []
    for p in parties:
        if not isinstance(p, dict):
            continue
        if p.get("party_role") not in ("primary_client", "co_client", "payer"):
            continue
        ln = p.get("legal_name")
        if isinstance(ln, str) and ln.strip():
            cps.append(ln.strip())
    cp_line: str | None = None
    if cps:
        dedup = list(dict.fromkeys(cps))
        joined = ", ".join(dedup[:4])
        if len(dedup) > 4:
            joined += ", ..."
        cp_line = f"Counterparties: {joined[:220]}"
    terms_line = contract_terms_summary_for_embedding(ct) if ct else None
    split_result = split_contract_markdown(
        markdown,
        document_title=document_title,
        contract_terms_summary=terms_line,
        client_summary=client_line,
        counterparties_summary=cp_line,
    )
    chunk_rows: list[dict[str, Any]] = split_result["chunks"]
    if not chunk_rows:
        return {"status": "skipped", "reason": "empty_markdown", "chunks": 0}

    extras = contract_chunk_filter_metadata_from_payload(contract_payload) if contract_payload else {}

    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY required for markdown vector indexing")

    collection_name = settings.qdrant_collection_agentos
    points: list[qm.PointStruct] = []
    batch_size = settings.o2c_embedding_batch_size
    texts = [c["text"] for c in chunk_rows]
    total = len(chunk_rows)

    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        timeout=settings.qdrant_timeout_seconds,
    )
    try:
        await ensure_agentos_collection_exists_async(qdrant)
        async with AsyncOpenAI(api_key=api_key) as emb_client:
            for start in range(0, len(texts), batch_size):
                batch_texts = texts[start : start + batch_size]
                batch_rows = chunk_rows[start : start + batch_size]
                vectors = await _embed_text_batch_with_retry_async(emb_client, batch_texts)
                if len(vectors) != len(batch_texts):
                    raise RuntimeError(
                        f"embedding length mismatch: got {len(vectors)} vectors for {len(batch_texts)} texts"
                    )
                for i, (row, vec) in enumerate(zip(batch_rows, vectors)):
                    idx = start + i
                    pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{contract_document_id}:{idx}"))
                    payload = {
                        "text": row["text"],
                        "metadata": {
                            "source": "o2c_contract_markdown",
                            "contract_document_id": contract_document_id,
                            "contract_terms_version_id": contract_terms_version_id,
                            "relative_path": relative_path,
                            "source_filename": source_filename,
                            "client_slug": client_slug,
                            "contract_title": document_title,
                            "chapter": row.get("chapter"),
                            "section": row.get("section"),
                            "parent_context": row.get("parent_context") or "",
                            "structure_source": row.get("structure_source"),
                            "chunk_index": idx,
                            "total_chunks": total,
                            "embedding_model": settings.o2c_embedding_model,
                            "embedding_dimensions": settings.o2c_embedding_dimensions,
                            **extras,
                        },
                    }
                    points.append(qm.PointStruct(id=pid, vector=vec, payload=payload))

                await _upsert_with_retry_async(qdrant, collection_name=collection_name, points=points)
                points.clear()
    finally:
        await qdrant.close()

    return {
        "status": "ok",
        "chunks": total,
        "collection": collection_name,
        "used_recursive_fallback": split_result.get("used_recursive_fallback", False),
    }

