"""
test_upload.py – Unit tests for the /upload endpoint.

Validates the file-type guard and the happy path (CSV/Excel accepted).
File I/O is redirected to a temp directory via monkeypatching.
"""
import io
import os
import pytest


# ─── Invalid file type ────────────────────────────────────────────────────────

def test_txt_file_rejected(maker_client):
    files = {"file": ("report.txt", io.BytesIO(b"dummy"), "text/plain")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 400
    assert "only excel/csv files are allowed" in r.json()["detail"].lower()


def test_pdf_file_rejected(maker_client):
    files = {"file": ("report.pdf", io.BytesIO(b"%PDF"), "application/pdf")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 400


def test_json_file_rejected(maker_client):
    files = {"file": ("data.json", io.BytesIO(b"{}"), "application/json")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 400


# ─── Valid file types ─────────────────────────────────────────────────────────

def test_csv_file_accepted(maker_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.payroll.UPLOADS_DIR", str(tmp_path))
    csv_bytes = b"empCode,amount\nEMP001,5000\n"
    files = {"file": ("employees.csv", io.BytesIO(csv_bytes), "text/csv")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 200
    body = r.json()
    assert "file uploaded successfully" in body["message"].lower()
    assert body["file"].endswith("employees.csv")


def test_xlsx_file_accepted(maker_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.payroll.UPLOADS_DIR", str(tmp_path))
    # Minimal bytes — just needs a valid extension
    files = {"file": ("payroll.xlsx", io.BytesIO(b"PK\x03\x04"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 200


def test_xls_file_accepted(maker_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.payroll.UPLOADS_DIR", str(tmp_path))
    files = {"file": ("old_format.xls", io.BytesIO(b"\xd0\xcf\x11\xe0"), "application/vnd.ms-excel")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 200


# ─── Saved file integrity ─────────────────────────────────────────────────────

def test_csv_content_written_correctly(maker_client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.payroll.UPLOADS_DIR", str(tmp_path))
    csv_bytes = b"empCode,amount\nEMP001,5000\n"
    files = {"file": ("data.csv", io.BytesIO(csv_bytes), "text/csv")}
    r = maker_client.post("/api/workflow/upload", files=files)
    assert r.status_code == 200

    saved_name = r.json()["file"]
    saved_path = tmp_path / saved_name
    assert saved_path.exists()
    assert saved_path.read_bytes() == csv_bytes
