"""End-to-end tests for the /packing-audit endpoint."""

from fastapi.testclient import TestClient

from app.main import app
client = TestClient(app)

GS = "\x1d"
GTIN_RAW = "09501101530003"  # valid Mod-10 check digit
GTIN_PRODUCT = "00000000000017"  # second valid GTIN


def audit(containers, target_unit="kg"):
    return client.post(
        "/packing-audit",
        json={"target_unit": target_unit, "containers": containers},
    )


def container(container_id, max_weight, items=None, parent_id=None):
    body = {"container_id": container_id, "max_weight": max_weight, "items": items or []}
    if parent_id is not None:
        body["parent_id"] = parent_id
    return body


def item(serial, weight="3103002000", gtin=GTIN_RAW):
    """Element-string item scan with 01/21 and one weight AI (default 2 kg)."""
    return f"01{gtin}21{serial}{GS}{weight}"


def by_id(body):
    return {entry["container_id"]: entry for entry in body["containers"]}


# ---------------------------------------------------------------- happy path


def test_empty_audit_returns_empty_result():
    resp = audit([])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "containers": []}


def test_single_container_direct_weights_and_counts():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    item("S1", "3103002000"),  # 2 kg
                    item("S2", "3103003000", gtin=GTIN_PRODUCT),  # 3 kg
                ],
            )
        ]
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["container_id"] == "A"
    assert entry["parent_id"] is None
    assert entry["max_weight"] == 10.0
    assert entry["unit"] == "kg"
    assert entry["direct_weight"] == 5.0
    assert entry["total_weight"] == 5.0
    assert entry["remaining_capacity"] == 5.0
    assert entry["direct_item_count"] == 2
    assert entry["recursive_item_count"] == 2
    assert entry["over_limit"] is False


def test_leaf_to_root_rollup_keeps_request_order():
    # PAL (root) -> A -> B; declarations are roots first here.
    resp = audit(
        [
            container("PAL", 100, [item("P1", "3103006000")]),
            container(
                "A",
                100,
                [item("A1", "3103002000")],
                parent_id="PAL",
            ),
            container(
                "B",
                100,
                [item("B1", "3103001000", gtin=GTIN_PRODUCT)],
                parent_id="A",
            ),
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["container_id"] for c in body["containers"]] == ["PAL", "A", "B"]
    entries = by_id(body)
    assert entries["B"]["total_weight"] == 1.0
    assert entries["B"]["recursive_item_count"] == 1
    assert entries["A"]["direct_weight"] == 2.0
    assert entries["A"]["total_weight"] == 3.0
    assert entries["A"]["recursive_item_count"] == 2
    assert entries["PAL"]["direct_weight"] == 6.0
    assert entries["PAL"]["total_weight"] == 9.0
    assert entries["PAL"]["recursive_item_count"] == 3
    assert all(c["over_limit"] is False for c in body["containers"])


def test_containers_declared_before_their_parents_still_aggregate():
    resp = audit(
        [
            container("B", 100, [item("B1", "3103001000")], parent_id="A"),
            container("A", 100, [item("A1", "3103002000")]),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["A"]["total_weight"] == 3.0
    assert entries["A"]["recursive_item_count"] == 2
    assert entries["B"]["total_weight"] == 1.0
    assert entries["A"]["parent_id"] is None
    assert entries["B"]["parent_id"] == "A"


def test_forest_of_disjoint_roots_does_not_share_weight():
    resp = audit(
        [
            container("R1", 100, [item("S1", "3103001000")]),
            container("R2", 100, [item("S2", "3103001000")]),
            container("C2", 100, [item("S3", "3103001000")], parent_id="R2"),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["R1"]["total_weight"] == 1.0
    assert entries["R1"]["recursive_item_count"] == 1
    assert entries["R2"]["total_weight"] == 2.0
    assert entries["R2"]["recursive_item_count"] == 2


def test_mixed_kg_and_lb_scans_convert_with_exact_factor():
    # 2 kg + 25.13 lb * 0.45359237 = 2 + 11.3987762581 = 13.3987762581
    resp = audit(
        [
            container(
                "A",
                100,
                [
                    item("S1", "3103002000"),
                    item("S2", "3202002513"),
                ],
            )
        ],
        target_unit="kg",
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 13.398776
    assert entry["total_weight"] == 13.398776
    assert entry["remaining_capacity"] == 86.601224


def test_target_unit_lb_converts_kilogram_items():
    # 1 kg = 1 / 0.45359237 = 2.204622621... lb
    resp = audit(
        [container("A", 10, [item("S1", "3103001000")])], target_unit="lb"
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["unit"] == "lb"
    assert entry["direct_weight"] == 2.204623
    assert entry["total_weight"] == 2.204623
    assert entry["max_weight"] == 10.0


def test_weights_render_with_exactly_six_decimal_places():
    # 10 lb = 4.5359237 kg -> half-even 4.535924
    resp = audit([container("A", 100, [item("S1", "3200000010")])])
    assert resp.status_code == 200
    raw = resp.json()["containers"][0]
    assert raw["direct_weight"] == 4.535924
    text = resp.text
    assert '"direct_weight":4.535924' in text
    assert '"max_weight":100.000000' in text
    assert '"remaining_capacity":95.464076' in text


def test_over_limit_is_flagged_but_audit_still_returns_every_container():
    resp = audit(
        [
            container("PAL", 8, [item("P1", "3103002000")]),  # only own 2 kg
            container(
                "A",
                5,
                [
                    item("A1", "3103002000"),  # 2 kg
                    item("A2", "3103006000"),  # 6 kg -> 8 kg direct, over
                ],
                parent_id="PAL",
            ),
            container(
                "B",
                100,
                [item("B1", "3103001000")],  # 1 kg
                parent_id="A",
            ),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    # B is within its own capacity.
    assert entries["B"]["total_weight"] == 1.0
    assert entries["B"]["over_limit"] is False
    assert entries["B"]["remaining_capacity"] == 99.0
    # A holds 9 kg (8 direct + 1 child) against 5.
    assert entries["A"]["direct_weight"] == 8.0
    assert entries["A"]["total_weight"] == 9.0
    assert entries["A"]["over_limit"] is True
    assert entries["A"]["remaining_capacity"] == -4.0
    # PAL holds 11 kg (2 + 9) against 8 even though its own goods fit.
    assert entries["PAL"]["total_weight"] == 11.0
    assert entries["PAL"]["over_limit"] is True
    assert entries["PAL"]["remaining_capacity"] == -3.0


def test_exact_capacity_is_not_over_limit():
    resp = audit([container("A", 2, [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False
    assert entry["remaining_capacity"] == 0.0


def test_empty_containers_roll_up_zero_weight():
    resp = audit(
        [
            container("PAL", 10, []),
            container("A", 10, [], parent_id="PAL"),
        ]
    )
    assert resp.status_code == 200
    for entry in resp.json()["containers"]:
        assert entry["direct_weight"] == 0.0
        assert entry["total_weight"] == 0.0
        assert entry["remaining_capacity"] == 10.0
        assert entry["direct_item_count"] == 0
        assert entry["recursive_item_count"] == 0
        assert entry["over_limit"] is False


def test_digital_link_scans_are_accepted():
    scan = (
        "https://id.gs1.org/01/" + GTIN_RAW + "/21/SN0001?3103=002513"
    )  # 2.513 kg
    resp = audit([container("A", 10, [scan])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 2.513
    assert entry["direct_item_count"] == 1


def test_string_max_weight_is_exact_decimal():
    resp = audit([container("A", "1.5", [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["max_weight"] == 1.5
    assert entry["over_limit"] is True
    assert entry["remaining_capacity"] == -0.5


# ---------------------------------------------- huge-capacity exact arithmetic


def test_huge_capacity_keeps_every_digit_of_the_load():
    # 1e60 vastly exceeds the default 50-digit arithmetic precision; a
    # fixed-context subtraction would round the 2 kg load away entirely.
    huge = "1" + "0" * 60
    resp = audit([container("HUGE", huge, [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 2.0
    assert entry["total_weight"] == 2.0
    assert entry["over_limit"] is False
    # Every one of the capacity's 60 integer digits is preserved: a fixed
    # 50-digit context renders the remaining capacity as exactly 1e60.
    assert f'"max_weight":{huge}.000000' in resp.text
    assert f'"remaining_capacity":{"9" * 59}8.000000' in resp.text
    assert f'"remaining_capacity":{huge}.000000' not in resp.text


def test_huge_capacity_empty_container_is_unaffected():
    huge = "1" + "0" * 60
    resp = audit([container("HUGE", huge, [])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["direct_weight"] == 0.0
    assert entry["total_weight"] == 0.0
    assert entry["over_limit"] is False
    assert f'"max_weight":{huge}.000000' in resp.text
    assert f'"remaining_capacity":{huge}.000000' in resp.text


def test_huge_capacity_with_fractional_tail_keeps_every_digit():
    # A capacity of 1e60 - 0.5 carries 61 significant digits; the empty
    # container's remaining capacity must preserve the .5 instead of
    # rounding up to 1e60 at the fixed 50-digit arithmetic context.
    cap = "9" * 60 + ".5"
    resp = audit([container("HUGE", cap, [])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False
    assert f'"remaining_capacity":{cap}00000' in resp.text
    assert '"remaining_capacity":' + "1" + "0" * 60 + ".000000" not in resp.text


def test_huge_capacity_with_fractional_tail_keeps_lb_conversion_digits():
    # In lb target the 2 kg load converts to a 50-significant-digit factor,
    # so 1e60 minus that load needs over a hundred digits to stay exact.
    huge = "1" + "0" * 60
    resp = audit(
        [container("HUGE", huge, [item("S1", "3103002000")])], target_unit="lb"
    )
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False
    token = resp.text.split('"remaining_capacity":')[1].split(",")[0]
    assert token != huge + ".000000"  # the load must not round away
    assert token.endswith("5.590755")  # 1e60 - 4.409245... lb


def test_huge_root_keeps_rolled_up_child_load_exact():
    huge = "1" + "0" * 60
    resp = audit(
        [
            container("ROOT", huge, []),
            container("CHILD", 100, [item("C1", "3103002000")], parent_id="ROOT"),
        ]
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["CHILD"]["total_weight"] == 2.0
    assert entries["ROOT"]["direct_weight"] == 0.0
    assert entries["ROOT"]["total_weight"] == 2.0
    assert entries["ROOT"]["recursive_item_count"] == 1
    assert entries["ROOT"]["over_limit"] is False
    assert f'"remaining_capacity":{"9" * 59}8.000000' in resp.text
    assert f'"remaining_capacity":{huge}.000000' not in resp.text


def test_over_limit_uses_exact_value_when_overload_is_below_six_places():
    # Capacity is 2 kg minus 1e-10: the 2 kg load overshoots by 1e-10,
    # which half-even six-place rounding renders as an exact match.
    resp = audit([container("A", "1.9999999999", [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is True
    assert '"remaining_capacity":-0.000000' in resp.text


def test_six_place_rendering_hides_positive_remainder_without_flagging_over():
    # 2 kg against a capacity of 2 kg + 1e-7 fits exactly on the unrounded
    # values even though the rendered remaining capacity rounds to zero.
    resp = audit([container("A", "2.0000001", [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False
    assert '"remaining_capacity":0.000000' in resp.text


# ------------------------------------------------- capacity envelope / DoS guard


def test_capacity_at_integer_envelope_boundary_returns_full_audit():
    # 61 integer digits (1e61 - 1) is the largest accepted magnitude; the
    # 2 kg load must still be visible in the six-place rendering.
    cap = "9" * 61
    resp = audit([container("EDGE", cap, [item("S1", "3103002000")])])
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is False
    assert f'"max_weight":{cap}.000000' in resp.text
    assert f'"remaining_capacity":{"9" * 60}7.000000' in resp.text


def test_capacity_at_fractional_envelope_boundary_returns_full_audit():
    # 50 decimal places is the deepest accepted fractional scale; a load
    # larger than the capacity by a sub-six-place amount must still be
    # flagged over by the exact comparison, with a rendered -0.000000.
    cap = "0." + "0" * 9 + "9" * 41  # 50 decimal places, ~9.9999e-10
    resp = audit([container("EDGE", cap, [item("S1", "3109000001")])])  # 1e-9 kg
    assert resp.status_code == 200
    entry = resp.json()["containers"][0]
    assert entry["over_limit"] is True
    assert '"remaining_capacity":-0.000000' in resp.text


def test_capacity_one_integer_digit_beyond_envelope_is_rejected():
    cap = "1" + "0" * 61  # 1e61: 62 integer digits
    resp = audit([container("HUGE", cap, [])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CAPACITY_OUT_OF_RANGE"
    assert detail["container_index"] == 0
    assert "integer digits" in detail["error"]["message"]
    assert detail["error"]["found"].startswith("62 integer digits")


def test_capacity_one_decimal_place_beyond_envelope_is_rejected():
    cap = "0." + "9" * 51
    resp = audit([container("DEEP", cap, [])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CAPACITY_OUT_OF_RANGE"
    assert detail["container_index"] == 0
    assert "decimal places" in detail["error"]["message"]


def test_capacity_error_locates_the_offending_container_index():
    resp = audit(
        [
            container("OK-1", 10, [item("S1", "3103001000")]),
            container("OK-2", 10, []),
            container("BAD", "1E999999", []),  # compact exponent bomb
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 2
    assert detail["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_huge_positive_exponent_bomb_is_rejected():
    # A single compact token otherwise asks for a million-digit context.
    resp = audit([container("BOMB", "1E999999", [])])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_huge_negative_exponent_bomb_is_rejected():
    resp = audit([container("BOMB", "1E-999999", [])])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_dense_long_digit_string_is_rejected():
    # A 200 000-digit dense coefficient (the memory-starvation axis) is
    # rejected before any subtraction context is built.
    resp = audit([container("BOMB", "9" * 200_000, [])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CAPACITY_OUT_OF_RANGE"
    assert detail["container_index"] == 0


def test_leading_zero_dense_fraction_is_rejected():
    # 0.00..001 with 100k zeros: the exponent, not the coefficient, is
    # what makes this dangerous.
    resp = audit([container("BOMB", "0." + "0" * 100_000 + "1", [])])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_cross_axis_capacity_stays_fast_and_is_rejected():
    # Dense integer block *and* deep fractional tail: the original
    # integer_digits + fractional_digits context expansion.  The request
    # must be rejected quickly instead of allocating the digits.
    payload = {"target_unit": "kg", "containers": []}
    cap_text = "9" * 200_000 + "." + "9" * 200_000
    payload["containers"].append(
        {"container_id": "BOMB", "max_weight": cap_text, "items": []}
    )
    resp = client.post("/packing-audit", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_exponent_normalized_coefficient_cannot_evade_envelope():
    # 50 coefficient digits shifted down 150 places means 150 fractional
    # places: the adjusted exponent, not the raw text, is the boundary.
    resp = audit([container("BOMB", "9" * 50 + "E-150", [])])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_capacity_bomb_is_rejected_without_touching_other_container_items():
    # Validation runs before item parsing: a bad capacity cannot be hidden
    # behind an item that would itself be expensive or invalid.
    resp = audit(
        [
            container("GOOD", 10, [item("S1", "3103001000")]),
            container("BAD", "1E700000", [item("S2", "3103001000")]),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 1
    assert detail["error"]["error"] == "CAPACITY_OUT_OF_RANGE"


def test_huge_capacity_60_still_audits_load_and_nested_rollup():
    # The documented 1e60 case must keep working end to end, including the
    # nested roll-up and the sub-six-place over_limit semantics.
    huge = "1" + "0" * 60
    resp = audit(
        [
            container("ROOT", huge, []),
            container(
                "CHILD",
                100,
                [item("C1", "3103002000"), item("C2", "3109000001")],
                parent_id="ROOT",
            ),
        ],
        target_unit="lb",
    )
    assert resp.status_code == 200
    entries = by_id(resp.json())
    assert entries["ROOT"]["over_limit"] is False
    assert entries["ROOT"]["recursive_item_count"] == 2
    token = resp.text.split('"remaining_capacity":')[1].split(",")[0]
    assert token != huge + ".000000"  # the load did not round away


# ------------------------------------------------------------ structural errors


def test_duplicate_container_id_is_rejected_with_first_index():
    resp = audit(
        [
            container("A", 10),
            container("B", 10),
            container("A", 10),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_CONTAINER"
    assert detail["container_index"] == 2
    assert detail["first_container_index"] == 0


def test_unknown_parent_is_rejected():
    resp = audit([container("A", 10, parent_id="GHOST")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "UNKNOWN_PARENT"
    assert detail["container_index"] == 0
    assert detail["error"]["found"] == "GHOST"


def test_self_parent_is_a_cycle():
    resp = audit([container("A", 10, parent_id="A")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["container_index"] == 0
    assert detail["cycle"] == [0]


def test_two_node_cycle_is_rejected():
    resp = audit(
        [
            container("A", 10, parent_id="B"),
            container("B", 10, parent_id="A"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["cycle"] == [0, 1]
    assert detail["container_index"] == 0


def test_cycle_rotation_starts_at_lowest_index():
    resp = audit(
        [
            container("R", 10),
            container("A", 10, parent_id="C"),
            container("B", 10, parent_id="A"),
            container("C", 10, parent_id="B"),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "CYCLE_DETECTED"
    assert detail["cycle"] == [1, 3, 2]
    assert detail["container_index"] == 1


def test_duplicate_item_across_containers_locates_both_occurrences():
    resp = audit(
        [
            container("A", 10, [item("SN1", "3103002000")]),
            container("B", 10, [item("SN1", "3103002000")]),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert detail["container_index"] == 1
    assert detail["item_index"] == 0
    assert detail["first_container_index"] == 0
    assert detail["first_item_index"] == 0


def test_duplicate_item_within_one_container_is_rejected():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    item("SN1", "3103001000"),
                    item("SN2", "3103001000"),
                    item("SN1", "3103001000"),
                ],
            )
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert detail["container_index"] == 0
    assert detail["item_index"] == 2
    assert detail["first_container_index"] == 0
    assert detail["first_item_index"] == 0


def test_same_serial_under_different_gtins_are_distinct_items():
    resp = audit(
        [
            container("A", 10, [item("SAME", "3103001000", gtin=GTIN_RAW)]),
            container("B", 10, [item("SAME", "3103001000", gtin=GTIN_PRODUCT)]),
        ]
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------- scan errors


def test_parse_error_locates_container_and_item():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    item("S1", "3103001000"),
                    f"01{GTIN_RAW}21S2{GS}310300",  # truncated weight field
                ],
            )
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 1
    assert detail["error"]["error"] == "FIELD_TOO_SHORT"


def test_missing_gtin_is_rejected_with_indices():
    resp = audit(
        [container("A", 10, [f"21SN0001{GS}3103002000"])]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 01" in detail["error"]["message"]


def test_missing_serial_is_rejected_with_indices():
    resp = audit([container("A", 10, [f"01{GTIN_RAW}3103002000"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 0
    assert detail["item_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 21" in detail["error"]["message"]


def test_missing_weight_is_rejected_with_indices():
    resp = audit([container("A", 10, [f"01{GTIN_RAW}21SN0001"])])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "MISSING_AI"
    assert "weight AI" in detail["error"]["message"]


def test_two_weight_ais_are_rejected():
    # Both weights are fixed-length fields, so they sit back to back
    # without a separator; the second 310n/320n must still be rejected.
    resp = audit(
        [
            container(
                "A",
                10,
                [f"01{GTIN_RAW}21S1{GS}31030020003202002000"],
            )
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103, 3202" in detail["error"]["found"]


def test_bad_check_digit_locates_container_and_item():
    resp = audit(
        [
            container("A", 10),
            container(
                "B",
                10,
                [
                    item("OK", "3103001000"),
                    item("BAD", "3103001000", gtin="09501101530004"),
                ],
            ),
        ]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["container_index"] == 1
    assert detail["item_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_optional_batch_and_expiry_are_allowed():
    resp = audit(
        [
            container(
                "A",
                10,
                [
                    f"01{GTIN_RAW}1725013110B1{GS}21S1{GS}3103002000",
                ],
            )
        ]
    )
    assert resp.status_code == 200
    assert resp.json()["containers"][0]["direct_weight"] == 2.0


# --------------------------------------------------------------- request shape


def test_negative_max_weight_is_rejected_by_validation():
    resp = audit([container("A", -1)])
    assert resp.status_code == 422


def test_empty_container_id_is_rejected_by_validation():
    resp = audit([container("", 10)])
    assert resp.status_code == 422


def test_missing_items_field_is_rejected_by_validation():
    resp = client.post(
        "/packing-audit",
        json={"containers": [{"container_id": "A", "max_weight": 10}]},
    )
    assert resp.status_code == 422


def test_unknown_target_unit_is_rejected_by_validation():
    resp = client.post(
        "/packing-audit",
        json={
            "target_unit": "stone",
            "containers": [container("A", 10)],
        },
    )
    assert resp.status_code == 422
