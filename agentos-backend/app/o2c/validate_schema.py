"""Validate LLM structured output against contract_ingestion_schema.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema
from jsonschema import Draft202012Validator

from app.config.settings import settings


def _schema_path() -> Path:
    p = (settings.o2c_contract_schema_path or "").strip()
    if p:
        return Path(p).expanduser().resolve()
    here = Path(__file__).resolve().parent / "data" / "contract_ingestion_schema.json"
    return here


@lru_cache(maxsize=8)
def _validator_for_path(path_str: str) -> Draft202012Validator:
    with open(path_str, encoding="utf-8") as f:
        schema = json.load(f)
    return Draft202012Validator(schema)


def validate_contract_json(data: dict) -> tuple[bool, list[str]]:
    """Returns (ok, error_messages)."""
    errs: list[str] = []
    try:
        path = _schema_path()
        v = _validator_for_path(str(path))
        for e in v.iter_errors(data):
            errs.append(f"{e.json_path}: {e.message}")
        return (len(errs) == 0, errs[:50])
    except Exception as ex:
        return False, [str(ex)]
