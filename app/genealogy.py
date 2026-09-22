"""Production batch genealogy audit over opening stock and transformations.

The request carries an opening batch inventory (``opening_stock``) and a
list of transformation ``processes`` in arbitrary order, each with an
``inputs`` (consumed) and an ``outputs`` (produced) scan list.  Every
material scan is fed through :func:`app.gs1.parse` and must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 10 (batch/lot) -- batches are identified by GTIN + lot,
  so the lot may not be absent,
* exactly one weight element of the 310n (kg) / 320n (lb) family.

Any parser failure or missing/duplicated required AI fails the whole
request with :class:`BatchGenealogyError` whose ``detail`` names the
offending scan: ``scan_index`` for opening stock, ``process_index`` plus
``input_index`` / ``output_index`` for process scans -- the same 422
shape as ``/weight-summary`` & friends.

Batches are identified by (GTIN, batch).  Every weight is rebuilt with
:class:`decimal.Decimal` from the original six digits and converted into
``target_unit`` with the fixed factors of :mod:`app.summary`; scans of
one batch are aggregated per process (and in the opening stock) before
any graph check runs.  Rounding to six places happens only when the
response is rendered.

The structural graph rules are validated before any inventory moves, and
every violation rejects the whole request with
:class:`GenealogyGraphError` naming the offending ``process_index``:

* ``MULTIPLE_PRODUCERS`` -- a batch may be produced by at most one
  process; the detail also carries ``first_index`` (the first producer).
* ``UNKNOWN_BATCH`` -- a consumed batch must come from the opening
  stock or from exactly one process's outputs.

Scheduling is then driven by the running inventory itself rather than
by a static topological sort.  In every round the process with the
lowest request index whose aggregated inputs are all covered by the
current *unrounded* balances is executed (it deducts its inputs and
adds its outputs); a process short of a batch whose producer has not
run yet simply keeps waiting -- an opening stock (or stock produced
earlier) is enough to enter a loop such as A -> B -> A.  When no
process is executable the remaining processes are stuck for one of
two reasons:

* ``CYCLE_DETECTED`` -- production chains still waiting on each other
  (each short batch of every remaining process is made by another
  remaining process); the detail carries ``cycle``, the process
  indices forming the loop (a process consuming its own output with no
  opening cover is a cycle of length one).
* ``INSUFFICIENT_STOCK`` -- a process is short of a batch whose source
  has already run (or that comes only from the opening stock); the
  detail names that ``process_index`` and batch.

Among the ready processes the lowest request index always goes first.
A batch present in the opening stock may also be produced by a process
-- the quantities add up.

The result lists the processes in that stable topological order, each
with its per-batch ``inputs`` / ``outputs`` (first-seen order within
the process) and the ``input_weight`` / ``output_weight`` /
``loss_weight`` totals (loss = input - output, negative when a process
yields more than it consumes), plus ``batches``: every batch in
first-seen order (opening stock first, then each process's outputs in
request order) with its ``opening`` / ``produced`` / ``consumed`` /
``remaining`` weights.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import (
    ARITHMETIC_PRECISION,
    ScanValidationError,
    _to_target,
)

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 10 (batch/lot), "
    "and exactly one weight AI 310n/320n"
)

SIX_PLACES = Decimal("0.000001")


class BatchGenealogyError(Exception):
    """A scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: ``scan_index`` for an
    opening-stock scan or ``process_index`` with ``input_index`` /
    ``output_index`` for a process scan, plus the original
    parser/validation error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class GenealogyGraphError(Exception):
    """The producer/consumer dependency graph is invalid.

    ``detail`` is the complete 422 payload: ``process_index`` (plus
    ``first_index`` for a second producer, ``cycle`` for a dependency
    cycle) plus the graph error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class InsufficientStockError(Exception):
    """A process consumes more of a batch than the balance holds.

    ``detail`` is the complete 422 payload: ``process_index`` plus the
    shortfall under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _extract(
    where: str, elements: list[Element]
) -> tuple[str, str, Decimal, str]:
    """Pull (GTIN, batch, exact weight, weight unit) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
    batches = [e for e in elements if e.ai == "10"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"{where}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{where}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(batches) != 1:
        if not batches:
            raise ScanValidationError(
                "MISSING_AI",
                f"{where}: missing required AI 10 (batch/lot)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{where}: AI 10 (batch/lot) appears {len(batches)} times; exactly one is required",
            expected="exactly one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"{where}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{where}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return gtins[0].value, batches[0].value, value, weight.unit


def _scan(
    detail: dict, where: str, scan: str
) -> tuple[str, str, Decimal, str]:
    """Parse and validate one material scan.

    ``detail`` holds the index fields of the 422 payload (``scan_index``
    or ``process_index`` + ``input_index`` / ``output_index``); ``where``
    is the scan's human-readable position used in validation messages.
    """
    try:
        elements = parse(scan)
    except GS1ParseError as exc:
        raise BatchGenealogyError({**detail, "error": exc.to_dict()}) from exc
    try:
        return _extract(where, elements)
    except ScanValidationError as exc:
        raise BatchGenealogyError({**detail, "error": exc.to_dict()}) from exc


def _aggregate_process(
    scans: list[str], process_index: int, kind: str, target_unit: str
) -> dict[tuple[str, str], Decimal]:
    """Aggregate one process's input/output scans per batch.

    ``kind`` is ``"input"`` or ``"output"``; it names the scan both in
    the 422 detail (``input_index`` / ``output_index``) and in the
    validation messages.  The returned dict keeps the first-seen batch
    order of ``scans``.
    """
    totals: dict[tuple[str, str], Decimal] = {}
    for index, scan in enumerate(scans):
        detail = {"process_index": process_index, f"{kind}_index": index}
        where = f"process {process_index} {kind} {index}"
        gtin, batch, weight, unit = _scan(detail, where, scan)
        key = (gtin, batch)
        converted = _to_target(weight, unit, target_unit)
        totals[key] = totals.get(key, Decimal(0)) + converted
    return totals


def _batch_label(key: tuple[str, str]) -> str:
    gtin, batch = key
    return f"GTIN '{gtin}' batch '{batch}'"


def _weight_text(value: Decimal, unit: str) -> str:
    """Six-place fixed-point weight with unit, e.g. '2.500000 kg'."""
    quantized = value.quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN)
    return f"{format(quantized, 'f')} {unit}"


def _find_cycle(waiting_on: list[set[int]]) -> list[int]:
    """One mutual-wait cycle among the unscheduled processes.

    Every unscheduled process waits on at least one other unscheduled
    producer, so the directed wait graph restricted to them contains a
    cycle.  Deterministic: the walk starts at the lowest remaining index
    and always follows the lowest-index remaining wait edge, so the
    first repeated node opens the returned cycle.
    """
    remaining = {i for i, waits in enumerate(waiting_on) if waits}
    path: list[int] = []
    position: dict[int, int] = {}
    node = min(remaining)
    while node not in position:
        position[node] = len(path)
        path.append(node)
        node = min(waiting_on[node] & remaining)
    return path[position[node] :]


def _insufficient_stock_error(
    process_index: int,
    key: tuple[str, str],
    amount: Decimal,
    available: Decimal,
    target_unit: str,
) -> InsufficientStockError:
    """Build the 422 payload for a process short of ``key``."""
    return InsufficientStockError(
        {
            "process_index": process_index,
            "error": {
                "error": "INSUFFICIENT_STOCK",
                "message": (
                    f"process {process_index}: batch {_batch_label(key)} "
                    f"requires {_weight_text(amount, target_unit)} but only "
                    f"{_weight_text(available, target_unit)} is available"
                ),
                "expected": (
                    f"at most {_weight_text(available, target_unit)} of batch "
                    f"{_batch_label(key)}"
                ),
                "found": _weight_text(amount, target_unit),
            },
        }
    )


def _cycle_error(cycle: list[int]) -> GenealogyGraphError:
    """Build the 422 payload for one mutual-wait production cycle."""
    chain = " -> ".join(str(p) for p in (*cycle, cycle[0]))
    return GenealogyGraphError(
        {
            "process_index": cycle[0],
            "cycle": cycle,
            "error": {
                "error": "CYCLE_DETECTED",
                "message": (
                    f"processes {chain} form a production/consumption "
                    "dependency cycle; the graph must be acyclic"
                ),
                "expected": "an acyclic producer/consumer dependency graph",
                "found": f"cycle {chain}",
            },
        }
    )


def genealogy(
    opening_stock: list[str],
    target_unit: str,
    processes: list[tuple[list[str], list[str]]],
) -> dict:
    """Validate the batch genealogy and deduct inventory process by process.

    ``processes`` is a list of ``(inputs, outputs)`` pairs of scan lists
    in request order.  Returns ``{"target_unit": ..., "processes": [...],
    "batches": [...]}`` where ``processes`` is in execution order (each
    entry holding ``process_index``, the per-batch ``inputs`` /
    ``outputs`` dicts of ``{"gtin", "batch", "weight"}`` and the
    ``input_weight`` / ``output_weight`` / ``loss_weight`` totals) and
    ``batches`` holds every batch in first-seen order with its
    ``opening`` / ``produced`` / ``consumed`` / ``remaining`` weights;
    every weight is an unrounded :class:`Decimal` in the target unit.

    Each round executes the lowest-request-index process whose inputs
    are all covered by the current *unrounded* stock; a process short of
    a batch whose producer has not run yet waits.  When nothing can run,
    processes still waiting on one another raise
    :class:`GenealogyGraphError` (``CYCLE_DETECTED``); a short batch
    whose sources have all run raises :class:`InsufficientStockError`.
    """
    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION

        batch_order: list[tuple[str, str]] = []
        noted: set[tuple[str, str]] = set()

        def note(key: tuple[str, str]) -> None:
            if key not in noted:
                noted.add(key)
                batch_order.append(key)

        # Opening stock: aggregated per batch, in first-seen order.
        opening: dict[tuple[str, str], Decimal] = {}
        for index, scan in enumerate(opening_stock):
            gtin, batch, weight, unit = _scan(
                {"scan_index": index}, f"opening stock scan {index}", scan
            )
            key = (gtin, batch)
            converted = _to_target(weight, unit, target_unit)
            opening[key] = opening.get(key, Decimal(0)) + converted
            note(key)

        # Per-process consumed/produced weights, aggregated per batch.
        consumed_per_process = []
        produced_per_process = []
        for process_index, (inputs, outputs) in enumerate(processes):
            consumed = _aggregate_process(
                inputs, process_index, "input", target_unit
            )
            produced = _aggregate_process(
                outputs, process_index, "output", target_unit
            )
            consumed_per_process.append(consumed)
            produced_per_process.append(produced)
            for key in produced:
                note(key)

        # A batch may be produced by at most one process.
        producer: dict[tuple[str, str], int] = {}
        for process_index, produced in enumerate(produced_per_process):
            for key in produced:
                first = producer.get(key)
                if first is not None:
                    raise GenealogyGraphError(
                        {
                            "process_index": process_index,
                            "first_index": first,
                            "error": {
                                "error": "MULTIPLE_PRODUCERS",
                                "message": (
                                    f"process {process_index}: batch "
                                    f"{_batch_label(key)} is already produced "
                                    f"by process {first}; each batch may be "
                                    "produced by at most one process"
                                ),
                                "expected": (
                                    f"batch produced only by process {first}"
                                ),
                                "found": (
                                    f"second producer at process {process_index}"
                                ),
                            },
                        }
                    )
                producer[key] = process_index

        # Every consumed batch needs a source: the opening stock or the
        # unique output of one process (``producer`` was built above).
        for process_index, consumed in enumerate(consumed_per_process):
            for key in consumed:
                if key not in producer and key not in opening:
                    raise GenealogyGraphError(
                        {
                            "process_index": process_index,
                            "error": {
                                "error": "UNKNOWN_BATCH",
                                "message": (
                                    f"process {process_index}: input batch "
                                    f"{_batch_label(key)} has no source: it "
                                    "is neither in the opening stock nor "
                                    "produced by any process"
                                ),
                                "expected": (
                                    "input batch present in opening_stock or "
                                    "produced by exactly one process"
                                ),
                                "found": _batch_label(key),
                            },
                        }
                    )

        # Inventory-driven scheduling.  A process is executable when the
        # unrounded balances cover every one of its inputs; opening stock
        # (and earlier outputs) can take a chain through a structural
        # loop, so no static topological sort is possible.  Among the
        # executable processes the lowest request index runs.  When none
        # is executable the rest are either mutually waiting
        # (CYCLE_DETECTED) or short of a batch whose sources have all run
        # (INSUFFICIENT_STOCK, naming the process and batch).
        stock = dict(opening)
        results = []
        executed: set[int] = set()
        while len(executed) < len(processes):
            ready: int | None = None
            waiting_on: list[set[int]] = [set() for _ in processes]
            starved: tuple[int, tuple[str, str], Decimal, Decimal] | None = None
            for process_index, consumed in enumerate(consumed_per_process):
                if process_index in executed:
                    continue
                waits: set[int] = set()
                short = False
                for key, amount in consumed.items():
                    available = stock.get(key, Decimal(0))
                    if amount <= available:
                        continue
                    short = True
                    producer_index = producer.get(key)
                    if (
                        producer_index is not None
                        and producer_index not in executed
                    ):
                        # The short batch may still be produced: hold on.
                        waits.add(producer_index)
                    elif starved is None:
                        # Every source has run: this process can never
                        # become ready; remember the lowest such index.
                        starved = (process_index, key, amount, available)
                if not short and ready is None:
                    ready = process_index
                waiting_on[process_index] = waits

            if ready is not None:
                process_index = ready
                consumed = consumed_per_process[process_index]
                produced = produced_per_process[process_index]
                for key, amount in consumed.items():
                    stock[key] -= amount
                for key, amount in produced.items():
                    stock[key] = stock.get(key, Decimal(0)) + amount

                input_weight = sum(consumed.values(), Decimal(0))
                output_weight = sum(produced.values(), Decimal(0))
                results.append(
                    {
                        "process_index": process_index,
                        "inputs": [
                            {"gtin": gtin, "batch": batch, "weight": weight}
                            for (gtin, batch), weight in consumed.items()
                        ],
                        "outputs": [
                            {"gtin": gtin, "batch": batch, "weight": weight}
                            for (gtin, batch), weight in produced.items()
                        ],
                        "input_weight": input_weight,
                        "output_weight": output_weight,
                        "loss_weight": input_weight - output_weight,
                    }
                )
                executed.add(process_index)
                continue

            if starved is not None:
                process_index, key, amount, available = starved
                raise _insufficient_stock_error(
                    process_index, key, amount, available, target_unit
                )
            raise _cycle_error(_find_cycle(waiting_on))

        # Final balance of every batch ever seen.
        consumed_totals: dict[tuple[str, str], Decimal] = {}
        produced_totals: dict[tuple[str, str], Decimal] = {}
        for consumed, produced in zip(
            consumed_per_process, produced_per_process
        ):
            for key, weight in consumed.items():
                consumed_totals[key] = consumed_totals.get(key, Decimal(0)) + weight
            for key, weight in produced.items():
                produced_totals[key] = produced_totals.get(key, Decimal(0)) + weight
        batches = []
        for gtin, batch in batch_order:
            key = (gtin, batch)
            batches.append(
                {
                    "gtin": gtin,
                    "batch": batch,
                    "opening": opening.get(key, Decimal(0)),
                    "produced": produced_totals.get(key, Decimal(0)),
                    "consumed": consumed_totals.get(key, Decimal(0)),
                    "remaining": stock.get(key, Decimal(0)),
                }
            )

    return {
        "target_unit": target_unit,
        "processes": results,
        "batches": batches,
    }
