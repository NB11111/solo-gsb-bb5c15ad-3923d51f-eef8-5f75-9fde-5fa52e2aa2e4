"""GS1 element-string and Digital Link URI parser.

Supports AI 01 (GTIN-14), AI 10 (batch/lot), AI 17 (expiry date),
AI 21 (serial number) and the variable-metric weight family
AI 310n (net weight, kilograms) and AI 320n (net weight, pounds),
where ``n`` (0-9) is the number of implied decimal places and the
field is a fixed 6-digit number.

The AI catalogue is an *immutable declarative registry* built once at
import time.  Two kinds of declarations exist:

* :class:`ExactAIDecl` -- a precise, fixed identifier (``01``, ``10``,
  ``17``, ``21``), and
* :class:`FamilyAIDecl` -- a prefix (``310`` / ``320``) carrying exactly
  one parameter character (the precision digit 0-9); the registry
  expands the family into the ten concrete member specs (``3100`` …
  ``3209``).

:meth:`AIRegistry.build` rejects any malformed catalogue up front:
ambiguous prefixes (an exact key shadowing a family prefix or vice
versa), duplicate keys, inconsistent length/charset fields, or a
missing semantic processor.  The built mappings are read-only
(:class:`types.MappingProxyType`) and every spec is a frozen dataclass.

Two input forms are accepted by :func:`parse`:

1. Raw GS1 element strings as emitted by scanners. Fixed-length fields
   (01, 17, 310n, 320n) are delimited implicitly by their length;
   variable-length fields (10, 21) must be terminated by the FNC1
   separator (ASCII 29, GS) unless they are the last field in the
   string. AIs are recognised by *longest match* through the same
   recognizer used by Digital Link: the 310n/320n family takes
   precedence over any 2-character AI, and a family prefix with a
   missing or illegal parameter is reported as INVALID_PRECISION
   rather than being mistaken for a 2-character AI.

2. GS1 Digital Link URIs (``http://`` / ``https://``).  AI/value pairs
   are read from the URI path (``/AI/value/AI/value``) first and then
   from query parameters (``?AI=value&AI=value``).  The AI segment is
   matched as a *complete key* by the very same recognizer, and
   percent-decoded values are dispatched through the shared value
   validator, so both carriers apply identical character-set, length,
   check-digit, date and weight semantics.
"""

from __future__ import annotations

import calendar
import codecs
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Callable, Mapping

GS = "\x1d"  # ASCII 29 <GS>, transmitted as FNC1 by scanners

DIGITS = frozenset("0123456789")
HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")

URI_SCHEMES = ("https://", "http://")

# GS1 AI encodable character set 82 (GS1 General Specifications, fig. 7.11-1)
AI82 = frozenset(
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "!\"%&'()*+,-./:;<=>?_"
)


# --------------------------------------------------------------- registry errors


class RegistryDefinitionError(Exception):
    """The declarative AI catalogue itself is inconsistent.

    Raised at registry construction time (module import); a production
    deployment must never ship a catalogue that triggers this -- the
    failure is deliberately loud and immediate rather than deferred to
    the first parse call.
    """


# --------------------------------------------------------- declarative catalogue


@dataclass(frozen=True)
class ExactAIDecl:
    """Declaration of a precise, fixed application identifier (e.g. ``01``)."""

    ai: str
    name: str
    min_length: int
    max_length: int
    fixed_length: bool
    charset: frozenset[str]
    charset_rule: str
    semantic: str | None = None  # key into the semantic handler table


@dataclass(frozen=True)
class FamilyAIDecl:
    """Declaration of a family AI carrying one parameter character.

    The concrete identifier is ``prefix + parameter``; for the weight
    families the parameter is the precision digit (0-9) and doubles as
    the number of implied decimal places.
    """

    prefix: str
    name: str
    unit: str
    field_length: int  # fixed number of value characters (6 for weights)
    charset: frozenset[str]
    charset_rule: str
    semantic: str  # key into the semantic handler table (e.g. "weight")
    parameter_chars: frozenset[str] = frozenset("0123456789")
    parameter_is_decimals: bool = True  # parameter becomes the decimals count


#: Precise AIs, in declaration order.
EXACT_AI_DECLARATIONS: tuple[ExactAIDecl, ...] = (
    ExactAIDecl("01", "GTIN", 14, 14, True, DIGITS, "digits 0-9", semantic="gtin"),
    ExactAIDecl("17", "EXPIRY", 6, 6, True, DIGITS, "digits 0-9", semantic="date"),
    ExactAIDecl("10", "BATCH/LOT", 1, 20, False, AI82, "GS1 AI character set 82"),
    ExactAIDecl("21", "SERIAL", 1, 20, False, AI82, "GS1 AI character set 82"),
)

#: Family AIs: prefix -> one parameter character -> concrete member AI.
FAMILY_AI_DECLARATIONS: tuple[FamilyAIDecl, ...] = (
    FamilyAIDecl("310", "NET WEIGHT (KG)", "kg", 6, DIGITS, "digits 0-9", "weight"),
    FamilyAIDecl("320", "NET WEIGHT (LB)", "lb", 6, DIGITS, "digits 0-9", "weight"),
)


@dataclass(frozen=True)
class AISpec:
    """Resolved, immutable description of one concrete AI (a member or an exact)."""

    ai: str
    name: str
    min_length: int
    max_length: int
    fixed_length: bool
    charset: frozenset[str]
    charset_rule: str
    ai_length: int = 2  # number of AI characters (4 for the 310n/320n family)
    decimals: int | None = None  # implied decimal places (310n/320n only)
    unit: str | None = None  # measurement unit emitted alongside decimal_value
    semantic: str | None = None  # key into the registry's semantic handler table


@dataclass(frozen=True)
class Recognition:
    """Outcome of the shared AI recognizer for one candidate token.

    ``kind`` is one of:

    * ``"ok"`` -- ``spec`` is the matched concrete AI;
    * ``"unsupported"`` -- well-formed numeric identifier that no
      declaration covers (``found`` is the text to report);
    * ``"invalid_ai"`` -- non-numeric / malformed identifier;
    * ``"invalid_precision"`` -- a family prefix whose single parameter
      is missing (``short``) or illegal (``error_index`` locates it).
    """

    kind: str
    spec: AISpec | None
    found: str
    error_index: int | None = None
    short: bool = False


# A semantic handler runs after charset/length checks and returns
# ``(decimal_value, unit)`` (both ``None`` for plain validators such as
# the GTIN check digit and expiry date checks).
SemanticHandler = Callable[[AISpec, str, "list[int]"], "tuple[float | None, str | None]"]


class AIRegistry:
    """Immutable, validated catalogue plus the shared recognition kernel.

    Instances are frozen and expose only read-only mappings; build them
    through :meth:`build`, which validates the whole declaration set.
    Two lookup modes share one kernel:

    * *stream* mode (``complete=False``) for element strings -- the
      token is the longest possible identifier prefix at the cursor, so
      a 4-character family member wins over the 2-character key space;
    * *complete* mode (``complete=True``) for Digital Link -- the token
      is the whole path segment / query key and must equal a declared
      identifier exactly.
    """

    __slots__ = (
        "_specs",
        "_families",
        "_exact",
        "_handlers",
        "_family_prefix_len",
        "_stream_probe_len",
    )

    def __init__(self, *, specs, families, exact, handlers,
                 family_prefix_len, stream_probe_len):
        object.__setattr__(self, "_specs", specs)
        object.__setattr__(self, "_families", families)
        object.__setattr__(self, "_exact", exact)
        object.__setattr__(self, "_handlers", handlers)
        object.__setattr__(self, "_family_prefix_len", family_prefix_len)
        object.__setattr__(self, "_stream_probe_len", stream_probe_len)

    # ------------------------------------------------------------------ views

    @property
    def specs(self) -> Mapping[str, AISpec]:
        return self._specs

    @property
    def families(self) -> Mapping[str, FamilyAIDecl]:
        return self._families

    @property
    def exact(self) -> Mapping[str, ExactAIDecl]:
        return self._exact

    @property
    def semantic_handlers(self) -> Mapping[str, SemanticHandler]:
        return self._handlers

    @property
    def family_prefix_len(self) -> int:
        return self._family_prefix_len

    @property
    def stream_probe_len(self) -> int:
        """Characters to probe at an element-string cursor for longest match."""
        return self._stream_probe_len

    # ------------------------------------------------------------ constructor

    @classmethod
    def build(
        cls,
        exact_declarations: "tuple[ExactAIDecl, ...]",
        family_declarations: "tuple[FamilyAIDecl, ...]",
        semantic_handlers: "Mapping[str, SemanticHandler]",
    ) -> "AIRegistry":
        def fail(message: str) -> None:
            raise RegistryDefinitionError(message)

        specs: dict[str, AISpec] = {}
        exact: dict[str, ExactAIDecl] = {}
        families: dict[str, FamilyAIDecl] = {}

        for decl in exact_declarations:
            if not decl.ai or not all(c in DIGITS for c in decl.ai):
                fail(f"exact AI {decl.ai!r} must be a non-empty numeric key")
            if decl.ai in exact:
                fail(f"duplicate exact AI declaration: {decl.ai!r}")
            _validate_field_shape(decl.ai, decl, fail)
            if decl.semantic is not None and decl.semantic not in semantic_handlers:
                fail(
                    f"exact AI {decl.ai!r} references undefined semantic "
                    f"handler {decl.semantic!r}"
                )
            exact[decl.ai] = decl
            specs[decl.ai] = AISpec(
                decl.ai,
                decl.name,
                decl.min_length,
                decl.max_length,
                decl.fixed_length,
                decl.charset,
                decl.charset_rule,
                ai_length=len(decl.ai),
                semantic=decl.semantic,
            )

        prefix_lengths = {len(decl.prefix) for decl in family_declarations}
        if len(prefix_lengths) > 1:
            fail(
                "all family AI prefixes must have the same length, got "
                + ", ".join(str(n) for n in sorted(prefix_lengths))
            )

        for decl in family_declarations:
            if not decl.prefix or not all(c in DIGITS for c in decl.prefix):
                fail(f"family AI prefix {decl.prefix!r} must be non-empty numeric")
            if decl.prefix in families:
                fail(f"duplicate family AI declaration: {decl.prefix!r}")
            if not decl.parameter_chars or any(
                len(c) != 1 for c in decl.parameter_chars
            ):
                fail(
                    f"family {decl.prefix!r} parameter set must be a non-empty "
                    "set of single characters"
                )
            if not all(c in DIGITS for c in decl.parameter_chars):
                fail(f"family {decl.prefix!r} parameter characters must be digits 0-9")
            if decl.semantic not in semantic_handlers:
                fail(
                    f"family {decl.prefix!r} references undefined semantic "
                    f"handler {decl.semantic!r}"
                )
            if decl.field_length < 1:
                fail(f"family {decl.prefix!r} field length must be positive")
            if not decl.charset or not decl.charset_rule or not decl.name:
                fail(f"family {decl.prefix!r} is missing name/charset metadata")

            # Ambiguity: a family prefix may neither equal nor be a prefix
            # of an exact key, nor vice versa -- otherwise longest match
            # would depend on input length alone.
            for key in exact:
                if key == decl.prefix or key.startswith(decl.prefix) or decl.prefix.startswith(key):
                    fail(
                        f"ambiguous AI catalogue: exact key {key!r} and family "
                        f"prefix {decl.prefix!r} overlap"
                    )

            families[decl.prefix] = decl
            for param in sorted(decl.parameter_chars):
                ai = f"{decl.prefix}{param}"
                if ai in specs:
                    fail(
                        f"family member AI {ai!r} (from prefix {decl.prefix!r}) "
                        "collides with an earlier declaration"
                    )
                specs[ai] = AISpec(
                    ai,
                    decl.name,
                    decl.field_length,
                    decl.field_length,
                    True,
                    decl.charset,
                    decl.charset_rule,
                    ai_length=len(decl.prefix) + 1,
                    decimals=int(param) if decl.parameter_is_decimals else None,
                    unit=decl.unit,
                    semantic=decl.semantic,
                )

        if not specs:
            fail("AI registry must declare at least one AI")

        # Determine prefix length (families may legitimately be absent).
        family_prefix_len = len(family_declarations[0].prefix) if family_declarations else 0
        stream_probe_len = max((len(key) for key in exact), default=0)
        if families:
            stream_probe_len = max(stream_probe_len, family_prefix_len + 1)

        return cls(
            specs=MappingProxyType(specs),
            families=MappingProxyType(families),
            exact=MappingProxyType(exact),
            handlers=MappingProxyType(dict(semantic_handlers)),
            family_prefix_len=family_prefix_len,
            stream_probe_len=stream_probe_len,
        )

    # ------------------------------------------------------------ recognition

    def recognize(self, token: str, *, complete: bool) -> Recognition:
        """Recognise ``token`` as a concrete AI.

        ``complete=True``  -- Digital Link: ``token`` is the entire
        decoded key and must match a declaration exactly.
        ``complete=False`` -- element string: ``token`` is the first
        :attr:`stream_probe_len` characters at the cursor (shorter at
        end of input); a matched family member consumes its declared
        identifier length regardless of how many characters followed.
        """
        if not token:
            return Recognition("invalid_ai", None, token)

        pfx_len = self._family_prefix_len
        if pfx_len and len(token) >= pfx_len:
            family = self._families.get(token[:pfx_len])
            if family is not None:
                return self._recognize_family(family, token, complete)

        if complete:
            if any(c not in DIGITS for c in token):
                return Recognition("invalid_ai", None, token)
            spec = self._specs.get(token)
            if spec is None or spec.ai_length != len(token):
                return Recognition("unsupported", None, token)
            return Recognition("ok", spec, token)

        # Stream mode: the non-family key space starts with the shortest
        # declared exact identifier (2 characters in this catalogue).
        if not self._exact:
            return Recognition("unsupported", None, token[:pfx_len] if pfx_len else token)
        key_len = min(len(k) for k in self._exact)
        head = token[:key_len]
        if len(head) < key_len or any(c not in DIGITS for c in head):
            return Recognition("invalid_ai", None, head)
        spec = self._specs.get(head)
        if spec is None:
            return Recognition("unsupported", None, head)
        return Recognition("ok", spec, head)

    def _recognize_family(
        self, family: FamilyAIDecl, token: str, complete: bool
    ) -> Recognition:
        pfx_len = len(family.prefix)
        if len(token) <= pfx_len:
            # Prefix with no parameter character at all.
            return Recognition("invalid_precision", None, token, short=True)
        if token[pfx_len] not in family.parameter_chars:
            return Recognition(
                "invalid_precision", None, token, error_index=pfx_len
            )
        member_ai = f"{family.prefix}{token[pfx_len]}"
        spec = self._specs.get(member_ai)
        if complete and token != member_ai:
            # Well-formed family prefix + parameter, but the key has
            # trailing characters: no such complete AI.
            return Recognition("unsupported", None, token)
        return Recognition("ok", spec, member_ai)

    # ------------------------------------------------------------- semantics

    def apply_semantics(
        self, spec: AISpec, value: str, offsets: "list[int]"
    ) -> "tuple[float | None, str | None]":
        """Run the spec's semantic processor after length/charset checks."""
        if spec.semantic is None:
            return None, None
        handler = self._handlers.get(spec.semantic)
        if handler is None:  # pragma: no cover - ruled out at build time
            raise RegistryDefinitionError(
                f"AI {spec.ai!r} has no semantic handler {spec.semantic!r}"
            )
        return handler(spec, value, offsets)


def _validate_field_shape(ai, decl, fail) -> None:
    """Length/charset completeness for one exact declaration."""
    if not decl.name:
        fail(f"exact AI {ai!r} is missing its name")
    if decl.min_length < 1 or decl.max_length < decl.min_length:
        fail(f"exact AI {ai!r} has inconsistent length bounds")
    if decl.fixed_length and decl.min_length != decl.max_length:
        fail(f"fixed-length AI {ai!r} must declare min_length == max_length")
    if not decl.charset or not isinstance(decl.charset, frozenset):
        fail(f"exact AI {ai!r} must declare a non-empty frozenset charset")
    if not decl.charset_rule:
        fail(f"exact AI {ai!r} is missing its charset_rule")


# ------------------------------------------------------------ semantic handlers


def _sem_gtin(spec: AISpec, value: str, offsets) -> "tuple[None, None]":
    _check_gtin(value, offsets)
    return None, None


def _sem_date(spec: AISpec, value: str, offsets) -> "tuple[None, None]":
    _check_date(value, offsets)
    return None, None


def _sem_weight(spec: AISpec, value: str, offsets) -> "tuple[float, str | None]":
    return float(decode_weight(value, spec.decimals)), spec.unit


SEMANTIC_HANDLERS: Mapping[str, SemanticHandler] = MappingProxyType(
    {
        "gtin": _sem_gtin,
        "date": _sem_date,
        "weight": _sem_weight,
    }
)


#: The process-wide immutable registry, validated once at import time.
REGISTRY = AIRegistry.build(
    EXACT_AI_DECLARATIONS,
    FAMILY_AI_DECLARATIONS,
    SEMANTIC_HANDLERS,
)

# Backwards-compatible read-only views of the catalogue.
AI_SPECS: Mapping[str, AISpec] = REGISTRY.specs
WEIGHT_AI_FAMILIES: Mapping[str, FamilyAIDecl] = REGISTRY.families

SUPPORTED_AIS = (
    "01, 10, 17, 21 and the weight family 310n/320n "
    "(n = 0-9 implied decimal places, fixed 6 digits)"
)


class GS1ParseError(Exception):
    """Parse/validation failure carrying a byte offset and the expected rule."""

    def __init__(self, code, message, offset, expected, found=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset
        self.expected = expected
        self.found = found

    def to_dict(self):
        detail = {
            "error": self.code,
            "message": self.message,
            "offset": self.offset,
            "expected": self.expected,
        }
        if self.found is not None:
            detail["found"] = self.found
        return detail


@dataclass(frozen=True)
class Element:
    ai: str
    name: str
    value: str
    offset: int  # byte offset of the AI within the input
    decimal_value: float | None = None  # 310n/320n only: value with implied decimal point
    unit: str | None = None  # 310n/320n only: "kg" or "lb"


def _byte_offset(data: str, index: int) -> int:
    """Byte offset (UTF-8) of the character at ``index``.

    Only used for isolated offsets (error locations, span boundaries);
    per-character offsets of a value are accumulated incrementally by the
    field readers instead, so parsing long values stays linear.
    """
    return len(data[:index].encode("utf-8"))


def _char_utf8_length(ch: str) -> int:
    """UTF-8 byte length of a single already-decoded character (1-4)."""
    codepoint = ord(ch)
    if codepoint < 0x80:
        return 1
    if codepoint < 0x800:
        return 2
    if codepoint < 0x10000:
        return 3
    return 4


@dataclass(frozen=True)
class _ValueScan:
    """Variable-length field gathered in a single linear scan.

    ``offsets[i]`` is the raw UTF-8 byte offset of ``field[i]``; both lists
    grow only up to the field's maximum allowed length plus one, so an
    overlong field is detected while scanning its first illegal character
    without ever touching the remainder of the input.
    """

    field: str
    offsets: tuple[int, ...]
    terminated: bool
    overlong: bool  # scanning stopped on character max_length + 1


def _scan_variable_field(data: str, pos: int, spec, byte_pos: int) -> _ValueScan:
    """Read a variable-length element-string value in one linear scan.

    ``pos`` is the character index where the value starts and ``byte_pos``
    its UTF-8 byte offset. The scan stops as soon as ``max_length + 1``
    characters have been seen, so the caller can report FIELD_TOO_LONG
    against that 21st character; the trailing input (separator or more
    characters) is never inspected.
    """
    chars: list[str] = []
    offsets: list[int] = []
    i = pos
    n_data = len(data)
    while i < n_data:
        ch = data[i]
        if ch == GS:
            return _ValueScan("".join(chars), tuple(offsets), True, False)
        if len(chars) == spec.max_length:
            # This is character max_length + 1: record only its first-byte
            # offset, then stop scanning immediately.
            offsets.append(byte_pos)
            chars.append(ch)
            return _ValueScan("".join(chars), tuple(offsets), False, True)
        chars.append(ch)
        offsets.append(byte_pos)
        byte_pos += _char_utf8_length(ch)
        i += 1
    return _ValueScan("".join(chars), tuple(offsets), False, False)


def parse(data: str) -> list[Element]:
    """Parse a GS1 element string or Digital Link URI into ordered elements."""
    if not data:
        raise GS1ParseError(
            "EMPTY_INPUT",
            "Input is empty",
            offset=0,
            expected=f"at least one element string starting with AI {SUPPORTED_AIS}",
        )
    if data.startswith(URI_SCHEMES):
        return _parse_digital_link(data)
    return _parse_element_string(data)


# ----------------------------------------------------------------- element strings


def _parse_element_string(data: str) -> list[Element]:
    elements = []
    pos = 0
    byte_pos = 0  # UTF-8 byte offset of data[pos], maintained incrementally
    total_bytes = len(data.encode("utf-8"))
    while pos < len(data):
        ai_offset = byte_pos
        if data[pos] == GS:
            raise GS1ParseError(
                "UNEXPECTED_SEPARATOR",
                "Unexpected separator (ASCII 29); expected an application identifier",
                offset=ai_offset,
                expected=f"numeric AI, one of: {SUPPORTED_AIS}",
                found="<GS>",
            )
        spec, pos = _match_element_string_ai(data, pos, ai_offset)
        # Recognised AIs are ASCII digits, so every AI character is one byte.
        byte_pos = ai_offset + spec.ai_length
        value_pos = pos

        if spec.fixed_length:
            field, offsets, pos, byte_pos = _read_fixed_field(
                data, pos, spec, byte_pos
            )
            # A separator embedded inside the fixed field is the field's end.
            sep_index = None
            sep_offset = None
            slice_end = min(value_pos + spec.max_length, len(data))
            end = data.find(GS, value_pos, slice_end)
            if end != -1:
                sep_index = end - value_pos
                sep_offset = _byte_offset(data, end)
                end_offset = sep_offset
            else:
                end_offset = total_bytes
            decimal_value, unit = _validate_value(
                spec,
                field,
                offsets,
                "element_string",
                end_offset=end_offset,
                sep_index=sep_index,
                sep_offset=sep_offset,
            )
        else:
            scan = _scan_variable_field(data, pos, spec, byte_pos)
            field = scan.field
            offsets = scan.offsets
            if scan.overlong:
                # The scan stopped on character max_length + 1 without
                # reading any further input. Locate the terminator in the
                # unread tail (C-level find, no per-character work) only to
                # preserve the original error wording.
                tail_sep = data.find(GS, pos + spec.max_length + 1)
                if tail_sep == -1:
                    actual_length = len(data) - value_pos
                    terminated = False
                else:
                    actual_length = tail_sep - value_pos
                    terminated = True
                end_offset = byte_pos
                decimal_value, unit = _validate_value(
                    spec,
                    field,
                    offsets,
                    "element_string",
                    end_offset=end_offset,
                    terminated=terminated,
                    actual_length=actual_length,
                )
            else:
                # FIELD_EMPTY reports the start of the (empty) value.
                end_offset = byte_pos
                value_bytes = len(field.encode("utf-8"))
                if scan.terminated:
                    pos = pos + len(field) + 1
                    byte_pos = byte_pos + value_bytes + 1
                else:
                    pos = pos + len(field)
                    byte_pos = byte_pos + value_bytes
                decimal_value, unit = _validate_value(
                    spec,
                    field,
                    offsets,
                    "element_string",
                    end_offset=end_offset,
                    terminated=scan.terminated,
                )

        elements.append(
            Element(
                ai=spec.ai,
                name=spec.name,
                value=field,
                offset=ai_offset,
                decimal_value=decimal_value,
                unit=unit,
            )
        )
    return elements


def _read_fixed_field(data, pos, spec, byte_pos):
    """Read a fixed-length value, accumulating per-character byte offsets.

    A separator inside the field truncates it (the length check reports
    FIELD_TOO_SHORT). Returns ``(field, offsets, new_pos, new_byte_pos)``.
    """
    chars: list[str] = []
    offsets: list[int] = []
    i = pos
    n_data = len(data)
    end = min(pos + spec.max_length, n_data)
    while i < end:
        ch = data[i]
        if ch == GS:
            return "".join(chars), offsets, i + 1, byte_pos + 1
        chars.append(ch)
        offsets.append(byte_pos)
        byte_pos += _char_utf8_length(ch)
        i += 1
    return "".join(chars), offsets, end, byte_pos


def _match_element_string_ai(data: str, pos: int, ai_offset: int):
    """Recognise the AI at ``pos`` via the shared recognizer (longest match).

    Returns ``(spec, new_pos)`` or raises :class:`GS1ParseError`. Family
    prefixes with a missing or non-digit parameter, or a parameter slot
    occupied by a separator, are reported as INVALID_PRECISION.
    """
    head = data[pos : pos + 2]
    if len(head) < 2 or not all(c in DIGITS for c in head):
        raise GS1ParseError(
            "INVALID_AI",
            f"Expected a numeric application identifier, found {head!r}",
            offset=ai_offset,
            expected=f"numeric AI, one of: {SUPPORTED_AIS}",
            found=head,
        )

    candidate = data[pos : pos + REGISTRY.stream_probe_len]
    result = REGISTRY.recognize(candidate, complete=False)

    if result.kind == "invalid_precision":
        family = candidate[: REGISTRY.family_prefix_len]
        if result.short:
            raise GS1ParseError(
                "INVALID_PRECISION",
                f"AI family {family} requires a precision digit (0-9) but the input ended",
                offset=_byte_offset(data, len(data)),
                expected=f"precision digit 0-9 for AI {family}n",
                found="end of input",
            )
        precision = data[pos + REGISTRY.family_prefix_len]
        if precision == GS:
            raise GS1ParseError(
                "INVALID_PRECISION",
                f"AI family {family} requires a precision digit (0-9) but a "
                "separator (ASCII 29) was found",
                offset=_byte_offset(data, pos + REGISTRY.family_prefix_len),
                expected=f"precision digit 0-9 for AI {family}n",
                found="<GS>",
            )
        raise GS1ParseError(
            "INVALID_PRECISION",
            f"AI family {family} requires a precision digit 0-9 as its 4th "
            f"character, found {precision!r}",
            offset=_byte_offset(data, pos + REGISTRY.family_prefix_len),
            expected=f"precision digit 0-9 for AI {family}n",
            found=precision,
        )

    if result.kind == "unsupported":
        ai = data[pos : pos + 2]
        raise GS1ParseError(
            "UNSUPPORTED_AI",
            f"Unsupported application identifier '{ai}'",
            offset=ai_offset,
            expected=f"one of: {SUPPORTED_AIS}",
            found=ai,
        )

    spec = result.spec
    return spec, pos + spec.ai_length


# ------------------------------------------------------------ shared value checks


def _validate_value(
    spec,
    value,
    offsets,
    carrier,
    *,
    end_offset,
    sep_index=None,
    sep_offset=None,
    terminated=False,
    actual_length=None,
    truncated=False,
):
    """Length, charset and semantic checks shared by both input carriers.

    ``carrier`` is ``"element_string"`` or ``"digital_link"``; only the
    wording/location of length errors differs between them.
    ``offsets[i]`` is the raw byte offset of ``value[i]``.

    ``actual_length`` is the true number of decoded characters when a
    field was cut short by the single-pass scan (``value`` then contains
    only ``max_length + 1`` characters); ``truncated`` marks a Digital
    Link value whose tail was not scanned at all, in which case only a
    lower bound on the length is known.
    """
    n = actual_length if actual_length is not None else len(value)
    if spec.fixed_length:
        if n == spec.max_length:
            pass
        elif carrier == "element_string":
            if sep_index is not None:
                raise GS1ParseError(
                    "FIELD_TOO_SHORT",
                    f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                    f"but a separator was found after {sep_index}",
                    offset=sep_offset,
                    expected=f"{spec.max_length} characters for AI {spec.ai}",
                    found="<GS>",
                )
            raise GS1ParseError(
                "FIELD_TOO_SHORT",
                f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                f"but the input ended after {n}",
                offset=end_offset,
                expected=f"{spec.max_length} characters for AI {spec.ai}",
                found=f"{n} character(s)",
            )
        elif n < spec.max_length:
            raise GS1ParseError(
                "FIELD_TOO_SHORT",
                f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                f"but found {n} after percent-decoding",
                offset=end_offset,
                expected=f"{spec.max_length} characters for AI {spec.ai}",
                found=f"{n} character(s)",
            )
        else:
            if truncated:
                raise GS1ParseError(
                    "FIELD_TOO_LONG",
                    f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                    f"but more than {spec.max_length} were found after percent-decoding",
                    offset=offsets[spec.max_length],
                    expected=f"{spec.max_length} characters for AI {spec.ai}",
                    found=f"more than {spec.max_length} characters",
                )
            raise GS1ParseError(
                "FIELD_TOO_LONG",
                f"AI {spec.ai} ({spec.name}) requires exactly {spec.max_length} characters "
                f"but found {n} after percent-decoding",
                offset=offsets[spec.max_length],
                expected=f"{spec.max_length} characters for AI {spec.ai}",
                found=f"{n} characters",
            )
    else:
        if n < spec.min_length:
            raise GS1ParseError(
                "FIELD_EMPTY",
                f"AI {spec.ai} ({spec.name}) requires at least {spec.min_length} character",
                offset=end_offset,
                expected=f"{spec.min_length}-{spec.max_length} characters for AI {spec.ai}",
                found="empty field",
            )
        if n > spec.max_length:
            if carrier == "element_string":
                raise GS1ParseError(
                    "FIELD_TOO_LONG",
                    f"AI {spec.ai} ({spec.name}) allows at most {spec.max_length} characters "
                    f"but got {n} before {'the separator' if terminated else 'end of input'}",
                    offset=offsets[spec.max_length],
                    expected=f"separator (ASCII 29) or end of input within {spec.max_length} characters",
                )
            if truncated:
                raise GS1ParseError(
                    "FIELD_TOO_LONG",
                    f"AI {spec.ai} ({spec.name}) allows at most {spec.max_length} characters "
                    f"but more than {spec.max_length} were found after percent-decoding",
                    offset=offsets[spec.max_length],
                    expected=f"at most {spec.max_length} characters for AI {spec.ai}",
                    found=f"more than {spec.max_length} characters",
                )
            raise GS1ParseError(
                "FIELD_TOO_LONG",
                f"AI {spec.ai} ({spec.name}) allows at most {spec.max_length} characters "
                f"but found {n} after percent-decoding",
                offset=offsets[spec.max_length],
                expected=f"at most {spec.max_length} characters for AI {spec.ai}",
                found=f"{n} characters",
            )

    _check_charset(spec, value, offsets)
    return REGISTRY.apply_semantics(spec, value, offsets)


def _check_charset(spec, field, offsets):
    """``offsets[i]`` is the raw byte offset of ``field[i]``."""
    for i, ch in enumerate(field):
        if ch not in spec.charset:
            raise GS1ParseError(
                "INVALID_CHARACTER",
                f"Character {ch!r} (U+{ord(ch):04X}) is not allowed in AI {spec.ai} ({spec.name})",
                offset=offsets[i],
                expected=spec.charset_rule,
                found=ch,
            )


def _check_gtin(gtin, offsets):
    total = 0
    for i, ch in enumerate(reversed(gtin[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    expected = (10 - total % 10) % 10
    if expected != int(gtin[-1]):
        raise GS1ParseError(
            "INVALID_CHECK_DIGIT",
            f"GTIN check digit mismatch: expected {expected}, found {gtin[-1]}",
            offset=offsets[-1],
            expected=f"Mod-10 check digit {expected}",
            found=gtin[-1],
        )


def _check_date(yymmdd, offsets):
    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    year = 2000 + yy
    if not 1 <= mm <= 12:
        raise GS1ParseError(
            "INVALID_DATE",
            f"Invalid expiry month {mm:02d} in AI 17 date '{yymmdd}'",
            offset=offsets[2],
            expected="month 01-12",
            found=yymmdd[2:4],
        )
    last_day = calendar.monthrange(year, mm)[1]
    if dd == 0:
        return  # GS1: day "00" denotes the last day of the month
    if dd > last_day:
        raise GS1ParseError(
            "INVALID_DATE",
            f"Invalid expiry day {dd:02d} for {year}-{mm:02d} (max {last_day})",
            offset=offsets[4],
            expected=f"day 01-{last_day} for {year}-{mm:02d} (00 = last day of month)",
            found=yymmdd[4:6],
        )


def decode_weight(value: str, decimals: int) -> Decimal:
    """Interpret the 6 fixed digits with ``decimals`` implied places exactly.

    The GS1 variable-metric rule is ``int(digits) / 10**n``; when n > 6
    the missing high-order fractional digits are implied zero, e.g.
    ``3109 000001`` -> Decimal('0.000000001').  The result is built from
    the integer digits and a power of ten, so no binary float is ever
    involved.
    """
    return Decimal(int(value)).scaleb(-decimals)


# ------------------------------------------------------------ Digital Link URIs


def _parse_digital_link(data: str) -> list[Element]:
    authority_start = data.index("://") + 3  # works for http:// and https://

    # Locate path / query / fragment boundaries directly on the raw URI so
    # that every error can carry an accurate byte offset.
    path_start = len(data)
    for marker in ("/", "?", "#"):
        j = data.find(marker, authority_start)
        if j != -1:
            path_start = min(path_start, j)
    fragment_start = data.find("#", path_start)
    if fragment_start == -1:
        fragment_start = len(data)
    query_mark = data.find("?", path_start, fragment_start)
    if query_mark == -1:
        path_end = fragment_start
        query_start = query_end = -1
    else:
        path_end = query_mark
        query_start = query_mark + 1
        query_end = fragment_start

    # ---- path: /AI/value/AI/value (a single trailing slash is tolerated) ----
    segments = _split_path(data, path_start, path_end)
    if len(segments) % 2:
        odd_start = segments[-1][0]
        odd_raw = data[segments[-1][0] : segments[-1][1]]
        raise GS1ParseError(
            "UNPAIRED_PATH",
            "URI path must contain AI/value pairs but ends with a value missing "
            f"its AI or an AI missing its value: {odd_raw!r}",
            offset=_byte_offset(data, odd_start),
            expected="even number of path segments: /AI/value[/AI/value]...",
            found=odd_raw,
        )

    # ---- query: AI=value&AI=value (query order follows path order) ----
    pairs = _split_query(data, query_start, query_end)

    if not segments and not pairs:
        raise GS1ParseError(
            "EMPTY_INPUT",
            "URI contains no AI/value pairs in its path or query string",
            offset=0,
            expected=f"at least one path segment or query parameter with AI {SUPPORTED_AIS}",
        )

    elements = []
    seen: dict[str, tuple[int, str]] = {}

    def consume(ai_span, value_span, location):
        ai_start, ai_end = ai_span
        val_start, val_end = value_span
        decoded_ai = _percent_decode(data, ai_start, ai_end)
        ai = decoded_ai.text
        ai_offsets = decoded_ai.offsets
        ai_byte = ai_offsets[0] if ai_offsets else _byte_offset(data, ai_start)

        # In a URI the AI is the whole path segment / query key; the same
        # recognizer used for element strings resolves it as a complete
        # key, including the INVALID_PRECISION rule for family prefixes.
        if not ai:
            raw_ai = data[ai_start:ai_end]
            raise GS1ParseError(
                "INVALID_AI",
                f"Expected a numeric application identifier in the {location}, "
                "found an empty segment",
                offset=ai_byte,
                expected=f"numeric AI, one of: {SUPPORTED_AIS}",
                found=raw_ai,
            )

        result = REGISTRY.recognize(ai, complete=True)
        if result.kind == "invalid_precision":
            family = ai[: REGISTRY.family_prefix_len]
            if result.short:
                raise GS1ParseError(
                    "INVALID_PRECISION",
                    f"AI family {family} requires a precision digit (0-9) as "
                    f"its 4th character in the {location}",
                    offset=_byte_offset(data, ai_end),
                    expected=f"4-character AI {family}n (precision digit 0-9)",
                    found=ai,
                )
            raise GS1ParseError(
                "INVALID_PRECISION",
                f"AI family {family} requires a precision digit 0-9 as its "
                f"4th character, found {ai[result.error_index]!r}",
                offset=ai_offsets[result.error_index],
                expected=f"precision digit 0-9 for AI {family}n",
                found=ai[result.error_index],
            )
        if result.kind == "invalid_ai":
            raw_ai = data[ai_start:ai_end]
            raise GS1ParseError(
                "INVALID_AI",
                f"Expected a numeric application identifier in the {location}, "
                f"found {ai!r}",
                offset=ai_byte,
                expected=f"numeric AI, one of: {SUPPORTED_AIS}",
                found=raw_ai,
            )
        if result.kind == "unsupported":
            raise GS1ParseError(
                "UNSUPPORTED_AI",
                f"Unsupported application identifier '{ai}' in the {location}",
                offset=ai_byte,
                expected=f"one of: {SUPPORTED_AIS}",
                found=ai,
            )
        spec = result.spec

        if ai in seen:
            first_offset, first_location = seen[ai]
            if first_location == location:
                message = (
                    f"Application identifier '{ai}' is repeated in the {location}; "
                    "each AI may appear only once"
                )
            else:
                message = (
                    f"Application identifier '{ai}' appears in both the "
                    f"{first_location} (byte {first_offset}) and the {location}; "
                    "an AI may not be given in both places"
                )
            raise GS1ParseError(
                "DUPLICATE_AI",
                message,
                offset=ai_byte,
                expected=f"AI '{ai}' to appear at most once",
                found=ai,
            )
        seen[ai] = (ai_byte, location)

        decoded = _percent_decode(data, val_start, val_end, spec.max_length)
        end_offset = _byte_offset(data, val_end)
        decimal_value, unit = _validate_uri_value(
            spec,
            decoded.text,
            decoded.offsets,
            end_offset,
            truncated=decoded.overlong,
        )
        elements.append(
            Element(
                ai=spec.ai,
                name=spec.name,
                value=decoded.text,
                offset=ai_byte,
                decimal_value=decimal_value,
                unit=unit,
            )
        )

    for k in range(0, len(segments), 2):
        consume(segments[k], segments[k + 1], "URI path")
    for key_span, value_span in pairs:
        consume(key_span, value_span, "query string")

    return elements


def _split_path(data, start, end):
    """Return ``[(seg_start, seg_end), ...]`` for raw path segments."""
    segments = []
    i = start
    if i < end and data[i] == "/":
        i += 1
    while i < end:
        slash = data.find("/", i, end)
        if slash == -1:
            slash = end
        segments.append((i, slash))
        i = slash + 1
    # A trailing slash yields one empty segment anchored at ``end``; ignore it
    # so that "/01/x/" and "/01/x" are equivalent.
    if segments and segments[-1] == (end, end):
        segments.pop()
    return segments


def _split_query(data, start, end):
    """Return ``[(key_span, value_span), ...]`` for raw ``key=value`` pairs."""
    if start == -1:
        return []
    pairs = []
    i = start
    while i < end:
        amp = data.find("&", i, end)
        if amp == -1:
            amp = end
        equals = data.find("=", i, amp)
        if equals == -1:
            # Treat "AI" without '=' as AI with an empty value; the field
            # validation reports the empty-field error.
            pairs.append(((i, amp), (amp, amp)))
        else:
            pairs.append(((i, equals), (equals + 1, amp)))
        i = amp + 1
    return pairs


@dataclass(frozen=True)
class _Decoded:
    """Result of the streaming percent-decoder.

    ``offsets[i]`` is the raw UTF-8 byte offset of the first raw byte of
    ``text[i]``. When the decoded text exceeds ``max_chars`` characters
    ``overlong`` is set and ``text``/``offsets`` hold exactly
    ``max_chars + 1`` entries -- the 21st character is enough to report
    FIELD_TOO_LONG, and the unread remainder of the span is never touched.
    """

    text: str
    offsets: tuple[int, ...]
    overlong: bool = False


def _percent_decode(
    data: str, start: int, end: int, max_chars: int | None = None
) -> _Decoded:
    """Percent-decode ``data[start:end]`` as UTF-8 in a single linear pass.

    Bytes are fed to an incremental UTF-8 decoder as they are read. Error
    precedence matches the historical assemble-then-decode behaviour: a
    malformed ``%`` escape anywhere in the span outranks an invalid-UTF-8
    failure, so a UTF-8 failure is recorded and the scan continues
    (syntax-checking only) until the end of the span; with no syntax
    error the recorded UTF-8 failure is reported. For every completed
    decoded character the raw UTF-8 byte offset of its first source byte
    is recorded immediately; offsets are never recomputed over the prefix.

    When ``max_chars`` is given the scan stops as soon as character
    ``max_chars + 1`` completes, returning it via :class:`_Decoded`
    without inspecting the rest of the span.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()

    chars: list[str] = []
    offsets: list[int] = []
    raw_byte_pos = _byte_offset(data, start)  # raw UTF-8 bytes before the span cursor
    # Origin (raw byte offset) of the first byte of the multibyte sequence
    # currently buffered inside the incremental decoder, if any.
    pending_offset: int | None = None
    # First invalid-UTF-8 failure as (raw byte offset, offending byte),
    # deferred until the whole span was syntax-checked.
    utf8_error: tuple[int, int] | None = None
    decoding = True

    def decode_chunk(chunk: bytes, chunk_offset: int):
        """Feed one assembled byte group; emit every character it completes."""
        nonlocal pending_offset, decoding, utf8_error
        try:
            out = decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            # With a pending buffer the exception object is buffer + chunk,
            # so its first byte is the pending sequence's first byte; the
            # only way a chunk fails with an empty buffer is at its own
            # first byte.
            if utf8_error is None:
                bad_offset = (
                    pending_offset if pending_offset is not None else chunk_offset
                )
                utf8_error = (bad_offset, (decoder.buffer + chunk)[exc.start])
            decoding = False  # decoder state is spent; only syntax is checked
            return
        if out:
            origin = pending_offset if pending_offset is not None else chunk_offset
            for _ch in out:
                chars.append(_ch)
                offsets.append(origin)
            # A chunk that completes a character never leaves bytes pending.
            pending_offset = None

    i = start
    while i < end:
        ch = data[i]
        if ch == "%":
            hex_ok = (
                i + 2 < end
                and data[i + 1] in HEX_DIGITS
                and data[i + 2] in HEX_DIGITS
            )
            if not hex_ok:
                raise GS1ParseError(
                    "INVALID_PERCENT_ENCODING",
                    f"Invalid percent-encoding at byte {raw_byte_pos}: "
                    f"expected %XX but found {data[i:end]!r}",
                    offset=raw_byte_pos,
                    expected="percent-encoded UTF-8 byte of the form %XX",
                    found=data[i : min(end, i + 3)],
                )
            if decoding:
                if pending_offset is None:
                    pending_offset = raw_byte_pos
                decode_chunk(bytes([int(data[i + 1 : i + 3], 16)]), raw_byte_pos)
            raw_byte_pos += 3
            i += 3
        else:
            encoded = ch.encode("utf-8")
            if decoding:
                decode_chunk(encoded, raw_byte_pos)
            raw_byte_pos += len(encoded)
            i += 1
        if decoding and max_chars is not None and len(chars) > max_chars and i < end:
            # More raw input remains beyond character max_chars + 1: stop
            # the scan here without inspecting it. A span consumed exactly
            # at the limit falls through and is reported with its exact
            # length, as before.
            return _Decoded("".join(chars), tuple(offsets), overlong=True)

    if utf8_error is not None:
        bad_offset, bad_byte = utf8_error
        raise GS1ParseError(
            "INVALID_PERCENT_ENCODING",
            "Percent-encoded bytes are not valid UTF-8: "
            f"0x{bad_byte:02X} at byte {bad_offset}",
            offset=bad_offset,
            expected="valid UTF-8 percent-encoded bytes",
            found=f"0x{bad_byte:02X}",
        ) from None

    # Flush: a dangling multibyte sequence at the end of the span is invalid.
    if decoder.buffer:
        bad_offset = pending_offset if pending_offset is not None else raw_byte_pos
        bad_byte = decoder.buffer[0]
        raise GS1ParseError(
            "INVALID_PERCENT_ENCODING",
            "Percent-encoded bytes are not valid UTF-8: "
            f"0x{bad_byte:02X} at byte {bad_offset}",
            offset=bad_offset,
            expected="valid UTF-8 percent-encoded bytes",
            found=f"0x{bad_byte:02X}",
        ) from None

    return _Decoded("".join(chars), tuple(offsets))


def _validate_uri_value(spec, value, offsets, end_offset, truncated=False):
    """Shared dispatch entry for a percent-decoded URI value."""
    return _validate_value(
        spec,
        value,
        offsets,
        "digital_link",
        end_offset=end_offset,
        truncated=truncated,
    )
