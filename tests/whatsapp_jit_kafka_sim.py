"""Test-only in-process Kafka simulation — runs real ``kafka_consumer.run()`` without a broker."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from app.config.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopicPartition:
    topic: str
    partition: int


@dataclass(frozen=True)
class OffsetAndMetadata:
    offset: int
    metadata: str


@dataclass(frozen=True)
class OffsetAndTimestamp:
    offset: int
    timestamp: int


@dataclass
class SimulatedKafkaMessage:
    topic: str
    partition: int
    offset: int
    value: str | bytes


@dataclass
class FakeAIOKafkaConsumer:
    """Minimal aiokafka consumer stand-in for local end-to-end simulation."""

    topic: str
    messages: list[str | bytes]
    group_id: str = "simulated"
    _pending: list[SimulatedKafkaMessage] = field(default_factory=list, init=False)
    commits: list[dict[Any, Any]] = field(default_factory=list, init=False)
    started: bool = False
    _listener: Any = field(default=None, init=False)
    _committed: dict[Any, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._pending = [
            SimulatedKafkaMessage(topic=self.topic, partition=0, offset=i, value=raw)
            for i, raw in enumerate(self.messages)
        ]

    def assignment(self) -> set[TopicPartition]:
        return {TopicPartition(self.topic, 0)}

    def subscribe(self, topics: Any = (), listener: Any = None, pattern: Any = None) -> None:
        if topics:
            self.topic = list(topics)[0]
        self._listener = listener

    async def committed(self, tp: Any) -> int | None:
        return self._committed.get(tp)

    async def offsets_for_times(self, timestamps: dict[Any, int]) -> dict[Any, OffsetAndTimestamp]:
        return {tp: OffsetAndTimestamp(0, ts) for tp, ts in timestamps.items()}

    async def end_offsets(self, tps: list[Any]) -> dict[Any, int]:
        return {tp: len(self.messages) for tp in tps}

    def seek(self, tp: Any, offset: int) -> None:
        self._pending = [
            SimulatedKafkaMessage(topic=self.topic, partition=0, offset=i, value=raw)
            for i, raw in enumerate(self.messages)
            if i >= int(offset)
        ]

    async def start(self) -> None:
        self.started = True
        if self._listener is not None:
            await self._listener.on_partitions_assigned(list(self.assignment()))
        log.info(
            "simulated kafka connected topic=%s group=%s messages=%s",
            self.topic,
            self.group_id,
            len(self._pending),
        )

    async def stop(self) -> None:
        self.started = False
        log.info("simulated kafka disconnected commits=%s", len(self.commits))

    async def getmany(self, *, timeout_ms: int = 1000, max_records: int = 10) -> dict[Any, list]:
        if not self._pending:
            await asyncio.sleep(min(timeout_ms / 1000.0, 0.05))
            return {}

        tp = TopicPartition(self.topic, 0)
        batch_size = min(max_records, len(self._pending))
        out = self._pending[:batch_size]
        self._pending = self._pending[batch_size:]
        return {tp: out}

    async def commit(self, offsets: dict[Any, Any]) -> None:
        self.commits.append(dict(offsets))


def _install_fake_aiokafka(consumer_factory: Any) -> None:
    structs = types.ModuleType("aiokafka.structs")
    structs.TopicPartition = TopicPartition
    structs.OffsetAndMetadata = OffsetAndMetadata
    structs.OffsetAndTimestamp = OffsetAndTimestamp

    aiokafka_mod = types.ModuleType("aiokafka")
    aiokafka_mod.AIOKafkaConsumer = consumer_factory
    aiokafka_mod.structs = structs

    sys.modules["aiokafka"] = aiokafka_mod
    sys.modules["aiokafka.structs"] = structs


def build_nexus_payload(
    *,
    event_id: str,
    order_id: str,
    on_hold: bool = True,
    triggered_at: str | None = None,
) -> dict[str, Any]:
    ts = triggered_at or f"2026-07-07T08:59:59.{event_id[-1:] if event_id else '0'}39+00:00"
    if on_hold:
        event: dict[str, Any] = {
            "event_type": "NON_ORDER_STATUS_UPDATE",
            "event_name": "PACKAGING",
            "sub_event_name": "ON_HOLD",
            "description": "Order On Hold at Odin",
            "triggered_at": ts,
        }
    else:
        event = {
            "event_type": "ORDER_STATUS_UPDATE",
            "event_name": "PACKAGING",
            "sub_event_name": "PACKAGING",
            "description": "Packaging your order",
            "triggered_at": ts,
        }
    return {
        "id": event_id,
        "order_id": order_id,
        "event": event,
        "order_details": {
            "basic_order_details": {
                "order_id": order_id,
                "group_order_id": order_id,
                "created_at": 1783414782,
                "source": "1mg",
            },
            "user_details": {"contact_number": "9555560920"},
            "vendor_details": {
                "id": 8212,
                "tags": {"store_type": "WAREHOUSE", "fc_type": "Fulfilment Center"},
            },
        },
    }


def build_eligibility_fixture(order_id: str) -> dict[str, Any]:
    from app.agents.whatsapp_jit_hold.fixtures import apply_fixture_overlay

    oid = order_id.strip().upper()
    phone = (settings.whatsapp_jit_hold_test_phone or "9555560920").strip()[-10:]
    return apply_fixture_overlay(
        {
            "order_id": oid,
            "order": {
                "order_id": oid,
                "user": {"number": phone, "properties": {"name": "Sim User"}},
                "order_lines": [
                    {"normalized_quantity": 9, "sku": {"sku_id": 1122085, "name": "Test SKU"}},
                ],
            },
            "allocation": {"data": {}},
            "status": {"data": {}},
        }
    )


async def _wait_for_simulation_drain(
    fake: FakeAIOKafkaConsumer,
    *,
    expected_commits: int,
    timeout_sec: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if len(fake.commits) >= expected_commits and not fake._pending:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"simulation timed out commits={len(fake.commits)}/{expected_commits} pending={len(fake._pending)}"
    )


async def run_kafka_simulation(
    *,
    order_id: str,
    messages: list[dict[str, Any]] | None = None,
    mock_meta: bool = True,
    mock_eligibility: bool = True,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    from app.agents.whatsapp_jit_hold import kafka_consumer, session_store

    oid = order_id.strip().upper()
    phone = (settings.whatsapp_jit_hold_test_phone or "9555560920").strip()[-10:]

    if messages is None:
        messages = [
            build_nexus_payload(event_id="sim-evt-1", order_id=oid, triggered_at="2026-07-07T08:59:59.001+00:00"),
            build_nexus_payload(event_id="sim-evt-1", order_id=oid, triggered_at="2026-07-07T08:59:59.001+00:00"),
            build_nexus_payload(event_id="sim-evt-2", order_id=oid, triggered_at="2026-07-07T08:59:59.002+00:00"),
            {"id": "sim-evt-3", "foo": "bar"},
        ]

    encoded = [json.dumps(m) for m in messages]
    topic = (settings.whatsapp_jit_hold_kafka_topic or "simulated-topic").strip()
    fake = FakeAIOKafkaConsumer(topic=topic, messages=encoded)

    r = session_store.get_redis()
    await r.delete(f"whatsapp_jit_hold:sent:{oid}")
    await r.delete(f"whatsapp_jit_hold:session:{phone}:{oid}")
    await r.delete(f"whatsapp_jit_hold:order_active:{oid}")
    await r.srem(f"whatsapp_jit_hold:phone_orders:{phone}", oid)
    for m in messages:
        parsed_eid = None
        ev = m.get("event") if isinstance(m.get("event"), dict) else {}
        triggered = ev.get("triggered_at")
        if triggered:
            parsed_eid = f"{oid}:ON_HOLD:{triggered}"
        eid = parsed_eid or str(m.get("id") or "")
        if eid:
            await r.delete(f"whatsapp_jit_hold:dedupe:order_status:{eid}")

    meta_send = AsyncMock(return_value={"messages": [{"id": "wamid.simulated", "message_status": "accepted"}]})

    def _consumer_factory(*args: Any, **kwargs: Any) -> FakeAIOKafkaConsumer:
        fake.group_id = str(kwargs.get("group_id") or "simulated")
        return fake

    _install_fake_aiokafka(_consumer_factory)
    kafka_consumer.reset_stop()

    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "whatsapp_jit_hold_kafka_bootstrap_servers", "simulated:9092"))
        stack.enter_context(patch.object(settings, "whatsapp_jit_hold_kafka_topic", topic))
        if mock_meta:
            stack.enter_context(
                patch("app.agents.whatsapp_jit_hold.handlers.meta_client.send_template", meta_send)
            )
        if mock_eligibility:
            fixture = build_eligibility_fixture(oid)
            stack.enter_context(
                patch(
                    "app.agents.whatsapp_jit_hold.handlers.fetch_eligibility_bundle",
                    AsyncMock(return_value=fixture),
                )
            )

        task = asyncio.create_task(kafka_consumer.run(), name="kafka_simulation")
        try:
            await _wait_for_simulation_drain(fake, expected_commits=len(encoded), timeout_sec=timeout_sec)
        finally:
            kafka_consumer.request_stop()
            await asyncio.wait_for(task, timeout=3.0)

    dedupe_keys: list[str] = []
    async for k in r.scan_iter(match="whatsapp_jit_hold:dedupe:order_status:*", count=40):
        dedupe_keys.append(k.decode() if isinstance(k, bytes) else k)

    session = await session_store.get_session(phone, oid)
    commit_summary = [
        {f"{tp.topic}:{tp.partition}": {"offset": om.offset, "metadata": om.metadata}}
        for entry in fake.commits
        for tp, om in entry.items()
    ]
    return {
        "order_id": oid,
        "messages_published": len(encoded),
        "commits": len(fake.commits),
        "meta_sends": meta_send.await_count if mock_meta else None,
        "mock_eligibility": mock_eligibility,
        "sent_lock": (await r.get(f"whatsapp_jit_hold:sent:{oid}")) or None,
        "session_state": (session or {}).get("state"),
        "dedupe_keys": sorted(dedupe_keys),
        "consumer_commits": commit_summary,
    }
