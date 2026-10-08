import pytest

from app.tools import rest_api


def test_blocks_private_ip_hostname():
    with pytest.raises(rest_api.RestApiError, match="not allowed"):
        rest_api.assert_url_allowed("http://192.168.1.1/x")


def test_blocks_metadata_host():
    with pytest.raises(rest_api.RestApiError, match="not allowed"):
        rest_api.assert_url_allowed("http://metadata.google.internal/computeMetadata/v1/")


def test_allows_public_https(monkeypatch):
    monkeypatch.setattr(rest_api.settings, "external_http_allowlist", [])
    rest_api.assert_url_allowed("https://example.com/api")


def test_allowlist_enforced(monkeypatch):
    monkeypatch.setattr(rest_api.settings, "external_http_allowlist", ["api.stripe.com"])
    rest_api.assert_url_allowed("https://api.stripe.com/v1/charges")
    with pytest.raises(rest_api.RestApiError, match="allowlist"):
        rest_api.assert_url_allowed("https://example.com/")
