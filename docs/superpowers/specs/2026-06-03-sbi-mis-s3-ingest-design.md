# SBI MIS — S3 Ingestion Design

**Date:** 2026-06-03
**Branch:** feat/sbi-mis-s3-ingest
**Status:** Approved

---

## Problem

The SBI MIS pipeline currently requires a human to manually upload 3 files (raw pharmacy dump, AHC file, PF/wallet summary) through the dashboard UI each month. The SBI team will now place these files in a shared S3 bucket; the server should ingest them automatically without manual intervention.

---

## S3 Layout

**Bucket:** `sbi-mis` (region: `ap-south-1`)

| S3 Folder | Pipeline kind |
|---|---|
| `sbi_pharmacy_dump` | `raw` |
| `sbi_ahc_dump` | `ahc` |
| `sbi_wallet_utilization_dump` | `pf_summary` |

Folder-to-kind mapping is a constant in code (`s3_ingest.py`) — not a setting.

---

## Credentials

The EC2 instance has an IAM role with S3 read access attached. boto3's default credential chain resolves this automatically via the instance metadata service. No explicit AWS keys are needed in `.env` or settings.

S3 access is only available from the staging EC2 server — not from local development machines.

---

## Config Changes (`settings.py` + `.env`)

Two new settings added to the `sbi_mis` block:

```
SBI_MIS_S3_BUCKET=sbi-mis
SBI_MIS_S3_REGION=ap-south-1
```

Both have sensible defaults so they can be omitted from `.env` and overridden only when needed.

---

## New Files

### `app/sbi_mis/s3_client.py`

Thin boto3 wrapper. Three functions:

- `check_access(bucket, region) -> bool` — calls `head_bucket`; returns True if reachable
- `list_folder(bucket, prefix, region) -> list[S3Object]` — lists all objects under a prefix, returns `[{key, size, last_modified, etag}]`
- `download_file(bucket, key, dest_path, region) -> None` — streams object to a local path

No business logic here — pure S3 I/O.

### `app/sbi_mis/s3_ingest.py`

Ingestion orchestrator. One public async function: `run_s3_ingest(session)`.

**Flow per folder:**
1. List objects in the S3 folder
2. Pick the object with the latest `LastModified`
3. Extract the month string from the filename via `_extract_month(filename) -> str`:
   - Tries regex patterns for `YYYY-MM` and `YYYY_MM` in the filename
   - Falls back to current calendar month and logs a warning
4. Download the file to `upload_dir() / month / filename` (overwrites if exists)
5. Call the same DB registration functions the manual upload uses:
   - `upsert_run_file(session, month, kind, path, original_filename, row_count)`
   - For `raw` kind additionally: `set_current_run(session, path, month, row_count)`, load DataFrame into `_cache`, trigger pipeline rerun
6. Log success or error per folder; a failure in one folder does not abort the others

`_extract_month` is a single isolated function — easy to update once real filenames are seen on staging.

---

## Scheduler Change (`app/kernel/scheduler.py`)

One new cron job added, gated behind `settings.sbi_mis_enabled`:

```python
scheduler.add_job(
    sbi_s3_ingest_job,
    "cron",
    hour=9,
    minute=0,
    id="sbi_mis_s3_ingest",
)
```

`sbi_s3_ingest_job` is a top-level async function in `s3_ingest.py` that opens a DB session and calls `run_s3_ingest(session)`.

---

## Test Script (`scripts/test_s3_access.py`)

Standalone script — no app imports, just boto3. Must be run from the **staging EC2 server** (where the IAM role is attached); it will not work locally since credentials are provided via the instance metadata service.

```bash
# on staging
python scripts/test_s3_access.py
```

Output:
1. Bucket reachability check (`head_bucket`)
2. For each of the 3 folders: list all objects with filename, size, last_modified, etag

Purpose: confirm EC2 IAM access works and reveal the real filename convention so `_extract_month` can be finalized.

---

## Dependency

`boto3` added to `pyproject.toml` under the main dependency list.

---

## What Is Not Changed

- Manual upload endpoints remain intact — S3 ingest is additive
- No new DB tables or migrations
- Pipeline execution path is identical to manual upload

---

## Filename Convention (confirmed on staging)

Files are named **`YYYY_MM_DD`** with no extension — one file per day, e.g. `2026_06_03`.

Month extraction regex: `^(\d{4})_(\d{2})_\d{2}$` → `"2026-06"`. No fallback needed.

Since S3 files have no extension, the ingester appends `.xlsx` when writing to the local upload directory so the pipeline's file-type validation continues to work.
