# SBI MIS S3 Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily 9 AM cron job that ingests the latest file from each of 3 S3 folders into the SBI MIS pipeline, replacing manual uploads.

**Architecture:** A thin boto3 wrapper (`s3_client.py`) handles raw S3 I/O. An orchestrator (`s3_ingest.py`) picks the latest file per folder, downloads it with a `.xlsx` extension, and registers it via the same DB functions the manual upload uses. A job wrapper (`jobs/sbi_mis_s3_ingest.py`) is registered in APScheduler under the existing `sbi_mis_enabled` gate.

**Tech Stack:** Python 3.12, boto3, FastAPI/SQLAlchemy async, APScheduler 3.x, existing `app/sbi_mis/db.py` + `state.py`.

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `pyproject.toml` | Add boto3 dependency |
| Modify | `app/config/settings.py` | Add `sbi_mis_s3_bucket`, `sbi_mis_s3_region` |
| **Create** | `app/sbi_mis/s3_client.py` | Thin boto3 wrapper: check_access, list_folder, download_file |
| **Create** | `app/sbi_mis/s3_ingest.py` | Orchestrator: pick latest, download, register in DB, trigger pipeline |
| **Create** | `jobs/sbi_mis_s3_ingest.py` | APScheduler job wrapper |
| Modify | `app/kernel/scheduler.py` | Register cron job under sbi_mis_enabled |
| **Create** | `tests/test_sbi_mis_s3_ingest.py` | Unit tests for pure functions |

---

## Task 1: Add boto3 dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add boto3 to pyproject.toml**

In the `dependencies = [` list (around line 15), add after the `openai` line:

```toml
"boto3>=1.35,<2",
```

- [ ] **Step 2: Install**

```bash
cd agentos-backend
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected: boto3 installs cleanly. Verify with `python -c "import boto3; print(boto3.__version__)"`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add boto3 dependency for SBI MIS S3 ingestion"
```

---

## Task 2: Add S3 settings

**Files:**
- Modify: `app/config/settings.py`

- [ ] **Step 1: Add two settings to the sbi_mis block**

Locate the `sbi_mis_enabled` and `sbi_mis_data_dir` lines in `settings.py`. Add two new fields immediately after `sbi_mis_data_dir`:

```python
sbi_mis_s3_bucket: str = "sbi-mis"
sbi_mis_s3_region: str = "ap-south-1"
```

- [ ] **Step 2: Verify settings load**

```bash
python -c "from app.config.settings import settings; print(settings.sbi_mis_s3_bucket, settings.sbi_mis_s3_region)"
```

Expected: `sbi-mis ap-south-1`

- [ ] **Step 3: Commit**

```bash
git add app/config/settings.py
git commit -m "feat(sbi-mis): add S3 bucket and region settings"
```

---

## Task 3: Create s3_client.py

**Files:**
- Create: `app/sbi_mis/s3_client.py`

- [ ] **Step 1: Create the file**

```python
"""Thin boto3 wrapper for SBI MIS S3 access.

All functions take explicit bucket/region args so they are easy to test
with a different bucket without touching settings.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# S3 object shape returned by list_folder:
# { "key": str, "size": int, "last_modified": datetime, "etag": str }


def _client(region: str):
    import boto3
    return boto3.client("s3", region_name=region)


def check_access(bucket: str, region: str) -> bool:
    """Return True if the bucket is reachable with current credentials."""
    try:
        _client(region).head_bucket(Bucket=bucket)
        return True
    except Exception as e:
        log.warning("SBI MIS S3: bucket %s not reachable — %s", bucket, e)
        return False


def list_folder(bucket: str, prefix: str, region: str) -> list[dict[str, Any]]:
    """List all objects under prefix. Returns list of dicts with key/size/last_modified/etag.

    Skips folder placeholder objects (keys ending with '/').
    Handles pagination automatically.
    """
    client = _client(region)
    paginator = client.get_paginator("list_objects_v2")
    objects: list[dict[str, Any]] = []
    folder_prefix = prefix.rstrip("/") + "/"
    for page in paginator.paginate(Bucket=bucket, Prefix=folder_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == folder_prefix or key.endswith("/"):
                continue
            objects.append({
                "key": key,
                "size": obj["Size"],
                "last_modified": obj["LastModified"],
                "etag": obj["ETag"].strip('"'),
            })
    return objects


def download_file(bucket: str, key: str, dest: Path, region: str) -> None:
    """Stream an S3 object to dest. Creates parent directories if needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _client(region).download_file(bucket, key, str(dest))
    log.info("SBI MIS S3: downloaded s3://%s/%s → %s", bucket, key, dest)
```

- [ ] **Step 2: Verify import**

```bash
python -c "from app.sbi_mis.s3_client import check_access, list_folder, download_file; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add app/sbi_mis/s3_client.py
git commit -m "feat(sbi-mis): add s3_client boto3 wrapper"
```

---

## Task 4: Write tests then create s3_ingest.py

**Files:**
- Create: `tests/test_sbi_mis_s3_ingest.py`
- Create: `app/sbi_mis/s3_ingest.py`

The two pure functions `_extract_month` and `_pick_latest` require no S3 connection and are fully unit-testable.

- [ ] **Step 1: Write failing tests**

Create `tests/test_sbi_mis_s3_ingest.py`:

```python
"""Unit tests for pure helper functions in s3_ingest — no S3 connection needed."""

from datetime import datetime, timezone

import pytest


def test_extract_month_plain_date():
    from app.sbi_mis.s3_ingest import _extract_month
    assert _extract_month("2026_06_03") == "2026-06"


def test_extract_month_embedded_in_longer_name():
    from app.sbi_mis.s3_ingest import _extract_month
    assert _extract_month("dump_2026_05_31_v2.xlsx") == "2026-05"


def test_extract_month_fallback_uses_current_month(monkeypatch):
    from app.sbi_mis import s3_ingest
    fake_today = datetime(2026, 6, 3, tzinfo=timezone.utc).date()

    class _FakeDate:
        @staticmethod
        def today():
            return fake_today

    monkeypatch.setattr(s3_ingest, "_date", _FakeDate)
    assert s3_ingest._extract_month("no_date_here") == "2026-06"


def test_pick_latest_returns_newest():
    from app.sbi_mis.s3_ingest import _pick_latest
    objs = [
        {"key": "f/2026_06_01", "last_modified": datetime(2026, 6, 1, tzinfo=timezone.utc)},
        {"key": "f/2026_06_03", "last_modified": datetime(2026, 6, 3, tzinfo=timezone.utc)},
        {"key": "f/2026_06_02", "last_modified": datetime(2026, 6, 2, tzinfo=timezone.utc)},
    ]
    assert _pick_latest(objs)["key"] == "f/2026_06_03"


def test_pick_latest_single_item():
    from app.sbi_mis.s3_ingest import _pick_latest
    objs = [{"key": "f/2026_06_01", "last_modified": datetime(2026, 6, 1, tzinfo=timezone.utc)}]
    assert _pick_latest(objs)["key"] == "f/2026_06_01"


def test_pick_latest_empty():
    from app.sbi_mis.s3_ingest import _pick_latest
    assert _pick_latest([]) is None
```

- [ ] **Step 2: Run — confirm they fail**

```bash
pytest tests/test_sbi_mis_s3_ingest.py -v
```

Expected: `ImportError: cannot import name '_extract_month' from 'app.sbi_mis.s3_ingest'` (module doesn't exist yet).

- [ ] **Step 3: Create app/sbi_mis/s3_ingest.py**

```python
"""SBI MIS S3 ingestion orchestrator.

For each of the 3 S3 folders, picks the latest file by LastModified,
downloads it to the local upload directory with a .xlsx extension, then
registers it in the DB and triggers the pipeline — identical to what
the manual HTTP upload does.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Folder → pipeline kind mapping (matches sbi_run_files.kind values)
_FOLDERS: dict[str, str] = {
    "sbi_pharmacy_dump":           "raw",
    "sbi_ahc_dump":                "ahc",
    "sbi_wallet_utilization_dump": "pf_summary",
}

_MONTH_RE = re.compile(r"(\d{4})[_-](\d{2})[_-]\d{2}")


def _extract_month(filename: str) -> str:
    """Extract YYYY-MM from a filename like '2026_06_03'. Falls back to current month."""
    m = _MONTH_RE.search(filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    today = _date.today()
    log.warning("SBI MIS S3: could not parse month from filename '%s', using %s-%02d",
                filename, today.year, today.month)
    return f"{today.year}-{today.month:02d}"


def _pick_latest(objects: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the object with the most recent LastModified, or None if list is empty."""
    if not objects:
        return None
    return max(objects, key=lambda o: o["last_modified"])


async def _ingest_folder(
    folder: str,
    kind: str,
    bucket: str,
    region: str,
    session,
) -> None:
    """Download the latest file from one S3 folder and register it in the DB."""
    from app.sbi_mis import db as sbi_db, state as sbi_state
    from app.sbi_mis.s3_client import download_file, list_folder

    objects = list_folder(bucket, folder, region)
    latest = _pick_latest(objects)
    if latest is None:
        log.info("SBI MIS S3: folder '%s' is empty, skipping", folder)
        return

    s3_key = latest["key"]
    raw_name = s3_key.split("/")[-1]
    month = _extract_month(raw_name)
    filename = raw_name + ".xlsx"

    dest = sbi_db.upload_dir() / month / filename
    loop = asyncio.get_event_loop()

    log.info("SBI MIS S3: ingesting s3://%s/%s → %s (kind=%s, month=%s)",
             bucket, s3_key, dest, kind, month)

    await loop.run_in_executor(None, lambda: download_file(bucket, s3_key, dest, region))

    if kind == "raw":
        from app.sbi_mis.engine import ingest as _ingest
        import pandas as pd

        df = await loop.run_in_executor(None, lambda: _ingest.load_raw(dest))
        rc = len(df)
        await sbi_db.set_current_run(session, str(dest), month, rc)
        await sbi_db.upsert_run_file(session, month, "raw", str(dest),
                                      original_filename=filename, row_count=rc)
        raw_gmv = float(
            pd.to_numeric(df.get("gmv_mrp", pd.Series(dtype=float)),
                          errors="coerce").fillna(0).sum()
        )
        raw_oids = int(df.get("order_id", pd.Series(dtype=object)).dropna().nunique())
        await sbi_db.update_run_recon_metrics(session, month, raw_gmv, raw_oids)
        sbi_state._cache.dump_df = df
        sbi_state._cache.active_month = month
        sbi_state._cache.raw_file_path = dest
        await sbi_state.do_one_rerun()

    elif kind == "pf_summary":
        from app.sbi_mis.engine import pf_ingest as _pf_ingest

        year = int(month.split("-")[0])
        records, info = await loop.run_in_executor(
            None, lambda: _pf_ingest.parse_pf_summary(dest, default_year=year)
        )
        if records:
            await sbi_db.bulk_upsert_pf_historicals(session, records)
        await sbi_db.upsert_run_file(session, month, "pf_summary", str(dest),
                                      original_filename=filename,
                                      row_count=info.get("row_count"))
        await sbi_state.do_one_rerun()

    else:  # ahc
        await sbi_db.upsert_run_file(session, month, "ahc", str(dest),
                                      original_filename=filename)
        await sbi_state.do_one_rerun()

    log.info("SBI MIS S3: folder '%s' ingested successfully (month=%s)", folder, month)


async def run_s3_ingest(session) -> None:
    """Ingest latest file from each S3 folder. Errors in one folder don't abort others."""
    from app.config.settings import settings

    bucket = settings.sbi_mis_s3_bucket
    region = settings.sbi_mis_s3_region

    for folder, kind in _FOLDERS.items():
        try:
            await _ingest_folder(folder, kind, bucket, region, session)
        except Exception:
            log.exception("SBI MIS S3: failed to ingest folder '%s' (continuing)", folder)
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
pytest tests/test_sbi_mis_s3_ingest.py -v
```

Expected output:
```
PASSED tests/test_sbi_mis_s3_ingest.py::test_extract_month_plain_date
PASSED tests/test_sbi_mis_s3_ingest.py::test_extract_month_embedded_in_longer_name
PASSED tests/test_sbi_mis_s3_ingest.py::test_extract_month_fallback_uses_current_month
PASSED tests/test_sbi_mis_s3_ingest.py::test_pick_latest_returns_newest
PASSED tests/test_sbi_mis_s3_ingest.py::test_pick_latest_single_item
PASSED tests/test_sbi_mis_s3_ingest.py::test_pick_latest_empty
6 passed
```

- [ ] **Step 5: Commit**

```bash
git add app/sbi_mis/s3_ingest.py tests/test_sbi_mis_s3_ingest.py
git commit -m "feat(sbi-mis): add S3 ingestion orchestrator with tests"
```

---

## Task 5: Create the APScheduler job wrapper

**Files:**
- Create: `jobs/sbi_mis_s3_ingest.py`

- [ ] **Step 1: Create the file**

```python
"""Daily SBI MIS S3 ingestion job — runs at 09:00, gated on sbi_mis_enabled."""

import logging

log = logging.getLogger(__name__)


async def sbi_mis_s3_ingest_job() -> None:
    from app.db.session import AsyncSessionLocal
    from app.sbi_mis.s3_ingest import run_s3_ingest

    log.info("SBI MIS S3 ingest job: starting")
    try:
        async with AsyncSessionLocal() as session:
            await run_s3_ingest(session)
            await session.commit()
        log.info("SBI MIS S3 ingest job: done")
    except Exception:
        log.exception("SBI MIS S3 ingest job: unhandled error")
```

- [ ] **Step 2: Verify import**

```bash
python -c "from jobs.sbi_mis_s3_ingest import sbi_mis_s3_ingest_job; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add jobs/sbi_mis_s3_ingest.py
git commit -m "feat(sbi-mis): add APScheduler job wrapper for S3 ingest"
```

---

## Task 6: Register cron in scheduler

**Files:**
- Modify: `app/kernel/scheduler.py`

- [ ] **Step 1: Add the cron job under the sbi_mis_enabled gate**

In `_register_builtin_jobs()`, add this block at the end of the function, before the closing of the function body:

```python
    if settings.sbi_mis_enabled:
        from jobs.sbi_mis_s3_ingest import sbi_mis_s3_ingest_job

        scheduler.add_job(
            sbi_mis_s3_ingest_job,
            "cron",
            hour=9,
            minute=0,
            id="sbi_mis_s3_ingest",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
```

- [ ] **Step 2: Verify scheduler module loads cleanly**

```bash
python -c "from app.kernel.scheduler import scheduler; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add app/kernel/scheduler.py
git commit -m "feat(sbi-mis): register daily 9 AM S3 ingest cron job"
```

---

## Verification on staging

After deploying:

1. Confirm `SBI_MIS_ENABLED=true` is set in the staging `.env`
2. Check the job is registered:
   ```bash
   # Look for the job in APScheduler logs at startup
   grep "sbi_mis_s3_ingest" /var/log/agentos/app.log
   ```
3. To trigger manually without waiting for 9 AM, run from the project root on staging:
   ```bash
   .venv/bin/python -c "
   import asyncio
   from jobs.sbi_mis_s3_ingest import sbi_mis_s3_ingest_job
   asyncio.run(sbi_mis_s3_ingest_job())
   "
   ```
4. Check SBI MIS dashboard — the active month and row counts should update.
