"""Order RCA — async diagnose + poll (no workflow)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.order_rca import graph, service
from app.agents.order_rca.constants import ORDER_RCA_INTERNAL_USER_ID
from app.api.deps import get_current_user
from app.config.settings import settings
from app.db.models import User

router = APIRouter(prefix="/order-rca", tags=["order-rca"])
internal_router = APIRouter(prefix="/__internal__/order-rca", tags=["order-rca-internal"])

INTERNAL_USER_ID = ORDER_RCA_INTERNAL_USER_ID


class DiagnoseBody(BaseModel):
    order_id: str = Field(..., min_length=4, max_length=64)
    chat_id: str | None = Field(default=None, max_length=128)


def _require_enabled() -> None:
    if not settings.order_rca_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Order RCA is disabled")


async def _diagnose(
    body: DiagnoseBody,
    *,
    user_id: str,
    background_tasks: BackgroundTasks,
    reuse_existing: bool = True,
):
    _require_enabled()
    try:
        await service.ensure_redis()
    except Exception as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Redis required for Order RCA: {e}",
        ) from e
    doc = await service.start_diagnosis(
        body.order_id,
        user_id=user_id,
        reuse_existing=reuse_existing,
        chat_id=body.chat_id,
    )
    reused = bool(doc.pop("_reused", False))
    if not reused:
        background_tasks.add_task(graph.run_graph, doc["run_id"], doc["order_id"])
    return doc


async def _get_run(run_id: str, *, user_id: str):
    _require_enabled()
    doc = await service.get_run(run_id, user_id=user_id)
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found or expired")
    return doc


@router.post("/diagnose", status_code=status.HTTP_202_ACCEPTED)
async def diagnose(
    body: DiagnoseBody,
    user: Annotated[User, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
):
    return await _diagnose(body, user_id=str(user.id), background_tasks=background_tasks)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: Annotated[User, Depends(get_current_user)],
):
    return await _get_run(run_id, user_id=str(user.id))


@internal_router.post("/diagnose", status_code=status.HTTP_202_ACCEPTED)
async def diagnose_internal(
    body: DiagnoseBody,
    background_tasks: BackgroundTasks,
):
    return await _diagnose(
        body,
        user_id=INTERNAL_USER_ID,
        background_tasks=background_tasks,
        reuse_existing=False,
    )


@internal_router.get("/runs/{run_id}")
async def get_run_internal(run_id: str):
    return await _get_run(run_id, user_id=INTERNAL_USER_ID)
