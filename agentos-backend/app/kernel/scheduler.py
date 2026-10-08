"""APScheduler — SQLAlchemy job store on PostgreSQL (sync URL), or MemoryJobStore when configured."""

import logging
from zoneinfo import ZoneInfo
from datetime import timezone

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config.settings import settings

log = logging.getLogger(__name__)


def _build_jobstores():
    if settings.scheduler_use_memory_jobstore:
        log.info("APScheduler: MemoryJobStore (SCHEDULER_USE_MEMORY_JOBSTORE=true)")
        return {"default": MemoryJobStore()}
    log.info("APScheduler: SQLAlchemyJobStore on PostgreSQL")
    return {
        "default": SQLAlchemyJobStore(
            url=settings.database_url_sync,
            tablename="apscheduler_jobs",
        )
    }


scheduler = AsyncIOScheduler(
    jobstores=_build_jobstores(),
    timezone=timezone.utc,
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 180,
    },
)


def _register_builtin_jobs() -> None:
    from jobs.context_refresh import context_refresh_job
    from jobs.daily_briefing import daily_briefing_job
    from jobs.payroll_initiation_reminder import payroll_initiation_reminder_job
    from jobs.email_automation_scan import (
        email_automation_dispatch_job,
        email_automation_scan_job,
    )
    from jobs.expiry_scan import expiry_scan_job
    from jobs.heartbeat import scheduler_heartbeat_job
    from jobs.pattern_analysis import pattern_analysis_job
    from jobs.procurement_reference_sync import procurement_reference_sync_job
    from jobs.procurement_sap_retry import procurement_sap_retry_job
    from jobs.schema_canary import run_schema_canary_job

    scheduler.add_job(
        scheduler_heartbeat_job,
        "interval",
        minutes=15,
        id="kernel_heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_schema_canary_job,
        "cron",
        hour=3,
        minute=12,
        id="schema_canary",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        procurement_sap_retry_job,
        "interval",
        minutes=2,
        id="procurement_sap_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.procurement_reference_sync_enabled:
        from app.procurement.reference_sync.constants import (
            SYNC_CRON_HOUR,
            SYNC_CRON_MINUTE,
        )

        scheduler.add_job(
            procurement_reference_sync_job,
            "cron",
            hour=SYNC_CRON_HOUR,
            minute=SYNC_CRON_MINUTE,
            id="procurement_reference_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # If the API was down at 02:30 UTC, still run once within 12h of restart.
            misfire_grace_time=43_200,
        )

    from jobs.o2c_contract_ingest import o2c_contract_ingest_job

    ist_timezone = ZoneInfo("Asia/Kolkata")
    scheduler.add_job(
        o2c_contract_ingest_job,
        "cron",
        hour=2,
        minute=0,
        timezone=ist_timezone,
        id="o2c_contract_ingest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=43_200,
    )

    scheduler.add_job(
        payroll_initiation_reminder_job,
        "cron",
        day=15,
        hour=10,
        minute=0,
        timezone=ist_timezone,
        id="payroll_initiation_reminder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    if settings.job_daily_briefing_enabled:
        scheduler.add_job(
            daily_briefing_job,
            "cron",
            hour=7,
            minute=30,
            id="daily_briefing",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.job_expiry_scan_enabled:
        scheduler.add_job(
            expiry_scan_job,
            "cron",
            hour=2,
            minute=0,
            id="expiry_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.job_context_refresh_enabled:
        scheduler.add_job(
            context_refresh_job,
            "cron",
            hour=4,
            minute=0,
            id="context_refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.job_pattern_analysis_enabled:
        scheduler.add_job(
            pattern_analysis_job,
            "cron",
            day_of_week="sun",
            hour=5,
            minute=0,
            id="pattern_analysis",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if settings.email_automation_enabled:
        # Both email-automation jobs run on a simple interval — scan every
        # ``email_automation_scan_interval_minutes`` (default 10), dispatch
        # every ``email_automation_dispatch_interval_minutes`` (default 2).
        # Per-tick concurrency is clamped by FOR UPDATE SKIP LOCKED inside the
        # pipeline so multiple workers take different rows.
        scheduler.add_job(
            email_automation_scan_job,
            "interval",
            minutes=settings.email_automation_scan_interval_minutes,
            id="email_automation_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            email_automation_dispatch_job,
            "interval",
            minutes=settings.email_automation_dispatch_interval_minutes,
            id="email_automation_dispatch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if settings.compliance_call_enabled:
        from jobs.compliance_call_tick import compliance_call_tick_job

        if settings.compliance_poll_interval_hours > 0:
            scheduler.add_job(
                compliance_call_tick_job,
                "interval",
                hours=settings.compliance_poll_interval_hours,
                id="compliance_call_tick",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        else:
            scheduler.add_job(
                compliance_call_tick_job,
                "interval",
                seconds=max(30, settings.compliance_min_sleep_seconds),
                id="compliance_call_tick",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

    if settings.responder_eval_enabled:
        from jobs.responder_eval_tick import responder_eval_tick_job

        scheduler.add_job(
            responder_eval_tick_job,
            "interval",
            minutes=settings.responder_eval_tick_interval_minutes,
            id="responder_eval_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    if settings.outreach_enabled:
        from jobs.outreach_replies import outreach_replies_job
        from jobs.outreach_send import outreach_send_job
        from jobs.outreach_sync import outreach_sync_job

        from app.email_automation.outreach.campaigns import SCHEDULE

        scheduler.add_job(
            outreach_sync_job,
            **SCHEDULE.sync,
            id="outreach_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            outreach_send_job,
            **SCHEDULE.send,
            id="outreach_send",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            outreach_replies_job,
            **SCHEDULE.replies,
            id="outreach_replies",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Prosight daily sync from Databricks
    if settings.optimus_prosight_enabled:
        from app.agents.optimus.prosight.service import is_databricks_configured

        if is_databricks_configured():
            from jobs.prosight_sync import prosight_sync_job

            # Use IST timezone explicitly to ensure correct scheduling
            # regardless of server timezone
            ist_timezone = ZoneInfo("Asia/Kolkata")

            scheduler.add_job(
                prosight_sync_job,
                "cron",
                hour=settings.prosight_sync_hour,
                minute=0,
                timezone=ist_timezone,
                id="prosight_sync",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            log.info(
                "Prosight sync job registered: daily at %02d:00 IST",
                settings.prosight_sync_hour,
            )


def start_scheduler() -> None:
    if scheduler.running:
        return
    _register_builtin_jobs()
    scheduler.start()
    log.info("APScheduler started")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
