"""Unified batch item for compliance_call — Google Drive or MySQL-sourced calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.compliance_call.adapters.input_gdrive import WorkItem

MediaMode = Literal["file", "stream"]


@dataclass
class ComplianceBatchItem:
    source: Literal["gdrive", "mysql_call"]
    drive_file_id: str = ""
    revision: str = ""
    filename: str = ""
    mime: str = ""
    relative_path: str = ""
    media_mode: MediaMode = "file"
    mysql_call_id: int | None = None
    mysql_second_opinion_id: int | None = None
    mysql_second_opinion_conversation_id: int | None = None
    mysql_room_name: str | None = None
    mysql_metadata: dict[str, Any] = field(default_factory=dict)
    mysql_provider_reference_id: str = ""
    mysql_doctor_id: int | None = None
    # Verbatim ``calls.updated_at`` from MySQL for Sheet call_timestamp (stored on workflow ``input_data``).
    mysql_call_updated_at: str | None = None
    # When set, ingest/transcribe merges all legs (ordered) for one conversation eval.
    mysql_call_legs: list[dict[str, Any]] = field(default_factory=list)
    doctor_slug: str = ""
    doctor_name: str = ""

    @classmethod
    def from_gdrive(cls, w: WorkItem, *, slug: str, dname: str) -> ComplianceBatchItem:
        return cls(
            source="gdrive",
            drive_file_id=w.drive_file_id,
            revision=w.revision,
            filename=w.filename,
            mime=w.mime,
            relative_path=w.relative_path,
            media_mode=w.media_mode,
            doctor_slug=slug,
            doctor_name=dname,
        )
