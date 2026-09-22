from workup.rules import REASON_CODES, get_rule


def test_all_fifteen_codes_present():
    assert len(REASON_CODES) == 15
    visa = [k for k in REASON_CODES if k.startswith("visa:")]
    mc = [k for k in REASON_CODES if k.startswith("mastercard:")]
    assert len(visa) == 9 and len(mc) == 6


def test_logic_and_required_count():
    assert get_rule("visa", "10.4").logic == "any_two"
    assert get_rule("visa", "10.4").required_count == 2
    assert get_rule("mastercard", "4837").required_count == 2
    assert get_rule("mastercard", "4863").logic == "any_one"
    assert get_rule("visa", "13.6").logic == "any_one"
    assert get_rule("visa", "13.1").logic == "all"
    assert get_rule("visa", "13.1").required_count == 4


def test_non_representable_codes():
    for scheme, code in [("visa", "10.5"), ("mastercard", "4870")]:
        rc = get_rule(scheme, code)
        assert rc.logic == "non_representable"
        assert rc.requirements == ()
        assert rc.required_count == 0
        assert rc.note


def test_every_dataset_case_has_a_rule():
    import json
    from pathlib import Path

    cases = json.loads((Path(__file__).parent.parent / "data" / "cases.json").read_text())
    for c in cases:
        get_rule(c["scheme"], c["reason_code"])


def test_unknown_code_raises():
    import pytest

    with pytest.raises(KeyError):
        get_rule("visa", "99.9")
