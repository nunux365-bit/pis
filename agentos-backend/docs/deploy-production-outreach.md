# Cold Outreach — Production Deployment Checklist

## 1. Database Migrations

Run before starting the server:

```bash
source .venv/bin/activate
alembic upgrade 031_cold_outreach_feature
```

Or simply run `alembic upgrade head` if the DB is starting fresh.

**What this migration does:**
- Creates the `outreach_leads` table (full schema)
- Creates the `outreach_replies` table (with `intent_level`, `next_action`, `key_signals`)
- Seeds the Cold Outreach Agent entry in `catalog_agents`

---

## 2. Google Workspace — One-Time Setup

These are manual steps that need to be done once by a Google Workspace admin.

- [ ] Share the CHW outreach Google Sheet with the service account as **Viewer + Editor**:
  ```
  service-automation@service-automation-491510.iam.gserviceaccount.com
  ```
- [ ] Add `corporatehealth@1mg.com` as a **"Send mail as"** alias in the `automation.agents@1mg.com` Gmail account settings so the `From:` header shows `corporatehealth@1mg.com` to recipients.

---

## 3. Environment Variables

Add these to your production `.env`:

```env
# ── Master switches ────────────────────────────────────────────
OUTREACH_ENABLED=true
OUTREACH_SYNC_ENABLED=true
OUTREACH_DISPATCH_ENABLED=true
OUTREACH_CLASSIFY_ENABLED=true
OUTREACH_SHEET_WRITEBACK_ENABLED=true

# ── Test mode OFF — emails go to real recipients ───────────────
OUTREACH_TEST_MODE=false

# ── Volume cap — set to desired batch size, 0 = unlimited ──────
OUTREACH_DAILY_SEND_LIMIT=0

# ── Sheet ──────────────────────────────────────────────────────
CHW_OUTREACH_GSHEET_ID=<production_sheet_id>
```

Variables NOT needed in production (leave unset or empty):
- `OUTREACH_TEST_REDIRECT_TO` — only used when test mode is on
- `OUTREACH_TEST_ALLOWED_CC` — only used when test mode is on

---

## 4. Initial Data Sync (run before first scheduled dispatch)

The daily sync cron runs at **07:00 IST**. Run this manually on first deploy so the DB is populated before the Friday dispatch:

```bash
source .venv/bin/activate

# Dry run first — verify headers and row count look right
python scripts/test_outreach_sheet_sync.py --dry-run

# If output looks correct, write to DB
python scripts/test_outreach_sheet_sync.py
```

Check the output carefully:
- Confirm row count matches what you expect from the sheet
- Confirm detected headers include `Email ID`, `SPOC`, `BD lead email ID`, `BD head email ID`
- Any leads with blocking issues (`missing_email`, `missing_spoc`, `missing_bd_lead_email`, `missing_bd_head_email`) will be on `hold` and skipped during dispatch — fix them in the sheet and re-sync

---

## 5. Scheduled Jobs (auto-started by the server)

Once `OUTREACH_ENABLED=true` and the server is running, these jobs register automatically:

| Job | Schedule | What it does |
|-----|----------|-------------|
| `outreach_sync` | Daily 07:00 IST | Reads sheet → upserts leads in DB |
| `outreach_send` | Friday 08:30 IST | Sends emails to all `pending` leads |
| `outreach_replies` | Every 6 hours | Scans Gmail for replies → LLM classify → writes category to DB + sheet |

---

## 6. Verify After Deploy

```bash
source .venv/bin/activate

# Check leads synced and check for blocking issues
python3 -c "
import asyncio
from app.db.session import AsyncSessionLocal
from app.db.models import OutreachLead
from sqlalchemy import select, func

async def check():
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count()).select_from(OutreachLead))).scalar()
        pending = (await db.execute(select(func.count()).select_from(OutreachLead).where(OutreachLead.status == 'pending'))).scalar()
        hold = (await db.execute(select(func.count()).select_from(OutreachLead).where(OutreachLead.status == 'hold'))).scalar()
        print(f'Total: {total}  Pending (will send): {pending}  Hold (blocked): {hold}')

asyncio.run(check())
"
```

**Before the first Friday dispatch**, confirm:
- `pending` count looks right
- `hold` count is understood (check dashboard for blocking issues — fix in sheet and re-sync)
- `OUTREACH_TEST_MODE=false` is confirmed in the running environment
