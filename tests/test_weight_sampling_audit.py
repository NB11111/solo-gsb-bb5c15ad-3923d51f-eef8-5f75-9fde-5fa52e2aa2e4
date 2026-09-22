"""End-to-end tests for the /weight-sampling-audit endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS = "\x1d"
GTIN_RAW = "09501101530003"  # valid Mod-10 check digit
GTIN_PRODUCT = "00000000000017"  # second valid GTIN


def plan(
    gtin=GTIN_RAW,
    batch="B1",
    min_weight="1.9",
    max_weight="2.1",
    n1=5,
    a1=0,
    r1=2,
    n2=10,
    a2=1,
    r2=2,
):
    return {
        "gtin": gtin,
        "batch": batch,
        "min_weight": min_weight,
        "max_weight": max_weight,
        "n1": n1,
        "a1": a1,
        "r1": r1,
        "n2": n2,
        "a2": a2,
        "r2": r2,
    }


def sample(serial, weight="3103002000", stage=1, gtin=GTIN_RAW, batch="B1"):
    """Element-string sample scan with 01/10/21 and one weight AI (default 2 kg)."""
    return {"stage": stage, "scan": f"01{gtin}10{batch}{GS}21{serial}{GS}{weight}"}


def audit(plans, samples=None, target_unit="kg"):
    return client.post(
        "/weight-sampling-audit",
        json={
            "target_unit": target_unit,
            "plans": plans,
            "samples": samples or [],
        },
    )


def good_stage1(serial_prefix="S", count=5, weight="3103002000"):
    return [sample(f"{serial_prefix}{i}", weight) for i in range(1, count + 1)]


# --------------------------------------------------------------- basic shape


def test_empty_audit_returns_empty_result():
    resp = audit([])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "plans": []}


def test_plan_without_samples_is_pending_with_zero_counts():
    resp = audit([plan()])
    assert resp.status_code == 200
    entry = resp.json()["plans"][0]
    assert entry["stage1_count"] == 0
    assert entry["stage1_defects"] == 0
    assert entry["stage2_count"] == 0
    assert entry["stage2_defects"] == 0
    assert entry["total_count"] == 0
    assert entry["total_defects"] == 0
    assert entry["out_of_range_samples"] == []
    assert entry["decision"] == "pending"


def test_response_echoes_plan_parameters_in_request_order():
    p1 = plan(batch="B1")
    p2 = plan(gtin=GTIN_PRODUCT, batch="B2", min_weight="0", max_weight="9")
    resp = audit([p1, p2])
    assert resp.status_code == 200
    plans = resp.json()["plans"]
    assert [p["plan_index"] for p in plans] == [0, 1]
    assert plans[0]["gtin"] == GTIN_RAW
    assert plans[0]["batch"] == "B1"
    assert plans[1]["gtin"] == GTIN_PRODUCT
    assert plans[1]["batch"] == "B2"
    assert all(
        (p["n1"], p["a1"], p["r1"], p["n2"], p["a2"], p["r2"]) == (5, 0, 2, 10, 1, 2)
        for p in plans
    )


def test_interval_renders_with_six_decimal_places_as_raw_json_number():
    resp = audit([plan()])
    assert resp.status_code == 200
    text = resp.text
    assert '"min_weight":1.900000' in text
    assert '"max_weight":2.100000' in text
    entry = resp.json()["plans"][0]
    assert entry["min_weight"] == 1.9
    assert entry["max_weight"] == 2.1
    assert entry["unit"] == "kg"


# ------------------------------------------------------------- stage 1 rules


def test_stage1_full_with_no_defects_accepts():
    resp = audit([plan()], good_stage1())
    entry = resp.json()["plans"][0]
    assert entry["stage1_count"] == 5
    assert entry["stage1_defects"] == 0
    assert entry["total_count"] == 5
    assert entry["decision"] == "accept"


def test_stage1_full_with_defects_at_or_above_r1_rejects():
    samples = [
        sample("S1", "3103002500"),  # 2.5 kg, defect
        sample("S2", "3103001500"),  # 1.5 kg, defect
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 2
    assert entry["out_of_range_samples"] == [0, 1]
    assert entry["decision"] == "reject"


def test_stage1_incomplete_is_pending_even_when_below_a1():
    resp = audit([plan()], good_stage1(count=4))
    assert resp.json()["plans"][0]["decision"] == "pending"


def test_stage1_incomplete_is_pending_even_when_at_r1():
    samples = [sample("S1", "3103002500"), sample("S2", "3103002500")]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_count"] == 2
    assert entry["stage1_defects"] == 2
    assert entry["decision"] == "pending"


def test_stage1_count_between_a1_and_r1_continues_to_stage_two():
    samples = [
        sample("S1", "3103002500"),  # the single stage-1 defect (a1=0 < 1 < r1=2)
        sample("S2", "3103002000"),
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 1
    assert entry["stage2_count"] == 0
    assert entry["decision"] == "pending"


# ------------------------------------------------------------- stage 2 rules


def continuing_samples():
    """Stage 1 full with exactly one defect (inconclusive)."""
    return [
        sample("S1", "3103002500"),
        sample("S2", "3103002000"),
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ]


def test_stage2_cumulative_at_a2_accepts_only_after_n2():
    samples = continuing_samples() + [
        sample("S6", "3103002000", stage=2),
        sample("S7", "3103002000", stage=2),
        sample("S8", "3103002000", stage=2),
        sample("S9", "3103002000", stage=2),
        sample("S10", "3103002000", stage=2),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage2_count"] == 5
    assert entry["stage2_defects"] == 0
    assert entry["total_count"] == 10
    assert entry["total_defects"] == 1  # 1 <= a2 -> accept
    assert entry["out_of_range_samples"] == [0]
    assert entry["decision"] == "accept"


def test_stage2_cumulative_at_r2_rejects():
    samples = continuing_samples() + [
        sample("S6", "3103002500", stage=2),  # second cumulative defect
        sample("S7", "3103002000", stage=2),
        sample("S8", "3103002000", stage=2),
        sample("S9", "3103002000", stage=2),
        sample("S10", "3103002000", stage=2),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage2_defects"] == 1
    assert entry["total_defects"] == 2  # 2 >= r2 -> reject
    assert entry["out_of_range_samples"] == [0, 5]
    assert entry["decision"] == "reject"


def test_stage2_short_of_n2_stays_pending():
    samples = continuing_samples() + [
        sample("S6", "3103002000", stage=2),
        sample("S7", "3103002000", stage=2),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage2_count"] == 2
    assert entry["total_count"] == 7
    assert entry["decision"] == "pending"


def test_stage2_full_but_cumulative_count_still_between_numbers_is_pending():
    # widen the gap: a2=1, r2=3, two cumulative defects at n2 -> neither rule
    wide = plan(a2=1, r2=3)
    samples = continuing_samples() + [
        sample("S6", "3103002500", stage=2),
        sample("S7", "3103002000", stage=2),
        sample("S8", "3103002000", stage=2),
        sample("S9", "3103002000", stage=2),
        sample("S10", "3103002000", stage=2),
    ]
    resp = audit([wide], samples)
    entry = resp.json()["plans"][0]
    assert entry["total_count"] == 10
    assert entry["total_defects"] == 2
    assert entry["decision"] == "pending"


def test_stage_two_after_stage_one_acceptance_is_rejected_at_first_sample():
    # Stage 1 full with 0 defects accepts immediately; every later
    # stage-2 sample is structurally impossible and refused with 422.
    samples = good_stage1() + [
        sample("S6", "3103002500", stage=2),
        sample("S7", "3103002500", stage=2),
        sample("S8", "3103002500", stage=2),
        sample("S9", "3103002500", stage=2),
        sample("S10", "3103002500", stage=2),
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_AFTER_DECISION"
    assert detail["sample_index"] == 5
    assert detail["plan_index"] == 0
    assert detail["error"]["decision"] == "accept"
    assert detail["error"]["expected_stage"] == 1
    assert detail["error"]["found_stage"] == 2

    # The refused draws must not change the response for the legal prefix.
    resp_ok = audit([plan()], good_stage1())
    entry = resp_ok.json()["plans"][0]
    assert entry["stage1_defects"] == 0
    assert entry["stage2_count"] == 0
    assert entry["stage2_defects"] == 0
    assert entry["total_count"] == 5
    assert entry["total_defects"] == 0
    assert entry["out_of_range_samples"] == []
    assert entry["decision"] == "accept"


def test_stage_two_after_stage_one_rejection_is_rejected_with_decision():
    samples = [
        sample("S1", "3103002500"),
        sample("S2", "3103002500"),
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ] + [
        sample("S6", "3103002000", stage=2),
        sample("S7", "3103002000", stage=2),
        sample("S8", "3103002000", stage=2),
        sample("S9", "3103002000", stage=2),
        sample("S10", "3103002000", stage=2),
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_AFTER_DECISION"
    assert detail["sample_index"] == 5
    assert detail["plan_index"] == 0
    assert detail["error"]["decision"] == "reject"
    assert detail["error"]["expected_stage"] == 1
    assert detail["error"]["found_stage"] == 2

    # Legal prefix keeps the stage-1 rejection with no stage-2 tallies.
    resp_ok = audit([plan()], samples[:5])
    entry = resp_ok.json()["plans"][0]
    assert entry["stage2_count"] == 0
    assert entry["total_count"] == 5
    assert entry["total_defects"] == 2
    assert entry["decision"] == "reject"


# ------------------------------------------------------------- interval rules


def test_closed_interval_boundaries_are_conforming():
    samples = [
        sample("S1", "3103001900"),  # exactly 1.9 kg
        sample("S2", "3103002100"),  # exactly 2.1 kg
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 0
    assert entry["out_of_range_samples"] == []
    assert entry["decision"] == "accept"


def test_weights_strictly_outside_bounds_are_defects():
    samples = [
        sample("S1", "3103001899"),  # 1.899 kg < 1.9
        sample("S2", "3103002101"),  # 2.101 kg > 2.1
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 2
    assert entry["out_of_range_samples"] == [0, 1]


def test_mixed_kg_and_lb_scans_compare_in_target_unit():
    # 2 kg and 25.13 lb * 0.45359237 = 11.3987762581 kg; interval [1, 12] kg
    # accepts both.
    wide = plan(min_weight="1", max_weight="12")
    samples = [
        sample("S1", "3103002000"),
        sample("S2", "3202002513"),
        sample("S3", "3103005000"),
        sample("S4", "3103005000"),
        sample("S5", "3103005000"),
    ]
    resp = audit([wide], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 0
    assert entry["decision"] == "accept"

    # The same 25.13 lb item is a defect against a tight [1, 2.1] kg interval.
    samples = [
        sample("S1", "3103002000"),
        sample("S2", "3202002513"),
        sample("S3", "3103002000"),
        sample("S4", "3103002000"),
        sample("S5", "3103002000"),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 1
    assert entry["out_of_range_samples"] == [1]


def test_target_unit_lb_compares_kilogram_scans_after_conversion():
    # 2 kg = 4.409245... lb, inside [4, 4.5] lb.
    lb_plan = plan(min_weight="4", max_weight="4.5")
    resp = audit([lb_plan], good_stage1(), target_unit="lb")
    entry = resp.json()["plans"][0]
    assert entry["unit"] == "lb"
    assert entry["min_weight"] == 4.0
    assert entry["stage1_defects"] == 0
    assert entry["decision"] == "accept"


def test_out_of_range_indices_span_both_stages_and_keep_request_indices():
    samples = continuing_samples() + [
        sample("S6", "3103001000", stage=2),  # defect
        sample("S7", "3103002000", stage=2),
        sample("S8", "3103001000", stage=2),  # defect
        sample("S9", "3103002000", stage=2),
        sample("S10", "3103002000", stage=2),
    ]
    resp = audit([plan()], samples)
    entry = resp.json()["plans"][0]
    assert entry["stage1_defects"] == 1
    assert entry["stage2_defects"] == 2
    assert entry["total_defects"] == 3
    assert entry["out_of_range_samples"] == [0, 5, 7]
    assert entry["decision"] == "reject"  # 3 >= r2


# ------------------------------------------------------- multi-plan isolation


def test_plans_are_isolated_and_keyed_by_gtin_plus_batch():
    p2 = plan(gtin=GTIN_PRODUCT, batch="B2", min_weight="0.9", max_weight="1.1")
    samples = [
        sample("A1", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("B1", "3103001000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("A2", "3103002500", gtin=GTIN_RAW, batch="B1"),
    ]
    resp = audit([plan(), p2], samples)
    assert resp.status_code == 200
    entries = {p["batch"]: p for p in resp.json()["plans"]}
    assert entries["B1"]["stage1_count"] == 2
    assert entries["B1"]["stage1_defects"] == 1
    assert entries["B1"]["out_of_range_samples"] == [2]
    assert entries["B2"]["stage1_count"] == 1
    assert entries["B2"]["stage1_defects"] == 0


def test_same_serial_under_different_gtins_are_distinct_items():
    p2 = plan(gtin=GTIN_PRODUCT, batch="X9", min_weight="0", max_weight="99")
    samples = [
        sample("SAME", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("SAME", "3103002000", gtin=GTIN_PRODUCT, batch="X9"),
    ]
    resp = audit([plan(), p2], samples)
    assert resp.status_code == 200


def test_digital_link_scan_is_accepted_and_matched_by_gtin_batch():
    samples = [
        {
            "stage": 1,
            "scan": "https://id.gs1.org/01/" + GTIN_RAW + "/10/B1/21/SN1?3103=002000",
        }
    ] + [
        {"stage": 1, "scan": f"01{GTIN_RAW}10B1{GS}21SN{i}{GS}3103002000"}
        for i in range(2, 6)
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 200
    entry = resp.json()["plans"][0]
    assert entry["stage1_count"] == 5
    assert entry["out_of_range_samples"] == []


# --------------------------------------------------------------- plan errors


def test_n2_not_greater_than_n1_is_rejected_with_plan_index():
    resp = audit([plan(n2=5)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "INVALID_THRESHOLDS"
    assert detail["plan_index"] == 0
    assert "n2 must be greater than n1" in detail["error"]["message"]


def test_zero_n1_is_rejected_with_plan_index():
    resp = audit([plan(n1=0, n2=5)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "INVALID_THRESHOLDS"
    assert detail["plan_index"] == 0


def test_a1_not_below_r1_is_rejected_with_plan_index():
    resp = audit([plan(a1=2)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "INVALID_THRESHOLDS"
    assert detail["plan_index"] == 0
    assert detail["error"]["found"]["a1"] == 2


def test_r1_above_n1_is_rejected_with_plan_index():
    resp = audit([plan(r1=6)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "INVALID_THRESHOLDS"
    assert detail["plan_index"] == 0


def test_stage2_threshold_pair_validated_against_n2():
    for kwargs in ({"a2": 2}, {"r2": 11}, {"a2": 10, "r2": 11}):
        resp = audit([plan(**kwargs)])
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"]["error"] == "INVALID_THRESHOLDS"
        assert detail["plan_index"] == 0


def test_inverted_interval_is_rejected_with_plan_index():
    resp = audit([plan(min_weight="3", max_weight="2")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "INVALID_INTERVAL"
    assert detail["plan_index"] == 0


def test_second_invalid_plan_is_located_by_its_own_index():
    resp = audit([plan(), plan(n2=3)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "INVALID_THRESHOLDS"
    assert detail["plan_index"] == 1


def test_duplicate_gtin_batch_plan_is_rejected_with_both_indices():
    resp = audit([plan(batch="B1"), plan(batch="B1")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_PLAN"
    assert detail["plan_index"] == 1
    assert detail["first_plan_index"] == 0


# ------------------------------------------------------------- sample errors


def test_stage_one_overflow_is_rejected_at_the_offending_sample():
    samples = good_stage1(count=6)
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_OVERFLOW"
    assert detail["sample_index"] == 5
    assert detail["plan_index"] == 0


def test_stage_two_overflow_is_rejected_at_the_offending_sample():
    # Capacity of stage 2 is n2 - n1 = 5; the 6th stage-2 sample overflows.
    # Stage 1 must be inconclusive first, else STAGE_AFTER_DECISION fires.
    samples = continuing_samples() + [
        sample(f"S{i}", "3103002000", stage=2) for i in range(6, 12)
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_OVERFLOW"
    assert detail["sample_index"] == 10
    assert detail["plan_index"] == 0


def test_stage_overflow_locates_the_plan_even_with_multiple_plans():
    p2 = plan(gtin=GTIN_PRODUCT, batch="B2", min_weight="0", max_weight="99", n1=2, a1=0, r1=2, n2=3, a2=1, r2=2)
    samples = [
        sample("A1", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("B1", "3103002000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("B2", "3103002000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("B3", "3103002000", gtin=GTIN_PRODUCT, batch="B2"),  # n1=2 full
    ]
    resp = audit([plan(), p2], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_OVERFLOW"
    assert detail["sample_index"] == 3
    assert detail["plan_index"] == 1


def test_stage_after_decision_is_enforced_per_plan_in_inspection_order():
    # Plan 0 accepts after five good stage-1 items while plan 1 is still
    # pending; a stage-2 item for the decided plan 0 is refused even
    # though other plans still have open sampling.
    p2 = plan(gtin=GTIN_PRODUCT, batch="B2", min_weight="0", max_weight="99")
    samples = [
        sample("A1", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("B1", "3103001000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("A2", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("B2", "3103002000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("A3", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("B3", "3103002000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("A4", "3103002000", gtin=GTIN_RAW, batch="B1"),
        sample("B4", "3103002000", gtin=GTIN_PRODUCT, batch="B2"),
        sample("A5", "3103002000", gtin=GTIN_RAW, batch="B1"),  # plan 0 accepts
        sample("A6", "3103002000", gtin=GTIN_RAW, batch="B1", stage=2),
    ]
    resp = audit([plan(), p2], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_AFTER_DECISION"
    assert detail["sample_index"] == 9
    assert detail["plan_index"] == 0
    assert detail["error"]["decision"] == "accept"


def test_stage_after_decision_takes_precedence_over_capacity_check():
    # No stage-2 samples were ever drawn for this accepted plan, so the
    # item is within stage-2 capacity; the closed-decision rule still wins.
    samples = good_stage1() + [sample("S6", "3103002000", stage=2)]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "STAGE_AFTER_DECISION"
    assert detail["sample_index"] == 5


def test_stage_two_before_stage_one_is_full_stays_a_legal_pending_flow():
    # Stage 1 is still incomplete when a stage-2 item arrives: no verdict
    # exists yet, so the draw is legal and the plan simply stays pending.
    samples = [
        sample("S1", "3103002000"),
        sample("S6", "3103002000", stage=2),
        sample("S2", "3103002000"),
        sample("S7", "3103002500", stage=2),  # out of range, still counted
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 200
    entry = resp.json()["plans"][0]
    assert entry["stage1_count"] == 2
    assert entry["stage1_defects"] == 0
    assert entry["stage2_count"] == 2
    assert entry["stage2_defects"] == 1
    assert entry["total_count"] == 4
    assert entry["total_defects"] == 1
    assert entry["out_of_range_samples"] == [3]
    assert entry["decision"] == "pending"


def test_inconclusive_stage_one_still_accepts_stage_two_samples():
    # One stage-1 defect between a1=0 and r1=2: stage 2 is the legal
    # continuation path and must not trip STAGE_AFTER_DECISION.
    samples = continuing_samples() + [
        sample("S6", "3103002000", stage=2),
        sample("S7", "3103002000", stage=2),
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 200
    assert resp.json()["plans"][0]["decision"] == "pending"


def test_sample_without_matching_plan_is_rejected():
    resp = audit([plan()], [sample("S1", "3103002000", batch="OTHER")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "NO_MATCHING_PLAN"
    assert detail["sample_index"] == 0
    assert "OTHER" in detail["error"]["found"]


def test_sample_with_wrong_gtin_has_no_matching_plan():
    resp = audit([plan()], [sample("S1", "3103002000", gtin=GTIN_PRODUCT)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "NO_MATCHING_PLAN"
    assert detail["sample_index"] == 0


def test_no_matching_plan_locates_second_sample():
    samples = good_stage1(count=1) + [sample("S2", "3103002000", batch="OTHER")]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "NO_MATCHING_PLAN"
    assert detail["sample_index"] == 1


def test_duplicate_gtin_serial_within_stage_one_is_rejected():
    samples = [
        sample("DUP", "3103002000"),
        sample("S2", "3103002000"),
        sample("DUP", "3103002000"),
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert detail["sample_index"] == 2
    assert detail["first_sample_index"] == 0


def test_duplicate_gtin_serial_across_stages_is_rejected():
    samples = good_stage1() + [sample("S1", "3103002000", stage=2)]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_ITEM"
    assert detail["sample_index"] == 5
    assert detail["first_sample_index"] == 0


def test_duplicate_detected_before_plan_lookup():
    # Same GTIN+serial repeated, but the second scan also has an unknown
    # batch: identity is established first, so DUPLICATE_ITEM wins.
    samples = [
        sample("S1", "3103002000"),
        sample("S1", "3103002000", batch="OTHER"),
    ]
    resp = audit([plan()], samples)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["error"] == "DUPLICATE_ITEM"


def test_missing_batch_ai_is_rejected_with_sample_index():
    resp = audit(
        [plan()],
        [{"stage": 1, "scan": f"01{GTIN_RAW}21S1{GS}3103002000"}],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 10" in detail["error"]["message"]


def test_missing_serial_ai_is_rejected_with_sample_index():
    resp = audit(
        [plan()],
        [{"stage": 1, "scan": f"01{GTIN_RAW}10B1{GS}3103002000"}],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 21" in detail["error"]["message"]


def test_missing_weight_ai_is_rejected_with_sample_index():
    resp = audit(
        [plan()],
        [{"stage": 1, "scan": f"01{GTIN_RAW}10B1{GS}21S1"}],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "weight AI" in detail["error"]["message"]


def test_missing_gtin_is_rejected_with_sample_index():
    resp = audit(
        [plan()],
        [{"stage": 1, "scan": f"10B1{GS}21S1{GS}3103002000"}],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "AI 01" in detail["error"]["message"]


def test_two_weight_ais_are_rejected():
    resp = audit(
        [plan()],
        [
            {
                "stage": 1,
                "scan": f"01{GTIN_RAW}10B1{GS}21S1{GS}31030020003202002000",
            }
        ],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103, 3202" in detail["error"]["found"]


def test_duplicate_batch_ai_is_rejected():
    resp = audit(
        [plan()],
        [{"stage": 1, "scan": f"01{GTIN_RAW}10B1{GS}10B2{GS}21S1{GS}3103002000"}],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"]["error"] == "DUPLICATE_AI"


def test_optional_expiry_ai_is_allowed():
    resp = audit(
        [plan()],
        [
            {
                "stage": 1,
                "scan": f"01{GTIN_RAW}1725013110B1{GS}21S1{GS}3103002000",
            }
        ],
    )
    assert resp.status_code == 200


def test_parse_error_locates_sample_index():
    resp = audit(
        [plan()],
        [{"stage": 1, "scan": f"01{GTIN_RAW}10B1{GS}21S2{GS}310300"}],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "FIELD_TOO_SHORT"


def test_bad_gtin_check_digit_locates_sample_index():
    resp = audit(
        [plan(gtin="09501101530004")],
        [sample("S1", "3103002000", gtin="09501101530004")],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["sample_index"] == 0
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


# ------------------------------------------------------------ request shape


def test_stage_must_be_one_or_two():
    resp = client.post(
        "/weight-sampling-audit",
        json={
            "plans": [plan()],
            "samples": [
                {"stage": 3, "scan": f"01{GTIN_RAW}10B1{GS}21S1{GS}3103002000"}
            ],
        },
    )
    assert resp.status_code == 422


def test_negative_threshold_is_rejected_by_validation():
    resp = audit([plan(a1=-1)])
    assert resp.status_code == 422


def test_non_integer_threshold_is_rejected_by_validation():
    resp = audit([plan(n1="five")])
    assert resp.status_code == 422


def test_missing_plans_field_is_rejected_by_validation():
    resp = client.post(
        "/weight-sampling-audit",
        json={"samples": []},
    )
    assert resp.status_code == 422


def test_unknown_target_unit_is_rejected_by_validation():
    resp = audit([plan()], target_unit="stone")
    assert resp.status_code == 422


def test_empty_batch_is_rejected_by_validation():
    resp = audit([plan(batch="")])
    assert resp.status_code == 422
