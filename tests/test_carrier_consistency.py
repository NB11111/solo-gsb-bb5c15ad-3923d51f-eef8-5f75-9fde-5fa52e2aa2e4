"""Cross-carrier consistency: element strings and Digital Link URIs.

The element-string parser and the Digital Link parser share one
recognizer and one value validator. These tests construct the same
logical content in both carriers and assert the parsed elements are
identical, and that every malformed value produces the same error code
through both carriers.
"""

import pytest

from app.gs1 import GS, GS1ParseError, parse

DL_BASE = "https://id.gs1.org"
GTIN_OK = "09501101530003"


def _element_string(pairs):
    """Render (ai, value) pairs as a raw element string.

    Fixed-length AIs need no delimiter; variable AIs are terminated by
    GS unless they are the final pair.
    """
    out = ""
    for index, (ai, value) in enumerate(pairs):
        out += ai + value
        is_fixed = ai == "01" or ai == "17" or ai[:3] in ("310", "320")
        if not is_fixed and index != len(pairs) - 1:
            out += GS
    return out


def _digital_link(pairs):
    from urllib.parse import quote

    # Encode every value fully (including '/', which would otherwise
    # split the path); the parser percent-decodes it back before
    # validation, so the logical value stays identical.
    return DL_BASE + "".join(
        f"/{quote(ai, safe='')}/{quote(value, safe='')}" for ai, value in pairs
    )


def _essential(elements):
    """Fields both carriers must agree on (URI offsets differ, by design)."""
    return [
        (e.ai, e.name, e.value, e.decimal_value, e.unit) for e in elements
    ]


# ------------------------------------------------------------------ happy paths


@pytest.mark.parametrize(
    "pairs",
    [
        [("01", GTIN_OK)],
        [("01", GTIN_OK), ("17", "250131"), ("10", "ABC123"), ("21", "SN0001")],
        [("17", "240229")],  # leap day
        [("17", "250200")],  # day 00 = last day of month
        [("10", "A" * 20)],
        [("21", "SN-001/A:OK_+x")],
        [("3103", "002513")],
        [("3202", "002513")],
        [("3109", "000001")],
        [("3200", "000000")],
        [("01", GTIN_OK), ("17", "250131"), ("3101", "012345")],
        [("3102", "123456"), ("10", "ABC")],
        [("21", "SN0001"), ("3203", "002513")],
        [("3103", "002513"), ("3203", "002513")],  # kg and lb coexist
    ],
)
def test_same_elements_from_both_carriers(pairs):
    from_string = parse(_element_string(pairs))
    from_uri = parse(_digital_link(pairs))
    assert _essential(from_string) == _essential(from_uri) == [
        (
            ai,
            {"01": "GTIN", "17": "EXPIRY", "10": "BATCH/LOT", "21": "SERIAL"}.get(
                ai, f"NET WEIGHT ({'KG' if ai.startswith('310') else 'LB'})"
            ),
            value,
            (
                int(value) / 10 ** int(ai[3])
                if ai[:3] in ("310", "320")
                else None
            ),
            "kg" if ai.startswith("310") else ("lb" if ai.startswith("320") else None),
        )
        for ai, value in pairs
    ]


def test_order_is_preserved_in_both_carriers():
    pairs = [("21", "S1"), ("10", "B1"), ("17", "250131"), ("01", GTIN_OK)]
    assert [e.ai for e in parse(_element_string(pairs))] == [
        "21",
        "10",
        "17",
        "01",
    ]
    assert [e.ai for e in parse(_digital_link(pairs))] == ["21", "10", "17", "01"]


# ------------------------------------------------------------- identical errors


def _error_code(data):
    with pytest.raises(GS1ParseError) as exc_info:
        parse(data)
    return exc_info.value.code


def test_bad_check_digit_same_code_in_both_carriers():
    bad = "09501101530004"
    assert _error_code(f"01{bad}") == "INVALID_CHECK_DIGIT"
    assert _error_code(f"{DL_BASE}/01/{bad}") == "INVALID_CHECK_DIGIT"


def test_bad_month_same_code_in_both_carriers():
    assert _error_code("17251331") == "INVALID_DATE"
    assert _error_code(f"{DL_BASE}/17/251331") == "INVALID_DATE"


def test_bad_day_same_code_in_both_carriers():
    assert _error_code("17250230") == "INVALID_DATE"
    assert _error_code(f"{DL_BASE}/17/250230") == "INVALID_DATE"


def test_weight_value_too_short_same_code_in_both_carriers():
    assert _error_code("310300251") == "FIELD_TOO_SHORT"
    assert _error_code(f"{DL_BASE}/3103/00251") == "FIELD_TOO_SHORT"


def test_weight_value_too_long_only_reachable_in_uri():
    # Element strings self-delimit fixed fields, so the 7th digit would be
    # read as the following AI; the URI can carry an over-long segment.
    assert _error_code(f"{DL_BASE}/3103/0025136") == "FIELD_TOO_LONG"


def test_weight_non_digit_value_same_code_in_both_carriers():
    assert _error_code("31030025A3") == "INVALID_CHARACTER"
    assert _error_code(f"{DL_BASE}/3103/0025A3") == "INVALID_CHARACTER"


def test_batch_too_long_same_code_in_both_carriers():
    assert _error_code("10" + "A" * 21) == "FIELD_TOO_LONG"
    assert _error_code(f"{DL_BASE}/10/{'A' * 21}") == "FIELD_TOO_LONG"


def test_batch_empty_same_code_in_both_carriers():
    assert _error_code(f"10{GS}21ABC") == "FIELD_EMPTY"
    assert _error_code(f"{DL_BASE}?10=") == "FIELD_EMPTY"


def test_batch_illegal_character_same_code_in_both_carriers():
    assert _error_code("10ABC#DEF") == "INVALID_CHARACTER"
    assert _error_code(f"{DL_BASE}/10/ABC%23DEF") == "INVALID_CHARACTER"


def test_unknown_ai_same_code_in_both_carriers():
    assert _error_code("99ABC") == "UNSUPPORTED_AI"
    assert _error_code(f"{DL_BASE}/99/abc") == "UNSUPPORTED_AI"
    assert _error_code(f"{DL_BASE}/999/abc") == "UNSUPPORTED_AI"
    assert _error_code(f"{DL_BASE}?99=abc") == "UNSUPPORTED_AI"


def test_non_numeric_ai_same_code_in_both_carriers():
    assert _error_code(f"{DL_BASE}/gtin/{GTIN_OK}") == "INVALID_AI"
    assert _error_code("1A") == "INVALID_AI"


@pytest.mark.parametrize("prefix", ["310", "320"])
def test_family_missing_precision_same_code_in_both_carriers(prefix):
    assert _error_code(prefix) == "INVALID_PRECISION"
    assert _error_code(f"{DL_BASE}/{prefix}/002513") == "INVALID_PRECISION"
    assert _error_code(f"{DL_BASE}?{prefix}=002513") == "INVALID_PRECISION"
    assert _error_code(f"{prefix}{GS}17250131") == "INVALID_PRECISION"


@pytest.mark.parametrize("prefix", ["310", "320"])
def test_family_non_digit_precision_same_code_in_both_carriers(prefix):
    assert _error_code(f"{prefix}A002513") == "INVALID_PRECISION"
    assert _error_code(f"{DL_BASE}/{prefix}x/002513") == "INVALID_PRECISION"


def test_family_longest_match_non_member_prefix_falls_back():
    # "3110" is a numeric non-family token: element string reports the
    # 2-character head "31", URI reports the whole key; both unsupported.
    assert _error_code("3110000000") == "UNSUPPORTED_AI"
    assert _error_code(f"{DL_BASE}/3110/000000") == "UNSUPPORTED_AI"
    # A well-formed 4-character key that is simply not declared is
    # unsupported as a complete key in the URI.
    assert _error_code(f"{DL_BASE}/3199/000000") == "UNSUPPORTED_AI"


def test_weight_decimal_values_agree_exactly_between_carriers():
    pairs = [
        ("3103", "002513"),
        ("3109", "000001"),
        ("3207", "000001"),
        ("3100", "012345"),
        ("3202", "002513"),
    ]
    string_elements = {e.ai: e for e in parse(_element_string(pairs))}
    uri_elements = {e.ai: e for e in parse(_digital_link(pairs))}
    assert set(string_elements) == set(uri_elements)
    for ai in string_elements:
        assert string_elements[ai].value == uri_elements[ai].value
        assert string_elements[ai].decimal_value == uri_elements[ai].decimal_value
        assert string_elements[ai].unit == uri_elements[ai].unit
        assert string_elements[ai].name == uri_elements[ai].name
