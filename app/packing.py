"""Packing (loading) audit over nested containers of parsed GS1 scans.

The request carries a list of containers in arbitrary order; each has a
unique ``container_id``, an optional ``parent_id`` nesting it inside
another container, a non-negative ``max_weight`` capacity and an
``items`` list of scans of the goods loaded directly into it.  Every
item scan is fed through :func:`app.gs1.parse` and must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 21 (serial number),
* exactly one weight element of the 310n (kg) / 320n (lb) family.

AI 10 (batch/lot) and AI 17 (expiry date) are optional (at most one of
each); their values do not influence the audit.  Any parser failure or
missing/duplicated required AI fails the whole request with
:class:`PackingAuditError` whose ``detail`` names the offending
``container_index`` and ``item_index`` -- the same nested 422 shape as
``/batch-genealogy``.

Physical items are identified by (GTIN, serial); the same item may be
loaded only once across *all* containers, so a repeated GTIN+serial pair
(even twice in one container) fails the whole request with
:class:`PackingStructureError` (``DUPLICATE_ITEM``) locating both the
second and the first occurrence.

The container parent links must form a *forest* (a disjoint union of
rooted trees).  The structural checks below reject the whole request
with :class:`PackingStructureError` and locate the offending container
index:

* ``DUPLICATE_CONTAINER`` -- a ``container_id`` may appear only once;
  the detail also carries ``first_container_index``.
* ``UNKNOWN_PARENT`` -- every ``parent_id`` must name one of the
  declared containers.
* ``CYCLE_DETECTED`` -- the parent links must be acyclic (a container
  naming itself is a cycle of length one); the detail also carries
  ``cycle``, the container indices forming the loop rotated so the
  lowest index comes first.

Weights are rebuilt with :class:`decimal.Decimal` straight from the
original six digits and converted into ``target_unit`` with the fixed
factors of :mod:`app.summary`.  Totals are rolled up leaves-to-roots:
each container's ``total_weight`` is its own direct item weight plus
the totals of its child containers, and the same recursion gives the
recursive item count.  A container whose (exact, unrounded) total
exceeds ``max_weight`` is flagged ``over_limit``; the audit still
returns every container, with a negative ``remaining_capacity`` for
the overloaded ones.  Rounding to six places happens only when the
response is rendered.

Capacities may be large (``max_weight`` may be something like
``1E+60``) but are not unbounded.  A capacity that sat at a huge
integer magnitude *and* carried a deep fractional tail used to widen
the subtraction context to ``integer_digits + fractional_digits`` -- a
width taken straight from request text -- so a single crafted value
forced a million-digit Decimal and starved the process of CPU and
memory.  Capacities must therefore stay inside the fixed envelope
:data:`MAX_CAPACITY_INTEGER_DIGITS` / :data:`MAX_CAPACITY_EXPONENT`
(values the arithmetic, the six-place renderer and the JSON output all
handle comfortably); an out-of-envelope value rejects the request with
:class:`PackingStructureError` (``CAPACITY_OUT_OF_RANGE``) naming the
container.  Inside the envelope the capacity-minus-total subtraction
runs at the fixed :data:`DIFFERENCE_PRECISION`, the ``over_limit``
sign is read from an exact Decimal comparison (never from the rounded
remainder), and six-place half-even rounding still happens only when
the response is rendered.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import ARITHMETIC_PRECISION, ScanValidationError, _to_target

# Capacity envelope.  The integer-digit bound keeps the six-place
# fixed-point JSON token bounded (1e60 plus .000000 is the documented
# large-capacity case); the exponent bound keeps the fractional tail
# bounded.  Together they make the largest representable capacity
# 10**61 - 10**-50, a 111-coefficient-digit value.
MAX_CAPACITY_INTEGER_DIGITS = 61
MAX_CAPACITY_EXPONENT = -50

# Working precision of the capacity-minus-total subtraction, large enough
# to hold every digit the bounded operands and the item-weight arithmetic
# can produce: 61 capacity integer digits plus the deepest total the
# conversions can reach (the smallest single-item weight 1e-9 converts to
# 2.2046...e-9 lb, exponent -36, under the 50-significant-digit reciprocal
# context), with margin for the six-place render and the rounding carry.
# The measured exact worst case spans 97 digits.
DIFFERENCE_PRECISION = 120

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), "
    "and exactly one weight AI 310n/320n"
)


def _difference(capacity: Decimal, total: Decimal) -> Decimal:
    """Return ``capacity - total`` in a fixed, request-independent context.

    The precision is :data:`DIFFERENCE_PRECISION`, never derived from the
    operands: both inputs are bounded (capacities by the envelope below,
    totals by the 50-digit item arithmetic), so every digit the audit may
    legitimately produce fits, while a hostile value cannot widen the
    context into a million-digit subtraction.  The ``over_limit`` verdict
    does not even depend on this value -- it is the exact comparison
    ``total > capacity`` -- so a rounded remainder cannot flip it.
    """
    with localcontext() as ctx:
        ctx.prec = DIFFERENCE_PRECISION
        return capacity - total


def _capacity_out_of_range(index: int, capacity: Decimal) -> None:
    raise PackingStructureError(
        {
            "container_index": index,
            "error": {
                "error": "CAPACITY_OUT_OF_RANGE",
                "message": (
                    f"container {index}: max_weight is outside the supported "
                    f"capacity envelope of at most "
                    f"{MAX_CAPACITY_INTEGER_DIGITS} integer digits and "
                    f"{-MAX_CAPACITY_EXPONENT} decimal places"
                ),
                "expected": (
                    f"a finite non-negative Decimal with at most "
                    f"{MAX_CAPACITY_INTEGER_DIGITS} integer digits and at most "
                    f"{-MAX_CAPACITY_EXPONENT} decimal places"
                ),
                "found": (
                    f"{max(capacity.adjusted(), 0) + 1} integer digits and "
                    f"{max(-capacity.as_tuple().exponent, 0)} decimal places"
                ),
            },
        }
    )


def _check_capacity(index: int, capacity: Decimal) -> None:
    """Validate one ``max_weight`` against the bounded capacity envelope.

    Only finite values can reach here (pydantic's ``ge=0`` already rejects
    NaN/Infinity); the checks bound the integer magnitude via
    :meth:`~decimal.Decimal.adjusted` (neutral against leading zeros and
    exponent tricks such as ``1E+999999``) and the fractional depth via
    the tuple exponent (neutral against trailing-zero coefficients).
    """
    if not capacity.is_finite():  # pragma: no cover - guarded by pydantic ge=0
        _capacity_out_of_range(index, capacity)
    integer_digits = max(capacity.adjusted(), 0) + 1
    fractional_digits = max(-capacity.as_tuple().exponent, 0)
    if (
        integer_digits > MAX_CAPACITY_INTEGER_DIGITS
        or fractional_digits > -MAX_CAPACITY_EXPONENT
    ):
        _capacity_out_of_range(index, capacity)


class PackingAuditError(Exception):
    """An item scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: ``container_index`` and
    ``item_index`` plus the original parser/validation error under
    ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class PackingStructureError(Exception):
    """The container forest or the loaded items are structurally invalid.

    ``detail`` is the complete 422 payload: one of ``DUPLICATE_CONTAINER``
    / ``UNKNOWN_PARENT`` / ``CYCLE_DETECTED`` / ``DUPLICATE_ITEM`` with
    the offending container (and item) indices plus the error under
    ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _extract(index: int, elements: list[Element]) -> tuple[str, str, Decimal, str]:
    """Pull (GTIN, serial, exact weight, weight unit) from one item scan."""
    gtins = [e for e in elements if e.ai == "01"]
    serials = [e for e in elements if e.ai == "21"]
    batches = [e for e in elements if e.ai == "10"]
    expiries = [e for e in elements if e.ai == "17"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"item {index}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"item {index}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(serials) != 1:
        if not serials:
            raise ScanValidationError(
                "MISSING_AI",
                f"item {index}: missing required AI 21 (serial number)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"item {index}: AI 21 (serial number) appears {len(serials)} times; exactly one is required",
            expected="exactly one AI 21 (serial number)",
            found=f"{len(serials)} AI 21 elements",
            offset=serials[1].offset,
        )

    if len(batches) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"item {index}: AI 10 (batch/lot) appears {len(batches)} times; at most one is allowed",
            expected="at most one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(expiries) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"item {index}: AI 17 (expiry date) appears {len(expiries)} times; at most one is allowed",
            expected="at most one AI 17 (expiry date)",
            found=f"{len(expiries)} AI 17 elements",
            offset=expiries[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"item {index}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"item {index}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return gtins[0].value, serials[0].value, value, weight.unit


def _duplicate_container(index: int, first_index: int, container_id: str) -> None:
    raise PackingStructureError(
        {
            "container_index": index,
            "first_container_index": first_index,
            "error": {
                "error": "DUPLICATE_CONTAINER",
                "message": (
                    f"container {index}: container_id '{container_id}' is "
                    f"already used by container {first_index}; every "
                    "container_id must be unique"
                ),
                "expected": "a unique container_id across the request",
                "found": container_id,
            },
        }
    )


def _duplicate_item(
    container_index: int,
    item_index: int,
    first_container_index: int,
    first_item_index: int,
    gtin: str,
    serial: str,
) -> None:
    raise PackingStructureError(
        {
            "container_index": container_index,
            "item_index": item_index,
            "first_container_index": first_container_index,
            "first_item_index": first_item_index,
            "error": {
                "error": "DUPLICATE_ITEM",
                "message": (
                    f"container {container_index} item {item_index}: item "
                    f"GTIN '{gtin}' serial '{serial}' was already loaded at "
                    f"container {first_container_index} item "
                    f"{first_item_index}; an item may be loaded only once"
                ),
                "expected": (
                    f"item first seen at container {first_container_index} "
                    f"item {first_item_index} to be the only occurrence"
                ),
                "found": (
                    f"GTIN '{gtin}' serial '{serial}' at container "
                    f"{container_index} item {item_index}"
                ),
            },
        }
    )


def _unknown_parent(index: int, parent_id: str) -> None:
    raise PackingStructureError(
        {
            "container_index": index,
            "error": {
                "error": "UNKNOWN_PARENT",
                "message": (
                    f"container {index}: parent_id '{parent_id}' does not "
                    "name any container in the request"
                ),
                "expected": "parent_id omitted/null or equal to a declared container_id",
                "found": parent_id,
            },
        }
    )


def _check_cycle(parent_of: list[int | None]) -> None:
    """Reject the request when the parent-pointer graph contains a cycle.

    Each node has at most one outgoing (parent) edge, so following
    parents from a node either reaches a root (``None``), an already
    settled node or a node on the current walk -- the latter closes a
    cycle.  Walks start from the lowest request index, and the reported
    cycle is rotated so its lowest index comes first.
    """
    n = len(parent_of)
    state = [0] * n  # 0 = unseen, 1 = on the current walk, 2 = settled
    for start in range(n):
        if state[start] != 0:
            continue
        path: list[int] = []
        position: dict[int, int] = {}
        node: int | None = start
        while node is not None and state[node] == 0:
            state[node] = 1
            position[node] = len(path)
            path.append(node)
            node = parent_of[node]
        if node is not None and state[node] == 1:
            cycle = path[position[node] :]
            first = min(range(len(cycle)), key=lambda k: cycle[k])
            cycle = cycle[first:] + cycle[:first]
            chain = " -> ".join(str(i) for i in (*cycle, cycle[0]))
            raise PackingStructureError(
                {
                    "container_index": cycle[0],
                    "cycle": cycle,
                    "error": {
                        "error": "CYCLE_DETECTED",
                        "message": (
                            f"containers {chain} form a parent/child cycle; "
                            "the container relationships must form a forest"
                        ),
                        "expected": "an acyclic container forest",
                        "found": f"cycle {chain}",
                    },
                }
            )
        for settled in path:
            state[settled] = 2


def audit(
    target_unit: str, containers: list[tuple[str, str | None, Decimal, list[str]]]
) -> dict:
    """Validate the container forest and roll item weights up leaf-to-root.

    ``containers`` is a list of ``(container_id, parent_id, max_weight,
    items)`` tuples in request order.  Returns
    ``{"target_unit": ..., "containers": [...]}`` with one result per
    requested container in the original order; every weight is an
    unrounded :class:`Decimal` already expressed in the target unit.

    Every ``max_weight`` must fit the capacity envelope
    (:data:`MAX_CAPACITY_INTEGER_DIGITS` / :data:`MAX_CAPACITY_EXPONENT`);
    an out-of-envelope value raises :class:`PackingStructureError`
    locating the container before any item is parsed.
    """
    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION

        # -- unique container ids (parents refer to these) and capacity bounds --
        index_of: dict[str, int] = {}
        for index, (container_id, _parent_id, max_weight, _items) in enumerate(
            containers
        ):
            first_index = index_of.get(container_id)
            if first_index is not None:
                _duplicate_container(index, first_index, container_id)
            index_of[container_id] = index
            _check_capacity(index, max_weight)

        # -- parse every loaded item and reject a re-loaded GTIN+serial --
        parsed: list[list[tuple[str, str, Decimal]]] = []
        direct_weights: list[Decimal] = []
        first_seen: dict[tuple[str, str], tuple[int, int]] = {}
        for container_index, (
            _container_id,
            _parent_id,
            _max_weight,
            items,
        ) in enumerate(containers):
            entries: list[tuple[str, str, Decimal]] = []
            direct_weight = Decimal(0)
            for item_index, scan_text in enumerate(items):
                try:
                    elements = parse(scan_text)
                except GS1ParseError as exc:
                    raise PackingAuditError(
                        {
                            "container_index": container_index,
                            "item_index": item_index,
                            "error": exc.to_dict(),
                        }
                    ) from exc
                try:
                    gtin, serial, weight, unit = _extract(item_index, elements)
                except ScanValidationError as exc:
                    raise PackingAuditError(
                        {
                            "container_index": container_index,
                            "item_index": item_index,
                            "error": exc.to_dict(),
                        }
                    ) from exc
                location = first_seen.get((gtin, serial))
                if location is not None:
                    _duplicate_item(
                        container_index,
                        item_index,
                        location[0],
                        location[1],
                        gtin,
                        serial,
                    )
                first_seen[(gtin, serial)] = (container_index, item_index)
                converted = _to_target(weight, unit, target_unit)
                entries.append((gtin, serial, converted))
                direct_weight += converted
            parsed.append(entries)
            direct_weights.append(direct_weight)

        # -- resolve and validate the parent links --
        parent_of: list[int | None] = []
        for index, (_container_id, parent_id, _max_weight, _items) in enumerate(
            containers
        ):
            if parent_id is None:
                parent_of.append(None)
                continue
            parent_index = index_of.get(parent_id)
            if parent_index is None:
                _unknown_parent(index, parent_id)
            parent_of.append(parent_index)
        _check_cycle(parent_of)

        # -- leaf-to-root roll-up (Kahn: peel container leaves) --
        children: list[list[int]] = [[] for _ in containers]
        for index, parent_index in enumerate(parent_of):
            if parent_index is not None:
                children[parent_index].append(index)

        total_weights: list[Decimal] = [Decimal(0)] * len(containers)
        recursive_counts = [0] * len(containers)
        remaining_children = [len(kids) for kids in children]
        leaves = [i for i, count in enumerate(remaining_children) if count == 0]
        finalized = 0
        while leaves:
            index = leaves.pop()
            total_weights[index] = direct_weights[index]
            recursive_counts[index] = len(parsed[index])
            for child in children[index]:
                total_weights[index] += total_weights[child]
                recursive_counts[index] += recursive_counts[child]
            finalized += 1
            parent_index = parent_of[index]
            if parent_index is not None:
                remaining_children[parent_index] -= 1
                if remaining_children[parent_index] == 0:
                    leaves.append(parent_index)

        # A forest always finalizes every node; guard against algorithm drift.
        if finalized != len(containers):  # pragma: no cover - cycles rejected above
            raise PackingStructureError(
                {
                    "container_index": 0,
                    "error": {
                        "error": "CYCLE_DETECTED",
                        "message": "container parent graph could not be aggregated",
                        "expected": "an acyclic container forest",
                        "found": "unprocessed containers",
                    },
                }
            )

        results = []
        for index, (container_id, parent_id, max_weight, items) in enumerate(
            containers
        ):
            total_weight = total_weights[index]
            # The over-limit verdict is the exact comparison of the
            # unrounded operands: the six-place render below cannot flip
            # it, and neither can the bounded subtraction context.  The
            # remainder itself is only rendered (six places half-even).
            over_limit = total_weight > max_weight
            remaining_capacity = _difference(max_weight, total_weight)
            results.append(
                {
                    "container_id": container_id,
                    "parent_id": parent_id,
                    "max_weight": max_weight,
                    "unit": target_unit,
                    "direct_weight": direct_weights[index],
                    "total_weight": total_weight,
                    "remaining_capacity": remaining_capacity,
                    "direct_item_count": len(items),
                    "recursive_item_count": recursive_counts[index],
                    "over_limit": over_limit,
                }
            )

    return {"target_unit": target_unit, "containers": results}
