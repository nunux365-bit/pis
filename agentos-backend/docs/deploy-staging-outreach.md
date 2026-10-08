# Cold Outreach — Staging Deployment Checklist

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
- [ ] Add `corporatehealth@1mg.com` as a **"Send mail as"** alias in the `automation.agents@1mg.com` Gmail account settings so the `From:` header is respected by Gmail on delivery.

---

## 3. Environment Variables

Add these to your staging `.env`:

```env
# ── Master switches ────────────────────────────────────────────
OUTREACH_ENABLED=true
OUTREACH_SYNC_ENABLED=true
OUTREACH_DISPATCH_ENABLED=true
OUTREACH_CLASSIFY_ENABLED=true
OUTREACH_SHEET_WRITEBACK_ENABLED=true

# ── Safety — all emails redirected to ayush only ───────────────
OUTREACH_TEST_MODE=true
OUTREACH_TEST_REDIRECT_TO=ayush.mehra@1mg.com
OUTREACH_TEST_ALLOWED_CC=vishakha.vartak@1mg.com   # allowed through as real CC

# ── Volume cap — send at most 5 emails per dispatch run ────────
OUTREACH_DAILY_SEND_LIMIT=5

# ── Sheet ──────────────────────────────────────────────────────
CHW_OUTREACH_GSHEET_ID=<staging_sheet_id>
```

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

---

## 5. Scheduled Jobs (auto-started by the server)

Once `OUTREACH_ENABLED=true` and the server is running, these jobs register automatically:

| Job | Schedule | What it does |
|-----|----------|-------------|
| `outreach_sync` | Daily 07:00 IST | Reads sheet → upserts leads in DB |
| `outreach_send` | Friday 08:30 IST | Sends emails to all `pending` leads (capped by `OUTREACH_DAILY_SEND_LIMIT`) |
| `outreach_replies` | Every 6 hours | Scans Gmail for replies → LLM classify → writes category to DB + sheet |

---

## 6. Verify After Deploy

```bash
# Check leads are in DB after sync
source .venv/bin/activate
python3 -c "
import asyncio
from app.db.session import AsyncSessionLocal
from app.db.models import OutreachLead
from sqlalchemy import select, func

async def check():
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count()).select_from(OutreachLead))).scalar()
        print(f'Total leads in DB: {total}')

asyncio.run(check())
"
```
