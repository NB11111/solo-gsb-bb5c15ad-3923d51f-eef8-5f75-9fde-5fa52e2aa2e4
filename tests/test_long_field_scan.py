"""Regression tests: overlong variable-length fields (AI 10/21).

Parsing a variable-length field must be a single linear scan that stops
as soon as more than 20 decoded characters are confirmed, for both
carriers (element strings and Digital Link URIs) and for every encoding
form (plain ASCII, direct Unicode, percent-encoded UTF-8). Overlong
input must fail fast with 422 FIELD_TOO_LONG whose offset is the raw
UTF-8 byte offset of the first byte of the 21st decoded character.
"""

import time

from fastapi.testclient import TestClient

from app.gs1 import GS, GS1ParseError, parse
from app.main import app

client = TestClient(app)

DL_BASE = "https://id.gs1.org"
DL_VALUE_OFFSET = len(DL_BASE) + len("/10/")  # byte offset where the value starts


def parse_http(data):
    return client.post("/parse", json={"data": data})


# ------------------------------------------------------------- error contract


def test_element_string_huge_ascii_field_fails_fast():
    resp = parse_http("10" + "A" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22  # first byte of the 21st character


def test_element_string_huge_field_before_separator():
    resp = parse_http("10" + "A" * 100_000 + GS + "21SN0001")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22


def test_element_string_serial_huge_field():
    resp = parse_http("21" + "B" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22


def test_element_string_direct_unicode_offset_counts_bytes():
    # 20 two-byte characters, then the 21st starts at byte 2 + 20*2 = 42.
    resp = parse_http("10" + "é" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 42


def test_element_string_mixed_unicode_offset():
    # 20 two-byte characters followed by a three-byte character: the offset
    # addresses the first byte of that 21st character.
    resp = parse_http("10" + "é" * 20 + "中")
    assert resp.status_code == 422
    assert resp.json()["detail"]["offset"] == 42


def test_digital_link_huge_ascii_value():
    resp = parse_http(f"{DL_BASE}/10/" + "A" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == DL_VALUE_OFFSET + 20


def test_digital_link_huge_percent_encoded_ascii():
    resp = parse_http(f"{DL_BASE}/10/" + "%41" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == DL_VALUE_OFFSET + 20 * 3


def test_digital_link_huge_percent_encoded_utf8():
    # Each decoded character comes from a 6-byte %C3%A9 sequence.
    resp = parse_http(f"{DL_BASE}/10/" + "%C3%A9" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == DL_VALUE_OFFSET + 20 * 6


def test_digital_link_direct_unicode_value():
    resp = parse_http(f"{DL_BASE}/10/" + "é" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == DL_VALUE_OFFSET + 20 * 2


def test_digital_link_query_value_huge():
    resp = parse_http(f"{DL_BASE}?21=" + "A" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == len(DL_BASE) + len("?21=") + 20


def test_overlong_error_structure_is_unchanged():
    # Element string: same keys as any other parse error.
    detail = parse_http("10" + "A" * 100_000).json()["detail"]
    assert set(detail) == {"error", "message", "offset", "expected"}
    # Digital Link keeps its "found" member.
    detail = parse_http(f"{DL_BASE}/10/" + "A" * 100_000).json()["detail"]
    assert set(detail) == {"error", "message", "offset", "expected", "found"}


# ------------------------------------------------------- validation ordering


def test_bad_percent_encoding_before_21st_char_still_wins():
    resp = parse_http(f"{DL_BASE}/10/" + "A" * 5 + "%ZZ" + "B" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == DL_VALUE_OFFSET + 5


def test_malformed_escape_outranks_earlier_invalid_utf8():
    # Historical order: a malformed % escape anywhere in the (scanned)
    # span beats an invalid-UTF-8 failure, wherever the bytes are.
    resp = parse_http(f"{DL_BASE}/10/%FF" + "A" * 3 + "%ZZ")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == DL_VALUE_OFFSET + 3 + 3  # the '%' of %ZZ
    assert detail["found"] == "%ZZ"


def test_invalid_utf8_reported_when_escapes_are_well_formed():
    resp = parse_http(f"{DL_BASE}/10/AB%FFCD")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == DL_VALUE_OFFSET + 2
    assert detail["found"] == "0xFF"


def test_empty_field_offset_is_value_start():
    resp = parse_http("10" + GS + "21ABC")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_EMPTY"
    assert detail["offset"] == 2


def test_invalid_utf8_before_21st_char_still_wins():
    resp = parse_http(f"{DL_BASE}/10/" + "A" * 20 + "%FF")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == DL_VALUE_OFFSET + 20


def test_dangling_multibyte_sequence_still_reported():
    resp = parse_http(f"{DL_BASE}/10/" + "A" * 19 + "%C3")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == DL_VALUE_OFFSET + 19


def test_length_check_still_precedes_charset_check():
    # A field that is both too long and contains an illegal character is
    # reported as FIELD_TOO_LONG (length is validated before the charset).
    resp = parse_http("10#" + "A" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22


def test_charset_error_within_20_characters_unchanged():
    resp = parse_http("10ABC#DEF")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 5


# ----------------------------------------------------------- boundary values


def test_exactly_20_characters_still_accepted_both_carriers():
    assert parse_http("10" + "A" * 20).status_code == 200
    assert parse_http(f"{DL_BASE}/10/" + "A" * 20).status_code == 200
    assert parse_http(f"{DL_BASE}/10/" + "%41" * 20).status_code == 200


def test_exactly_20_multibyte_characters_count_as_characters_not_bytes():
    # 20 two-byte characters are 40 bytes but only 20 decoded characters:
    # the length check must pass (no FIELD_TOO_LONG) and the failure must
    # be the charset violation of the first multibyte character.
    resp = parse_http("10" + "é" * 20)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 2  # first byte of the first 'é'


def test_multibyte_charset_error_offset_is_byte_exact():
    # 19 ASCII characters, then 'é' as the 20th: byte offset 2 + 19 = 21.
    resp = parse_http("10" + "A" * 19 + "é")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 21


def test_legal_values_after_long_scan_unchanged():
    # A long but legal element string keeps exact element offsets.
    resp = parse_http("10" + "A" * 20 + GS + "21ABC")
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert elements[1]["ai"] == "21"
    assert elements[1]["offset"] == 2 + 20 + 1  # AI + value + separator


def test_21st_character_percent_encoded_multibyte():
    # 20 ASCII characters, then %C3%A9: offset points at the '%'.
    resp = parse_http(f"{DL_BASE}/10/" + "A" * 20 + "%C3%A9")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == DL_VALUE_OFFSET + 20


def test_many_fixed_fields_offsets_stay_exact():
    # Thousands of elements: offsets must be exact (linear scan, no drift).
    count = 5_000
    resp = parse_http("3103002513" * count)
    assert resp.status_code == 200
    elements = resp.json()["elements"]
    assert len(elements) == count
    assert elements[-1]["offset"] == (count - 1) * 10


# -------------------------------------------------------------- performance


def test_huge_element_string_parses_in_linear_time():
    # Quadratic prefix recomputation needed minutes here; a single scan
    # with early stop is milliseconds. The bound is deliberately generous.
    start = time.perf_counter()
    try:
        parse("10" + "A" * 1_000_000)
    except GS1ParseError as exc:
        assert exc.code == "FIELD_TOO_LONG"
        assert exc.offset == 22
    assert time.perf_counter() - start < 2.0


def test_huge_digital_link_value_parses_in_linear_time():
    start = time.perf_counter()
    try:
        parse(f"{DL_BASE}/10/" + "%41" * 300_000)
    except GS1ParseError as exc:
        assert exc.code == "FIELD_TOO_LONG"
        assert exc.offset == DL_VALUE_OFFSET + 20 * 3
    assert time.perf_counter() - start < 2.0


def test_many_elements_parse_in_linear_time():
    start = time.perf_counter()
    elements = parse("3103002513" * 20_000)
    assert len(elements) == 20_000
    assert time.perf_counter() - start < 2.0
