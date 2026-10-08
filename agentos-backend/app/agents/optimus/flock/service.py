"""Flock account linking and user management."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OptimusFlockAccount, User
from app.db.session import AsyncSessionLocal
from app.security.passwords import hash_password
from app.agents.optimus import config
from app.agents.optimus.flock import api_client
from app.agents.optimus.flock.formatting import dual_message

log = logging.getLogger(__name__)

# Single source of truth for the support/issue form shown to users across ALL
# Optimus/Flock issue messages (keep every "raise a request" pointer consistent).
# Sourced from config (optimus_flock_support_form_url) like all other Flock params.
SUPPORT_FORM_URL = config.FLOCK_SUPPORT_FORM_URL

# Sample questions shown in the install welcome to help users get started.
_SAMPLE_QUESTIONS = (
    "What health insurance benefits are available?",
    "How does the performance review process work?",
    "What is the policy for remote work?",
)


def _welcome_texts(greeting: str) -> dict[str, str]:
    """Build the welcome body (plain + FlockML) sent on Flock app install.

    Authored once in Markdown; both Flock variants are derived via dual_message().
    A warm greeting, a one-line intro to Optimus, and a few sample questions.
    """
    bullets = "\n".join(f"- {q}" for q in _SAMPLE_QUESTIONS)
    return dual_message(
        f"{greeting} I'm **Optimus**, your HR assistant at Tata 1mg.\n\n"
        "Ask me anything about company policies, benefits, and processes — I'll "
        "answer from our official documents.\n\n"
        f"**Try asking:**\n{bullets}\n\n"
        "Just type your question to get started! 😊\n\n"
        f"_Optimus is also available on the web: [{config.WEB_APP_URL}]({config.WEB_APP_URL})_"
    )


async def link_account(
    flock_user_id: str,
    user_email: str,
    flock_email: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Link a Flock user ID to an AgentOS user by email.

    Args:
        flock_user_id: Flock's user identifier
        user_email: Email to look up in AgentOS users table
        flock_email: Optional Flock email for reference

    Returns:
        Dict with link status and user info

    Raises:
        ValueError: If user not found or already linked
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        # Check if Flock ID is already linked
        existing = await session.scalar(
            select(OptimusFlockAccount).where(
                OptimusFlockAccount.flock_user_id == flock_user_id
            )
        )
        if existing:
            return {
                "status": "already_linked",
                "user_id": str(existing.user_id),
                "flock_user_id": flock_user_id,
            }

        # Find user by email
        user = await session.scalar(
            select(User).where(User.email == user_email.lower())
        )
        if not user:
            raise ValueError(f"No user found with email: {user_email}")

        # Check if user already has a Flock account linked
        existing_user_link = await session.scalar(
            select(OptimusFlockAccount).where(
                OptimusFlockAccount.user_id == user.id
            )
        )
        if existing_user_link:
            raise ValueError(f"User {user_email} is already linked to Flock account {existing_user_link.flock_user_id}")

        # Create link
        link = OptimusFlockAccount(
            flock_user_id=flock_user_id,
            user_id=user.id,
            flock_email=flock_email,
        )
        session.add(link)
        await session.commit()

        return {
            "status": "linked",
            "user_id": str(user.id),
            "user_email": user.email,
            "flock_user_id": flock_user_id,
        }

    finally:
        if own_session:
            await session.close()


async def get_user_by_flock_id(
    flock_user_id: str,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """
    Get AgentOS user info by Flock user ID.

    Returns None if not linked.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        link = await session.scalar(
            select(OptimusFlockAccount).where(
                OptimusFlockAccount.flock_user_id == flock_user_id
            )
        )
        if not link:
            return None

        user = await session.get(User, link.user_id)
        if not user:
            return None

        return {
            "user_id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "flock_user_id": flock_user_id,
            "flock_email": link.flock_email,
            "linked_at": link.linked_at.isoformat(),
        }

    finally:
        if own_session:
            await session.close()


async def unlink_account(
    flock_user_id: str,
    session: AsyncSession | None = None,
) -> bool:
    """
    Unlink a Flock account.

    Returns True if unlinked, False if not found.
    """
    own_session = session is None
    if own_session:
        session = AsyncSessionLocal()

    try:
        link = await session.scalar(
            select(OptimusFlockAccount).where(
                OptimusFlockAccount.flock_user_id == flock_user_id
            )
        )
        if not link:
            return False

        await session.delete(link)
        await session.commit()
        return True

    finally:
        if own_session:
            await session.close()


async def auto_link_on_install(
    flock_user_id: str,
    flock_token: str,
) -> dict[str, Any]:
    """
    Link a Flock user to an AgentOS account on app install, auto-provisioning if needed.

    Called on app.install (the only Flock event that supplies a per-user API token,
    which we need to resolve the user's email). Lets any eligible Tata 1mg employee use
    Optimus immediately without manually creating an account.

    Flow:
    1. Already linked → return it.
    2. Resolve the user's email from Flock (via the per-user install token).
    3. Email matches an existing AgentOS user → link to it (any domain).
    4. No match, email domain is eligible → auto-provision a User (mirrors the
       Google SSO / payroll provisioning pattern) → link.
    5. No match, ineligible domain → politely decline. No email → politely decline.

    Args:
        flock_user_id: Flock's user identifier
        flock_token: Per-user token from the Flock app.install event

    Returns:
        Dict with ``status`` and a user-facing ``message`` (+ ``flockml`` for declines).
        On success (``linked`` / ``already_linked``) includes ``user_id``.
    """
    async with AsyncSessionLocal() as session:
        # Check if already linked
        existing = await session.scalar(
            select(OptimusFlockAccount).where(
                OptimusFlockAccount.flock_user_id == flock_user_id
            )
        )
        if existing:
            return {
                "status": "already_linked",
                "user_id": str(existing.user_id),
                **_welcome_texts("👋 Welcome back!"),
            }

        # Get user info from Flock API
        flock_user = await api_client.get_user_info(flock_token)
        if not flock_user:
            log.warning("Could not get Flock user info for %s", flock_user_id)
            return {
                "status": "error",
                **dual_message(
                    "We couldn't retrieve your Flock account info. Please try again in a bit. "
                    f"If it keeps happening, [raise a request here]({SUPPORT_FORM_URL})."
                ),
            }

        flock_email = flock_user.get("email", "").lower()
        flock_name = f"{flock_user.get('firstName', '')} {flock_user.get('lastName', '')}".strip()

        if not flock_email:
            return {
                "status": "no_email",
                **dual_message(
                    "We couldn't read your email from Flock, so we can't set you up "
                    "automatically. Please enable email sharing in Flock, or "
                    f"[raise a request here]({SUPPORT_FORM_URL})."
                ),
            }

        # Find AgentOS user by email
        user = await session.scalar(
            select(User).where(User.email == flock_email)
        )

        # No existing account → auto-provision if the email domain is eligible.
        if not user:
            # Reuse the same email-domain gate as Google SSO login (no duplicated logic).
            # Imported lazily to avoid pulling the API layer into the flock import graph.
            from app.api.routes.auth import _email_domain_allowed

            if not _email_domain_allowed(flock_email):
                return {
                    "status": "not_eligible",
                    **dual_message(
                        "Optimus is available to Tata 1mg employees. We couldn't verify a "
                        f"Tata 1mg email for your Flock account ({flock_email}). If you believe "
                        f"this is an error, [raise a request here]({SUPPORT_FORM_URL})."
                    ),
                }

            # Auto-provision (mirrors app.api.routes.auth / payroll provisioning).
            user = User(
                email=flock_email,
                hashed_password=hash_password(secrets.token_urlsafe(48)),
                full_name=flock_name or flock_email.split("@")[0].title().replace(".", " "),
                department="General",
                roles=["employee"],
                is_active=True,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                # Race: another install/message created the user concurrently.
                await session.rollback()
                user = await session.scalar(select(User).where(User.email == flock_email))
                if not user:
                    return {
                        "status": "error",
                        **dual_message(
                            "We couldn't set up your account. Please try again in a bit. "
                            f"If it keeps happening, [raise a request here]({SUPPORT_FORM_URL})."
                        ),
                    }
            else:
                log.info("Auto-provisioned AgentOS user for Flock install: %s", flock_email)

        # Check if user already has a different Flock account linked
        existing_user_link = await session.scalar(
            select(OptimusFlockAccount).where(
                OptimusFlockAccount.user_id == user.id
            )
        )
        if existing_user_link:
            log.warning(
                "User %s already linked to different Flock account %s",
                flock_email,
                existing_user_link.flock_user_id,
            )
            return {
                "status": "different_account",
                **dual_message(
                    "Optimus is already linked to a different Flock account. "
                    f"To update the link, [raise a request here]({SUPPORT_FORM_URL})."
                ),
            }

        # Create link
        link = OptimusFlockAccount(
            flock_user_id=flock_user_id,
            user_id=user.id,
            flock_token=flock_token,
            flock_email=flock_email,
            flock_name=flock_name or None,
        )
        session.add(link)
        await session.commit()

        return {
            "status": "linked",
            "user_id": str(user.id),
            **_welcome_texts(f"👋 Welcome, {flock_name or user.full_name}!"),
        }


async def list_linked_accounts() -> list[dict[str, Any]]:
    """
    List all linked Flock accounts (for admin view).

    Returns list of linked accounts with user info.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OptimusFlockAccount, User)
            .join(User, OptimusFlockAccount.user_id == User.id)
            .order_by(OptimusFlockAccount.linked_at.desc())
        )
        rows = result.all()

        return [
            {
                "id": str(link.id),
                "flock_user_id": link.flock_user_id,
                "flock_email": link.flock_email,
                "flock_name": link.flock_name,
                "user_id": str(user.id),
                "user_email": user.email,
                "user_full_name": user.full_name,
                "linked_at": link.linked_at.isoformat(),
            }
            for link, user in rows
        ]
