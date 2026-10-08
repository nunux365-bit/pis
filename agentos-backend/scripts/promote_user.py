#!/usr/bin/env python3
"""Promote a user to all roles (maker, hrbp, hod, payroll, payroll_admin). Run from backend directory."""

import asyncio
import sys

async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/promote_user.py <email>")
        return

    email = sys.argv[1].lower().strip()

    # Setup paths and imports
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models import User

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.email == email)
        )
        user = result.scalar_one_or_none()
        if not user:
            print(f"Error: User {email} not found in database.")
            print("Please log in via the web application once first so your account is provisioned, then run this script.")
            return

        # Grant all roles to allow end-to-end testing
        user.roles = ["employee", "maker", "hrbp", "hod", "payroll", "payroll_admin"]
        await session.commit()

        print(f"Successfully promoted {email} to roles: {user.roles}")

if __name__ == "__main__":
    asyncio.run(main())
