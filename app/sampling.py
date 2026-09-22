"""Batch weight double-sampling acceptance audit over parsed GS1 scans.

The request carries ``target_unit``, a list of acceptance *plans* keyed
by (GTIN, batch/lot) and a list of drawn *samples* in inspection order;
every sample names a sampling ``stage`` (1 or 2) and a raw GS1 scan.
Once the full first stage of a plan is drawn and its verdict is final
(``defects <= a1`` accepts or ``defects >= r1`` rejects), any further
sample that still names ``stage=2`` is structurally impossible: it is
rejected immediately with ``STAGE_AFTER_DECISION`` at the offending
``sample_index`` (and ``plan_index``), without being counted in any
stage or altering the response -- the verdict must stay final rather
than merely tolerating stray stage-2 draws.

Each plan declares the **closed** per-item weight interval
``[min_weight, max_weight]`` expressed in ``target_unit`` together with
the double-sampling parameters ``n1/a1/r1`` and ``n2/a2/r2``:

* ``n1`` / ``n2`` -- cumulative sample sizes of stage 1 and of both
  stages together (``n2 > n1``);
* ``a1`` / ``a2`` -- acceptance numbers (a cumulative defect count at or
  below the number accepts the batch);
* ``r1`` / ``r2`` -- rejection numbers (a cumulative defect count at or
  above the number rejects the batch).

The thresholds must satisfy ``0 <= a1 < r1 <= n1`` and
``0 <= a2 < r2 <= n2``; an illegal plan fails the whole request with
:class:`SamplingPlanError` (``INVALID_THRESHOLDS`` / ``INVALID_INTERVAL``
/ ``DUPLICATE_PLAN``) naming the offending ``plan_index``.

Every sample scan is fed through :func:`app.gs1.parse` and must contain
exactly one AI 01 (GTIN), exactly one AI 10 (batch/lot), exactly one
AI 21 (serial number) and exactly one weight element of the 310n (kg) /
320n (lb) family; AI 17 is optional and ignored.  A parse/shape failure
fails the whole request with :class:`WeightSamplingError` naming the
``sample_index``.  The same physical item (GTIN + serial) may be drawn
only once across all samples (``DUPLICATE_ITEM``), a sample whose
GTIN+batch matches no plan fails with ``NO_MATCHING_PLAN``, and a plan
may receive at most ``n1`` stage-1 samples and at most ``n2 - n1``
stage-2 samples -- an extra sample fails with ``STAGE_OVERFLOW`` at the
offending ``sample_index`` (and ``plan_index``).  A stage-2 sample
presented to a plan whose stage-1 verdict is already final fails with
``STAGE_AFTER_DECISION`` at the offending ``sample_index`` (and
``plan_index``); the sample is never counted.

A converted sample weight strictly outside the closed interval is a
defect; the interval boundaries themselves are conforming.  The
acceptance decision per plan is:

* stage 1 full (``n1`` samples): ``defects <= a1`` accepts,
  ``defects >= r1`` rejects, anything in between continues to stage 2;
* a stage-2 sample is only admissible while stage 1 is still open or
  its verdict was inconclusive; once stage 1 has accepted or rejected
  the next stage-2 sample is refused with
  ``STAGE_AFTER_DECISION`` instead of being tallied;
* the second stage is judged only once both stages together hold
  ``n2`` samples, on the *cumulative* defect count against ``a2/r2``
  (again ``<= a2`` accepts, ``>= r2`` rejects);
* an incomplete stage, or a cumulative count still strictly between
  ``a2`` and ``r2`` once ``n2`` is reached, stays ``pending``;
* a verdict reached at stage 1 is final -- stage-2 samples never
  overwrite it.

Weights are rebuilt with :class:`decimal.Decimal` straight from the
original six digits and converted into the target unit with the fixed
factors of :mod:`app.summary`; the boundary comparisons use those exact
values, never float.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import ARITHMETIC_PRECISION, ScanValidationError, _to_target

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 10 (batch/lot), "
    "exactly one AI 21 (serial number), and exactly one weight AI "
    "310n/320n"
)

ACCEPT = "accept"
REJECT = "reject"
PENDING = "pending"


class SamplingPlanError(Exception):
    """A plan is invalid (thresholds, interval or a duplicate GTIN+batch).

    ``detail`` is the complete 422 payload: ``plan_index`` (plus
    ``first_plan_index`` for duplicates) and the error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class WeightSamplingError(Exception):
    """A sample scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: ``sample_index`` plus the
    original parser/validation error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class SamplingStructureError(Exception):
    """The samples violate the plan structure.

    ``detail`` is the complete 422 payload: ``DUPLICATE_ITEM`` (with the
    first occurrence), ``NO_MATCHING_PLAN``, ``STAGE_OVERFLOW`` (with
    ``plan_index``) or ``STAGE_AFTER_DECISION`` (with ``plan_index`` and
    the final stage-1 decision) and the offending ``sample_index``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _extract(
    index: int, elements: list[Element]
) -> tuple[str, str, str, Decimal, str]:
    """Pull (GTIN, batch, serial, exact weight, weight unit) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
    batches = [e for e in elements if e.ai == "10"]
    serials = [e for e in elements if e.ai == "21"]
    expiries = [e for e in elements if e.ai == "17"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"sample {index}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"sample {index}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(batches) != 1:
        if not batches:
            raise ScanValidationError(
                "MISSING_AI",
                f"sample {index}: missing required AI 10 (batch/lot)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"sample {index}: AI 10 (batch/lot) appears {len(batches)} times; exactly one is required",
            expected="exactly one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(serials) != 1:
        if not serials:
            raise ScanValidationError(
                "MISSING_AI",
                f"sample {index}: missing required AI 21 (serial number)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"sample {index}: AI 21 (serial number) appears {len(serials)} times; exactly one is required",
            expected="exactly one AI 21 (serial number)",
            found=f"{len(serials)} AI 21 elements",
            offset=serials[1].offset,
        )

    if len(expiries) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"sample {index}: AI 17 (expiry date) appears {len(expiries)} times; at most one is allowed",
            expected="at most one AI 17 (expiry date)",
            found=f"{len(expiries)} AI 17 elements",
            offset=expiries[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"sample {index}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"sample {index}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return gtins[0].value, batches[0].value, serials[0].value, value, weight.unit


def _invalid_thresholds(index: int, plan: tuple, message: str) -> None:
    (_gtin, _batch, _min_weight, _max_weight, n1, a1, r1, n2, a2, r2) = plan
    raise SamplingPlanError(
        {
            "plan_index": index,
            "error": {
                "error": "INVALID_THRESHOLDS",
                "message": f"plan {index}: {message}",
                "expected": "0 <= a1 < r1 <= n1, 0 <= a2 < r2 <= n2 and n2 > n1",
                "found": {
                    "n1": n1,
                    "a1": a1,
                    "r1": r1,
                    "n2": n2,
                    "a2": a2,
                    "r2": r2,
                },
            },
        }
    )


def _validate_plan(index: int, plan: tuple) -> None:
    min_weight, max_weight = plan[2], plan[3]
    n1, a1, r1, n2, a2, r2 = plan[4:10]

    if min_weight > max_weight:
        raise SamplingPlanError(
            {
                "plan_index": index,
                "error": {
                    "error": "INVALID_INTERVAL",
                    "message": (
                        f"plan {index}: min_weight ({min_weight}) must be no "
                        f"greater than max_weight ({max_weight})"
                    ),
                    "expected": "min_weight <= max_weight",
                    "found": [str(min_weight), str(max_weight)],
                },
            }
        )

    if n1 < 1:
        _invalid_thresholds(index, plan, "n1 must be a positive integer")
    if n2 <= n1:
        _invalid_thresholds(index, plan, "n2 must be greater than n1")
    if not 0 <= a1 < r1 <= n1:
        _invalid_thresholds(index, plan, "stage 1 requires 0 <= a1 < r1 <= n1")
    if not 0 <= a2 < r2 <= n2:
        _invalid_thresholds(index, plan, "stage 2 requires 0 <= a2 < r2 <= n2")


def _duplicate_plan(index: int, first_index: int, gtin: str, batch: str) -> None:
    raise SamplingPlanError(
        {
            "plan_index": index,
            "first_plan_index": first_index,
            "error": {
                "error": "DUPLICATE_PLAN",
                "message": (
                    f"plan {index}: GTIN '{gtin}' batch '{batch}' is already "
                    f"defined by plan {first_index}; every GTIN+batch pair "
                    "must be unique"
                ),
                "expected": "a unique GTIN+batch pair across the request",
                "found": f"GTIN '{gtin}' batch '{batch}'",
            },
        }
    )


def _duplicate_item(
    index: int, first_index: int, gtin: str, serial: str
) -> None:
    raise SamplingStructureError(
        {
            "sample_index": index,
            "first_sample_index": first_index,
            "error": {
                "error": "DUPLICATE_ITEM",
                "message": (
                    f"sample {index}: item GTIN '{gtin}' serial '{serial}' was "
                    f"already drawn at sample {first_index}; an item may be "
                    "sampled only once"
                ),
                "expected": (
                    f"item first seen at sample {first_index} to be the only "
                    "occurrence"
                ),
                "found": f"GTIN '{gtin}' serial '{serial}' at sample {index}",
            },
        }
    )


def _no_matching_plan(index: int, gtin: str, batch: str) -> None:
    raise SamplingStructureError(
        {
            "sample_index": index,
            "error": {
                "error": "NO_MATCHING_PLAN",
                "message": (
                    f"sample {index}: no plan matches GTIN '{gtin}' batch "
                    f"'{batch}'"
                ),
                "expected": "a plan with the same gtin and batch",
                "found": f"GTIN '{gtin}' batch '{batch}'",
            },
        }
    )


def _stage_overflow(
    sample_index: int,
    plan_index: int,
    stage: int,
    capacity: int,
    gtin: str,
    batch: str,
) -> None:
    raise SamplingStructureError(
        {
            "sample_index": sample_index,
            "plan_index": plan_index,
            "error": {
                "error": "STAGE_OVERFLOW",
                "message": (
                    f"sample {sample_index}: plan {plan_index} (GTIN '{gtin}' "
                    f"batch '{batch}') already holds {capacity} stage-{stage} "
                    f"samples, which is its stage-{stage} capacity"
                ),
                "expected": (
                    f"at most {capacity} stage-{stage} sample(s) for this plan"
                ),
                "found": f"stage {stage} sample number {capacity + 1}",
            },
        }
    )


def _stage1_verdict(state: dict) -> str:
    """Return the final stage-1 verdict (``accept``/``reject``) or ``""``.

    The empty string covers both an incomplete first stage and an
    inconclusive one (``a1 < defects < r1``): in either case drawing a
    stage-2 sample is still legal.
    """
    if state["stage1_count"] == state["n1"]:
        if state["stage1_defects"] <= state["a1"]:
            return ACCEPT
        if state["stage1_defects"] >= state["r1"]:
            return REJECT
    return ""


def _stage_after_decision(
    sample_index: int,
    plan_index: int,
    state: dict,
    gtin: str,
    batch: str,
) -> None:
    verdict = _stage1_verdict(state)
    raise SamplingStructureError(
        {
            "sample_index": sample_index,
            "plan_index": plan_index,
            "error": {
                "error": "STAGE_AFTER_DECISION",
                "message": (
                    f"sample {sample_index}: plan {plan_index} (GTIN '{gtin}' "
                    f"batch '{batch}') already reached a final stage-1 "
                    f"decision of '{verdict}' after {state['n1']} samples; "
                    "no further samples may be drawn for a decided plan"
                ),
                "expected": "no further samples after a final stage-1 decision",
                "expected_stage": 1,
                "found_stage": 2,
                "decision": verdict,
            },
        }
    )


def _decision(state: dict) -> str:
    """Apply the double-sampling rules to one plan's accumulated counters."""
    n1, a1, r1 = state["n1"], state["a1"], state["r1"]
    n2, a2, r2 = state["n2"], state["a2"], state["r2"]

    s1_count = state["stage1_count"]
    s1_defects = state["stage1_defects"]

    # A stage-1 verdict is reached only once the full first stage is drawn
    # and is final: stage-2 samples can never rewrite it.
    if s1_count == n1:
        if s1_defects <= a1:
            return ACCEPT
        if s1_defects >= r1:
            return REJECT
        total_count = s1_count + state["stage2_count"]
        total_defects = s1_defects + state["stage2_defects"]
        if total_count == n2:
            if total_defects <= a2:
                return ACCEPT
            if total_defects >= r2:
                return REJECT
    return PENDING


def audit(
    target_unit: str,
    plans: list[tuple],
    samples: list[tuple[int, str]],
) -> dict:
    """Validate plans and samples and run the double-sampling decision.

    ``plans`` is a list of ``(gtin, batch, min_weight, max_weight,
    n1, a1, r1, n2, a2, r2)`` tuples in request order; ``samples`` is a
    list of ``(stage, scan)`` tuples.  Returns
    ``{"target_unit": ..., "plans": [...]}`` with one result per plan in
    the original order; the interval bounds are unrounded
    :class:`Decimal` values already expressed in the target unit.
    """
    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION

        # -- validate the plans themselves (thresholds, interval, identity) --
        plan_index_of: dict[tuple[str, str], int] = {}
        plan_states: list[dict] = []
        for index, plan in enumerate(plans):
            _validate_plan(index, plan)
            gtin, batch = plan[0], plan[1]
            first_index = plan_index_of.get((gtin, batch))
            if first_index is not None:
                _duplicate_plan(index, first_index, gtin, batch)
            plan_index_of[(gtin, batch)] = index
            plan_states.append(
                {
                    "plan_index": index,
                    "gtin": gtin,
                    "batch": batch,
                    "min_weight": plan[2],
                    "max_weight": plan[3],
                    "unit": target_unit,
                    "n1": plan[4],
                    "a1": plan[5],
                    "r1": plan[6],
                    "n2": plan[7],
                    "a2": plan[8],
                    "r2": plan[9],
                    "stage1_count": 0,
                    "stage1_defects": 0,
                    "stage2_count": 0,
                    "stage2_defects": 0,
                    "out_of_range_samples": [],
                }
            )

        # -- draw the samples in request order --
        first_seen: dict[tuple[str, str], int] = {}
        for sample_index, (stage, scan_text) in enumerate(samples):
            try:
                elements = parse(scan_text)
            except GS1ParseError as exc:
                raise WeightSamplingError(
                    {"sample_index": sample_index, "error": exc.to_dict()}
                ) from exc
            try:
                gtin, batch, serial, weight, unit = _extract(sample_index, elements)
            except ScanValidationError as exc:
                raise WeightSamplingError(
                    {"sample_index": sample_index, "error": exc.to_dict()}
                ) from exc

            first_index = first_seen.get((gtin, serial))
            if first_index is not None:
                _duplicate_item(sample_index, first_index, gtin, serial)
            first_seen[(gtin, serial)] = sample_index

            plan_index = plan_index_of.get((gtin, batch))
            if plan_index is None:
                _no_matching_plan(sample_index, gtin, batch)
            state = plan_states[plan_index]

            converted = _to_target(weight, unit, target_unit)
            out_of_range = (
                converted < state["min_weight"] or converted > state["max_weight"]
            )

            if stage == 1:
                capacity = state["n1"]
                if state["stage1_count"] == capacity:
                    _stage_overflow(
                        sample_index,
                        plan_index,
                        stage,
                        capacity,
                        gtin,
                        batch,
                    )
                state["stage1_count"] += 1
                if out_of_range:
                    state["stage1_defects"] += 1
            else:
                # A final stage-1 verdict closes the plan: a later stage-2
                # sample is refused outright and must never be tallied.
                if _stage1_verdict(state):
                    _stage_after_decision(
                        sample_index,
                        plan_index,
                        state,
                        gtin,
                        batch,
                    )
                capacity = state["n2"] - state["n1"]
                if state["stage2_count"] == capacity:
                    _stage_overflow(
                        sample_index,
                        plan_index,
                        stage,
                        capacity,
                        gtin,
                        batch,
                    )
                state["stage2_count"] += 1
                if out_of_range:
                    state["stage2_defects"] += 1
            if out_of_range:
                state["out_of_range_samples"].append(sample_index)

        # -- decide each plan (a stage-1 verdict can no longer change) --
        results = []
        for state in plan_states:
            total_count = state["stage1_count"] + state["stage2_count"]
            total_defects = state["stage1_defects"] + state["stage2_defects"]
            result = dict(state)
            result["total_count"] = total_count
            result["total_defects"] = total_defects
            result["decision"] = _decision(state)
            results.append(result)

    return {"target_unit": target_unit, "plans": results}
