#!/usr/bin/env bash
#
# email_automation_local.sh — populate AR email-automation Data Viz + Reply Tracker
# locally by reading the automation.agents@1mg.com mailbox. Sends NO email.
#
# See scripts/EMAIL_AUTOMATION_LOCAL.md for the full explanation.
#
# Usage:
#   scripts/email_automation_local.sh [steps...] [options]
#
# Steps (default when none given: scan reconstruct classify verify):
#   truncate     Wipe the 4 email-automation tables (fresh start)
#   scan         Process receivables sheet + classify -> Data Viz + overdue rows
#   reconstruct  Rebuild 'sent' anchors from the mailbox (Reply Tracker prereq)
#   classify     Classify anchored threads -> Reply Tracker reply categories (OpenAI)
#   verify       Print row counts + category breakdown
#   all          truncate + scan + reconstruct + classify + verify
#
# Options:
#   --query "Q"        Gmail inbox query for scan
#                      (default: from:bhawna.gandhi@1mg.com subject:Receivable newer_than:30d)
#   --anchor-limit N   reconstruct cap; 0 = all threads (default: 0)
#   --max-threads N    classify cap (default: 100000 = effectively uncapped)
#   -h | --help        Show this help
#
# Examples:
#   scripts/email_automation_local.sh                 # populate everything (no truncate)
#   scripts/email_automation_local.sh all             # fresh: truncate + full populate
#   scripts/email_automation_local.sh scan verify     # just Data Viz
#   scripts/email_automation_local.sh classify --max-threads 200   # quick reply sample

set -euo pipefail

# ── Resolve backend root (this script lives in agentos-backend/scripts/) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BACKEND_ROOT"

PY="$BACKEND_ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: venv python not found at $PY"; exit 1; }
export PYTHONPATH="$BACKEND_ROOT"

# ── Defaults ──
QUERY='from:bhawna.gandhi@1mg.com subject:Receivable newer_than:30d'
ANCHOR_LIMIT=0
MAX_THREADS=100000
STEPS=()

# ── Parse args ──
while [ $# -gt 0 ]; do
  case "$1" in
    truncate|scan|reconstruct|classify|verify) STEPS+=("$1"); shift ;;
    all) STEPS=(truncate scan reconstruct classify verify); shift ;;
    --query) QUERY="$2"; shift 2 ;;
    --anchor-limit) ANCHOR_LIMIT="$2"; shift 2 ;;
    --max-threads) MAX_THREADS="$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^#//'; exit 0 ;;
    *) echo "Unknown argument: $1 (use -h for help)"; exit 1 ;;
  esac
done
# Default flow if no steps specified: populate without truncating.
[ ${#STEPS[@]} -eq 0 ] && STEPS=(scan reconstruct classify verify)

# ── Step implementations ──
step_truncate() {
  echo "==> TRUNCATE (email_automation_messages, _sends, gmail_intelligence, receivable_dashboard_snapshots)"
  "$PY" - <<'PY'
import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
T=["email_automation_messages","email_automation_sends","gmail_intelligence","receivable_dashboard_snapshots"]
async def m():
    async with AsyncSessionLocal() as db:
        await db.execute(text(f"TRUNCATE TABLE {', '.join(T)} RESTART IDENTITY CASCADE")); await db.commit()
        print("  truncated:", ", ".join(T))
asyncio.run(m())
PY
}

step_scan() {
  echo "==> SCAN (receivables + classification -> Data Viz snapshot). query: $QUERY"
  "$PY" scripts/run_email_automation_local.py --scan --query "$QUERY"
}

step_reconstruct() {
  echo "==> RECONSTRUCT sent anchors from mailbox (Gmail reads only, no send). limit=$ANCHOR_LIMIT"
  "$PY" scripts/reconstruct_send_anchors_local.py --limit "$ANCHOR_LIMIT"
}

step_classify() {
  echo "==> CLASSIFY anchored threads -> Reply Tracker categories (OpenAI). max-threads=$MAX_THREADS"
  echo "    (long-running; ^C is safe — re-run resumes via --classify-anchors-only-missing)"
  PYTHONUNBUFFERED=1 "$PY" scripts/backfill_collections_local.py \
    --classify-anchors --classify-anchors-only-missing \
    --force-classify --skip-alembic --max-anchor-threads "$MAX_THREADS"
}

step_verify() {
  echo "==> VERIFY"
  "$PY" - <<'PY'
import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
async def m():
    async with AsyncSessionLocal() as db:
        q=lambda s: db.execute(text(s))
        print("  snapshots        :", (await q("SELECT count(*) FROM receivable_dashboard_snapshots")).scalar())
        print("  sent anchors     :", (await q("SELECT count(*) FROM email_automation_sends WHERE status='sent'")).scalar())
        print("  reply intel rows :", (await q("SELECT count(*) FROM gmail_intelligence WHERE kind='collections_reply'")).scalar())
        for r in (await q("SELECT category,count(*) FROM gmail_intelligence WHERE kind='collections_reply' GROUP BY 1 ORDER BY 2 DESC")).all():
            print(f"     {r[0]}: {r[1]}")
asyncio.run(m())
PY
}

# ── Run requested steps in canonical order ──
echo "Backend: $BACKEND_ROOT"
for s in truncate scan reconstruct classify verify; do
  for want in "${STEPS[@]}"; do
    if [ "$s" = "$want" ]; then
      "step_$s"
      echo
    fi
  done
done
echo "Done. Open AR Automation -> Data Viz and -> Reply Tracker."
