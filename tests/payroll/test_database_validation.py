"""
test_database_validation.py – Live database validation tests for payroll model writes, updates, deletes, transaction rollbacks, and truncation/validation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
import uuid

from app.config.settings import settings
from app.db.models import PayrollWorkflowQueue
from app.api.routes.payroll import _safe_str, has_emoji


def get_test_sessionmaker():
    # Use NullPool to prevent event loop connection reuse conflicts across pytest-asyncio loops
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False), engine


@pytest.mark.asyncio
async def test_payroll_db_write_lifecycle() -> None:
    SessionMaker, engine = get_test_sessionmaker()
    async with SessionMaker() as session:
        # Create a unique empCode to prevent collisions
        unique_emp = f"TEST-VAL-{uuid.uuid4().hex[:8]}"

        # 1. Insert Operation
        item = PayrollWorkflowQueue(
            empCode=unique_emp,
            empName="DB Validation Test User",
            grade="M2",
            designation="Principal Test Architect",
            employeeHome="Headquarters",
            type="OVERTIME",
            module="OVERTIME HOURS",
            amount="7500",
            remarks="Unit Test Entry",
            status="MAKER",
            initiatorEmail="tester@1mg.com"
        )
        session.add(item)
        await session.commit()

        # Verify insert happened
        res = await session.execute(
            select(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        retrieved = res.scalar_one_or_none()
        assert retrieved is not None, "Row did not insert"
        assert retrieved.empName == "DB Validation Test User"

        # 2. Update Operation
        retrieved.status = "HRBP"
        retrieved.remarks = "Remarks updated by reviewer"
        await session.commit()

        # Verify update happened
        res_updated = await session.execute(
            select(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        updated = res_updated.scalar_one()
        assert updated.status == "HRBP", "Update did not persist"
        assert updated.remarks == "Remarks updated by reviewer"

        # 3. Delete Operation
        await session.execute(
            delete(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        await session.commit()

        # Verify delete happened
        res_deleted = await session.execute(
            select(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        deleted = res_deleted.scalar_one_or_none()
        assert deleted is None, "Row delete failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_payroll_transaction_rollback() -> None:
    SessionMaker, engine = get_test_sessionmaker()
    async with SessionMaker() as session:
        unique_emp = f"ROLLBACK-TEST-{uuid.uuid4().hex[:8]}"

        item = PayrollWorkflowQueue(
            empCode=unique_emp,
            empName="Rollback Candidate",
            grade="M3",
            designation="Engineer",
            employeeHome="Headquarters",
            module="OVERTIME HOURS",
            amount="1000",
            status="MAKER",
            initiatorEmail="tester@1mg.com"
        )
        session.add(item)
        await session.flush() # Send to DB but do not commit within transaction

        # Verify it exists in transaction session
        res = await session.execute(
            select(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        assert res.scalar_one_or_none() is not None

        # Rollback transaction
        await session.rollback()

        # Verify row does not exist in DB after rollback
        res_after = await session.execute(
            select(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        assert res_after.scalar_one_or_none() is None, "Transaction rollback failed to discard writes"

    await engine.dispose()


@pytest.mark.asyncio
async def test_payroll_safe_truncation_limits() -> None:
    # Test capping helper
    huge_grade = "A" * 500
    truncated_grade = _safe_str(huge_grade, max_len=5)
    assert len(truncated_grade) == 5
    assert truncated_grade == "AAAAA"

    SessionMaker, engine = get_test_sessionmaker()
    async with SessionMaker() as session:
        unique_emp = f"TRUNC-TEST-{uuid.uuid4().hex[:8]}"
        
        # Grade column length limit is VARCHAR(50).
        # Passing 100 characters should be truncated safely by _safe_str to 50 characters, preventing SQL length error.
        capped_grade = _safe_str("G" * 100, max_len=50)

        item = PayrollWorkflowQueue(
            empCode=unique_emp,
            empName="Truncation User",
            grade=capped_grade,
            designation="Principal Architect",
            employeeHome="Headquarters",
            module="OVERTIME HOURS",
            amount="2000",
            status="MAKER",
            initiatorEmail="tester@1mg.com"
        )
        session.add(item)
        await session.commit()

        # Verify retrieved data length is exactly 50
        res = await session.execute(
            select(PayrollWorkflowQueue).where(PayrollWorkflowQueue.empCode == unique_emp)
        )
        retrieved = res.scalar_one()
        assert len(retrieved.grade) == 50, "Capping failed to limit string length"
        assert retrieved.grade == "G" * 50

        # Clean up
        await session.delete(retrieved)
        await session.commit()

    await engine.dispose()


def test_payroll_emoji_validation_checker() -> None:
    # 1. Normal alphanumeric and symbols must pass (has_emoji == False)
    assert has_emoji("John Doe") is False
    assert has_emoji("EMP101-ABC_123") is False
    assert has_emoji("Remarks: approved! +15% extra.") is False

    # 2. Emojis must be detected (has_emoji == True)
    assert has_emoji("John Doe 💸") is True
    assert has_emoji("Approved ⚠️ check reason") is True
    assert has_emoji("🔥 New submission") is True
