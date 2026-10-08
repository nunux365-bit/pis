"""Kafka consumer — nexus order-status trigger (async aiokafka)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

from app.agents.whatsapp_jit_hold.order_status_trigger import (
    normalize_kafka_bootstrap_servers,
    process_order_status_raw,
)
from app.config.settings import settings

log = logging.getLogger(__name__)

_stop = asyncio.Event()
_MS_PER_HOUR = 3_600_000
_PROCESS_BACKOFF_SEC = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)


def decode_kafka_value(raw: Any) -> str:
    """Decode a Kafka record value. Raises UnicodeDecodeError on invalid UTF-8 bytes."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8") if raw else ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def process_failure_backoff_sec(consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return _PROCESS_BACKOFF_SEC[0]
    idx = min(consecutive_failures - 1, len(_PROCESS_BACKOFF_SEC) - 1)
    return _PROCESS_BACKOFF_SEC[idx]


async def _wait_stop_or_sleep(seconds: float) -> None:
    if seconds <= 0 or _stop.is_set():
        return
    try:
        await asyncio.wait_for(_stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


def request_stop() -> None:
    _stop.set()


def reset_stop() -> None:
    _stop.clear()


def is_stopping() -> bool:
    return _stop.is_set()


def lookback_timestamp_ms(*, hours: int, now_ms: int | None = None) -> int:
    """Unix epoch ms for ``now - hours``. ``hours<=0`` → current time."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if hours <= 0:
        return now
    return max(0, now - hours * _MS_PER_HOUR)


async def seek_uncommitted_partitions_to_lookback(
    consumer: Any,
    assigned: Iterable[Any],
    *,
    lookback_hours: int,
    now_ms: int | None = None,
) -> dict[Any, int]:
    """
    For partitions with **no committed offset** (new group or expired offsets),
    seek to the first message at/after ``now - lookback_hours``.

    Partitions that already have a committed offset are left alone so downtime
    catch-up is complete. Returns ``{tp: offset}`` for partitions we sought.
    """
    partitions = list(assigned)
    if not partitions or lookback_hours <= 0:
        return {}

    uncommitted: list[Any] = []
    for tp in partitions:
        committed = await consumer.committed(tp)
        if committed is None:
            uncommitted.append(tp)
    if not uncommitted:
        return {}

    ts = lookback_timestamp_ms(hours=lookback_hours, now_ms=now_ms)
    found = await consumer.offsets_for_times({tp: ts for tp in uncommitted})
    if not isinstance(found, dict):
        found = {}

    sought: dict[Any, int] = {}
    missing: list[Any] = []
    for tp in uncommitted:
        info = found.get(tp)
        offset = getattr(info, "offset", None) if info is not None else None
        # aiokafka / Kafka use offset=-1 when no message exists at/after the timestamp.
        if offset is None or int(offset) < 0:
            missing.append(tp)
            continue
        consumer.seek(tp, int(offset))
        sought[tp] = int(offset)
        log.debug(
            "whatsapp_jit_hold kafka lookback seek tp=%s offset=%s hours=%s",
            tp,
            offset,
            lookback_hours,
        )

    if missing:
        ends = await consumer.end_offsets(missing)
        for tp in missing:
            end = int((ends or {}).get(tp) or 0)
            consumer.seek(tp, end)
            sought[tp] = end
            log.debug(
                "whatsapp_jit_hold kafka lookback no message in window tp=%s seek_end=%s hours=%s",
                tp,
                end,
                lookback_hours,
            )
    return sought


async def run() -> None:
    bootstrap_raw = (settings.whatsapp_jit_hold_kafka_bootstrap_servers or "").strip()
    topic = (settings.whatsapp_jit_hold_kafka_topic or "").strip()
    group = (settings.whatsapp_jit_hold_kafka_consumer_group or "").strip()
    bootstrap = normalize_kafka_bootstrap_servers(bootstrap_raw)
    if not bootstrap or not topic:
        log.warning("whatsapp_jit_hold kafka not configured — consumer idle")
        await _stop.wait()
        return

    try:
        from aiokafka import AIOKafkaConsumer
        from aiokafka.structs import OffsetAndMetadata, TopicPartition
        try:
            from aiokafka.abc import ConsumerRebalanceListener as _RebalanceBase
        except ImportError:
            _RebalanceBase = object  # type: ignore[misc,assignment]
    except ImportError as e:
        log.error("aiokafka not installed: %s", e)
        await _stop.wait()
        return

    lookback_hours = int(settings.whatsapp_jit_hold_kafka_lookback_hours)

    class _Listener(_RebalanceBase):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            self._hours = lookback_hours

        async def on_partitions_revoked(self, revoked: Iterable[Any]) -> None:
            return None

        async def on_partitions_assigned(self, assigned: Iterable[Any]) -> None:
            await seek_uncommitted_partitions_to_lookback(
                consumer,
                assigned,
                lookback_hours=self._hours,
            )

    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        group_id=group or "agentos-whatsapp-jit-hold",
        enable_auto_commit=False,
        auto_offset_reset="latest",
        value_deserializer=lambda v: v,
    )
    consumer.subscribe([topic], listener=_Listener())
    await consumer.start()
    log.info(
        "whatsapp_jit_hold kafka consumer started topic=%s group=%s brokers=%s lookback_hours=%s",
        topic,
        group or "agentos-whatsapp-jit-hold",
        len(bootstrap),
        lookback_hours,
    )
    try:
        fail_streak: dict[tuple[str, int], int] = {}
        pause_until: dict[tuple[str, int], float] = {}
        while not _stop.is_set():
            try:
                records = await asyncio.wait_for(
                    consumer.getmany(timeout_ms=1000, max_records=10),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                continue
            now = time.monotonic()
            for _tp, messages in records.items():
                for msg in messages:
                    tp = TopicPartition(msg.topic, msg.partition)
                    streak_key = (msg.topic, msg.partition)
                    if pause_until.get(streak_key, 0.0) > now:
                        consumer.seek(tp, msg.offset)
                        break
                    try:
                        text = decode_kafka_value(msg.value)
                    except UnicodeDecodeError:
                        log.error(
                            "kafka utf-8 poison topic=%s partition=%s offset=%s — skipping",
                            msg.topic,
                            msg.partition,
                            msg.offset,
                        )
                        await consumer.commit({tp: OffsetAndMetadata(msg.offset + 1, "")})
                        fail_streak.pop(streak_key, None)
                        pause_until.pop(streak_key, None)
                        continue
                    try:
                        await process_order_status_raw(text)
                    except Exception:
                        n = fail_streak.get(streak_key, 0) + 1
                        fail_streak[streak_key] = n
                        delay = process_failure_backoff_sec(n)
                        log.exception(
                            "kafka message processing failed topic=%s partition=%s offset=%s "
                            "streak=%s backoff_sec=%s — seeking back; will not commit past this offset",
                            msg.topic,
                            msg.partition,
                            msg.offset,
                            n,
                            delay,
                        )
                        # Stop this partition batch so a later success cannot commit
                        # past the failure. Seek so the next poll redelivers from here.
                        # Do not sleep the full backoff here — that stalls every
                        # other partition on this consumer.
                        consumer.seek(tp, msg.offset)
                        pause_until[streak_key] = time.monotonic() + delay
                        break
                    fail_streak.pop(streak_key, None)
                    pause_until.pop(streak_key, None)
                    await consumer.commit({tp: OffsetAndMetadata(msg.offset + 1, "")})
            remaining = [t - time.monotonic() for t in pause_until.values() if t > time.monotonic()]
            if remaining:
                await _wait_stop_or_sleep(min(1.0, min(remaining)))
    finally:
        await consumer.stop()
        log.info("whatsapp_jit_hold kafka consumer stopped")
