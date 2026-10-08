#!/usr/bin/env python3
"""Create a test user for UI testing. Run from backend directory."""

import asyncio
import sys

async def main():
    # Import after setting up path
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.db.models import Feature, User, UserRole
    from app.security.passwords import hash_password

    email = "testuser@example.com"
    password = "testpassword123"

    async with AsyncSessionLocal() as session:
        # Check if user exists
        existing = await session.scalar(
            select(User).where(User.email == email)
        )
        if existing:
            print(f"User {email} already exists (id: {existing.id})")
            print(f"  Roles: {existing.roles}")
            print(f"  Has Optimus access: {existing.has_feature_access(Feature.OPTIMUS)}")
            return

        # Create test user
        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name="Test User",
            department="Testing",
            roles=[UserRole.EMPLOYEE.value],
        )
        session.add(user)
        await session.commit()

        print(f"Created test user:")
        print(f"  Email: {email}")
        print(f"  Password: {password}")
        print(f"  Roles: {user.roles}")
        print(f"\nLogin at your app and try accessing /optimus - you should get a 403 error.")
        print(f"Then go to /admin/optimus as admin and grant this user Optimus access.")

if __name__ == "__main__":
    asyncio.run(main())
