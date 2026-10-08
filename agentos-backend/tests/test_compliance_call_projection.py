"""Unit tests for compliance_call_runs projection from workflow_runs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.services.compliance_call_projection import compliance_call_run_from_workflow_run


def test_projection_source_file_id_prefers_explicit_and_maps_gdrive() -> None:
    wid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    wr = MagicMock()
    wr.id = wid
    wr.created_at = now
    wr.updated_at = now
    wr.status = "running"
    wr.input_data = {"source": "gdrive", "drive_file_id": "drive-1", "filename": "a.wav"}
    wr.output_data = {}
    wr.error_message = None
    wr.ingest_fingerprint = "fp1"
    d = compliance_call_run_from_workflow_run(wr)
    assert d["source_file_id"] == "drive-1"
    assert d["source_type"] == "drive"

    wr.input_data = {"source": "aws", "source_file_id": "s3-key-9", "filename": "b.wav"}
    d2 = compliance_call_run_from_workflow_run(wr)
    assert d2["source_file_id"] == "s3-key-9"
    assert d2["source_type"] == "aws"

    wr.input_data = {"source_type": "ozontel", "source_file_id": "call-42", "filename": "c.wav"}
    d3 = compliance_call_run_from_workflow_run(wr)
    assert d3["source_file_id"] == "call-42"
    assert d3["source_type"] == "ozontel"
