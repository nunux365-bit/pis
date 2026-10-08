import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from jobs.payroll_initiation_reminder import payroll_initiation_reminder_job
from app.config.settings import settings
from app.db.models import User

@pytest.mark.asyncio
async def test_payroll_initiation_reminder_job_disabled():
    settings.payroll_email_enabled = False
    
    with patch("jobs.payroll_initiation_reminder.gmail_sa.send_email") as mock_send_email:
        await payroll_initiation_reminder_job()
        mock_send_email.assert_not_called()

@pytest.mark.asyncio
async def test_payroll_initiation_reminder_job_success():
    settings.payroll_email_enabled = True
    
    # Mock database session to return a list of active users, some of whom are makers
    mock_db = AsyncMock()
    
    mock_user_1 = MagicMock(spec=User)
    mock_user_1.is_active = True
    mock_user_1.email = "maker1@example.com"
    mock_user_1.roles = ["maker"]
    mock_user_1.role_set = frozenset(["maker"])
    
    mock_user_2 = MagicMock(spec=User)
    mock_user_2.is_active = True
    mock_user_2.email = "hrbp1@example.com"
    mock_user_2.roles = ["hrbp"]
    mock_user_2.role_set = frozenset(["hrbp"])
    
    mock_user_3 = MagicMock(spec=User)
    mock_user_3.is_active = True
    mock_user_3.email = "maker2@example.com"
    mock_user_3.roles = ["maker", "employee"]
    mock_user_3.role_set = frozenset(["maker", "employee"])
    
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [mock_user_1, mock_user_3]
    mock_db.execute.return_value = mock_execute_result
    
    # Mock AsyncSessionLocal to return mock_db context manager
    mock_session_class = MagicMock()
    mock_session_class.return_value.__aenter__.return_value = mock_db
    
    with patch("jobs.payroll_initiation_reminder.AsyncSessionLocal", mock_session_class):
        with patch("jobs.payroll_initiation_reminder.gmail_sa.send_email") as mock_send_email:
            await payroll_initiation_reminder_job()
            
            mock_send_email.assert_called_once()
            args, kwargs = mock_send_email.call_args
            
            # Recipient list should contain only maker emails (lowercase)
            assert set(kwargs["to"]) == {"maker1@example.com", "maker2@example.com"}
            assert kwargs["subject"] == "[PIS] Reminder: Payroll input sheet initiation is now open"
            assert "Payroll Input Sheet (PIS)" in kwargs["body_html"]
