"""Paginated OData GET for SAP master-data entities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config.settings import settings
from app.procurement.reference_sync.constants import (
    HTTP_GET_MAX_ATTEMPTS,
    HTTP_GET_RETRY_BACKOFF_SEC,
    HTTP_GET_RETRYABLE_STATUS,
    ODATA_MAX_PAGES,
    ODATA_PAGE_SIZE,
    odata_collection_params,
    odata_page_size_for,
)
from app.procurement.sap_odata_utils import (
    odata_base_root,
    odata_deferred_uri,
    odata_entity_properties,
    odata_results_list,
)
from app.procurement.sap_pr_client import (
    _httpx_timeout,
    _initial_sap_usercontext_cookie,
    sap_gateway_query_params,
    sap_pr_configured,
)

log = logging.getLogger(__name__)


def sap_master_configured() -> bool:
    return sap_pr_configured()


def _auth() -> tuple[str, str]:
    user = (settings.procurement_sap_username or "").strip()
    password = settings.procurement_sap_password or ""
    if not user or not settings.procurement_sap_base_url.strip():
        raise RuntimeError("PROCUREMENT_SAP_* is not configured")
    return user, password


def _page_fingerprint(entry: dict[str, Any]) -> str:
    """Stable id for first row of a page — detect ignored ``$skip``."""
    props = odata_entity_properties(entry)
    if not props:
        return ""
    return "|".join(f"{k}={props[k]}" for k in sorted(props.keys())[:8])


def _skip_pagination_should_stop(
    *,
    len_rows: int,
    page_size: int,
    skip: int,
    prev_fingerprint: str | None,
    cur_fingerprint: str,
    has_next: bool,
) -> bool:
    """Return True when $skip/$top pagination should not continue."""
    if has_next:
        return False
    if len_rows < page_size:
        return True
    # SAP ignored $top and returned the full set in one response (e.g. TaxCodeSet).
    if len_rows > page_size:
        return True
    if skip > 0 and prev_fingerprint and cur_fingerprint == prev_fingerprint:
        return True
    return False


def _should_retry(exc: BaseException, status: int | None) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = exc.response.status_code
    if status is not None and status in HTTP_GET_RETRYABLE_STATUS:
        return True
    return False


class ODataClient:
    """Reusable async HTTP client for one sync run."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._base = odata_base_root(settings.procurement_sap_base_url)

    @classmethod
    @asynccontextmanager
    async def open(cls):
        user, password = _auth()
        async with httpx.AsyncClient(
            timeout=_httpx_timeout(),
            verify=bool(settings.procurement_sap_verify_ssl),
            auth=(user, password),
        ) as client:
            yield cls(client)

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        collection_path: str = "",
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Cookie": _initial_sap_usercontext_cookie(),
        }
        last_err: Exception | None = None
        for attempt in range(HTTP_GET_MAX_ATTEMPTS):
            try:
                resp = await self._client.get(url, params=params, headers=headers)
                if resp.status_code in HTTP_GET_RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"OData GET {collection_path or url} HTTP {resp.status_code}: "
                        f"{(resp.text or '')[:500]}"
                    )
                data = resp.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"OData GET {collection_path}: expected JSON object")
                return data
            except Exception as e:
                last_err = e
                if attempt + 1 >= HTTP_GET_MAX_ATTEMPTS or not _should_retry(
                    e, getattr(getattr(e, "response", None), "status_code", None)
                ):
                    break
                delay = HTTP_GET_RETRY_BACKOFF_SEC[
                    min(attempt, len(HTTP_GET_RETRY_BACKOFF_SEC) - 1)
                ]
                log.warning(
                    "OData GET retry %s/%s %s in %.0fs (%s)",
                    attempt + 1,
                    HTTP_GET_MAX_ATTEMPTS,
                    collection_path or url,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
        assert last_err is not None
        raise last_err

    async def iter_entities(
        self,
        collection_path: str,
        *,
        params: dict[str, str] | None = None,
        page_size: int = ODATA_PAGE_SIZE,
        max_pages: int = ODATA_MAX_PAGES,
        skip_increment: int | None = None,
        stop_pagination_on_error: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        extra = odata_collection_params(collection_path, params)
        extra.setdefault("$format", "json")
        skip = 0
        pages = 0
        prev_skip = -1
        prev_fingerprint: str | None = None

        while pages < max_pages:
            page_params = {
                **sap_gateway_query_params(),
                **extra,
                "$top": str(page_size),
                "$skip": str(skip),
            }
            url = f"{self._base}{collection_path}"
            try:
                data = await self.get_json(
                    url, params=page_params, collection_path=collection_path
                )
            except Exception as e:
                if stop_pagination_on_error and skip > 0:
                    log.warning(
                        "OData pagination stopped at skip=%s for %s (%s)",
                        skip,
                        collection_path,
                        e,
                    )
                    break
                raise
            root = data.get("d") if isinstance(data.get("d"), dict) else data
            if not isinstance(root, dict):
                break

            rows = odata_results_list(root)
            if not rows:
                break

            for entry in rows:
                if isinstance(entry, dict):
                    yield entry

            pages += 1
            next_link = root.get("__next") or odata_deferred_uri(root)
            if next_link:
                async for entry in self._iter_next_link(next_link, collection_path):
                    yield entry
                break

            cur_fp = _page_fingerprint(rows[0]) if rows else ""
            if _skip_pagination_should_stop(
                len_rows=len(rows),
                page_size=page_size,
                skip=skip,
                prev_fingerprint=prev_fingerprint,
                cur_fingerprint=cur_fp,
                has_next=False,
            ):
                if len(rows) > page_size:
                    log.debug(
                        "OData page exceeded $top=%s (%s rows) for %s — single response",
                        page_size,
                        len(rows),
                        collection_path,
                    )
                elif skip > 0 and cur_fp == prev_fingerprint:
                    log.warning(
                        "OData $skip ignored for %s (repeated page) — stopping",
                        collection_path,
                    )
                break
            prev_fingerprint = cur_fp
            if skip == prev_skip:
                log.error(
                    "OData pagination stuck at skip=%s for %s — stopping",
                    skip,
                    collection_path,
                )
                break
            prev_skip = skip
            skip += skip_increment if skip_increment is not None else len(rows)

        if pages >= max_pages:
            log.error(
                "OData pagination hit max_pages=%s for %s — catalogue may be incomplete",
                max_pages,
                collection_path,
            )

    async def _iter_next_link(
        self, next_url: str, collection_path: str
    ) -> AsyncIterator[dict[str, Any]]:
        url: str | None = next_url
        pages = 0
        while url and pages < ODATA_MAX_PAGES:
            data = await self.get_json(url, collection_path=collection_path)
            root = data.get("d") if isinstance(data.get("d"), dict) else data
            if not isinstance(root, dict):
                break
            rows = odata_results_list(root)
            if not rows:
                break
            for entry in rows:
                if isinstance(entry, dict):
                    yield entry
            pages += 1
            url = root.get("__next") or odata_deferred_uri(root) or None

    async def fetch_properties(
        self,
        collection_path: str,
        *,
        params: dict[str, str] | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        ps = page_size if page_size is not None else odata_page_size_for(collection_path)
        out: list[dict[str, Any]] = []
        async for entry in self.iter_entities(
            collection_path, params=params, page_size=ps
        ):
            props = odata_entity_properties(entry)
            props["_raw_entry"] = entry
            out.append(props)
        return out


async def fetch_collection_properties(
    *,
    collection_path: str,
    params: dict[str, str] | None = None,
    odata: ODataClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch one collection; uses a disposable client when *odata* is omitted."""
    if odata is not None:
        return await odata.fetch_properties(collection_path, params=params)
    async with ODataClient.open() as client:
        return await client.fetch_properties(collection_path, params=params)
