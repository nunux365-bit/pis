"""
Contract extract step of ``product_flow.CONTRACT_INGEST_PRODUCT_FLOW``:

PDF (+ embedded JSON Schema in instructions) → ``document_markdown`` + ``contract_payload``
(candidate JSON). Downstream: ``pipeline`` normalizes file metadata, validates, then DB ingest.

**LLM stack:** OpenAI **Responses** API; model from ``settings.openai_chat_model`` (default
``gpt-5.4`` in ``app.config.settings``). Override with env ``OPENAI_CHAT_MODEL``.

Instruction profiles: ``generic`` (default) vs ``taco`` — see ``normalize_contract_prompt_profile``
and ``base_instructions_for_prompt_profile``. The pipeline sets profile from the first folder
segment under ``contracts_root`` (``taco/`` only selects the TACO prompt).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import fitz
import httpx

from app.agents.o2c_ohc.agenos_async_session import run_agenos_async
from app.agents.o2c_ohc.contract_extract_prompt_shared import CONTRACT_EXTRACT_SHARED_RULES_SUFFIX
from app.config.settings import settings

log = logging.getLogger(__name__)

_PKG = Path(__file__).resolve().parent


def _openai_safe_pdf_filename(name: str) -> str:
    """Normalize extension casing for OpenAI file inputs."""
    p = Path(name)
    if p.suffix.lower() == ".pdf" and p.suffix != ".pdf":
        return f"{p.stem}.pdf"
    return name


def _schema_path_for_llm() -> Path:
    custom = (settings.o2c_contract_schema_path or "").strip()
    if custom:
        p = Path(custom).expanduser().resolve()
        if p.is_file():
            return p
        log.warning("o2c_contract_schema_path not found (%s); using bundled schema", p)
    return _PKG / "schemas" / "contract_ingestion_schema.json"


@lru_cache(maxsize=4)
def _contract_schema_raw_text(path_str: str) -> str:
    return Path(path_str).read_text(encoding="utf-8")


def _optional_schema_block() -> str:
    if not settings.o2c_llm_include_json_schema_in_prompt:
        return ""
    try:
        sp = _schema_path_for_llm()
        raw = _contract_schema_raw_text(str(sp.resolve()))
    except OSError as e:
        log.warning("Could not read contract schema for LLM prompt: %s", e)
        return ""
    return (
        "\n\n--- JSON Schema for contract_payload (match types, enums, required fields) ---\n"
        + raw
        + "\n--- end schema ---\n"
    )


_BASE_INSTRUCTIONS_GENERIC = ("""You are the contract data extraction engine for Tata 1MG Healthcare Solutions Private Limited ("AgentOS Billing Agent"). Your job is to read the contract PDF and return structured contract terms for downstream billing and agenos DB ingest.

## Identity and context
- Tata 1MG is the Service Provider: Occupational Health Center (OHC) services, medical rooms, ambulances, medicines, equipment, and wellness programs for corporate clients.
- Contracts are between Tata 1MG and Tata Group companies or external clients. Output feeds downstream billing and MIS — wrong rates are a critical failure. ACCURACY IS CRITICAL.

## Output shape (required)
Return ONLY one JSON object that validates against the appended AGENOS contract ingestion JSON schema (no prose before/after JSON and no markdown fences).
Required top-level keys include `extraction_metadata`, `client`, `sites`, `contract`, `parties`, `payment_terms`, `rate_lines` unless the schema text explicitly differs.
Follow the schema exactly: every **required** field present; use **null** only where the schema allows null for that property, otherwise omit unknown optionals; never invent facts.
Do not output internal pass maps, checklists, or reasoning outside this JSON.

## Determinism (required)
- This is a deterministic extraction task: **one rule-based reading** of the PDF for billing — not creative paraphrase or arbitrary choices.
- Given the same input, keep output **maximally consistent** across runs; **avoid** alternative interpretations where the contract layout is clear.
- **Note:** Hosted LLM APIs are not bitwise-deterministic; still apply **only** PDF + schema + these rules. Numeric splits must obey **K**, rounding, and merge rules below even if phrasing in `billing_rule_text` varies slightly.

## Strict extraction rules
- Output ONLY that one JSON object — no explanations outside it.
- Extract ALL rate_lines: every billable line from every rate table, fee schedule, annexure, or commercial section = separate rate_line. Missing a line is a critical failure.
- Site-specific rates: if rates differ by location or headcount tier, create ONE rate_line per site (or per tiered site) with the correct rate. Do not collapse tiered pricing into one generic row when the contract is site- or tier-specific. Use site_key = null on a rate_line only when the schema allows and the contract explicitly applies one rate to ALL sites with no per-site variation.
- **Rate tables for MIS (staffing / OHC) — merged cells and bundles (multi-plant annexures):**
  - **TCS / general IT OHC (default for this profile):** **Headcount-tiered doctor annexures** and **one INR row per site/location** (no vertical merge across sites) → **one `rate_line` per annexure row** for that site. **Do not** split one site’s printed rate onto another site; **do not** force Type 3 shared logic unless the PDF shows **one** merged INR **spanning** multiple named locations.
  - **Rightmost fee column per role (merged / multi-row fees only):** When a **single** fee cell **spans multiple** table rows, use the **rightmost** INR column for **that** role as the **finest billing grain**: **headline + K** from **drawn** merges; **assign shares** by pairing **table-left** rows to the **same row band** on the right **top → bottom**. If table-left lists **more** names than **distinct** fee cells imply, **prefer the fee column’s** cell boundaries for **K**. Do not add extra `rate_lines` only because the left column lists more plants than the **drawn** fee structure supports.
  - **How to read it in one line (Types 1–4):** For each fee cell, decide what the rupee amount binds to — **Type 1** one total for **N identical seats at one site** and no “each/per” → `contracted_quantity` = N, `rate_amount` = total÷N; **Type 2** **per seat** rate × N → `rate_amount` = unit, `contracted_quantity` = N; **Type 3** one total for **K named sites** in a merged/shared row (same role, one amount for the group) → emit **K** `rate_lines` (one per `site_key`), each `contracted_quantity` = 1, `rate_amount` = total÷K, and say “shared fee ÷K sites” in `billing_rule_text`; **Type 4** if ambiguous → `needs_human_review` and explain in `review_notes`.
  - **Do not break TCS-style annexures:** When each **site has its own tier** (<1000 / 1000–2000 / etc.) with **different rates per location**, that is **not Type 3** — keep **one rate_line per site** using that site’s tier row; **never** divide one site’s tier rate across unrelated sites.
  - **Unmerge logically:** If a PDF table uses merged cells (one site name spanning several rows, or one role spanning qty+rate rows), **each billable staffing row** must still become its own `rate_line` with the correct `site_key` copied down onto every row that belongs to that site. Do not drop rows because the site column is blank on continuation lines.
  - **Lump vs per-person (same site):** If the table shows **one monthly price for N identical posts** (e.g. “3 Nurses”, single INR amount) and the contract does **not** say “each”, “per nurse”, or “per person”, treat the figure as a **bundle**: set `contracted_quantity` = N, `rate_amount` = (total / N), `rate_unit` = "month", and state in `billing_rule_text` that the PDF showed a total for N posts. If it **does** say per person/month each, set `rate_amount` = that unit rate and `contracted_quantity` = N.
  - **Two dimensions:** If text says a fee is **shared across several units** and within a unit nurses share one lump, apply **site split first** (total÷sites in that fee group), then **per-seat split within the site** (site share÷nurses) only when the cell reads as one lump for N nurses — document both steps in `billing_rule_text`.
  - **`schedule_type` and `schedule_config`:** Keep them aligned (e.g. periodic visits → `frequency`-style config with visit counts in `schedule_config`; desk days/hours → coherent `prescribed` or `frequency`). Avoid mismatched pairs such as `schedule_type` **prescribed** with `schedule_config._type` **rate_attendance** unless intentional per schema examples.
  - **Attendance / MIS row cap:** For manpower billed with attendance, prefer `billing_model` = `rate_attendance` and `deduction.scope` = `per_person` when each head is tracked separately (multiple nurses/doctors). Use `aggregate` only when the contract bills one lump for the group with a single attendance pool. This aligns with downstream MIS headcount caps (top N employees per `contract_rate_line_id`).
  - **attendance_required (critical — human OHC roster only):** Set **true** when the line is **human** physician/nurse/paramedic staffing tracked on the **OHC attendance workbook** — role_code prefixes **`MO_`**, **`FMO_`**, **`SR_MO_`**, **`JR_MO_`**, **`COMPANY_MO_`**, **`NURSE_`**, **`PARAMEDIC_`**, or exact **`DOCTOR`**, **`PHYSICIAN`**, **`VISITING_MEDICAL_OFFICER`**. Include **visiting physicians** billed as **`per_visit`** or session/monthly fees if they appear on the OHC roll. Set **false** for **`fixed_monthly`**, **`as_per_actuals`**, **`per_head`**, **`milestone`**, **`retainer_variable`** when the line is **not** individual roster staffing. **Never** set **true** for vehicles, supplies, or pass-through: if **role_code** contains **`AMBULANCE`**, **`BMW`**, **`MEDICINES`**, **`EQUIP`**, **`HEALTH_PACKAGE`**, **`DRIVER`** (vehicle/driver retainer), **`DISPOSAL`**, or **`WASTE`** (e.g. **ACLS_AMBULANCE**, **BMW_DISPOSAL**, **EQUIP_CALIB**) — those are **not** human headcount even if `billing_model` is wrong. Wrong **false** on a real MO/nurse line breaks MIS (lump billing without `employee_external_id`).
  - **Shared resources (one ambulance / BMW across sites):** If the contract states a **single** fee for a resource shared by named plants, still emit **one rate_line per site** when the schedule differs by site, OR one line with `site_key` null only if the contract truly bills once for all sites together. In `billing_rule_text`, explicitly say **shared / common / pooled** and name the sites; that helps automation split billing so only the primary site is charged when the contract requires it.
"""
    + CONTRACT_EXTRACT_SHARED_RULES_SUFFIX
    + """
- **Before you output:** Single JSON object only; **no** markdown code fences around it; required schema fields present or explicitly **null** as allowed; `rate_lines` and `sites` aligned; payment_terms and parties consistent with the PDF.

## Confidence and review
- Be honest with overall_confidence and per-field confidence where the schema provides them: 0.95+ clear tables; 0.80–0.94 inferred or partial; 0.60–0.79 educated guesses from prose; below 0.60 or material uncertainty → needs_human_review true and review_notes listing what to verify.
- Do not invent rates, dates, or party names; use null and needs_human_review true when unsure.
- **Second review (internal):** Before emitting JSON, **re-check** merged staffing tables: **K** from **rightmost** fee-column **drawn** merges; shared group totals ÷K across **all** spanned site rows unless the contract **clearly** bills one site only; never split tiered **per-site** annexure rates across unrelated sites. If two careful passes over the same PDF would still leave **numeric** splits ambiguous, set **`needs_human_review` true** and explain.
- **Determinism self-check (internal):** Would a repeat application of **only** these rules to the same PDF yield the **same** splits and `site_key` mapping? If not, flag **`needs_human_review`**. APIs are not guaranteed **byte-identical** output each run; still avoid arbitrary choices not grounded in PDF + rules.

The full JSON Schema for contract_payload is included after this block — treat it as the source of truth for required fields, enums, nesting, and in-schema examples (no separate few-shot extract is attached to this prompt)."""
)


# TACO profile: Schedule B merged-cell table — single rule block (see _TACO_SCHEDULE_B_MERGED_TABLE_RULES).
_TACO_SCHEDULE_B_MERGED_TABLE_RULES = """
## Internal 3-pass (silent — do not output Pass 1/2/3 text in the JSON)

Do these steps **in order** mentally; emit only the final `contract_payload`.

**Pass 1 — Span map:** For Col **D**, **E**, **F** (and **C** where merged), list each merged cell: printed headline text, ordered **Col B** site names inside that vertical band, **N** = count of **Col B** data rows in that band (**exclude** **HELD IN ABEYANCE** from the master site list and from every span). **Master site list** = every **Col B** name top-to-bottom, minus abeyance.

**Pass 2 — Allocate:** Apply FMO / MO / Nurse rules below (use **verbatim INR from the PDF** for every headline — never template numbers from this prompt).

**Pass 3 — Validate:** Run the **Self-validation** checklist; fix inconsistencies before output.

## How to read the table

This table has **MERGED CELLS**. A single cell in one column may visually span multiple rows.

1. **Empty cell = inherited from above.** If a cell is blank, it belongs to the nearest non-empty cell **above** in the **same** column. That non-empty cell is a merged cell spanning downward until the merge **ends** (next rule).

2. **Where a vertical merge ends:** Stop the span at the **first** of: (a) next non-empty cell in **that** column; (b) **thick horizontal rule** or an obvious **section break** (new region title, repeated table header row) if the PDF clearly starts a **new** fee band; (c) end of the table. **N** for fees is always **the number of Col B site rows** in that band — **not** the Col A group label count.

3. **Regional / repeated Schedule B:** The annexure may show **multiple blocks** (e.g. different clusters or geographies). **Restart** merge detection for each block: a Col D / Col F headline applies **only** to **Col B** rows in **its** band — never carry **N** or rupee totals across a visual break into the next block.

## Column reading rules

Left to right:
- Col A: Group label (informational; **do not** use group row count as **N**)
- Col B: Site/Location name (**authoritative row grain** for **N_FMO** / **N_MO**)
- Col C: FMO description — may merge like D/E/F; blank = same description block as above (use for `schedule_*` / visit text)
- Col D: FMO fee (INR) — may span multiple rows
- Col E: MO quantity — may span multiple rows (**must be read in the same vertical band as Col F** for MO decisions)
- Col F: MO fee (INR) — may span multiple rows
- Col G: Nurse qty
- Col H: Nurse fee (INR)

## Authoritative reference grid (Tata Autocomp / TACO FY24 Schedule B family)

When the PDF’s Schedule B is the **standard merged Chakan + Sanand** layout (same Col B row order and merge pattern as below), your emitted **`rate_amount`** for each staffing row **must match** this table. Use the PDF only to **confirm** merges and row presence; **do not** invent different splits when the table clearly matches.

| Col B site row (Schedule B) | FMO `rate_amount` (₹) | MO `rate_amount` (₹) | Nurse `rate_amount` (₹ per nurse / month) | `contracted_quantity` (nurses) |
| --------------------------- | --------------------: | -------------------: | ----------------------------------------: | -----------------------------: |
| IPD – Chakan-Ranjangaon | **19286** | **12000** | **25500** | **3** |
| CD – Chakan-Ranjangaon | **19286** | **12000** | **25500** | **2** |
| TTR (Incl. Hinjewadi) – Chakan-Ranjangaon | **19286** | **12000** | **25500** | **3** |
| TF – Chakan-Ranjangaon | **19286** | **12000** | **omit nurse line** | — |
| Gotion ESR – Chakan-Ranjangaon | **19286** | **48000** | **25500** | **3** |
| Punch, Prestolite & Bus-bar – Chakan-Ranjangaon | **19285** | **48000** | **25500** | **3** |
| ASAL – Chakan-Ranjangaon | **19286** | **48000** | **25500** | **3** |
| AITTR Bhosari – Chakan-Ranjangaon | **omit FMO line** | **16000** | **omit nurse line** | — |
| IPD Chinchwad – Chakan-Ranjangaon | **omit FMO line** | **16000** | **25500** | **3** |
| TM Seating – Chakan-Ranjangaon (Chinchwad) | **omit FMO line** | **16000** | **25500** | **2** |
| IPD Hinjewadi – Chakan-Ranjangaon | **omit FMO line** | **48000** | **25500** | **3** |
| IPD Katcon – Chakan-Ranjangaon | **omit all staffing lines** for this row in this layout | **omit** | **omit** | — |
| IPD – Sanand | **45000** | **24000** | **25500** | **2** |
| TTR – Sanand | **45000** | **24000** | **omit nurse line** | — |
| Gotion – Sanand | **45000** | **48000** | **25500** | **3** |

**Grid semantics (must match your `rate_lines`):**
- **Chakan Col D pool (1,35,000/-, N_FMO=7):** Six sites at **19286**, **Punch/Prestolite/Bus-bar** row at **19285** so the **sum is exactly 135000**.
- **Chakan first Col F MO pool (48,000/- over IPD+CD+TTR+TF):** **Four** MO lines at **12000** each — **never** 48000 on TTR only.
- **Sanand Col D pool (1,35,000/-, N_FMO=3):** **Three** FMO lines at **45000** each (IPD, TTR, Gotion Sanand) — **never** 67500 on only two sites.
- **Sanand Col F for IPD+TTR shared MO (48,000/-, two sites, 3a):** **Two** MO lines at **24000** each; **Gotion – Sanand** MO is a **separate** dedicated **48000** line (not part of that two-way split).
- **IPD Katcon** in this layout: still list in **`sites[]`** if Col B has the row; **no** FMO/MO/nurse `rate_lines` unless the PDF shows fees for that row.
- **Annexure vs Schedule B:** Do **not** emit **duplicate** staffing `rate_lines` for the **same physical Chakan Gotion (ESR)** plant under two different `site_key`s (e.g. annex summary + Schedule B row). Prefer **one** `site_key` (**Gotion ESR – Chakan-Ranjangaon** pattern) and **one** set of fees.

## Extraction rules

### FMO
- Find each FMO fee cell (Col D). **N_FMO** = number of **Col B** site rows inside **that** cell’s full vertical span (from the row where the INR appears through the last inherited row before the merge ends).
- **Positive headline:** Allocated FMO per row in the span = headline ÷ **N_FMO** (round to nearest rupee per row; **paise-safe:** first **N_FMO−1** rows in **top-to-bottom Col B order** get the same rounded share; the **last** row in that merge absorbs any remainder so **sum = headline exactly**).
- **Col D shows 0 / N/A / — / similar “no FMO”** spanning several **Col B** rows: that is its **own** band — **each** covered row has **FMO = 0**. **Do not** count those rows toward **N_FMO** of a **previous** positive headline merge.
- Rows with **no** Col D coverage (outside any merge) get FMO = 0 unless another Col D cell covers them.

**Anti-patterns (symbolic):** Let **H** = headline INR and **N** = **N_FMO**. Wrong: assigning full **H** to one site when **N>1**; wrong: **H÷(N−1)** or any split that omits a **Col B** row still inside the merge; wrong: sum of allocated FMO **≠** **H**.

- **Sanand vs Chakan (separate FMO bands):** The same printed figure (e.g. **1,35,000/-**) may appear in **more than one** Schedule B **region**. Each **Col D** merge defines its **own** **N_FMO** from **Col B** rows **only inside that vertical band**. **Never** copy Chakan’s row count to Sanand or merge two regions into one pool.
- **Sanand — count every Col B row in the Col D span:** If **Col D** (FMO) **inherits** through **IPD – Sanand**, **TTR – Sanand**, and **Gotion – Sanand** (one merged fee cell for all three), then **N_FMO = 3** and you MUST emit **three** FMO `rate_lines` (**H ÷ 3** each, paise-safe). For headline **1,35,000/-**, the **Authoritative reference grid** target is **45000** on **each** of the three sites. **Wrong:** **H ÷ 2** (e.g. **67500** on only IPD and TTR) while **Gotion – Sanand** is in **`sites[]`** but has **no** FMO line for that band. **Right:** either include Gotion in **N_FMO** or prove a **new** Col D headline / visual break **before** Gotion’s row starts a separate band (then **N_FMO** for the first band may be 2).
- **`billing_rule_text`:** For pooled FMO, state **N_FMO** and the region (e.g. “across **3** Sanand sites in this merged band”) so reviewers can reconcile the scan.

### MO
- Find each MO fee cell (Col F). **N_MO** = number of **Col B** rows inside **that** merge’s span (same boundary rules as Col D). Read **Col E** in the **same** band as **Col F** (merged E inherits with F).
- **Col D vs Col F (critical):** **FMO** INR amounts come **only** from **Col D**. **MO** INR amounts come **only** from **Col F**. **Never** set an **`MO_*`** `rate_amount` using the **FMO pooled headline** from Col D (e.g. **1,35,000**) when Col F for that staffing row shows a **different** MO figure (on typical TACO Schedule B annexures, per-site MO fees are often **~48,000/-** per merge group, **not** the FMO pool total). Treat **pasting Col D into MO `rate_lines`** as a **critical extraction error** — re-read the **rightmost MO fee column** for that row band.
- **Parse Col F:** Split into **label** (text before the rupee amount) and **amount** (headline INR). Use the **PDF Col F amount** only.

### MO decision order per Col F merge (**apply 3a → 3b → 3c; do not skip**)

- **3a — Col E shared-MO (overrides label match):** If merged **Col E** indicates **one** MO post **shared** across **multiple named sites** (slash **/**, **"1 for A/B"**, **"IPD/TTR"**, **"shared"** across plants): → **equal split** **H_MO ÷ N_MO** for **every** row in the merge. **Col F label cannot override 3a.**

- **3b — Exception A (single named site):** **Only if 3a does not apply:** If **label** (trimmed, text before the rupee amount) **exactly equals** **one** full **Col B** string in the span (verbatim Col B wording) **and** **no other** Col B row in the span repeats that **exact** string: → **full H_MO** on **that** `site_key` **only**; **omit** `MO_*` lines for **other** rows in the merge. **Not** **3b:** **PO / short names** (**"TTR"**, **"IPD"**, **"CD"**, **"TF"**) when Col B is longer (**"TTR (Incl. Hinjewadi), Chakan-Ranjangaon"**, **"IPD, Chakan-Ranjangaon"**, …) — substring-only → **3c** with **N_MO** equal splits. **Partial / substring** label match → **not** **3b**.

- **3c — Default equal split:** If neither 3a nor 3b: **N_MO > 1** → **H_MO ÷ N_MO** (paise-safe; **last** Col B row in merge absorbs remainder). Emit **N_MO** MO lines.

- **Hard wrong (common scan error):** Col F like **"IPD Hinjewadi … 48000/-"** spanning **[IPD Hinjewadi, IPD Katcon]** with Col E **not** stating one MO shared across both → **3b** applies → **48000** on **IPD Hinjewadi** only, **no** MO line for **IPD Katcon**. Outputting **24000 + 24000** here is **incorrect** unless **3a** applies.

- If **Col E** for a **Col B** row shows **0** / **N/A** / **—** and means **no MO seat** at that site, **MO allocated amount** for that site is **0** — but **N_MO** is still defined by the **Col F** merge span unless the PDF **visually** excludes that row from the fee cell (then do not count it in **N_MO**).

**Anti-patterns (symbolic):** Let **H_MO** = MO headline and **N** = **N_MO**. Wrong: full **H_MO** on one site when **N>1** **unless** **3b** applies; wrong: **H_MO÷2** on two sites when **3b** applies (one named site only); wrong: splitting across **N−1** rows only.

### MO → `rate_lines` emission (**MANDATORY**)

- **One Col F merge = one pool, many rows:** If **3a** or **3c** applies with **N_MO ≥ 2**, emit **exactly N_MO** MO `rate_lines`—one per Col B row—each `rate_amount` = **H_MO ÷ N_MO** (paise-safe). **Forbidden:** one line at full **H_MO** while another row in the same merge has **no** MO line **unless** **3b** applies.
- **Label ≠ site name:** Short PO label (e.g. **"TTR"**, **"AITTR"**) ≠ full Col B string → **3c**, **not** **3b**. If **N_MO ≥ 2** for one Col F merge, full **H_MO** on a **single** `site_key` is **forbidden** unless **3b** truly applies (full Col B text match). **₹48,000** only on **TTR** while **IPD, CD, TF** share the **same** Col F span is **always wrong** → **four** lines at **₹12,000** each (paise-safe) or **`needs_human_review`**.
- **Chakan cluster — first merged MO band (common layout):** The **first** merged **Col F** MO cell in the Chakan-Ranjangaon block often spans **four** Col B rows (e.g. IPD, CD, TTR (Incl. Hinjewadi), TF) with one headline (commonly **48,000/-**). You MUST emit **four** MO `rate_lines` with **equal** shares (**headline ÷ 4**). **Forbidden:** leaving **48,000** only on the TTR row while IPD, CD, and TF have **no** MO line — that is **misapplied 3b** (treating Col F shorthand as exact Col B match) or **N_MO** miscount.
- **Hard-coded pattern (AITTR / TACO — same merge, four sites):** When **one** Col **F** cell (**Rs 48,000/-**) spans **four** Col B rows — **IPD**, **CD**, **TTR (Incl. Hinjewadi)**, **TF** — and Col **E** does **not** force **3a** on a smaller subset, output **exactly four** `MO_BAMS_BHMS` lines: `rate_amount` **12000** each (48000÷4; last row takes paise remainder if any), `billing_rules.deduction.scope` **`aggregate`**. **Do not** output a single line with `rate_amount` **48000** on **TTR**-only `site_key`. The next **Col F** cells for **Gotion ESR**, **Punch/Prestolite/Bus-bar**, **ASAL** are usually **different merges** with **N_MO = 1** each → **48000** on **each** of those rows is correct and **not** a contradiction.
- **Later Chakan MO cells (dedicated seats):** **Separate** Col F merges **below** the four-row band may show **48,000/-** each on **Gotion ESR**, **Punch/Prestolite/Bus-bar**, **ASAL** with **N_MO = 1** per merge — full **H_MO** on each of those rows is correct. Do **not** merge those into the first four-row pool.
- **Three-row AITTR-style band:** If one Col F cell spans **three** Col B sites (e.g. Bhosari + Chinchwad + TM Seating), emit **three** MO lines with **equal** shares (**H_MO ÷ 3**). **Forbidden:** only two lines at **H_MO ÷ 2** while the third site in the merge has no MO line.
- **Sanand (or any block) — Col E shared across two sites:** If Col E indicates **one** MO post **shared** between **two** Col B rows (slash between site names, **"1 for X/Y"**, **"IPD/TTR"**, etc.), emit **two** MO `rate_lines` with **H_MO ÷ 2** each—even if Col F text looks like **"IPD …"** for the full headline. For the standard **48,000/-** shared **IPD – Sanand** + **TTR – Sanand** band, the **Authoritative reference grid** target is **24000** on **each** of those two rows; **Gotion – Sanand** keeps a **separate** MO line at **48000** (not half of that pool). **Forbidden:** full **H_MO** on the first site and **no** MO line on the second named site.
- **Recap:** **3a** = shared qty text → equal split. **3b** = label equals exactly one Col B name → full **H_MO** on that site only. **3c** = else → equal split **N_MO** lines.

### Nurse
- Col **G** and **H** are **per Col B row** only (no vertical merge across sites).
- Col **H** = **total** monthly nurse fee for that row; **rate_per_nurse** = **H ÷ G** when **G > 0**.
- If **G** = 0 / **-** / **NA** → no nurse allocation for that site.
- **Coverage:** Emit **`NURSE_*`** for **every** Col B row where **G > 0** and **H > 0** — do not skip a **middle** site (e.g. **TTR – Sanand** between IPD and Gotion) if the PDF shows nurse qty/fee for that row.

**Consistency check:** If the contract implies **one** uniform per-nurse rate, **H÷G** should agree across sites; if not, re-read **G/H** and row boundaries — if still inconsistent, **`needs_human_review`**.

### Special cases
- **HELD IN ABEYANCE:** omit from **`sites[]`** and all spans.
- **One Col B row** listing multiple locations (e.g. comma-separated): treat as **one** site row — do not split inside the cell.
- **All-zero site** (FMO, MO, nurse): still include in **`sites[]`**; staffing lines absent or zero as allowed by schema.

Rules:
- Site format: **"SiteName – Region"** where the PDF supplies region
- Use **"—"** in human reasoning for zero; JSON uses schema **null**/omission as appropriate
- **FMO** and **MO** splits must each **sum to their headline** per merge group

## Self-validation (silent before output)

1. Per **Col D** merge with positive headline: sum of allocated FMO = headline INR exactly.
1a. **Sanand (and any secondary block) FMO:** Count FMO `rate_lines` for that **Col D** band — must equal **N_FMO** (every **Col B** row inside the merge). If **`sites[]`** lists **Gotion – Sanand** (or another row) between IPD and TTR under the **same** inherited **Col D** cell, you cannot leave FMO only on two sites at **H÷2**; fix **K** or document a clear visual break + **`needs_human_review`**. If the layout matches the **Authoritative reference grid**, confirm **three** FMO lines at **45000** each for Sanand.
2. Per **Col F** merge: sum of allocated MO = that headline exactly.
3. **MO line count:** For each default-split Col F merge (**N_MO ≥ 2**), count MO `rate_lines` for the spanned `site_key`s—must equal **N_MO** (each site in the merge has its share line). If you only emitted **one** MO line at the full headline, **fix it** before output.
4. **MO amount sanity:** No **`MO_*`** `rate_amount` may equal the **FMO pooled headline** (e.g. **135000**) unless Col **F** in the PDF **explicitly** prints that same number as the **MO** fee for that merge. If you see **135000** on an MO line, **re-check** you did not read **Col D** by mistake.
5. **Two-row MO merges:** If you emitted **equal halves** (**H_MO÷2** on each), confirm **3a** (Col E shared). If Col E does **not** share, re-check **3b** — do not split when label names **one** site only. **Sanand IPD+TTR:** If the layout matches the **Authoritative reference grid**, confirm **24000+24000** for that shared **48000** band (not **48000** on one site only).
6. **N check:** **N_FMO** / **N_MO** equals **Col B** row count in that merge (no **—** on one row and equal splits on others in the same merge unless the PDF explicitly allows).
7. **Nurse:** **H÷G** coherent across sites with the same contract nurse terms, or **`needs_human_review`**.
8. No **HELD IN ABEYANCE** in output.
9. **Multi-block tables:** each region’s FMO/MO sums reconcile **within that block only**.
10. **Chakan first MO vs TTR-only mistake:** If **`sites[]`** lists **IPD, CD, TTR (Incl. Hinjewadi), TF** in order and you have **one** MO line at **48000** on **TTR** only — **delete** that pattern and emit **four** MO lines at **12000** each (or **`needs_human_review`**). Col F mentioning **"TTR"** is **not** enough for **3b** when Col B is the long form.
11. **`sites[]` vs staffing:** A Col B row in **`sites[]`** with **no** FMO/MO/nurse lines while neighbors are billed → re-read or **`needs_human_review`** — **except** when the row matches the **Authoritative reference grid** **IPD Katcon** row (no staffing lines in that standard layout). **AITTR / TF:** grid rows with **no** FMO and/or **no** nurse must **omit** those lines, not copy from neighbors.
12. **Nurse:** Emit **`NURSE_*`** for **every** Col B row with **G > 0** and **H > 0** (e.g. do not skip **TTR – Sanand** between IPD and Gotion if the scan shows G/H). **Authoritative reference grid:** **omit** nurse lines for **TF – Chakan**, **AITTR Bhosari**, **IPD Katcon**, and **TTR – Sanand** in the standard layout (dash in nurse column).

## contract_payload JSON (required — not a markdown table)

Apply the rules above, then emit **one** `contract_payload` object per the JSON Schema below.

- **`sites[]`:** One entry per **Col B** site row (minus abeyance), in **the same top-to-bottom order** as Schedule B (do not move a row to the top unless it is the first Col B row in the printed table). **All-zero** sites still listed.
- **`display_name` / naming:** Prefer **"SiteName – Region"** when the PDF shows region.
- **`rate_lines` — FMO:** For each site with allocated FMO **>** 0: `role_code` **`FMO_MBBS_AFIH`** unless Schedule B states otherwise; `billing_model` **`rate_attendance`**; `rate_amount` = allocated monthly FMO (integer INR); `contracted_quantity` from Col C / annexure when applicable, else **1**; `schedule_type` / `schedule_config` from **Col C** when present.
- **`rate_lines` — MO:** Follow **3a / 3b / 3c** above. For **each** Col B row with **non-zero** MO, emit one **`MO_*`** line; **`MO_BAMS_BHMS`** (or schema-allowed) per Schedule A/B; `contracted_quantity` from **Col E** per row else **1**. **3a/3c:** **N_MO** lines with equal shares. **3b:** **one** line at full **H_MO**; **no** MO line on other spanned rows. Pooled splits → `billing_rules.deduction.scope` **`aggregate`**; single dedicated MO seat → **`per_person`** where appropriate.
- **`rate_lines` — Nurse:** When **G** and **H** > 0: `role_code` **`NURSE_GNM_SHIFT`** or **`NURSE_GNM`**; `contracted_quantity` = **G**; `rate_amount` = **H ÷ G**.
- **`OHC_ADMIN_INVOICE_PCT`:** Use **`billing_model`** **`fixed_monthly`**, **`rate_amount` 0**, **`service_charge_type`** **`none`**, admin % in **`billing_rules.invoice_admin_pct`** (e.g. **10**) — do **not** put the percentage in **`rate_amount`**.
- **Rounding:** Nearest rupee per line within each merge; **last Col B row in that merge** carries FMO/MO reconciliation so sums match headlines.
- **MIS / attendance:** `attendance_required` **true** for human FMO/MO/nurse staffing; **false** for invoice admin %, medicines, equipment, ambulances — see shared suffix below.
- Ambiguity → **`needs_human_review`** + **`review_notes`**.
"""

_BASE_INSTRUCTIONS_TACO = ("""You are the contract data extraction engine for Tata 1MG Healthcare Solutions Private Limited ("AgentOS Billing Agent"). Your job is to read the contract PDF and return structured contract terms for downstream billing and agenos DB ingest.

## Identity and context
- Tata 1MG is the Service Provider: Occupational Health Center (OHC) services, medical rooms, ambulances, medicines, equipment, and wellness programs for corporate clients.
- Contracts are between Tata 1MG and Tata Group companies or external clients. Output feeds downstream billing and MIS — wrong rates are a critical failure. ACCURACY IS CRITICAL.

## Output shape (required)
Return ONLY one JSON object that validates against the appended AGENOS contract ingestion JSON schema (no prose before/after JSON and no markdown fences).
Required top-level keys include `extraction_metadata`, `client`, `sites`, `contract`, `parties`, `payment_terms`, `rate_lines` unless the schema text explicitly differs.
Follow the schema exactly: every **required** field present; use **null** only where the schema allows null for that property, otherwise omit unknown optionals; never invent facts.
Do not output internal pass maps, checklists, or reasoning outside this JSON.

## Determinism (required)
- This is a deterministic extraction task: **one rule-based reading** of the PDF for billing — not creative paraphrase or arbitrary choices.
- Given the same input, keep output **maximally consistent** across runs; **avoid** alternative interpretations where the contract layout is clear.
- **Note:** Hosted LLM APIs are not bitwise-deterministic; still apply **only** PDF + schema + the Schedule B rules below. For staffing annexures, use **merged-cell** logic (Col A–H), **N** from **Col B**, MO **3a→3b→3c** order, and **self-validation** before output.

## Strict extraction rules
- Output ONLY that one JSON object — no explanations outside it.
- Extract ALL rate_lines: every billable line from every rate table, fee schedule, annexure, or commercial section = separate rate_line. Missing a line is a critical failure.
- **Extract ALL sites (TACO / Schedule B):** **`sites[]` must list every distinct Col B site row** (unmerge inherited blanks upward). **Omit** **HELD IN ABEYANCE** rows entirely. **Wrong:** collapsing the annexure to only a handful of **`sites[]`** when Col B lists many plants.
- Site-specific rates: if rates differ by location or headcount tier, create ONE rate_line per site (or per tiered site) with the correct rate. Do not collapse tiered pricing into one generic row when the contract is site- or tier-specific. Use site_key = null on a rate_line only when the schema allows and the contract explicitly applies one rate to ALL sites with no per-site variation.
- **Rate tables for MIS (staffing / OHC) — columnar map (TACO / merged annexures):**
"""
    + _TACO_SCHEDULE_B_MERGED_TABLE_RULES
    + CONTRACT_EXTRACT_SHARED_RULES_SUFFIX
    + """
- **Before you output:** Single JSON object only; **no** markdown code fences; schema-valid `contract_payload`; `rate_lines` and `sites` aligned; payment_terms and parties consistent with the PDF. Re-run the **Self-validation** checklist in the Schedule B block silently (FMO/MO sums, nurse consistency, no abeyance rows).

## Confidence and review
- Be honest with overall_confidence and per-field confidence where the schema provides them: 0.95+ clear tables; 0.80–0.94 inferred or partial; 0.60–0.79 educated guesses from prose; below 0.60 or material uncertainty → needs_human_review true and review_notes listing what to verify.
- Do not invent rates, dates, or party names; use null and needs_human_review true when unsure.
- **Second review (internal):** Re-check Col B order, **N_FMO** / **N_MO** spans, **Sanand Col D** vs **Gotion – Sanand**, MO **3a/3b/3c** — **never** **₹48k MO on TTR-only** when **IPD/CD/TF** share the **same** Col F merge (**four lines × ₹12k**), **IPD Hinjewadi + IPD Katcon**, Sanand **IPD/TTR**, dedicated **48k** rows for Gotion/ASAL **below** that band, FMO/MO sums, **`OHC_ADMIN_INVOICE_PCT`**, nurse **H÷G** and **TTR – Sanand** if applicable; if anything fails self-validation or is unreadable, **`needs_human_review`** + **`review_notes`**.

The full JSON Schema for contract_payload is included after this block — treat it as the source of truth for required fields, enums, nesting, and in-schema examples (no separate few-shot extract is attached to this prompt)."""
)


CONTRACT_PROMPT_PROFILE_GENERIC = "generic"
CONTRACT_PROMPT_PROFILE_TACO = "taco"


def normalize_contract_prompt_profile(profile: str | None) -> str:
    """Default ``generic`` (TCS / general). ``taco`` selects Schedule B–style merged-fee instructions."""
    if profile is None or not str(profile).strip():
        return CONTRACT_PROMPT_PROFILE_GENERIC
    p = str(profile).strip().lower()
    if p == CONTRACT_PROMPT_PROFILE_TACO:
        return CONTRACT_PROMPT_PROFILE_TACO
    return CONTRACT_PROMPT_PROFILE_GENERIC


def base_instructions_for_prompt_profile(profile: str | None) -> str:
    if normalize_contract_prompt_profile(profile) == CONTRACT_PROMPT_PROFILE_TACO:
        return _BASE_INSTRUCTIONS_TACO
    return _BASE_INSTRUCTIONS_GENERIC


def _system_text(
    *,
    validation_error_hint: str | None,
    delivery_note: str,
    schema_block: str,
    contract_prompt_profile: str | None = None,
) -> str:
    repair = (
        f"\n\nPrevious validation error (fix contract_payload):\n{validation_error_hint}"
        if validation_error_hint
        else ""
    )
    base = base_instructions_for_prompt_profile(contract_prompt_profile)
    return base + schema_block + delivery_note + repair


def _supplemental_extracted_text(pdf_path: Path, full_text: str) -> str:
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
    return (
        f"PDF filename: {pdf_path.name}\n"
        f"Page count: {page_count}\n\n"
        "--- Extracted native text from the PDF (may be empty or partial for scans; the attached PDF or page images are authoritative) ---\n"
        f"{full_text[:120_000]}"
    )


def _pdf_full_text(path: Path) -> str:
    with fitz.open(path) as doc:
        parts: list[str] = []
        for i in range(doc.page_count):
            parts.append(doc.load_page(i).get_text("text") or "")
    return "\n\n".join(parts).strip()


def _parse_llm_json(raw: str) -> tuple[str, dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        raise ValueError("LLM returned empty content")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(
            "LLM output is not valid JSON (%s); first 400 chars: %r",
            e,
            raw[:400],
        )
        raise ValueError(f"llm_invalid_json: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("LLM response JSON must be an object")
    md = data.get("document_markdown") or ""
    payload = data.get("contract_payload")
    if not isinstance(payload, dict):
        raise ValueError("LLM response missing or invalid contract_payload object")
    return md, payload


def _parse_payload_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        raise ValueError("LLM returned empty content")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(
            "LLM payload output is not valid JSON (%s); first 400 chars: %r",
            e,
            raw[:400],
        )
        raise ValueError(f"llm_invalid_json: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("LLM payload response JSON must be an object")
    return data


def _extract_with_responses_pdf(
    client: Any,
    pdf_path: Path,
    model: str,
    *,
    validation_error_hint: str | None,
    full_text: str,
    schema_block: str,
    contract_prompt_profile: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """OpenAI Responses API: one request with full PDF as input_file + json_object text format."""
    pdf_bytes = pdf_path.read_bytes()
    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    delivery_note = (
        "\n\nInput: the complete PDF is attached as a file. "
        "Process all pages. Supplemental extracted text (if any) is secondary."
    )
    system_text = _system_text(
        validation_error_hint=validation_error_hint,
        delivery_note=delivery_note,
        schema_block=schema_block,
        contract_prompt_profile=contract_prompt_profile,
    )
    supplemental = _supplemental_extracted_text(pdf_path, full_text)
    safe_name = _openai_safe_pdf_filename(pdf_path.name)
    try:
        resp = client.responses.create(
            model=model,
            temperature=0,
            max_output_tokens=settings.o2c_llm_max_output_tokens,
            input=[
                {"role": "system", "content": system_text},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "filename": safe_name,
                            "file_data": f"data:application/pdf;base64,{b64}",
                        },
                        {"type": "input_text", "text": supplemental},
                    ],
                },
            ],
            text={"format": {"type": "json_object"}},
        )
    except Exception as e:
        log.exception("OpenAI responses.create (full PDF) failed for %s", pdf_path.name)
        raise RuntimeError(f"openai_responses_pdf_error: {e}") from e

    raw = (getattr(resp, "output_text", None) or "").strip()
    md, payload = _parse_llm_json(raw)
    usage = getattr(resp, "usage", None)
    usage_dict: dict[str, Any] | None
    if usage is None:
        usage_dict = None
    elif hasattr(usage, "model_dump"):
        usage_dict = usage.model_dump()
    else:
        usage_dict = {"repr": str(usage)}
    llm_audit = {
        "provider": "openai",
        "model": getattr(resp, "model", None) or model,
        "id": getattr(resp, "id", None),
        "usage": usage_dict,
        "response_format": "responses_json_object",
        "pdf_delivery": "responses_input_file",
        "vision_pages_sent": 0,
        "schema_in_prompt": bool(schema_block),
        "schema_path": str(_schema_path_for_llm().resolve()) if schema_block else None,
        "contract_prompt_profile": normalize_contract_prompt_profile(contract_prompt_profile),
    }
    return md, payload, llm_audit


async def _extract_contract_payload_with_responses_file_id_async(
    client: Any,
    *,
    file_id: str,
    filename: str,
    model: str,
    validation_error_hint: str | None,
    schema_block: str,
    contract_prompt_profile: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload_instructions = """Return ONLY one JSON object matching the contract_payload schema exactly.
Do NOT include document_markdown. Do NOT add prose or markdown fences."""
    base = base_instructions_for_prompt_profile(contract_prompt_profile)
    system_text = base + schema_block + "\n\n" + payload_instructions
    if validation_error_hint:
        system_text += f"\n\nPrevious validation error (fix contract_payload):\n{validation_error_hint}"
    resp = await client.responses.create(
        model=model,
        temperature=0,
        max_output_tokens=settings.o2c_llm_max_output_tokens,
        input=[
            {"role": "system", "content": system_text},
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": file_id},
                    {"type": "input_text", "text": "Extract contract_payload JSON only."},
                ],
            },
        ],
        text={"format": {"type": "json_object"}},
    )
    raw = (getattr(resp, "output_text", None) or "").strip()
    payload = _parse_payload_json(raw)
    usage = getattr(resp, "usage", None)
    usage_dict = usage.model_dump() if hasattr(usage, "model_dump") else ({"repr": str(usage)} if usage else None)
    prof = normalize_contract_prompt_profile(contract_prompt_profile)
    return payload, {
        "id": getattr(resp, "id", None),
        "model": getattr(resp, "model", None) or model,
        "usage": usage_dict,
        "contract_prompt_profile": prof,
    }


async def _extract_document_markdown_with_responses_file_id_async(
    client: Any,
    *,
    file_id: str,
    filename: str,
    model: str,
) -> tuple[str, dict[str, Any]]:
    markdown_instructions = """Return ONLY the contract as markdown text.
No JSON, no code fences, no preamble. Include headings/tables where possible."""
    resp = await client.responses.create(
        model=model,
        temperature=0,
        max_output_tokens=settings.o2c_llm_max_output_tokens,
        input=[
            {"role": "system", "content": markdown_instructions},
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": file_id},
                    {"type": "input_text", "text": "Extract full document markdown only."},
                ],
            },
        ],
    )
    md = (getattr(resp, "output_text", None) or "").strip()
    usage = getattr(resp, "usage", None)
    usage_dict = usage.model_dump() if hasattr(usage, "model_dump") else ({"repr": str(usage)} if usage else None)
    return md, {
        "id": getattr(resp, "id", None),
        "model": getattr(resp, "model", None) or model,
        "usage": usage_dict,
    }


async def extract_markdown_and_payload_async(
    pdf_path: Path,
    *,
    validation_error_hint: str | None = None,
    contract_prompt_profile: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """
    Async OpenAI Responses API: same two-pass flow as ``extract_markdown_and_payload``
    (upload PDF once, payload JSON + markdown).
    """
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for O2C_OHC LLM extraction")

    pdf_path = pdf_path.expanduser().resolve()
    schema_block = _optional_schema_block()
    prof = normalize_contract_prompt_profile(contract_prompt_profile)

    from openai import AsyncOpenAI

    _to = max(30.0, float(settings.o2c_openai_http_timeout_seconds))
    model = settings.openai_chat_model
    pdf_size = pdf_path.stat().st_size
    log.info(
        "OpenAI: uploading PDF for extraction (%.2f MiB): %s",
        pdf_size / (1024 * 1024),
        pdf_path.name,
    )
    t_upload = time.monotonic()
    file_id: str | None = None
    try:
        async with AsyncOpenAI(
            api_key=api_key,
            timeout=httpx.Timeout(_to, connect=30.0),
        ) as client:
            pdf_bytes = await asyncio.to_thread(pdf_path.read_bytes)
            safe_name = _openai_safe_pdf_filename(pdf_path.name)
            uploaded = await client.files.create(
                file=(safe_name, pdf_bytes),
                purpose="user_data",
            )
            log.info(
                "OpenAI: file upload finished in %.1fs",
                time.monotonic() - t_upload,
            )
            file_id = getattr(uploaded, "id", None)
            if not file_id:
                raise RuntimeError("openai_file_upload_error: missing file id")

            try:
                payload, payload_audit = await _extract_contract_payload_with_responses_file_id_async(
                    client,
                    file_id=file_id,
                    filename=pdf_path.name,
                    model=model,
                    validation_error_hint=validation_error_hint,
                    schema_block=schema_block,
                    contract_prompt_profile=prof,
                )
                md, md_audit = await _extract_document_markdown_with_responses_file_id_async(
                    client,
                    file_id=file_id,
                    filename=pdf_path.name,
                    model=model,
                )
            except Exception as e:
                raise RuntimeError(f"openai_responses_pdf_error: {e}") from e
            finally:
                try:
                    await client.files.delete(file_id)
                except Exception:
                    log.debug("Could not delete uploaded OpenAI file_id=%s", file_id)

        llm_audit = {
            "provider": "openai",
            "model": model,
            "response_format": "split_calls_payload_json_plus_markdown_text",
            "pdf_delivery": "uploaded_file_id_reused",
            "uploaded_file_id": file_id,
            "schema_in_prompt": bool(schema_block),
            "schema_path": str(_schema_path_for_llm().resolve()) if schema_block else None,
            "contract_prompt_profile": prof,
            "calls": {
                "payload": payload_audit,
                "markdown": md_audit,
            },
        }
        return md, payload, llm_audit
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"openai_responses_pdf_error: {e}") from e


def extract_markdown_and_payload(
    pdf_path: Path,
    *,
    validation_error_hint: str | None = None,
    contract_prompt_profile: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """
    Sync entry when **no** asyncio loop is running: delegates to ``extract_markdown_and_payload_async``.

    From async code, ``await extract_markdown_and_payload_async(...)`` instead.

    Call OpenAI Responses API in two passes using one uploaded PDF file_id:
    (1) contract_payload JSON, (2) document_markdown text.
    """
    return run_agenos_async(
        extract_markdown_and_payload_async(
            pdf_path,
            validation_error_hint=validation_error_hint,
            contract_prompt_profile=contract_prompt_profile,
        )
    )
