"""Tests for the immutable declarative AI registry.

Covers three things:

* the catalogue built at import time has the exact and family members
  with the right length/charset/semantic metadata and is immutable;
* :meth:`AIRegistry.build` rejects every kind of inconsistent
  declaration up front (ambiguity, duplicates, missing semantic
  processor, bad length/charset metadata);
* the shared recognizer resolves complete keys (Digital Link) and
  longest-match stream tokens (element strings) identically, including
  the family-prefix INVALID_PRECISION rule.
"""

import dataclasses

import pytest

from app.gs1 import (
    DIGITS,
    AI82,
    AISpec,
    AIRegistry,
    ExactAIDecl,
    FamilyAIDecl,
    REGISTRY,
    RegistryDefinitionError,
)


# ------------------------------------------------------------------ import-time registry


def test_family_members_expanded_for_every_precision_digit():
    for prefix, unit, name in (("310", "kg", "NET WEIGHT (KG)"),
                               ("320", "lb", "NET WEIGHT (LB)")):
        for n in range(10):
            ai = f"{prefix}{n}"
            spec = REGISTRY.specs[ai]
            assert spec.ai == ai
            assert spec.name == name
            assert spec.unit == unit
            assert spec.ai_length == 4
            assert spec.fixed_length is True
            assert spec.min_length == spec.max_length == 6
            assert spec.charset is DIGITS
            assert spec.decimals == n
            assert spec.semantic == "weight"


def test_exact_ai_metadata():
    gtin = REGISTRY.specs["01"]
    assert (gtin.min_length, gtin.max_length, gtin.fixed_length) == (14, 14, True)
    assert gtin.ai_length == 2 and gtin.semantic == "gtin"

    expiry = REGISTRY.specs["17"]
    assert (expiry.min_length, expiry.max_length, expiry.fixed_length) == (6, 6, True)
    assert expiry.semantic == "date"

    for ai, name in (("10", "BATCH/LOT"), ("21", "SERIAL")):
        spec = REGISTRY.specs[ai]
        assert spec.name == name
        assert spec.fixed_length is False
        assert (spec.min_length, spec.max_length) == (1, 20)
        assert spec.charset is AI82
        assert spec.semantic is None
        assert spec.unit is None and spec.decimals is None


def test_family_prefix_index_matches_expansion():
    assert set(REGISTRY.families) == {"310", "320"}
    assert {f"{p}{n}" for p in REGISTRY.families for n in range(10)} <= set(
        REGISTRY.specs
    )
    assert REGISTRY.family_prefix_len == 3
    assert REGISTRY.stream_probe_len == 4


def test_specs_mapping_is_read_only():
    with pytest.raises(TypeError):
        REGISTRY.specs["99"] = None
    with pytest.raises(TypeError):
        REGISTRY.families["310"] = None
    with pytest.raises(TypeError):
        REGISTRY.exact["01"] = None
    with pytest.raises(TypeError):
        del REGISTRY.specs["01"]


def test_specs_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        REGISTRY.specs["01"].name = "OTHER"
    with pytest.raises(dataclasses.FrozenInstanceError):
        REGISTRY.specs["3103"].decimals = 9


def test_registry_has_no_mutable_attributes():
    with pytest.raises(AttributeError):
        REGISTRY.specs = None  # type: ignore[misc]


def test_semantic_handlers_are_complete_for_every_declared_spec():
    # Startup validation guarantees a resolvable handler for every spec
    # that names one; specs without semantics must find None.
    for spec in REGISTRY.specs.values():
        if spec.semantic is None:
            assert spec.ai in ("10", "21")
        else:
            assert spec.semantic in REGISTRY.semantic_handlers


# --------------------------------------------------------------- build-time validation


def _handlers(**extra):
    def passthrough(spec, value, offsets):
        return None, None

    handlers = {"gtin": passthrough, "date": passthrough, "weight": passthrough}
    handlers.update(extra)
    return handlers


def test_build_rejects_duplicate_exact_ai():
    decls = (
        ExactAIDecl("01", "A", 1, 1, True, DIGITS, "digits"),
        ExactAIDecl("01", "B", 1, 1, True, DIGITS, "digits"),
    )
    with pytest.raises(RegistryDefinitionError, match="duplicate exact AI"):
        AIRegistry.build(decls, (), _handlers())


def test_build_rejects_non_numeric_exact_key():
    decls = (ExactAIDecl("1A", "A", 1, 1, True, DIGITS, "digits"),)
    with pytest.raises(RegistryDefinitionError, match="numeric key"):
        AIRegistry.build(decls, (), _handlers())


def test_build_rejects_undefined_semantic_handler():
    decls = (ExactAIDecl("01", "GTIN", 1, 1, True, DIGITS, "digits", semantic="nope"),)
    with pytest.raises(RegistryDefinitionError, match="semantic"):
        AIRegistry.build(decls, (), _handlers())


def test_build_rejects_inconsistent_length_bounds():
    decls = (ExactAIDecl("01", "A", 5, 2, True, DIGITS, "digits"),)
    with pytest.raises(RegistryDefinitionError, match="length"):
        AIRegistry.build(decls, (), _handlers())


def test_build_rejects_fixed_length_with_unequal_bounds():
    decls = (ExactAIDecl("01", "A", 2, 5, True, DIGITS, "digits"),)
    with pytest.raises(RegistryDefinitionError, match="min_length == max_length"):
        AIRegistry.build(decls, (), _handlers())


def test_build_rejects_missing_charset_metadata():
    decls = (ExactAIDecl("01", "A", 1, 1, True, frozenset(), "digits"),)
    with pytest.raises(RegistryDefinitionError, match="charset"):
        AIRegistry.build(decls, (), _handlers())


def test_build_rejects_empty_catalogue():
    with pytest.raises(RegistryDefinitionError, match="at least one AI"):
        AIRegistry.build((), (), _handlers())


def test_build_rejects_duplicate_family_prefix():
    fams = (
        FamilyAIDecl("310", "W", "kg", 6, DIGITS, "digits", "weight"),
        FamilyAIDecl("310", "X", "lb", 6, DIGITS, "digits", "weight"),
    )
    with pytest.raises(RegistryDefinitionError, match="duplicate family"):
        AIRegistry.build((), fams, _handlers())


def test_build_rejects_exact_key_equal_to_family_prefix():
    decls = (ExactAIDecl("310", "A", 1, 1, True, DIGITS, "digits"),)
    fams = (FamilyAIDecl("310", "W", "kg", 6, DIGITS, "digits", "weight"),)
    with pytest.raises(RegistryDefinitionError, match="ambiguous"):
        AIRegistry.build(decls, fams, _handlers())


def test_build_rejects_family_member_colliding_with_exact_key():
    # Exact key "3103" would be shadowed by family prefix "310" + param "3".
    decls = (ExactAIDecl("3103", "A", 4, 4, True, DIGITS, "digits"),)
    fams = (FamilyAIDecl("310", "W", "kg", 6, DIGITS, "digits", "weight"),)
    with pytest.raises(RegistryDefinitionError, match="ambiguous"):
        AIRegistry.build(decls, fams, _handlers())


def test_build_rejects_family_prefix_extending_an_exact_key():
    # Exact key "31" is a prefix of family prefix "310": ambiguous.
    decls = (ExactAIDecl("31", "A", 2, 2, True, DIGITS, "digits"),)
    fams = (FamilyAIDecl("310", "W", "kg", 6, DIGITS, "digits", "weight"),)
    with pytest.raises(RegistryDefinitionError, match="ambiguous"):
        AIRegistry.build(decls, fams, _handlers())


def test_build_rejects_inconsistent_family_prefix_lengths():
    fams = (
        FamilyAIDecl("310", "W", "kg", 6, DIGITS, "digits", "weight"),
        FamilyAIDecl("3201", "X", "lb", 6, DIGITS, "digits", "weight"),
    )
    with pytest.raises(RegistryDefinitionError, match="same length"):
        AIRegistry.build((), fams, _handlers())


def test_build_rejects_family_without_semantic_handler():
    fams = (FamilyAIDecl("310", "W", "kg", 6, DIGITS, "digits", "missing"),)
    with pytest.raises(RegistryDefinitionError, match="semantic"):
        AIRegistry.build((), fams, _handlers())


def test_build_rejects_family_with_empty_parameter_set():
    fams = (
        FamilyAIDecl(
            "310", "W", "kg", 6, DIGITS, "digits", "weight",
            parameter_chars=frozenset(),
        ),
    )
    with pytest.raises(RegistryDefinitionError, match="parameter"):
        AIRegistry.build((), fams, _handlers())


# --------------------------------------------------------------- shared recognizer


def test_recognize_complete_keys():
    assert REGISTRY.recognize("01", complete=True).spec.ai == "01"
    assert REGISTRY.recognize("10", complete=True).spec.ai == "10"
    result = REGISTRY.recognize("3207", complete=True)
    assert result.kind == "ok" and result.spec.ai == "3207" and result.spec.decimals == 7


def test_recognize_complete_unknown_numeric_is_unsupported():
    for key in ("99", "999", "9999", "31030"):
        result = REGISTRY.recognize(key, complete=True)
        assert result.kind == "unsupported"
        assert result.found == key
        assert result.spec is None


def test_recognize_complete_non_numeric_is_invalid_ai():
    result = REGISTRY.recognize("gtin", complete=True)
    assert result.kind == "invalid_ai" and result.found == "gtin"


def test_recognize_complete_empty_key_is_invalid_ai():
    assert REGISTRY.recognize("", complete=True).kind == "invalid_ai"


def test_recognize_complete_family_missing_parameter():
    result = REGISTRY.recognize("310", complete=True)
    assert result.kind == "invalid_precision" and result.short is True


def test_recognize_complete_family_bad_parameter():
    result = REGISTRY.recognize("310x", complete=True)
    assert result.kind == "invalid_precision"
    assert result.short is False and result.error_index == 3


def test_recognize_stream_longest_match_prefers_family_member():
    result = REGISTRY.recognize("3103", complete=False)
    assert result.kind == "ok" and result.spec.ai == "3103"
    # Only the prefix + parameter is consumed; trailing value characters
    # belong to the field and never influence recognition.
    result = REGISTRY.recognize("3103002513", complete=False)
    assert result.spec.ai == "3103"


def test_recognize_stream_non_family_uses_two_character_key():
    result = REGISTRY.recognize("0109501101530003", complete=False)
    assert result.kind == "ok" and result.spec.ai == "01"


def test_recognize_stream_unknown_ai_reports_two_character_head():
    # "3110..." is not a family prefix; longest match falls back to "31".
    result = REGISTRY.recognize("3110000000", complete=False)
    assert result.kind == "unsupported" and result.found == "31"
    result = REGISTRY.recognize("99ABC", complete=False)
    assert result.kind == "unsupported" and result.found == "99"


def test_recognize_stream_non_numeric_head_is_invalid_ai():
    result = REGISTRY.recognize("9", complete=False)
    assert result.kind == "invalid_ai" and result.found == "9"
    result = REGISTRY.recognize("1A", complete=False)
    assert result.kind == "invalid_ai" and result.found == "1A"


def test_recognize_stream_family_missing_parameter():
    result = REGISTRY.recognize("310", complete=False)
    assert result.kind == "invalid_precision" and result.short is True


def test_recognize_stream_family_bad_parameter():
    result = REGISTRY.recognize("320x", complete=False)
    assert result.kind == "invalid_precision" and result.error_index == 3


def test_kg_and_lb_families_are_distinct():
    kg = REGISTRY.recognize("3103", complete=True).spec
    lb = REGISTRY.recognize("3203", complete=True).spec
    assert kg.unit == "kg" and lb.unit == "lb"
    assert kg.ai != lb.ai
