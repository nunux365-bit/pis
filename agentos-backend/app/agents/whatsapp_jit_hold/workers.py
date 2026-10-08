"""Start/stop order-status Kafka worker (inbound via Meta webhook)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.whatsapp_jit_hold import kafka_consumer
from app.config.settings import settings
from app.infra.redis_client import get_redis

log = logging.getLogger(__name__)

_tasks: list[asyncio.Task[Any]] = []
_SUPERVISOR_RESTART_DELAY_SEC = 5


async def _supervise(
    name: str,
    runner: Callable[[], Awaitable[None]],
    *,
    is_stopping: Callable[[], bool],
) -> None:
    """Restart consumer loops after unexpected exits until shutdown is requested."""
    while not is_stopping():
        try:
            await runner()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("whatsapp_jit_hold %s consumer crashed", name)
        if is_stopping():
            return
        log.warning(
            "whatsapp_jit_hold %s consumer restarting in %ss",
            name,
            _SUPERVISOR_RESTART_DELAY_SEC,
        )
        await asyncio.sleep(_SUPERVISOR_RESTART_DELAY_SEC)


async def start_workers() -> None:
    if not settings.whatsapp_jit_hold_enabled:
        log.debug("whatsapp_jit_hold disabled")
        return

    try:
        await get_redis().ping()
    except Exception as e:
        log.error("whatsapp_jit_hold requires Redis — workers not started: %s", e)
        return

    if settings.whatsapp_jit_hold_test_mode and not (settings.whatsapp_jit_hold_test_phone or "").strip():
        log.error(
            "whatsapp_jit_hold test_mode requires WHATSAPP_JIT_HOLD_TEST_PHONE — workers not started"
        )
        return

    if settings.whatsapp_jit_hold_use_fixtures:
        log.error(
            "whatsapp_jit_hold WHATSAPP_JIT_HOLD_USE_FIXTURES=true — Kafka worker not started "
            "(would evaluate live POs against fixtures). Use internal trigger or set fixtures false."
        )
        return

    if settings.whatsapp_jit_hold_split_stub:
        log.error(
            "whatsapp_jit_hold WHATSAPP_JIT_HOLD_SPLIT_STUB=true — Kafka worker not started "
            "(would consume live POs and send stub split-done). Use internal trigger."
        )
        return

    kafka_bootstrap = (settings.whatsapp_jit_hold_kafka_bootstrap_servers or "").strip()
    kafka_topic = (settings.whatsapp_jit_hold_kafka_topic or "").strip()
    if not kafka_bootstrap or not kafka_topic:
        log.warning(
            "whatsapp_jit_hold kafka not configured (bootstrap/topic) — order-status consumer will idle"
        )
    if not (settings.whatsapp_meta_access_token or "").strip():
        log.warning("whatsapp_jit_hold WHATSAPP_META_ACCESS_TOKEN empty — outbound sends will fail")
    if not (settings.whatsapp_meta_phone_number_id or "").strip():
        log.warning("whatsapp_jit_hold WHATSAPP_META_PHONE_NUMBER_ID empty — outbound sends will fail")

    kafka_consumer.reset_stop()

    global _tasks
    _tasks = [
        asyncio.create_task(
            _supervise(
                "kafka",
                kafka_consumer.run,
                is_stopping=kafka_consumer.is_stopping,
            ),
            name="whatsapp_jit_hold_kafka",
        ),
    ]
    log.debug(
        "whatsapp_jit_hold workers started tasks=%s test_mode=%s lookback_hours=%s topic=%s group=%s split_stub=%s",
        len(_tasks),
        settings.whatsapp_jit_hold_test_mode,
        settings.whatsapp_jit_hold_kafka_lookback_hours,
        kafka_topic or "-",
        (settings.whatsapp_jit_hold_kafka_consumer_group or "agentos-whatsapp-jit-hold"),
        settings.whatsapp_jit_hold_split_stub,
    )


async def stop_workers() -> None:
    kafka_consumer.request_stop()
    global _tasks
    for task in _tasks:
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks = []
    log.debug("whatsapp_jit_hold workers stopped")
