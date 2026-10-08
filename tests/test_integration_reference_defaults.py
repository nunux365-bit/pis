"""DB integration default picker (plant|sloc, material priority)."""

from scripts.integration.integration_reference_defaults import _sloc_for_form, _sloc_for_sap


def test_sloc_for_sap_splits_plant_pipe() -> None:
    assert _sloc_for_sap("H001|3021", plant="H001") == "3021"


def test_sloc_for_sap_plain_code() -> None:
    assert _sloc_for_sap("3021", plant="H001") == "3021"


def test_sloc_for_form_keeps_composite() -> None:
    assert _sloc_for_form("H001|3021", plant="H001") == "H001|3021"


def test_sloc_for_form_prefixes_bare_sloc() -> None:
    assert _sloc_for_form("3021", plant="H001") == "H001|3021"
