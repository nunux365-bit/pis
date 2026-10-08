
if __name__ == '__main__':
    import asyncio
    from pathlib import Path
    from app.email_automation.workflow_packs import REGISTRY
    from app.email_automation.pipeline.process import build_variant_plans

    pack = REGISTRY["PAYMENT_REMINDER_WEEKLY"]
    variant = pack.variant("diagnostics_aggregator")
    xlsx = Path("Receivable-27.05.2026.xlsx")  # or full path

    # Minimal tracker row — use a real Code from your xlsx + master sheet
    tracker = [{"Code": "1000001489", "Mail ID 1": "sankar.yadalam@1mg.com"}]

    plans = asyncio.run(build_variant_plans(
        pack=pack, variant=variant, xlsx_path=xlsx,
        tracker_rows=tracker, period="2026-W22",
    ))
    for p in plans:
        print(p["business_key"], p["row_count"], p["rendered_subject"])

    # print(",".join(plans[0].keys()))
    #
    # for p in plans:
    #     print("|".join(str(v) for v in p.values()))
