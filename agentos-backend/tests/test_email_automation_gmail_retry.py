"""Gmail SA retry helper — bounded backoff for 429 / 5xx, no retry on 4xx."""

from __future__ import annotations

from app.email_automation import gmail_sa


class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status: int, msg: str = "boom") -> None:
        super().__init__(msg)
        self.resp = _FakeResp(status)


def _install_fake_http_error(monkeypatch):
    """Make ``_is_retryable`` think any ``_FakeHttpError`` is a real ``HttpError``."""

    def fake_is_retryable(exc):  # noqa: ANN001
        if not isinstance(exc, _FakeHttpError):
            return False
        return exc.resp.status in {429, 500, 502, 503, 504}

    monkeypatch.setattr(gmail_sa, "_is_retryable", fake_is_retryable)


def test_retry_returns_quickly_on_success(monkeypatch):
    monkeypatch.setattr(gmail_sa.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        return "ok"

    assert gmail_sa._retry("op", op) == "ok"
    assert calls["n"] == 1


def test_retry_retries_on_5xx_then_succeeds(monkeypatch):
    _install_fake_http_error(monkeypatch)
    monkeypatch.setattr(gmail_sa.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHttpError(503)
        return "ok"

    assert gmail_sa._retry("op", op) == "ok"
    assert calls["n"] == 3


def test_retry_does_not_retry_on_4xx(monkeypatch):
    _install_fake_http_error(monkeypatch)
    monkeypatch.setattr(gmail_sa.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        raise _FakeHttpError(403)

    try:
        gmail_sa._retry("op", op)
        assert False, "expected raise"
    except _FakeHttpError:
        pass
    assert calls["n"] == 1


def test_retry_gives_up_after_max_attempts(monkeypatch):
    _install_fake_http_error(monkeypatch)
    monkeypatch.setattr(gmail_sa.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        raise _FakeHttpError(503)

    try:
        gmail_sa._retry("op", op)
        assert False, "expected raise"
    except _FakeHttpError:
        pass
    assert calls["n"] == gmail_sa._MAX_ATTEMPTS


def test_retry_absorbs_transient_transport_errors(monkeypatch):
    """``httplib2`` can raise ``BrokenPipeError`` / ``ConnectionResetError`` /
    ``SSLError`` mid-request when Google drops a keep-alive socket. The inner
    retry must absorb those (they are **not** ``HttpError`` subclasses) so the
    scan tick survives a single blip instead of falling all the way through to
    the cron wrapper."""

    import socket
    import ssl

    monkeypatch.setattr(gmail_sa.time, "sleep", lambda _s: None)

    for transient in (
        BrokenPipeError(32, "Broken pipe"),
        ConnectionResetError(54, "Connection reset by peer"),
        ssl.SSLError("TLS blip"),
        socket.timeout("read timed out"),
        TimeoutError("handshake stalled"),
    ):
        calls = {"n": 0}

        def op(_e=transient):
            calls["n"] += 1
            if calls["n"] < 2:
                raise _e
            return "ok"

        assert gmail_sa._retry("op", op) == "ok", type(transient).__name__
        assert calls["n"] == 2, type(transient).__name__
