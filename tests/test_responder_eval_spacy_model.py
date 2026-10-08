"""Tests for lazy spaCy model provisioning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.responder_eval.spacy_model import (
    ensure_spacy_model,
    reset_spacy_model_cache_for_tests,
    spacy_model_installed,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_spacy_model_cache_for_tests()
    yield
    reset_spacy_model_cache_for_tests()


def test_spacy_model_installed_delegates_to_spacy_util():
    with patch("spacy.util.is_package", return_value=True) as mock_is_package:
        assert spacy_model_installed("en_core_web_sm") is True
    mock_is_package.assert_called_once_with("en_core_web_sm")


def test_ensure_spacy_model_skips_download_when_installed():
    with (
        patch("app.agents.responder_eval.spacy_model.spacy_model_installed", return_value=True),
        patch("spacy.cli.download") as mock_download,
    ):
        ensure_spacy_model("en_core_web_sm")
    mock_download.assert_not_called()


def test_ensure_spacy_model_downloads_when_missing():
    mock_download = MagicMock()
    with (
        patch(
            "app.agents.responder_eval.spacy_model.spacy_model_installed",
            side_effect=[False, True],
        ),
        patch("spacy.cli.download", mock_download),
    ):
        ensure_spacy_model("en_core_web_sm")

    mock_download.assert_called_once_with("en_core_web_sm")


def test_ensure_spacy_model_raises_when_auto_download_disabled():
    with patch("app.agents.responder_eval.spacy_model.spacy_model_installed", return_value=False):
        with pytest.raises(OSError, match="not installed"):
            ensure_spacy_model("en_core_web_sm", auto_download=False)


def test_build_analyzer_calls_ensure_spacy_model(monkeypatch):
    from app.agents.responder_eval import presidio_engine
    from app.config.settings import settings

    monkeypatch.setattr(settings, "responder_eval_spacy_model", "en_core_web_sm")
    presidio_engine.reset_presidio_engines_for_tests()

    mock_analyzer = MagicMock()
    mock_provider = MagicMock()
    mock_provider.create_engine.return_value = MagicMock()
    mock_ensure = MagicMock()
    captured: dict = {}

    def _provider_ctor(*, nlp_configuration=None, **_kwargs):
        captured["nlp_configuration"] = nlp_configuration
        return mock_provider

    with (
        patch("app.agents.responder_eval.presidio_engine.ensure_spacy_model", mock_ensure),
        patch("app.agents.responder_eval.presidio_engine.NlpEngineProvider", side_effect=_provider_ctor),
        patch("app.agents.responder_eval.presidio_engine.AnalyzerEngine", return_value=mock_analyzer),
    ):
        presidio_engine._build_analyzer()

    mock_ensure.assert_called_once_with(
        "en_core_web_sm",
        auto_download=settings.responder_eval_spacy_auto_download,
    )
    ner_cfg = (captured.get("nlp_configuration") or {}).get("ner_model_configuration") or {}
    assert "CARDINAL" in ner_cfg.get("labels_to_ignore", [])
