"""Regression tests: over-long AI 10/21 variable fields.

The parser used to recompute the UTF-8 byte offset of every character
of a variable-length field from the start of the input, both for
element strings and for Digital Link values -- quadratic time that let
a single ~100k-character field occupy the process for seconds to
minutes.  The parser now scans once and stops as soon as the 21st
decoded character (one past the 20-character maximum) is confirmed.

These tests pin, for both carriers and for ASCII / direct Unicode /
percent-encoded UTF-8 inputs alike:

* HTTP 422 with error ``FIELD_TOO_LONG``;
* ``offset`` pointing at the first byte of the 21st decoded character
  in the *original* input;
* the existing validation order (decoding errors, then length, then
  character set) and the existing error structure (exact character
  count in the message);
* linear, non-blocking running time on hundred-thousand-character
  fields.
"""

import time

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
DL_BASE = "https://id.gs1.org"  # 18 ASCII bytes; value of "/10/..." starts at byte 22

# Wall-clock budget for the performance regression tests.  The fixed
# parser answers these inputs in well under a second; the quadratic
# parser needed ~20s (element string) and ~15s (Digital Link) for them,
# so a generous 5s bound still separates the two orders of growth.
SLOW_PARSE_SECONDS = 5.0


def parse(data):
    return client.post("/parse", json={"data": data})


# ------------------------------------------------------- element strings


def test_element_string_serial_multibyte_too_long_offset():
    # 21 two-byte characters: the 21st starts at byte 2 + 20 * 2.
    resp = parse("21" + "é" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 42


def test_element_string_batch_four_byte_char_too_long_offset():
    # U+10000 encodes to 4 bytes: the 21st starts at byte 2 + 20 * 4.
    resp = parse("10" + "\U00010000" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 82


def test_element_string_mixed_width_too_long_offset():
    # 20 ASCII characters then one multibyte character: the 21st
    # character's first byte is byte 22 regardless of its width.
    resp = parse("10" + "A" * 20 + "é")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22


def test_element_string_too_long_before_separator_reports_count():
    resp = parse("21" + "A" * 100 + GS + "10ABC")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22
    assert "100" in detail["message"]
    assert "separator" in detail["message"]


def test_element_string_too_long_wins_over_invalid_character():
    # Length is validated before the character set: an illegal '#'
    # inside an over-long field must not change the error.
    resp = parse("10" + "ABC#D" + "A" * 100)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 22


def test_element_string_exactly_20_multibyte_chars_reaches_charset_check():
    # 20 characters is within the limit, so validation proceeds to the
    # character set (é is not in GS1 AI character set 82); the offset
    # must locate the first é, proving per-character offsets are still
    # exact for in-limit fields.
    resp = parse("10" + "é" * 20)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 2
    assert detail["found"] == "é"


def test_element_string_hundred_thousand_multibyte_chars_is_fast():
    data = "10" + "é" * 100_000
    start = time.perf_counter()
    resp = parse(data)
    elapsed = time.perf_counter() - start
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 2 + 20 * 2  # 21st decoded character
    assert "100000" in detail["message"]
    assert elapsed < SLOW_PARSE_SECONDS


# ------------------------------------------------------- Digital Link


def test_digital_link_ascii_too_long_offset():
    resp = parse(f"{DL_BASE}/10/{'A' * 21}")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 42  # 22 (value start) + 20


def test_digital_link_direct_unicode_too_long_offset():
    # Literal é in the URI: 2 UTF-8 bytes each, 21st starts at 22 + 40.
    resp = parse(f"{DL_BASE}/10/" + "é" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 62


def test_digital_link_percent_encoded_too_long_offset():
    # "%C3%A9" is é: 6 raw bytes each, 21st starts at 22 + 20 * 6.
    resp = parse(f"{DL_BASE}/10/" + "%C3%A9" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 142


def test_digital_link_percent_encoded_four_byte_too_long_offset():
    # U+10000 as "%F0%90%80%80": 12 raw bytes each, 21st at 22 + 240.
    resp = parse(f"{DL_BASE}/21/" + "%F0%90%80%80" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 262


def test_digital_link_query_value_too_long_offset():
    resp = parse(f"{DL_BASE}?21=" + "%C3%A9" * 21)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 142  # same layout as the path carrier


def test_digital_link_direct_and_encoded_unicode_agree():
    # The same logical value (21 × é) written directly and percent-
    # encoded decodes identically; both offsets name the first byte of
    # the 21st decoded character in their respective raw forms.
    direct = parse(f"{DL_BASE}/10/" + "é" * 21).json()["detail"]
    encoded = parse(f"{DL_BASE}/10/" + "%C3%A9" * 21).json()["detail"]
    assert direct["error"] == encoded["error"] == "FIELD_TOO_LONG"
    assert direct["offset"] == 22 + 20 * 2
    assert encoded["offset"] == 22 + 20 * 6
    assert direct["found"] == encoded["found"] == "21 characters"


def test_digital_link_too_long_wins_over_invalid_character():
    resp = parse(f"{DL_BASE}/10/" + "ABC%23D" + "A" * 100)  # %23 == '#'
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    # 21st decoded character: "ABC" (3 bytes) + "%23" (3 bytes) + "D"
    # (1 byte) + 15 'A's land it at byte 22 + 7 + 15 of the raw URI.
    assert detail["offset"] == 44


def test_digital_link_bad_percent_encoding_still_wins_over_length():
    # Decoding is validated before length: a malformed escape anywhere
    # in an over-long value keeps reporting INVALID_PERCENT_ENCODING.
    resp = parse(f"{DL_BASE}/10/" + "A" * 50 + "%ZZ")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == 72


def test_digital_link_invalid_utf8_still_wins_over_length():
    resp = parse(f"{DL_BASE}/10/" + "A" * 50 + "%FF")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_PERCENT_ENCODING"
    assert detail["offset"] == 72


def test_digital_link_too_long_reports_exact_decoded_count():
    resp = parse(f"{DL_BASE}/10/" + "A" * 100_000)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 42
    assert detail["found"] == "100000 characters"
    assert "100000" in detail["message"]


def test_digital_link_exactly_20_percent_encoded_chars_reaches_charset():
    resp = parse(f"{DL_BASE}/10/" + "%C3%A9" * 20)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "INVALID_CHARACTER"
    assert detail["offset"] == 22  # '%' of the first é's encoding
    assert detail["found"] == "é"


def test_digital_link_hundred_thousand_percent_encoded_chars_is_fast():
    data = f"{DL_BASE}/10/" + "%C3%A9" * 200_000  # 1.2M raw characters
    start = time.perf_counter()
    resp = parse(data)
    elapsed = time.perf_counter() - start
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "FIELD_TOO_LONG"
    assert detail["offset"] == 142  # 21st decoded character
    assert detail["found"] == "200000 characters"
    assert elapsed < SLOW_PARSE_SECONDS
