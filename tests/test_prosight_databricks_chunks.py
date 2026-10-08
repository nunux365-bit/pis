"""Unit tests for EXTERNAL_LINKS chunk pagination in the Prosight Databricks sync.

Regression cover for unbounded paging: the stitcher followed `next_chunk_index`
for as long as the server kept offering one, accumulating every row in memory
with no cap on chunk count, bytes or wall-clock — so a source-side blow-up
became an OOM (or an hours-long sync) instead of a failed sync.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.optimus.prosight.databricks_sync import (
    DatabricksSyncError,
    _materialize_result,
)
from app.config.settings import settings

MODULE = "app.agents.optimus.prosight.databricks_sync"


class _FakeResponse:
    def __init__(self, payload: Any, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _link(idx: int, *, last: bool = False) -> dict[str, Any]:
    link: dict[str, Any] = {
        "chunk_index": idx,
        "external_link": f"https://presigned.example/chunk/{idx}",
    }
    if not last:
        link["next_chunk_index"] = idx + 1
    return link


class _FakeClient:
    """A warehouse that serves `total` chunks, or never stops if total is None."""

    def __init__(self, total: int | None = None, chunk_bytes: int = 1024) -> None:
        self.total = total
        self.chunk_bytes = chunk_bytes
        self.chunk_fetches = 0

    async def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        if "/result/chunks/" in url:
            idx = int(url.rsplit("/", 1)[1])
            last = self.total is not None and idx >= self.total - 1
            return _FakeResponse({"external_links": [_link(idx, last=last)]})
        # Presigned chunk download — one row per chunk.
        self.chunk_fetches += 1
        idx = int(url.rsplit("/", 1)[1])
        return _FakeResponse([[f"row-{idx}"]], b"x" * self.chunk_bytes)


def _result(manifest: dict | None = None) -> dict[str, Any]:
    return {
        "statement_id": "stmt-1",
        "manifest": manifest or {},
        "result": {"external_links": [_link(0)]},
    }


def _limits(monkeypatch, *, chunks: int = 1000, mb: int = 1024, seconds: int = 600) -> None:
    monkeypatch.setattr(settings, "prosight_databricks_max_result_chunks", chunks)
    monkeypatch.setattr(settings, "prosight_databricks_max_result_mb", mb)
    monkeypatch.setattr(settings, "prosight_databricks_materialize_timeout_seconds", seconds)


async def test_inline_result_is_returned_untouched(monkeypatch) -> None:
    _limits(monkeypatch)
    result = {"result": {"data_array": [["a"]]}}

    assert await _materialize_result(_FakeClient(), "host", {}, result) is result


async def test_chunks_are_stitched_in_order_until_the_chain_ends(monkeypatch) -> None:
    _limits(monkeypatch)
    client = _FakeClient(total=3)

    out = await _materialize_result(client, "host", {}, _result())

    assert out["result"]["data_array"] == [["row-0"], ["row-1"], ["row-2"]]
    assert client.chunk_fetches == 3


async def test_endless_next_chunk_index_trips_the_chunk_cap(monkeypatch) -> None:
    """The server decides when to stop offering chunks; the client must not rely on it."""
    _limits(monkeypatch, chunks=5)
    client = _FakeClient(total=None)

    with pytest.raises(DatabricksSyncError, match="5-chunk cap"):
        await _materialize_result(client, "host", {}, _result())

    assert client.chunk_fetches == 5


async def test_oversized_result_trips_the_byte_cap_mid_walk(monkeypatch) -> None:
    _limits(monkeypatch, mb=1)
    client = _FakeClient(total=None, chunk_bytes=512 * 1024)

    with pytest.raises(DatabricksSyncError, match="1 MB cap"):
        await _materialize_result(client, "host", {}, _result())

    # Two chunks fit in the budget; the third crosses it and stops the walk.
    assert client.chunk_fetches == 3


async def test_manifest_size_fails_before_any_chunk_is_downloaded(monkeypatch) -> None:
    """The manifest sizes the result up front — no reason to page for minutes first."""
    _limits(monkeypatch, mb=1)
    client = _FakeClient(total=2)

    with pytest.raises(DatabricksSyncError, match="over the 1 MB cap"):
        await _materialize_result(
            client, "host", {}, _result({"total_byte_count": 50 * 1024 * 1024})
        )

    assert client.chunk_fetches == 0


async def test_manifest_chunk_count_fails_before_any_chunk_is_downloaded(monkeypatch) -> None:
    _limits(monkeypatch, chunks=10)
    client = _FakeClient(total=2)

    with pytest.raises(DatabricksSyncError, match="over the 10-chunk cap"):
        await _materialize_result(client, "host", {}, _result({"total_chunk_count": 99}))

    assert client.chunk_fetches == 0


async def test_slow_walk_stops_at_the_deadline(monkeypatch) -> None:
    """Per-request timeouts don't bound the walk — 200 chunks x 120s is hours."""

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            self.now += 100.0
            return self.now

    monkeypatch.setattr(f"{MODULE}.time", _Clock())
    _limits(monkeypatch, seconds=250)
    client = _FakeClient(total=None)

    with pytest.raises(DatabricksSyncError, match="Timed out after 250s"):
        await _materialize_result(client, "host", {}, _result())

    assert client.chunk_fetches == 2


@pytest.mark.parametrize(
    "name",
    [
        "prosight_databricks_max_result_chunks",
        "prosight_databricks_max_result_mb",
        "prosight_databricks_materialize_timeout_seconds",
    ],
)
async def test_non_positive_limits_are_rejected(monkeypatch, name: str) -> None:
    _limits(monkeypatch)
    monkeypatch.setattr(settings, name, 0)

    with pytest.raises(DatabricksSyncError, match="expected a positive integer"):
        await _materialize_result(_FakeClient(total=1), "host", {}, _result())
