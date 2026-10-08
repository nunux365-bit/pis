"""Telephony ingest: Exotel URL wins over Twilio room_name."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.compliance_call.adapters.telephony_ingest_media import resolve_mysql_call_media_file


@pytest.mark.asyncio
async def test_exotel_url_used_when_present_even_with_room_name(tmp_path: Path):
    exotel_path = tmp_path / "exotel.mp3"
    exotel_path.write_bytes(b"mp3")

    meta = {
        "media_url": "https://recordings.exotel.com/exotelrecordings/abc.mp3",
        "composition_sid": "ignored",
    }

    with (
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._exotel_to_temp",
            new_callable=AsyncMock,
            return_value=exotel_path,
        ) as mock_exotel,
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._pick_best_room_sid",
            new_callable=AsyncMock,
        ) as mock_twilio,
    ):
        got = await resolve_mysql_call_media_file(
            room_name="39be13d6-684f-48de-83c9-e83b68ffa386",
            metadata_raw=meta,
        )

    assert got == exotel_path
    mock_exotel.assert_awaited_once()
    mock_twilio.assert_not_awaited()


@pytest.mark.asyncio
async def test_twilio_used_when_no_exotel_url(tmp_path: Path):
    twilio_path = tmp_path / "twilio.mp4"
    twilio_path.write_bytes(b"mp4")

    async def _fake_pick(client, auth, rn):
        return "RM123"

    async def _fake_create(client, auth, room_sid):
        return "CJ456"

    async def _fake_poll(client, auth, sid):
        return None

    async def _fake_download(client, auth, sid):
        return twilio_path

    with (
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._twilio_auth",
            return_value=("sid", "tok"),
        ),
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._pick_best_room_sid",
            side_effect=_fake_pick,
        ),
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._create_composition",
            side_effect=_fake_create,
        ),
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._poll_composition_ready",
            side_effect=_fake_poll,
        ),
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._download_composition_media",
            side_effect=_fake_download,
        ),
        patch(
            "app.agents.compliance_call.adapters.telephony_ingest_media._exotel_to_temp",
            new_callable=AsyncMock,
        ) as mock_exotel,
    ):
        got = await resolve_mysql_call_media_file(
            room_name="room-uuid",
            metadata_raw={"media_url": "https://other-cdn.example.com/rec.mp3"},
        )

    assert got == twilio_path
    mock_exotel.assert_not_awaited()
