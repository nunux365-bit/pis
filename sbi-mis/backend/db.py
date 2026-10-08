"""SQLite setup + rule seeding on first launch."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import format_spec


APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
UPLOAD_DIR = DATA_DIR / "uploads"


def path_for_storage(path: str | Path) -> str:
    """Persist file refs relative to DATA_DIR when possible so DB + data/ can move machines."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = Path(path).expanduser()
    try:
        return p.relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def resolve_stored_path(stored: Optional[str]) -> Optional[Path]:
    """Turn a DB path into a Path on this host (handles copied DBs with foreign absolute paths)."""
    if not stored or not str(stored).strip():
        return None
    raw = str(stored).strip()
    p = Path(raw)
    if p.is_absolute():
        if p.exists():
            return p
        guess = UPLOAD_DIR / p.name
        if guess.exists():
            return guess.resolve()
        return p
    return (DATA_DIR / p).resolve()


SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  sheet TEXT NOT NULL,
  column_letter TEXT NOT NULL,
  rule_type TEXT NOT NULL,
  config_json TEXT NOT NULL,
  notes TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(sheet, column_letter)
);

CREATE TABLE IF NOT EXISTS lookup_tables (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_run (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  raw_file_path TEXT,
  month TEXT,
  row_count INTEGER,
  uploaded_at TIMESTAMP
);

-- Multi-month archive: one row per uploaded month
CREATE TABLE IF NOT EXISTS runs (
  month TEXT PRIMARY KEY,
  raw_file_path TEXT NOT NULL,
  row_count INTEGER,
  uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-column number format overrides (applies to any sheet/column, including raw Dump).
CREATE TABLE IF NOT EXISTS column_formats (
  sheet TEXT NOT NULL,
  column_letter TEXT NOT NULL,
  number_format TEXT NOT NULL,
  PRIMARY KEY (sheet, column_letter)
);

-- Clients. v1 is single-tenant (SBI). Table exists so future work can be multi-client.
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

-- Per-PF, per-month historical totals (for the PF Summary monthly carry-over).
-- Populated each month by the pipeline, plus seeded with Jan/Feb from the base file.
CREATE TABLE IF NOT EXISTS pf_historicals (
  pf TEXT NOT NULL,
  month TEXT NOT NULL,             -- 'YYYY-MM'
  pharma_total REAL,               -- sum of co_pay_discount (PF Summary monthly col)
  ahc_total REAL,                  -- sum for AHC
  pf_type TEXT,
  wallet_limit REAL,
  PRIMARY KEY (pf, month)
);

-- Uploaded file registry per (month, kind). Up to 3 files per month:
--   'raw'         → Metabase dump / query result (the required one to run the pipeline)
--   'pf_summary'  → PF summary xlsx from prior workflow — ingested into pf_historicals
--   'ahc'         → AHC data file
CREATE TABLE IF NOT EXISTS run_files (
  month TEXT NOT NULL,
  kind TEXT NOT NULL,
  file_path TEXT NOT NULL,
  original_filename TEXT,
  uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  row_count INTEGER,
  PRIMARY KEY (month, kind)
);

-- Per-sheet column definitions. Source of truth for what columns exist on a pivot sheet
-- and in what order. Seeded from format_spec on first init; thereafter fully editable:
-- users can add custom columns with new letters or delete ones they don't need.
CREATE TABLE IF NOT EXISTS sheet_columns (
  id INTEGER PRIMARY KEY,
  sheet TEXT NOT NULL,
  position INTEGER NOT NULL,       -- 1-based display order
  col_letter TEXT NOT NULL,        -- 'A', 'B', 'O', etc.
  header TEXT NOT NULL,
  number_format TEXT,
  is_system INTEGER NOT NULL DEFAULT 0,   -- 1 = seeded from spec, 0 = user-added
  notes TEXT,
  UNIQUE(sheet, col_letter),
  UNIQUE(sheet, position)
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 30s busy-timeout so background-thread writes don't trip 'database is locked'
    # while the main request is reading. Combined with WAL mode (set once at init)
    # this lets reads + writes happen concurrently without contention.
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and seed rules on first launch."""
    with cursor() as cur:
        cur.executescript(SCHEMA)

        # Seed only if rules table is empty
        cur.execute("SELECT COUNT(*) FROM rules")
        count = cur.fetchone()[0]
        if count == 0:
            for r in format_spec.seed_rules():
                cur.execute(
                    "INSERT INTO rules (sheet, column_letter, rule_type, config_json, notes) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (r["sheet"], r["column_letter"], r["rule_type"], r["config_json"], r.get("notes", "")),
                )

        # Seed pf_wallet_limits lookup table from historical seed if available.
        cur.execute("SELECT COUNT(*) FROM lookup_tables WHERE name='pf_wallet_limits'")
        wallet_data: List[Dict[str, Any]] = []
        seed_path = Path(__file__).resolve().parent.parent / "data" / "pf_historicals_seed.json"
        if seed_path.exists():
            try:
                with open(seed_path) as f:
                    for rec in json.load(f):
                        pf = str(rec.get("pf") or "").strip()
                        w = rec.get("wallet")
                        if pf and w is not None:
                            wallet_data.append({"key": pf, "value": float(w)})
            except Exception:
                pass
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO lookup_tables (name, data_json) VALUES (?, ?)",
                ("pf_wallet_limits", json.dumps(wallet_data)),
            )
        else:
            # Lookup already exists but may be empty — backfill with seed wallets if empty.
            cur.execute("SELECT data_json FROM lookup_tables WHERE name='pf_wallet_limits'")
            row = cur.fetchone()
            current = []
            if row:
                try: current = json.loads(row[0] or "[]")
                except: current = []
            if not current and wallet_data:
                cur.execute(
                    "UPDATE lookup_tables SET data_json = ? WHERE name = 'pf_wallet_limits'",
                    (json.dumps(wallet_data),),
                )

        # Backfill: ensure runs table includes the current_run row (one-time migration)
        cur.execute("SELECT COUNT(*) FROM runs")
        if cur.fetchone()[0] == 0:
            cur.execute("SELECT raw_file_path, month, row_count, uploaded_at FROM current_run WHERE id = 1")
            cr = cur.fetchone()
            if cr and cr["month"]:
                cur.execute(
                    "INSERT OR IGNORE INTO runs (month, raw_file_path, row_count, uploaded_at) VALUES (?, ?, ?, ?)",
                    (cr["month"], cr["raw_file_path"], cr["row_count"], cr["uploaded_at"]),
                )

        # Prune orphan rules: columns the spec no longer considers editable.
        # Dump columns AH-AS are now raw (Metabase direct), so any pre-existing rules are dead.
        _orphan_dump_cols = ("AH", "AI", "AJ", "AK", "AL", "AM", "AN", "AO", "AP", "AQ", "AR", "AS")
        placeholders = ",".join(["?"] * len(_orphan_dump_cols))
        cur.execute(
            f"DELETE FROM rules WHERE sheet = 'Dump' AND column_letter IN ({placeholders})",
            _orphan_dump_cols,
        )

        # Migrate Order level sheets: they're now pivots, not filter+map. Wipe old data-col rules
        # so the pivot defaults (pivot_group_key/pivot_first/pivot_agg/counter) can seed below.
        # Keep the __filter__ row since that's user-editable.
        for ol_sheet in ("Order level ", "Order level - non permissible"):
            # Delete rules whose rule_type is no longer compatible with the pivot model
            cur.execute(
                """DELETE FROM rules
                   WHERE sheet = ?
                     AND column_letter != '__filter__'
                     AND rule_type IN ('direct', 'formula')""",
                (ol_sheet,),
            )

        # Reseed any rule slot that's missing — guarantees every (sheet, column_letter)
        # defined in format_spec exists as a row in the DB, so everything is editable via UI.
        for r in format_spec.seed_rules():
            cur.execute(
                "INSERT OR IGNORE INTO rules (sheet, column_letter, rule_type, config_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (r["sheet"], r["column_letter"], r["rule_type"], r["config_json"], r.get("notes", "")),
            )

        # Seed clients table if empty.
        cur.execute("SELECT COUNT(*) FROM clients")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO clients (code, display_name, active) VALUES (?, ?, 1)",
                        ("SBI", "SBI (Pharmacy + AHC)"))

        # Backfill run_files from the legacy `runs` table so earlier uploads surface in the
        # new 3-slot UI. One-time migration; subsequent uploads populate both tables.
        cur.execute("SELECT COUNT(*) FROM run_files")
        if cur.fetchone()[0] == 0:
            cur.execute("SELECT month, raw_file_path, row_count, uploaded_at FROM runs")
            for row in cur.fetchall():
                if not row["raw_file_path"]: continue
                import os as _os
                fname = _os.path.basename(row["raw_file_path"]) if row["raw_file_path"] else None
                cur.execute(
                    "INSERT OR IGNORE INTO run_files (month, kind, file_path, original_filename, row_count, uploaded_at) "
                    "VALUES (?, 'raw', ?, ?, ?, ?)",
                    (row["month"], row["raw_file_path"], fname, row["row_count"], row["uploaded_at"]),
                )

        # Seed sheet_columns from format_spec on first init.
        # This is the point the column-editor takes over from the static spec.
        cur.execute("SELECT COUNT(*) FROM sheet_columns")
        if cur.fetchone()[0] == 0:
            for spec in format_spec.OUTPUT_SHEETS:
                for pos, c in enumerate(spec.columns, start=1):
                    cur.execute(
                        "INSERT OR IGNORE INTO sheet_columns "
                        "(sheet, position, col_letter, header, number_format, is_system) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        (spec.name, pos, c.col, c.header, c.number_format),
                    )

        # Seed PF historicals on first init from a JSON file if present.
        cur.execute("SELECT COUNT(*) FROM pf_historicals")
        if cur.fetchone()[0] == 0:
            seed_path = Path(__file__).resolve().parent.parent / "data" / "pf_historicals_seed.json"
            if seed_path.exists():
                try:
                    with open(seed_path) as f:
                        records = json.load(f)
                    for rec in records:
                        pf = str(rec.get("pf") or "").strip()
                        if not pf: continue
                        ptype = rec.get("type"); wallet = rec.get("wallet")
                        for m, v, ahc in (("2026-01", rec.get("jan"), rec.get("jan_ahc")),
                                           ("2026-02", rec.get("feb"), rec.get("feb_ahc"))):
                            if v is None and ahc is None: continue
                            cur.execute(
                                "INSERT OR IGNORE INTO pf_historicals (pf, month, pharma_total, ahc_total, pf_type, wallet_limit) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (pf, m, v, ahc, ptype, wallet),
                            )
                except Exception as e:
                    # Non-fatal: platform still works without historicals, columns just stay blank
                    print(f"[db] pf_historicals seed skipped: {e}")


# -------- Rule CRUD -------- #

def get_all_rules() -> List[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT * FROM rules ORDER BY sheet, column_letter")
        return [dict(r) for r in cur.fetchall()]


def get_rule(sheet: str, col: str) -> Optional[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM rules WHERE sheet = ? AND column_letter = ?",
            (sheet, col),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_rule(sheet: str, col: str, rule_type: str, config: Dict[str, Any], status: Optional[str] = None) -> Dict[str, Any]:
    with cursor() as cur:
        if status is not None:
            cur.execute(
                "UPDATE rules SET rule_type = ?, config_json = ?, status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE sheet = ? AND column_letter = ?",
                (rule_type, json.dumps(config), status, sheet, col),
            )
        else:
            cur.execute(
                "UPDATE rules SET rule_type = ?, config_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE sheet = ? AND column_letter = ?",
                (rule_type, json.dumps(config), sheet, col),
            )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO rules (sheet, column_letter, rule_type, config_json) VALUES (?, ?, ?, ?)",
                (sheet, col, rule_type, json.dumps(config)),
            )
        cur.execute("SELECT * FROM rules WHERE sheet = ? AND column_letter = ?", (sheet, col))
        return dict(cur.fetchone())


# -------- Lookup tables -------- #

def get_lookup(name: str) -> List[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT data_json FROM lookup_tables WHERE name = ?", (name,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else []


def set_lookup(name: str, data: List[Dict[str, Any]]) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO lookup_tables (name, data_json) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET data_json = excluded.data_json",
            (name, json.dumps(data)),
        )


def list_lookups() -> List[str]:
    with cursor() as cur:
        cur.execute("SELECT name FROM lookup_tables ORDER BY name")
        return [r[0] for r in cur.fetchall()]


# -------- Current run state -------- #

def set_current_run(raw_file_path: str, month: str, row_count: int) -> None:
    """Legacy: keep updating current_run for backwards compat AND upsert into runs."""
    stored = path_for_storage(raw_file_path)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO current_run (id, raw_file_path, month, row_count, uploaded_at) "
            "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET "
            "raw_file_path = excluded.raw_file_path, month = excluded.month, "
            "row_count = excluded.row_count, uploaded_at = excluded.uploaded_at",
            (stored, month, row_count),
        )
        cur.execute(
            "INSERT INTO runs (month, raw_file_path, row_count, uploaded_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(month) DO UPDATE SET "
            "raw_file_path = excluded.raw_file_path, "
            "row_count = excluded.row_count, uploaded_at = excluded.uploaded_at",
            (month, stored, row_count),
        )


def get_current_run() -> Optional[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT * FROM current_run WHERE id = 1")
        row = cur.fetchone()
        return dict(row) if row else None


def list_runs() -> List[Dict[str, Any]]:
    """All uploaded months, newest first."""
    with cursor() as cur:
        cur.execute("SELECT month, raw_file_path, row_count, uploaded_at FROM runs ORDER BY month DESC")
        return [dict(r) for r in cur.fetchall()]


def get_run(month: str) -> Optional[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE month = ?", (month,))
        row = cur.fetchone()
        return dict(row) if row else None


# -------- Column format overrides -------- #

def get_column_formats() -> Dict[str, Dict[str, str]]:
    """Return {sheet: {column_letter: number_format}}"""
    out: Dict[str, Dict[str, str]] = {}
    with cursor() as cur:
        cur.execute("SELECT sheet, column_letter, number_format FROM column_formats")
        for row in cur.fetchall():
            out.setdefault(row["sheet"], {})[row["column_letter"]] = row["number_format"]
    return out


def set_column_format(sheet: str, col: str, number_format: Optional[str]) -> None:
    """Upsert a column format. Passing None or '' deletes the override (falls back to spec default)."""
    with cursor() as cur:
        if not number_format:
            cur.execute("DELETE FROM column_formats WHERE sheet = ? AND column_letter = ?", (sheet, col))
            return
        cur.execute(
            "INSERT INTO column_formats (sheet, column_letter, number_format) VALUES (?, ?, ?) "
            "ON CONFLICT(sheet, column_letter) DO UPDATE SET number_format = excluded.number_format",
            (sheet, col, number_format),
        )


# -------- Clients -------- #

def list_clients() -> List[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute("SELECT id, code, display_name, active FROM clients ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


# -------- PF historicals -------- #

def pf_historicals_for_months(months: List[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return {pf: {month: {pharma_total, ahc_total, pf_type, wallet_limit}}}"""
    if not months:
        return {}
    placeholders = ",".join(["?"] * len(months))
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    with cursor() as cur:
        cur.execute(
            f"SELECT pf, month, pharma_total, ahc_total, pf_type, wallet_limit "
            f"FROM pf_historicals WHERE month IN ({placeholders})",
            months,
        )
        for row in cur.fetchall():
            out.setdefault(str(row["pf"]), {})[row["month"]] = dict(row)
    return out


def upsert_pf_historical(pf: str, month: str, pharma_total: Optional[float],
                         ahc_total: Optional[float] = None,
                         pf_type: Optional[str] = None,
                         wallet_limit: Optional[float] = None) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO pf_historicals (pf, month, pharma_total, ahc_total, pf_type, wallet_limit) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pf, month) DO UPDATE SET "
            "pharma_total = excluded.pharma_total, ahc_total = excluded.ahc_total, "
            "pf_type = COALESCE(excluded.pf_type, pf_historicals.pf_type), "
            "wallet_limit = COALESCE(excluded.wallet_limit, pf_historicals.wallet_limit)",
            (str(pf), month, pharma_total, ahc_total, pf_type, wallet_limit),
        )


def bulk_upsert_pf_historicals(records: List[Dict[str, Any]]) -> None:
    if not records: return
    with cursor() as cur:
        for r in records:
            cur.execute(
                "INSERT INTO pf_historicals (pf, month, pharma_total, ahc_total, pf_type, wallet_limit) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(pf, month) DO UPDATE SET "
                "pharma_total = excluded.pharma_total, ahc_total = excluded.ahc_total, "
                "pf_type = COALESCE(excluded.pf_type, pf_historicals.pf_type), "
                "wallet_limit = COALESCE(excluded.wallet_limit, pf_historicals.wallet_limit)",
                (str(r["pf"]), r["month"], r.get("pharma_total"), r.get("ahc_total"),
                 r.get("pf_type"), r.get("wallet_limit")),
            )


def month_minus(month: str, n: int) -> str:
    """'2026-03' minus 2 months → '2026-01'."""
    y, m = month.split("-")
    y, m = int(y), int(m)
    m -= n
    while m <= 0:
        m += 12; y -= 1
    return f"{y:04d}-{m:02d}"


# -------- Run files (multiple kinds per month) -------- #

def upsert_run_file(month: str, kind: str, file_path: str,
                    original_filename: Optional[str] = None,
                    row_count: Optional[int] = None) -> None:
    stored = path_for_storage(file_path)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO run_files (month, kind, file_path, original_filename, row_count, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(month, kind) DO UPDATE SET "
            "file_path = excluded.file_path, original_filename = excluded.original_filename, "
            "row_count = excluded.row_count, uploaded_at = excluded.uploaded_at",
            (month, kind, stored, original_filename, row_count),
        )


def get_run_file(month: str, kind: str) -> Optional[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            "SELECT month, kind, file_path, original_filename, row_count, uploaded_at "
            "FROM run_files WHERE month = ? AND kind = ?",
            (month, kind),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def run_files_for_month(month: str) -> Dict[str, Dict[str, Any]]:
    """Return {kind: {file_path, original_filename, row_count, uploaded_at}}"""
    with cursor() as cur:
        cur.execute(
            "SELECT kind, file_path, original_filename, row_count, uploaded_at "
            "FROM run_files WHERE month = ? ORDER BY kind",
            (month,),
        )
        return {row["kind"]: dict(row) for row in cur.fetchall()}


def all_run_files() -> List[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            "SELECT month, kind, file_path, original_filename, row_count, uploaded_at "
            "FROM run_files ORDER BY month DESC, kind"
        )
        return [dict(r) for r in cur.fetchall()]


# -------- PF historicals listing / admin -------- #

def pf_historicals_list(month_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """All pf_historicals rows (for display). Optionally filtered by month."""
    q = "SELECT pf, month, pharma_total, ahc_total, pf_type, wallet_limit FROM pf_historicals"
    params: Tuple = ()
    if month_filter:
        q += " WHERE month = ?"; params = (month_filter,)
    q += " ORDER BY month DESC, pf"
    with cursor() as cur:
        cur.execute(q, params)
        return [dict(r) for r in cur.fetchall()]


def pf_historicals_months() -> List[str]:
    with cursor() as cur:
        cur.execute("SELECT DISTINCT month FROM pf_historicals ORDER BY month")
        return [r["month"] for r in cur.fetchall()]


def pf_historicals_delete_month(month: str) -> int:
    with cursor() as cur:
        cur.execute("DELETE FROM pf_historicals WHERE month = ?", (month,))
        return cur.rowcount


# -------- Sheet columns (editable pivot layouts) -------- #

def sheet_columns_for(sheet: str) -> List[Dict[str, Any]]:
    with cursor() as cur:
        cur.execute(
            "SELECT id, sheet, position, col_letter, header, number_format, is_system, notes "
            "FROM sheet_columns WHERE sheet = ? ORDER BY position",
            (sheet,),
        )
        return [dict(r) for r in cur.fetchall()]


def next_col_letter(sheet: str) -> str:
    """Return the next auto-assigned column letter for a sheet (first unused A..ZZ)."""
    existing = set(r["col_letter"] for r in sheet_columns_for(sheet))
    import string
    for a in string.ascii_uppercase:
        if a not in existing: return a
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            cand = a + b
            if cand not in existing: return cand
    raise RuntimeError("No free column letter")


def add_sheet_column(sheet: str, header: str,
                     col_letter: Optional[str] = None,
                     number_format: Optional[str] = None,
                     notes: Optional[str] = None,
                     position: Optional[int] = None) -> Dict[str, Any]:
    cols = sheet_columns_for(sheet)
    existing_letters = {c["col_letter"] for c in cols}
    if col_letter:
        col_letter = col_letter.upper()
        if col_letter in existing_letters:
            raise ValueError(f"Column {col_letter} already exists on {sheet}")
    else:
        col_letter = next_col_letter(sheet)
    if position is None:
        position = (max((c["position"] for c in cols), default=0) + 1)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO sheet_columns (sheet, position, col_letter, header, number_format, is_system, notes) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (sheet, position, col_letter, header, number_format, notes),
        )
        new_id = cur.lastrowid
        cur.execute("SELECT * FROM sheet_columns WHERE id = ?", (new_id,))
        return dict(cur.fetchone())


def delete_sheet_column(sheet: str, col_letter: str) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM sheet_columns WHERE sheet = ? AND col_letter = ?", (sheet, col_letter))
        deleted = cur.rowcount > 0
        if deleted:
            # Also drop the rule for this column
            cur.execute("DELETE FROM rules WHERE sheet = ? AND column_letter = ?", (sheet, col_letter))
            # And any column format override
            cur.execute("DELETE FROM column_formats WHERE sheet = ? AND column_letter = ?", (sheet, col_letter))
            # Renumber positions so they stay contiguous
            rows = sheet_columns_for(sheet)
            for i, r in enumerate(rows, start=1):
                if r["position"] != i:
                    cur.execute("UPDATE sheet_columns SET position = ? WHERE id = ?", (i, r["id"]))
        return deleted


def update_sheet_column(sheet: str, col_letter: str,
                        header: Optional[str] = None,
                        number_format: Optional[str] = None) -> Optional[Dict[str, Any]]:
    sets, params = [], []
    if header is not None:
        sets.append("header = ?"); params.append(header)
    if number_format is not None:
        sets.append("number_format = ?"); params.append(number_format)
    if not sets:
        with cursor() as cur:
            cur.execute("SELECT * FROM sheet_columns WHERE sheet = ? AND col_letter = ?", (sheet, col_letter))
            r = cur.fetchone(); return dict(r) if r else None
    params.extend([sheet, col_letter])
    with cursor() as cur:
        cur.execute(f"UPDATE sheet_columns SET {', '.join(sets)} WHERE sheet = ? AND col_letter = ?", params)
        cur.execute("SELECT * FROM sheet_columns WHERE sheet = ? AND col_letter = ?", (sheet, col_letter))
        r = cur.fetchone(); return dict(r) if r else None
