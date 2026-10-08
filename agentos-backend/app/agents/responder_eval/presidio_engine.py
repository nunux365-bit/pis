"""Presidio analyzer/anonymizer for chat free-text PII (lazy singleton)."""

from __future__ import annotations

import re
import logging
import threading
from typing import Any

from app.agents.responder_eval.pii_labels import (
    HINT_ADDRESS,
    HINT_EMAIL,
    HINT_ID,
    HINT_PHONE,
    Speaker,
    person_hint,
)
from app.agents.responder_eval.pii_patterns import (
    _redact_greeting_names,
    apply_regex_redactions,
    mask_order_ids,
    unmask_order_ids,
)
from app.agents.responder_eval.spacy_model import ensure_spacy_model, reset_spacy_model_cache_for_tests
from app.config.settings import settings

log = logging.getLogger(__name__)

try:
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import (
        InAadhaarRecognizer,
        InPanRecognizer,
        InPassportRecognizer,
    )
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig

    try:
        from presidio_analyzer.predefined_recognizers import InUpiRecognizer
    except ImportError:
        InUpiRecognizer = None  # type: ignore[misc, assignment]
    _PRESIDIO_IMPORT_OK = True
except ImportError:
    AnalyzerEngine = None  # type: ignore[misc, assignment]
    Pattern = None  # type: ignore[misc, assignment]
    PatternRecognizer = None  # type: ignore[misc, assignment]
    NlpEngineProvider = None  # type: ignore[misc, assignment]
    InAadhaarRecognizer = None  # type: ignore[misc, assignment]
    InPanRecognizer = None  # type: ignore[misc, assignment]
    InPassportRecognizer = None  # type: ignore[misc, assignment]
    InUpiRecognizer = None  # type: ignore[misc, assignment]
    AnonymizerEngine = None  # type: ignore[misc, assignment]
    OperatorConfig = None  # type: ignore[misc, assignment]
    _PRESIDIO_IMPORT_OK = False
    log.warning(
        "presidio not installed; responder_eval uses regex-only redaction. "
        "Reinstall backend dependencies (presidio-analyzer, presidio-anonymizer, spacy)."
    )

_PERSON_SKIP_TOKEN_RE = re.compile(
    r"^(?:\d+%?|\d+\s*(?:g|gm|ml|mg|mcg|iu|tab|tabs|cap|caps)|spf|pack|of|x|tube|cream|gel|lotion|syrup)$",
    re.I,
)


def _skip_person_span(text: str, start: int, end: int) -> bool:
    """Drop spaCy/Presidio PERSON hits that are usually product titles, not people."""
    span = text[start:end].strip()
    if not span or len(span) <= 2:
        return True
    if re.search(r"\d", span):
        return True
    if _PERSON_SKIP_TOKEN_RE.match(span):
        return True
    window = text[max(0, start - 4): min(len(text), end + 4)]
    if "|" in window:
        return True
    return False


def _filter_analyzer_results(text: str, results: list[Any]) -> list[Any]:
    filtered: list[Any] = []
    for result in results:
        entity = getattr(result, "entity_type", None)
        if entity == "PERSON":
            start = int(getattr(result, "start", 0))
            end = int(getattr(result, "end", 0))
            if _skip_person_span(text, start, end):
                continue
        filtered.append(result)
    return filtered

_lock = threading.Lock()
_analyzer: Any = None
_anonymizer: Any = None

_BASE_ENTITIES = (
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "LOCATION",
    "IN_AADHAAR",
    "IN_PAN",
    "IN_PASSPORT",
)
_ANALYZE_ENTITIES = list(_BASE_ENTITIES)
if InUpiRecognizer is not None:
    _ANALYZE_ENTITIES.append("IN_UPI")

# spaCy NER labels with no Presidio mapping — ignore to avoid per-token WARNING spam.
_SPACY_LABELS_TO_IGNORE = (
    "CARDINAL",
    "ORDINAL",
    "QUANTITY",
    "PERCENT",
    "MONEY",
    "PRODUCT",
    "EVENT",
    "WORK_OF_ART",
    "LAW",
    "LANGUAGE",
    "FAC",
)


def presidio_available() -> bool:
    return _PRESIDIO_IMPORT_OK


def reset_presidio_engines_for_tests() -> None:
    """Test helper — clear lazy singletons."""
    global _analyzer, _anonymizer
    with _lock:
        _analyzer = None
        _anonymizer = None
    reset_spacy_model_cache_for_tests()


def _presidio_operators(speaker: Speaker) -> dict[str, Any]:
    person = person_hint(speaker)
    return {
        "PERSON": OperatorConfig("replace", {"new_value": person}),
        "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": HINT_EMAIL}),
        "PHONE_NUMBER": OperatorConfig("replace", {"new_value": HINT_PHONE}),
        "LOCATION": OperatorConfig("replace", {"new_value": HINT_ADDRESS}),
        "IN_AADHAAR": OperatorConfig("replace", {"new_value": HINT_ID}),
        "IN_PAN": OperatorConfig("replace", {"new_value": HINT_ID}),
        "IN_PASSPORT": OperatorConfig("replace", {"new_value": HINT_ID}),
        "IN_UPI": OperatorConfig("replace", {"new_value": HINT_ID}),
        "DEFAULT": OperatorConfig("replace", {"new_value": HINT_ID}),
    }


def _india_recognizers() -> list[Any]:
    recognizers: list[Any] = [
        InAadhaarRecognizer(),
        InPanRecognizer(),
        InPassportRecognizer(),
    ]
    if InUpiRecognizer is not None:
        recognizers.append(InUpiRecognizer())
    return recognizers


def _india_phone_recognizer() -> Any:
    return PatternRecognizer(
        supported_entity="PHONE_NUMBER",
        name="IN Mobile Phone",
        patterns=[
            Pattern(
                name="in_mobile",
                regex=r"(?:\+?91[\s-]?)?0?[6-9]\d{9}",
                score=0.6,
            ),
        ],
        context=["phone", "mobile", "call", "whatsapp", "contact", "number"],
        supported_language="en",
    )


def _pincode_recognizer() -> Any:
    return PatternRecognizer(
        supported_entity="LOCATION",
        name="IN Pincode",
        patterns=[Pattern(name="in_pin", regex=r"\b[1-9]\d{5}\b", score=0.35)],
        context=["pin", "pincode", "postal", "zip", "address"],
        supported_language="en",
    )


def _build_analyzer() -> Any:
    logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

    model = (settings.responder_eval_spacy_model or "en_core_web_sm").strip()
    ensure_spacy_model(model, auto_download=settings.responder_eval_spacy_auto_download)
    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": model}],
        "ner_model_configuration": {
            "labels_to_ignore": list(_SPACY_LABELS_TO_IGNORE),
        },
    }
    provider = NlpEngineProvider(nlp_configuration=configuration)
    nlp_engine = provider.create_engine()

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    for rec in _india_recognizers():
        analyzer.registry.add_recognizer(rec)
    analyzer.registry.add_recognizer(_india_phone_recognizer())
    analyzer.registry.add_recognizer(_pincode_recognizer())
    return analyzer


def _get_engines() -> tuple[Any, Any]:
    global _analyzer, _anonymizer
    if _analyzer is not None and _anonymizer is not None:
        return _analyzer, _anonymizer
    with _lock:
        if _analyzer is None:
            _analyzer = _build_analyzer()
            _anonymizer = AnonymizerEngine()
        return _analyzer, _anonymizer


def redact_free_text_presidio(text: str, *, speaker: Speaker = "customer") -> str:
    """Presidio analyze/anonymize on text that already passed regex redaction."""
    if not text or not text.strip():
        return text
    masked, placeholders = mask_order_ids(text)
    analyzer, anonymizer = _get_engines()
    results = analyzer.analyze(
        text=masked,
        language="en",
        entities=_ANALYZE_ENTITIES,
        score_threshold=0.35,
    )
    results = _filter_analyzer_results(masked, results)
    if not results:
        return unmask_order_ids(masked, placeholders)
    out = anonymizer.anonymize(
        text=masked,
        analyzer_results=results,
        operators=_presidio_operators(speaker),
    )
    return unmask_order_ids(out.text, placeholders)


def redact_free_text(text: str, *, speaker: Speaker = "customer") -> str:
    """Full free-text redaction: regex always; Presidio when enabled and installed."""
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    s = apply_regex_redactions(s, speaker=speaker)
    if settings.responder_eval_presidio_enabled and presidio_available():
        try:
            s = redact_free_text_presidio(s, speaker=speaker)
        except Exception:
            log.exception("presidio redaction failed; using regex-only result")
    return _redact_greeting_names(s, speaker)
