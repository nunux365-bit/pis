# SBI MIS Pipeline

> Module: `app/sbi_mis/` · API prefix: `/api/v1/sbi-mis` · Role guard: `PHARMA_MIS_OPERATOR`

The SBI MIS module ingests a monthly Metabase pharmacy dump and produces a reconciled Excel workbook with four derived sheets: enriched Dump, two Order Level pivots, a PF Summary, and a scalar Summary. All computation happens in-process (pandas, no Celery), results are cached in module-level memory, and the finished xlsx is pre-generated to disk so downloads are instant.

---

## Table of Contents

1. [Data Flow Overview](#1-data-flow-overview)
2. [Input Files](#2-input-files)
3. [Pipeline Stages](#3-pipeline-stages)
4. [Rule System](#4-rule-system)
5. [Partial Rerun / Trigger System](#5-partial-rerun--trigger-system)
6. [Caching Layers](#6-caching-layers)
7. [Database Schema](#7-database-schema)
8. [API Endpoints](#8-api-endpoints)
9. [Adding New Rules or Sheets](#9-adding-new-rules-or-sheets)

---

## 1. Data Flow Overview

```
Upload
  │
  ├─ kind=raw          ─► parse xlsx/csv ─► dump_df in memory
  ├─ kind=ahc          ─► store path in sbi_run_files
  ├─ kind=wallet_checker ► store path in sbi_run_files
  └─ kind=pf_summary   ─► parse → bulk-upsert sbi_pf_historicals
         │
         ▼
  do_one_rerun(trigger=<kind>)
         │
         ├─ _load_supplementary_files()   ← mtime-cached AHC + wallet_checker DFs
         │
         └─ pipeline.run(dump_df, skip_stages=..., ahc_df=..., wallet_checker_df=...)
                │
                ├─ Stage 1: Dump enrichment  → cols AT..AY (formulas, overrides)
                ├─ Stage 2: Order Level      → pivot by (group_id, order_id), permissible
                ├─ Stage 3: Order Level NP   → same pivot, non-permissible
                ├─ Stage 4: PF Summary       → pivot by CORPORATE_IDENTIFIER + historicals
                └─ Stage 5: Summary          → per-cell scalar formulas
                         │
                         ▼
               _cache.results  ──► _pregenerate_xlsx_background()
                                         │
                                         ▼
                                   outputs/sbi_mis_{month}.xlsx
```

The `pipeline.run()` call is dispatched to a thread-pool executor (pandas is not async-safe). All DB calls inside the pipeline use a synchronous SQLAlchemy engine (`db.get_all_rules_sync`, etc.) to avoid async-in-thread issues.

---

## 2. Input Files

| Kind | Format | Purpose |
|------|--------|---------|
| `raw` | xlsx / csv | Metabase query #3180 export. Cols A–AS come verbatim; AH–AS are raw Metabase columns added Apr 2026. Cols AT–AY are computed enrichment. |
| `ahc` | xlsx / csv | AHC order file. Col B = PF Number, col K = Order Status. Used to count delivered AHC orders per PF (× ₹600 = col I of PF Summary). If not separately uploaded, the pipeline falls back to an `AHC` sheet embedded in the raw xlsx. |
| `wallet_checker` | xlsx / csv | Metabase "Query result" export. Cols: `corporate_identifier`, `service_limit`. Overwrites col C (Wallet Balance) of PF Summary. |
| `pf_summary` | xlsx | Prior-month PF Summary. Parsed and stored in `sbi_pf_historicals` to populate historical columns D/E/F/G of the current month's PF Summary. |

All uploaded files are stored under `{SBI_MIS_DATA_DIR}/uploads/` and their paths recorded in `sbi_run_files`.

### Raw Dump Schema

The raw file must have exactly these 46 headers in row 1 (cols A–AS):

```
A  corporate_partner      N  delivered_month       AA upfront_discount
B  financial_bu           O  pack_form             AB gmv_list_price
C  bu                     P  item_size             AC coupon_discount
D  group_id               Q  CORPORATE_IDENTIFIER  AD shipping_charges
E  order_id               R  rejection_reasons     AE vas_charges
F  channel_gateway        S  cancel_reason         AF co_pay_discount
G  sku_id                 T  return_reason         AG net_payable_by_customer
H  sku_name               U  erx_generated         AH checker
I  sku_sub_category       V  mobile_number         AI group_ids
J  sku_sub_category_l2    W  tags                  AJ group_idss
K  order_placed_date      X  payment_method        AK return_group_ids
L  order_status           Y  payment_method_tag    AL order_id (dupe)
M  order_completed_date   Z  gmv_mrp               AM wallet_balance
                                                   AN upfront_checker
                                                   AO copay_checker
                                                   AP billing_flag
                                                   AQ copay_check
                                                   AR financial_bu (dupe)
                                                   AS bu (dupe)
```

---

## 3. Pipeline Stages

### Stage 1 — Dump Enrichment

Source: `dump_raw` (A–AS from upload). Output: same frame + computed cols AT–AY.

Enrichment columns are defined in `format_spec.DUMP_ENRICHMENT_COLS` with default formulas. Each can be overridden via a DB rule (rule_type `formula`, `override`, `blank`, etc.).

**Pass order matters:**
1. `override` rules on any column are applied first (so AH can be rewritten before formula cols read it).
2. Then formula/blank enrichment cols AT–AY are evaluated row-by-row.

| Col | Header | Default formula |
|-----|--------|-----------------|
| AT | Disc % | `=(AA+AC)/Z` |
| AU | Co_Pay % | `=AF/Z` |
| AV | Net_Pay% | `=1-AT-AU` |
| AW | Ratio | `=AU/AV` |
| AX | Next Step | Decision tree → Non-permissible / Non cashless / wallet exhausted / No issue |
| AY | Order classification | Group-level: if any line in the order is Non-permissible → whole order tagged Non-permissible |

The enriched Dump is cached at `_state._cache._dump_enriched` and reused by partial reruns that don't touch Dump rules.

### Stage 2 & 3 — Order Level Pivots

Two sheets with identical column structure, split by filter rule:
- **Order level** (trailing space in name) — permissible orders (`__filter__` rule typically filters on AY = "OK")
- **Order level - non permissible** — non-permissible orders

Each pivot groups by `(group_id, order_id)` and per column applies one of:

| Rule type | Behaviour |
|-----------|-----------|
| `pivot_group_key` | Group key component — first key = group_id (col D), second = order_id (col E) |
| `counter` | Sequential 1…N row number |
| `pivot_first` | First non-null value in the group for the source column |
| `pivot_agg` | Aggregation (sum/mean/count/min/max) of the source column across the group |
| `formula` | Evaluated in a second pass over the already-pivoted frame |
| `static` | Constant value |

Default columns A–N:

| Col | Header | Rule |
|-----|--------|------|
| A | S No | counter |
| B | group_id | pivot_group_key (key=0, source=D) |
| C | order_id | pivot_group_key (key=1, source=E) |
| D | USER_TYPE | pivot_first (source=A) |
| E | PF ID | pivot_first (source=Q) |
| F | Placed Date | pivot_first (source=K) |
| G | Completed Date | pivot_first (source=M) |
| H | Payment_method | pivot_first (source=F) |
| I | Next Step | pivot_first (source=AX) |
| J | GMV_MRP | pivot_agg sum (source=Z) |
| K | DISCOUNT | pivot_agg sum (source=AA) |
| L | GMV_LIST | pivot_agg sum (source=AB) |
| M | CO_PAY_DISCOUNT | pivot_agg sum (source=AF) |
| N | USER_PAID | pivot_agg sum (source=AG) |

Results cached at `_state._cache._ol_df` and `_state._cache._olnp_df`.

### Stage 4 — PF Summary

One row per unique `CORPORATE_IDENTIFIER` (col Q of Dump). Structure:

| Col | Header | Source |
|-----|--------|--------|
| A | PF | pivot_key from Dump.Q |
| B | Type | pivot_first from Dump.A (corporate_partner) |
| C | Wallet Balance | From wallet_checker file → `service_limit` per PF; falls back to `pf_historicals.wallet_limit` |
| D | Month−2 Pharma | `sbi_pf_historicals` for active_month − 2 |
| E | Month−2 AHC | same |
| F | Month−1 Pharma | `sbi_pf_historicals` for active_month − 1 |
| G | Month−1 AHC | same |
| H | Current Pharma | pivot_agg sum of Dump.AF per PF |
| I | Current AHC | Count of delivered AHC orders × ₹600 from AHC file |
| J | Grand Total | formula `=D+E+F+G+H+I` |
| K | Overutilised | formula `=IF(C="",0,MAX(0,J-C))` |

Column headers for D/E/F/G/H/I show actual month names (e.g. "Apr", "Apr AHC") based on `active_month` via `pf_summary_dynamic_spec()`.

**Historicals:** PFs that exist in prior months but not in the current Dump are appended as extra rows so the sheet remains cumulative. After a successful pf_summary computation, `bulk_upsert_pf_historicals_sync` writes the current month's totals back to `sbi_pf_historicals` for future runs.

Result cached at `_state._cache._pfs_df`.

### Stage 5 — Summary

Per-cell scalar values. Each cell is defined in `format_spec.SUMMARY_CELLS` and evaluated by `rules.eval_summary_cell()` which can reference any other sheet by name (e.g. `Order level .J`, `AHC.G`). Computed in definition order; already-computed cells are passed as context so later cells can reference earlier ones.

---

## 4. Rule System

Rules are stored in `sbi_rules` (PostgreSQL). Each rule targets `(sheet, column_letter)` and has a `rule_type` + `config_json`. The pipeline reads all rules once at the top of `pipeline.run()` via `_rules_by_sheet()`.

### Rule Types

| Type | Where used | Effect |
|------|-----------|--------|
| `formula` | Dump enrichment, Order Level, PF Summary | Evaluates an Excel-compatible formula per row. Supported functions: IF, AND, OR, SUM, SUMIF, SUMIFS, MAX, MIN, ABS, ROUND. Cross-sheet refs: `SheetName.Col`. |
| `override` | Dump | Rewrites a raw column value. Evaluated before formula enrichment. |
| `pivot_group_key` | Order Level | Identifies a component of the group key. |
| `pivot_first` | Order Level, PF Summary | First non-null group value. |
| `pivot_agg` | Order Level, PF Summary | Numeric aggregation over the group. |
| `pivot_key` | PF Summary | Column to pivot by (produces one row per unique value). |
| `lookup` | PF Summary | Map via a named `sbi_lookup_tables` entry. |
| `static` | any | Constant value for all rows. |
| `blank` | any | Leaves column null. |
| `counter` | Order Level | Sequential row number. |
| `group_any` | Dump.AY | True if ANY line in the (group_id, order_id) group matches a condition. |

### Lookup Tables

Named key→value mappings stored in `sbi_lookup_tables.data_json`. Used by `lookup` rules. Managed via `PUT /lookup-tables/{name}`.

### Adding a Formula Rule via NL

`POST /nl-to-formula` accepts plain English and uses GPT-4o-mini to generate an Excel-compatible formula. The formula is returned for review before saving.

---

## 5. Partial Rerun / Trigger System

Every trigger that causes a pipeline rerun carries a `trigger` string. `state.do_one_rerun(trigger=)` maps this to a `skip_stages` frozenset, and `pipeline.run(skip_stages=)` skips the corresponding stages if their intermediate cache is warm.

### Dependency Map (`_TRIGGER_SKIP`)

| Trigger | Stages skipped | Stages recomputed |
|---------|---------------|------------------|
| `"raw"` or `None` | none | all (full run) |
| `"rule:Dump"` | none | all |
| `"rule:Order level "` | dump, pf_summary | order_level + summary |
| `"rule:Order level - non permissible"` | dump, pf_summary | order_level_np + summary |
| `"rule:PF Summary"` | dump, order_level, order_level_np | pf_summary + summary |
| `"rule:Summary"` | dump, order_level, order_level_np, pf_summary | summary only |
| `"ahc"` | dump, order_level, order_level_np | pf_summary + summary |
| `"wallet_checker"` | dump, order_level, order_level_np | pf_summary + summary |
| `"pf_summary_file"` | dump, order_level, order_level_np | pf_summary + summary |
| `"historicals"` | dump, order_level, order_level_np | pf_summary + summary |

**Cold cache guard:** if a stage is in `skip_stages` but its intermediate (`_cache._dump_enriched`, `_cache._ol_df`, etc.) is `None`, the stage is computed anyway and the result stored. This ensures correctness on the first run after a server restart.

**Full-run reset:** when trigger is `None` or `"raw"`, all four intermediate caches are explicitly cleared before the run so stale data from a previous month can never be reused.

**Coalesced reruns:** if a second rerun is requested while the lock is held, `_pipeline_pending` is set. After the locked run completes, one additional full run (no trigger) drains the pending flag. This prevents a burst of rule edits from queuing many redundant runs.

---

## 6. Caching Layers

Three distinct cache tiers operate independently:

### In-memory pipeline cache (`state._cache`)

Module-level `_SbiCache` dataclass, one instance per process. Holds:
- `dump_df` — the raw+enriched Dump DataFrame (loaded once per upload, reused across runs)
- `results` — the full `{sheets, warnings}` output of the last successful `pipeline.run()`
- Intermediate results: `_dump_enriched`, `_ol_df`, `_olnp_df`, `_pfs_df` — reused by partial reruns

Lost on process restart; recovered by the disk cache (see below).

### Disk pipeline cache (`{data_dir}/pipeline_cache/`)

After every successful run, `state.save_pipeline_cache()` pickles `results + dump_df` to `{month}.pkl` alongside a fingerprint `{month}.fp`.

**Fingerprint** = SHA-256 of:
1. Raw file `st_mtime_ns + st_size` (fast stat, no file read)
2. AHC and wallet_checker file mtimes
3. All rules serialised as sorted JSON

On startup (`_restore_sbi_cache` in `main.py`), if the fingerprint matches, results are loaded from pickle instead of re-running the pipeline — making restarts instant.

### Supplementary file DataFrame cache (`_SbiCache` fields)

Before each `pipeline.run()`, `state._load_supplementary_files()` checks whether the AHC/wallet_checker files on disk have changed since the last load (via `st_mtime_ns`). If mtime is unchanged, the cached DataFrame is reused — no disk I/O, no file parse.

The AHC-in-raw-dump fallback (checking if the raw xlsx contains an embedded `AHC` sheet) is also cached per raw file path in `_cache.raw_has_ahc_sheet` / `_cache._raw_ahc_checked_for`, so the openpyxl `sheetnames` check only runs once per raw file.

### Output xlsx cache

`_pregenerate_xlsx_background()` writes the xlsx to `{data_dir}/outputs/sbi_mis_{month}.xlsx` immediately after each pipeline run (fire-and-forget task). The download endpoint serves this file directly via `FileResponse`. An in-memory bytes cache (`_output_cache`) also keeps the last-generated bytes for sub-millisecond re-serves.

---

## 7. Database Schema

All tables are prefixed `sbi_`. Migration: `033_add_sbi_mis_tables`.

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `sbi_rules` | `(sheet, column_letter)` unique | Rule type + config per output cell or column |
| `sbi_lookup_tables` | `name` unique | Named key→value maps for `lookup` rules |
| `sbi_current_run` | singleton (id=1) | Active month + raw file path |
| `sbi_runs` | `month` PK | Per-month upload metadata + recon metrics (raw GMV, unique order IDs) |
| `sbi_run_files` | `(month, kind)` PK | Paths for raw / ahc / wallet_checker / pf_summary files |
| `sbi_pf_historicals` | `(pf, month)` PK | Per-PF monthly totals (pharma, AHC, wallet_limit) for historical columns |
| `sbi_sheet_columns` | `(sheet, col_letter)` unique | User-added columns on Order Level sheets |
| `sbi_column_formats` | `(sheet, column_letter)` PK | openpyxl number_format overrides |
| `sbi_clients` | `code` unique | SBI client reference list |
| `sbi_jobs` | `id` UUID | Background job tracking (status, progress, result_json) |

### pf_historicals data flow

```
Upload pf_summary file
  → pf_ingest.parse_pf_summary()
  → bulk_upsert_pf_historicals()
  → sbi_pf_historicals (pf, month, pharma_total, ahc_total, wallet_limit)

Upload raw file with embedded PF Summary sheet
  → auto-ingested on upload (seeds wallet_limits + prior-month totals)

pipeline.run() (pf_summary stage)
  → reads sbi_pf_historicals for months (active−2) and (active−1)
  → populates cols D/E/F/G of PF Summary
  → after computation: writes current month's totals back via bulk_upsert_pf_historicals_sync
```

---

## 8. API Endpoints

All endpoints require `PHARMA_MIS_OPERATOR` role (or `SYSTEM_ADMIN`).

### Upload

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/upload` | Direct upload (multipart). `kind`: raw / ahc / wallet_checker / pf_summary. Returns `job_id`. |
| `POST` | `/upload/session` | Create a chunked upload session. Returns `upload_id`, `chunk_size`, `chunk_count`. |
| `PUT` | `/upload/chunk/{upload_id}/{chunk_index}` | Upload one chunk. |
| `POST` | `/upload/complete` | Finalise chunked upload. Returns `job_id`. |

### Pipeline & Runs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Active month, computing flag, last error, available months. |
| `GET` | `/runs` | List all months that have a raw upload. |
| `POST` | `/runs/{month}/activate` | Load a previously-uploaded raw file and re-run the pipeline. |
| `GET` | `/jobs/{job_id}` | Poll background job status (status, progress, stage, result_json). |
| `POST` | `/recon/run` | Start a reconciliation job comparing raw file metrics vs pipeline output. Returns `job_id`. |

### Output

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/download` | Download the pre-generated xlsx for the active month. |
| `GET` | `/preview/{sheet}` | Paginated preview of any output sheet (offset/limit). Summary sheet returns cell array. |
| `GET` | `/billing-summary` | Permissible vs non-permissible GMV and order counts. Memoised per run. |

### Rules & Lookups

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/rules` | All rules. |
| `GET` | `/rules/export` | Same as GET /rules — for bulk export. |
| `POST` | `/rules/import` | Bulk upsert from exported JSON array. |
| `GET/PUT` | `/rules/{sheet}/{col}` | Get or update a single rule. PUT triggers rerun. |
| `GET` | `/lookup-tables` | All lookup tables. |
| `PUT` | `/lookup-tables/{name}` | Replace a lookup table. Triggers rerun. |
| `POST` | `/nl-to-formula` | Natural language → Excel formula via GPT-4o-mini. |

### Sheet Columns & Formats

| Method | Path | Description |
|--------|------|-------------|
| `GET/POST` | `/sheet-columns/{sheet}` | List or add user-defined columns on Order Level sheets. |
| `PATCH/DELETE` | `/sheet-columns/{sheet}/{col}` | Update or remove a column. |
| `GET` | `/column-formats` | All number_format overrides. |
| `PUT` | `/column-formats/{sheet}/{col}` | Set a number_format override (no pipeline rerun needed). |

### Historicals & Clients

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/historicals/stats` | Fast: per-month PF counts (GROUP BY). |
| `GET` | `/historicals` | Slow: full row-level detail, optional `?month=` filter. |
| `DELETE` | `/historicals/{month}` | Delete a month's historicals and rerun. |
| `GET` | `/clients` | SBI client list. |

---

## 9. Adding New Rules or Sheets

### Add a new enrichment column to Dump

1. Append a new entry to `format_spec.DUMP_ENRICHMENT_COLS` in `format_spec.py` with the next column letter and a default rule.
2. The column will appear in the preview UI and be overridable via the rules editor.

### Add a new column to Order Level

1. Use `POST /sheet-columns/Order level ` (note trailing space) with `header`, `rule_type`, and `config`.
2. Or append an `OutputCol` to `format_spec.ORDER_LEVEL.columns` for a system-default column.

### Add a new trigger for a new sheet

1. Add an entry to `_TRIGGER_SKIP` in `state.py` mapping the new trigger string to the set of stages it does not affect.
2. Pass the trigger string from the relevant route handler.
3. Add a corresponding intermediate cache field to `_SbiCache` and store/reuse it in `pipeline.run()`.

### Extend pf_historicals columns

The `sbi_pf_historicals` table schema (`pf`, `month`, `pharma_total`, `ahc_total`, `pf_type`, `wallet_limit`) is read-only from the pipeline's perspective — add a migration column and update `_build_pf_summary` and `bulk_upsert_pf_historicals_sync` together.
