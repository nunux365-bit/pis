from __future__ import annotations

import hashlib


def make_ingest_fingerprint(*, drive_file_id: str, revision: str) -> str:
    raw = f"{drive_file_id}:{revision}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make_ingest_fingerprint_mysql(*, mysql_call_id: int) -> str:
    raw = f"mysql:{mysql_call_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make_ingest_fingerprint_mysql_conversation(*, conversation_id: int) -> str:
    raw = f"mysql:conv:{conversation_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
