"""Resolve local media for MySQL-sourced calls — Exotel HTTPS or Twilio Video compositions.

HTTP retries (429, 5xx, timeouts, connection errors) use
:class:`~app.agents.compliance_call.api_retry.async_call_with_retry` with
``settings.compliance_retry_attempts`` / ``compliance_retry_base_seconds`` /
``compliance_retry_max_sleep_seconds``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.agents.compliance_call.api_retry import async_call_with_retry, retryable_httpx_error
from app.config.settings import settings

log = logging.getLogger(__name__)

_VIDEO_BASE = "https://video.twilio.com/v1"


def _twilio_auth() -> tuple[str, str] | None:
    sid = (settings.twilio_account_sid or "").strip()
    tok = (settings.twilio_auth_token or "").strip()
    if not sid or not tok:
        return None
    return (sid, tok)


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


async def _download_bytes(client: httpx.AsyncClient, url: str, *, auth: tuple[str, str] | None) -> bytes:
    async def one() -> bytes:
        r = await client.get(url, auth=auth, follow_redirects=True, timeout=httpx.Timeout(600.0))
        r.raise_for_status()
        return r.content

    return await async_call_with_retry(
        "telephony_download_bytes",
        one,
        is_retryable=retryable_httpx_error,
    )


async def _exotel_to_temp(url: str) -> Path:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        data = await _download_bytes(client, url, auth=None)
    suffix = ".mp3"
    low = url.lower()
    if ".mp4" in low:
        suffix = ".mp4"
    elif ".m4a" in low:
        suffix = ".m4a"
    fd, name = tempfile.mkstemp(prefix="cc_exotel_", suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.write_bytes(data)
    return path


async def _pick_best_room_sid(client: httpx.AsyncClient, auth: tuple[str, str], room_name: str) -> str | None:
    """Rooms sharing UniqueName — keep those with ≥1 audio recording; pick longest ``duration``."""
    page = 0
    rooms: list[dict[str, Any]] = []
    while True:
        q = quote(room_name, safe="")
        url = f"{_VIDEO_BASE}/Rooms?Status=completed&PageSize=50&Page={page}&UniqueName={q}"

        async def one() -> Any:
            r = await client.get(url, auth=auth, timeout=httpx.Timeout(60.0))
            r.raise_for_status()
            return r.json()

        body = await async_call_with_retry(
            "twilio_rooms_list",
            one,
            is_retryable=retryable_httpx_error,
        )
        chunk = body.get("rooms") or []
        rooms.extend(chunk)
        meta = body.get("meta") or {}
        if not meta.get("next_page_url"):
            break
        page += 1
        if page > 50:
            log.warning("twilio rooms pagination exceeded 50 pages for UniqueName=%s", room_name)
            break

    best_sid: str | None = None
    best_dur = -1
    for room in rooms:
        rsid = str(room.get("sid") or "")
        if not rsid:
            continue
        ru = f"{_VIDEO_BASE}/Rooms/{rsid}/Recordings"

        async def one_rec() -> Any:
            rr = await client.get(ru, auth=auth, timeout=httpx.Timeout(60.0))
            rr.raise_for_status()
            return rr.json()

        rec_body = await async_call_with_retry(
            "twilio_room_recordings",
            one_rec,
            is_retryable=retryable_httpx_error,
        )
        recs = (rec_body.get("recordings")) or []
        if not any(str(x.get("type") or "").lower() == "audio" for x in recs):
            continue
        dur = int(room.get("duration") or 0)
        if dur > best_dur:
            best_dur = dur
            best_sid = rsid
    return best_sid


async def _poll_composition_ready(
    client: httpx.AsyncClient, auth: tuple[str, str], composition_sid: str
) -> dict[str, Any]:
    deadline = float(settings.compliance_twilio_composition_max_wait_seconds or 600.0)
    interval = float(settings.compliance_twilio_poll_interval_seconds or 15.0)
    t0 = time.monotonic()
    url = f"{_VIDEO_BASE}/Compositions/{composition_sid}"
    last: dict[str, Any] = {}
    while time.monotonic() - t0 < deadline:
        async def one_poll() -> dict[str, Any]:
            r = await client.get(url, auth=auth, timeout=httpx.Timeout(60.0))
            r.raise_for_status()
            return r.json()

        last = await async_call_with_retry(
            "twilio_composition_status",
            one_poll,
            is_retryable=retryable_httpx_error,
        )
        st = str(last.get("status") or "").lower()
        if st == "completed":
            return last
        if st == "failed":
            raise RuntimeError(f"Twilio composition failed: {last.get('status')}")
        await asyncio.sleep(interval)
    raise TimeoutError(f"Twilio composition {composition_sid} not ready within {deadline}s")


async def _create_composition(client: httpx.AsyncClient, auth: tuple[str, str], room_sid: str) -> str:
    url = f"{_VIDEO_BASE}/Compositions"
    data = {"RoomSid": room_sid, "AudioSources": "*", "Format": "mp4"}

    async def one() -> dict[str, Any]:
        r = await client.post(url, auth=auth, data=data, timeout=httpx.Timeout(120.0))
        r.raise_for_status()
        return r.json()

    body = await async_call_with_retry(
        "twilio_composition_create",
        one,
        is_retryable=retryable_httpx_error,
    )
    sid = str(body.get("sid") or "")
    if not sid:
        raise RuntimeError("Twilio composition create returned no sid")
    return sid


async def _download_composition_media(client: httpx.AsyncClient, auth: tuple[str, str], composition_sid: str) -> Path:
    media_url = f"{_VIDEO_BASE}/Compositions/{composition_sid}/Media"
    data = await _download_bytes(client, media_url, auth=auth)
    fd, name = tempfile.mkstemp(prefix="cc_twilio_", suffix=".mp4")
    os.close(fd)
    path = Path(name)
    path.write_bytes(data)
    return path


async def resolve_mysql_call_media_file(
    *,
    room_name: str | None,
    metadata_raw: Any,
) -> Path:
    """Download or compose Twilio media, or HTTP-get Exotel recording → temp file."""
    meta = _parse_metadata(metadata_raw)
    media_url = str(meta.get("media_url") or "").strip()

    # Exotel direct MP3 (absolute URL on Exotel CDN)
    if "recordings.exotel.com" in media_url:
        if not media_url.startswith("http"):
            raise ValueError("Exotel media_url must be absolute https URL")
        return await _exotel_to_temp(media_url)

    rn = (room_name or "").strip()
    auth = _twilio_auth()

    # Twilio Video: ``room_name`` matches Twilio Room UniqueName.
    # Always create a **new** composition from room recordings (ignore ``composition_sid`` in DB metadata).
    if rn:
        if auth is None:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required for Twilio calls")
        async with httpx.AsyncClient() as client:
            best = await _pick_best_room_sid(client, auth, rn)
            if not best:
                raise RuntimeError(f"No Twilio room with audio recordings for UniqueName={rn!r}")

            new_sid = await _create_composition(client, auth, best)
            await _poll_composition_ready(client, auth, new_sid)
            return await _download_composition_media(client, auth, new_sid)

    raise ValueError("Cannot resolve mysql call media (need recordings.exotel.com URL or Twilio room_name)")
