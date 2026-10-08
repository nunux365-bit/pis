"""Payroll Initiation Reminder Job.

Runs periodically (on the 15th of every month) to notify all active payroll Makers
that the payroll input sheets are ready to be initiated.
"""

import logging
from sqlalchemy import select

from app.config.settings import settings
from app.db.models import User
from app.db.session import AsyncSessionLocal
from app.email_automation import gmail_sa

log = logging.getLogger(__name__)

# A standard HTML template for the email
REMINDER_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
  <div style="background: #f8f9fa; border-radius: 8px; padding: 24px; border: 1px solid #dee2e6;">
    <h2 style="color: #1a1a1a; margin-top: 0;">📅 Payroll Input Initiation Open</h2>
    <p>Dear Maker,</p>
    <p>
      This is a friendly reminder that the <strong>Payroll Input Sheet (PIS)</strong> initiation period for the current month has started today (15th of the month).
    </p>
    <p>
      Kindly log in to the PIS Workflow portal at your earliest convenience to initiate and compile the payroll inputs for your designated component modules and employee home sites.
    </p>
    <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #dee2e6; font-size: 12px; color: #6c757d;">
      Best regards,<br/>
      <strong>PIS Workflow Notification System</strong>
    </div>
  </div>
</body>
</html>
"""

async def payroll_initiation_reminder_job() -> None:
    if not settings.payroll_email_enabled:
        log.info("payroll_initiation_reminder_job: email notifications are disabled; skipping reminders.")
        return

    log.info("payroll_initiation_reminder_job: Scanning active Makers to send reminder...")
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).filter(
                    User.is_active == True,
                    User.roles.contains(["maker"])
                )
            )
            makers = result.scalars().all()
            
        maker_emails = list(set([u.email.strip().lower() for u in makers if u.email]))
        
        if not maker_emails:
            log.warning("payroll_initiation_reminder_job: No active payroll Makers found in the system.")
            return
            
        log.info(f"payroll_initiation_reminder_job: Found {len(maker_emails)} active Maker email(s). Sending reminders...")
        
        # Send email via Gmail SA client
        try:
            gmail_sa.send_email(
                to=maker_emails,
                cc=None,
                bcc=None,
                subject="[PIS] Reminder: Payroll input sheet initiation is now open",
                body_html=REMINDER_HTML_TEMPLATE,
                body_text=None,
                reply_to=None,
                headers=None,
            )
            log.info("payroll_initiation_reminder_job: Initiation reminder emails sent successfully!")
        except Exception as e:
            log.warning("payroll_initiation_reminder_job: Google SA email dispatch failed: %s", e)
        
    except Exception as e:
        log.exception("payroll_initiation_reminder_job: Failed to run payroll initiation reminder job.")
