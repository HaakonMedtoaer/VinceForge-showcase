"""
Classic XML -> GENERIC PIPELINE: the translation the native M3-API path
cannot do.

WHY THIS EXISTS
---------------
`build.py` renders an `ir.Workflow` into Live's confirmed M3-API shape
(trigger -> [generic filter] -> m3_api -> excel). That shape has one hard
limit, and Classic functions hit it constantly: `GENERIC_FILTER` sits
between the trigger and the M3 API step, runs ONCE, and sees only raw
spreadsheet rows. So it cannot

  * gate one M3 call while the next runs unconditionally,
  * read a column an earlier M3 call WROTE (both real customer files gate
    on exactly that - C05 on P and Q, "Existing Price Lists" on BK/BM/BO),
  * or express `<BasedOnTransaction>` at all, which is the construct 14 of
    the 20 real `<ImportDetail>`s use.

This module renders the same IR onto the generic path instead, where each
call carries its own condition and a condition may read an earlier call's
own result. See `docs/vince-generic-m3-pipeline.md`.

WHY A CODE STEP AND NOT A CHAIN OF Transform+REST PAIRS
-------------------------------------------------------
The design sketch in that doc proposed one `m3Bulk` step per
`<ImportDetail>`. Writing the translator against the two real files showed
that shape cannot carry a Classic import faithfully, for two independent
reasons - both of which the Code-step path answers exactly:

1. **Correlation.** A bulk step sends N rows as N transactions and reads
   an earlier step's answer for row i as `records[$i]`. That alignment
   only holds while every step saw the same rows in the same order. A
   Classic chain gates nearly every call, so each step sends a DIFFERENT
   subset - and "Add Price List" chains nine calls, each gated on the one
   before. By the third step the index means nothing. A row pipeline holds
   one row at a time, so there is no index to misalign.

2. **Per-call success/failure.** `<BasedOnTransaction TransactionFailed>`
   needs to know whether ONE M3 transaction succeeded. The bulk response's
   confirmed failure signal is the aggregate `nrOfFailedTransactions`; what
   a single failed result entry looks like inside a multi-transaction
   payload is NOT confirmed. Posting exactly one transaction per request -
   which is what the row pipeline does - turns that aggregate counter into
   a per-call verdict using only confirmed fields. That is the difference
   between expressing the get-or-create idiom ("Add Price List" runs
   `AddPriceList` only when `GetPriceList` FAILED) and guessing at it.

The cost is stated plainly and `build_pipeline.pipeline_warnings()` repeats
it: a Code step is invisible in the builder UI, spends wall clock on
sequential HTTP, and has the 29-second synchronous-execution ceiling to
respect. For a migration that reproduces Classic's own per-row sequential
behaviour, that is the right trade - but prefer `build.py` whenever the
native path fits.

WHAT IS NOT TRANSLATED HERE
---------------------------
An `<Export>` block (`ir.Transaction.per_input_row == False`) queries M3
and writes rows INTO the sheet. It has no input row to iterate, no filters
and no chaining, so the native path renders it perfectly - including
`excel_1`, which this path cannot use at all. A unit made only of Export
calls therefore raises `ClassicPipelineError` pointing back at `build.py`
rather than being bent into a pipeline; in a MIXED unit the Export calls
are reported and left out.

THE TWO UNKNOWNS, BOTH NOW SETTLED
-----------------------------------
Both were isolated behind one named thing each, as the migration summary's
next-steps item 3 asked, so real runs could settle each without touching
the translation - and now have:

  * `row_key()` - CONFIRMED 2026-09-11: a spreadsheet row is keyed by
    column letter (see the comment at its definition). That run also
    surfaced a real `rows_js` bug in `pipeline_ir.py`, now fixed, and a
    follow-on caveat about leading blank columns getting trimmed.
  * `GROUP_RESULT_SCOPE` - CONFIRMED 2026-09-11, in a later run on the
    same workflow once the bug above was fixed: with two rows sharing a
    group key, the once-per-group call's carried result reached the
    second row correctly (see the comment at the constant).

Both still print what they found in every conversion's warnings, since a
different sheet or a different Classic file is still worth a sanity check.
"""

from typing import Optional

from build import is_numeric_filter_value
from ir import Workflow
from pipeline_ir import (
    CALL_STATUS_FAILED,
    CALL_STATUS_OK,
    Gate,
    CodeCall,
    M3RowPipelineStep,
    Pipeline,
    TriggerField,
    VXL_LABEL,
)


class ClassicPipelineError(ValueError):
    """A Classic unit that should not be built on the generic path."""


# --- Formerly unknown 1: how a spreadsheet row is addressed -------------
#
# Classic addresses a value by COLUMN LETTER ("*B", "*AM"). CONFIRMED
# 2026-09-11 against a real VXL-triggered run (WFEXEC-08330923...): a
# spreadsheet row arrives keyed by COLUMN LETTER, not header name - unlike
# every widget-triggered example on record, which is keyed by header name
# ("OrderNumber", "Order Number"). `column_headers` stays as an override for
# the rare sheet that needs one, not as a hedge against this being wrong.
#
# That same run also caught a real bug this question's uncertainty was
# masking: `M3RowPipelineStep.rows_js` defaulted to `trigger.body.data`,
# but a VXL trigger's `body` is a bare ARRAY, not `{data: [...]}` - so every
# row pipeline was silently processing zero rows until this was fixed.
#
# A FOLLOW-ON CAVEAT, confirmed 2026-09-11 in a later run on the same
# workflow: the column letter is NOT the worksheet's real column - VXL's
# read feature trims entirely-EMPTY LEADING columns from the used range and
# re-indexes what is left starting at "A". Two runs on the same sheet
# nailed this down: with real data typed into worksheet column A, JSON key
# "A" held it; with worksheet column A left blank and the same data typed
# into worksheet column B instead, JSON key "A" held THAT - a real shift,
# reproduced by changing exactly one thing (whether column A had content).
# It bit this project's own test sheet for a mundane reason: column A is
# where the paired `<Export>` block's OJPRRF answer would land, and an
# Import-only conversion (this module refuses to translate Export calls)
# never writes it, so column A sits blank and gets trimmed - shifting every
# subsequent Classic letter left by one in the JSON that actually arrives.
# `row_key()` itself needs no change - it is right about ABSOLUTE Classic
# column letters - but whoever runs a converted Import-only pipeline needs
# to know: if the columns the original Export used are left blank, every
# Import column downstream of them shifts. Safest fix at the sheet level is
# to keep something (even a placeholder) in every column Classic's *lower*
# letters ever wrote, blank or not.


def row_key(column: str, column_headers: Optional[dict] = None) -> str:
    """
    The property name a `{"row": ...}` value should look up for `column`.

    The column letter, unless the caller supplied a letter -> header map.
    The generated Code step then tries the key as given, then that mapped
    name, then case-insensitively, and reports the row it actually received
    in `_diagnostics.firstRow` / `_diagnostics.rowKeys` - confirmed real,
    kept as a safety net rather than removed.
    """
    if column_headers and column in column_headers:
        return str(column_headers[column])
    return column


# --- Formerly unknown 2: how far a once-per-group call's result reaches --
#
# Confirmed (2026-09-10, from a real Classic run): a `<StartRun>` call
# fires once per group, on the group's FIRST row, group breaks are detected
# against the PREVIOUS row only, and its `<Cell>` result is written to that
# first row alone with NO fill-down. Classic's own source contradicted
# itself on what that means for the group's OTHER rows: read literally off
# the sheet, a per-row filter on that column passes on row 1 and fails on
# rows 2..n, so the group's remaining line rows would never run.
#
# "group" was chosen as the default because both real files only make
# sense that way. C05_MMS100's "Create RO" groups by B C E O (one
# destruction order) and its `MMZ101MI.AddLine` - one LINE per row - is
# gated on P, the column the once-per-group `ChkAuthority` writes. Under
# "firstRow" exactly one line per order would ever be added, which is not
# a workflow anyone would write.
#
# CONFIRMED 2026-09-11, in the SAME run that settled Unknown 1
# (WFEXEC-3cca6a49...): with two rows sharing one group key, `SelUsers`
# (once-per-group) fired on row 1 and was correctly skipped on row 2
# (`_diagnostics.groupRepeatsSkipped: 1`) - and row 2's own
# `AUTCHKMI_ChkAuthority` still came back "ok", because it read row 1's
# CARRIED result rather than seeing "notrun". That is exactly the "group"
# behaviour this constant claims: the once-per-group answer reaches every
# row of the group, not just its first. This confirms the CODE does what
# it is documented to do; it is not an independent replay of Classic's own
# engine (nobody here can run that), but it is the run that would have
# caught a wrong default - row 2's chain would have died at the first gate
# under "firstRow", and it did not.
GROUP_RESULT_SCOPE = "group"


# Classic filter wrappers this path can render. Kept to the operators real
# files have actually shown (EQ, NE, GT, and - from a real CustomerA customer
# file set, 2026-09 - GE, LT, LE) - `pipeline_ir.GATE_OPERATORS` has a few
# more (contains/in/notIn), but mapping an unseen Classic suffix onto one of
# them by name similarity is exactly the kind of guess that turns into a
# wrong gate in a customer's tenant. Anything else is reported, never
# approximated.
FILTER_OPERATORS = {"EQ": "eq", "NE": "ne", "GT": "gt", "GE": "ge", "LT": "lt", "LE": "le"}


def _ref_for(program: str, transaction: str, taken: set) -> str:
    """
    A JS-safe handle for one call, occurrence-suffixed.

    `AUTCHKMI.ChkAuthority` legitimately appears twice in one real block,
    and refs become keys in the generated plan, so the second occurrence
    has to be distinguishable rather than silently overwrite the first.
    """
    base = "".join(c if c.isalnum() else "_" for c in f"{program}_{transaction}")
    ref = base
    suffix = 1
    while ref in taken:
        suffix += 1
        ref = f"{base}_{suffix}"
    taken.add(ref)
    return ref


def _describe_filter(rf) -> str:
    label = f" [{rf.label}]" if rf.label else ""
    return f"column {rf.column} {rf.operator} {rf.value!r}{label}"


def translate_classic_to_pipeline(
    wf: Workflow,
    base_url: Optional[str] = None,
    connection_id: Optional[str] = None,
    column_headers: Optional[dict] = None,
    # False since 2026-09-29: debug's {records, _diagnostics} wrapper is not
    # the bare row array a sheet write-back needs, so it never displays in
    # Excel. Opt in for a diagnostic run only.
    debug: bool = False,
    # 1-based sheet row holding the table's header cells; default is the row
    # directly above InputStartRow. See pipeline_ir.M3RowPipelineStep.
    header_row: Optional[int] = None,
) -> tuple:
    """
    One Classic unit (`ir.Workflow`) -> (`pipeline_ir.Pipeline`, warnings).

    Raises `ClassicPipelineError` when the unit has no per-input-row calls
    at all - that is an `<Export>`-only block, which belongs on `build.py`'s
    native path (see the module docstring).
    """
    warnings = []
    column_headers = dict(column_headers or {})
    if header_row is not None and not (wf.input_start_row and header_row < wf.input_start_row):
        # One header-row setting can be applied to a file whose blocks start at different rows.
        warnings.append(
            f"TABLE HEADER ROW {header_row} IGNORED: this block's InputStartRow is "
            f"{wf.input_start_row!r}, and the header must be above it. Using the default "
            "(the row directly above the data start).")
        header_row = None

    export_txns = [t for t in wf.transactions if not t.per_input_row]
    row_txns = [t for t in wf.transactions if t.per_input_row]

    if not row_txns:
        raise ClassicPipelineError(
            f"{wf.label!r} has no per-row calls - it is an <Export> block, which queries M3 and "
            "writes rows into the spreadsheet. The generic pipeline path exists for per-call "
            "filtering and cannot use excel_1 at all, so there is nothing here it does better: "
            "build this one with the native path (build.build_live_workflow / "
            "`cli.py classic-xml`)."
        )

    # A column an <Export> call writes reaches a later call through the
    # SHEET, so it is ordinary row input on this path - and an API_CHAIN
    # input pointing at an Export call can fall back to that column.
    export_field_column = {}
    for txn in export_txns:
        for cell in txn.cells:
            export_field_column.setdefault((txn.ir_id, cell.field_name), cell.column)

    if export_txns:
        names = ", ".join(f"{t.program}.{t.transaction}" for t in export_txns)
        warnings.append(
            "EXPORT CALL(S) NOT TRANSLATED: " + names + " read M3 and write the answer into the "
            "spreadsheet (Classic's <Export> block). They are not driven by an input row, so they "
            "have no place in a row pipeline, and this path has no Excel step to write into. The "
            "rows they produced are treated as ordinary spreadsheet input here - build that half "
            "with the native path (`cli.py classic-xml`), which renders an Export block and its "
            "excel_1 columns properly, and run it first."
        )

    # ir_id -> ref, and the calls in order. Built first so gates and
    # API_CHAIN inputs can name a producer.
    taken = set()
    ref_by_ir_id = {}
    for txn in row_txns:
        ref_by_ir_id[txn.ir_id] = _ref_for(txn.program, txn.transaction, taken)

    trigger_fields = {}      # field name -> TriggerField
    reads_by_ref = {}        # ref -> set of field names other calls read from it
    uses_row_values = [False]

    def _row_value(column: str) -> dict:
        uses_row_values[0] = True
        return {"row": row_key(column, column_headers)}

    def _writer_before(column: str, position: int):
        """
        The nearest EARLIER per-row call whose <Cell> writes `column`.

        Nearest-earlier, not first-in-file: Classic resolves a reference by
        document order, and a column can legitimately be rewritten further
        down (both real files rewrite nothing, but the same rule already
        governs BasedOnTransaction and API_CHAIN, so it should not differ
        here).
        """
        found = None
        for earlier in row_txns[:position]:
            for cell in earlier.cells:
                if cell.column == column:
                    found = (earlier, cell)
        return found

    def _filter_value(rf, position: int) -> dict:
        """The left-hand side of a Classic RowFilter on this path."""
        writer = _writer_before(rf.column, position)
        if writer is None:
            return _row_value(rf.column)
        earlier, cell = writer
        ref = ref_by_ir_id[earlier.ir_id]
        reads_by_ref.setdefault(ref, set()).add(cell.field_name)
        return {"call": f"{ref}.{cell.field_name}"}

    def _gates_for(rf_list, position: int, where: str) -> list:
        gates = []
        for rf in rf_list:
            operator = FILTER_OPERATORS.get(rf.operator)
            if operator is None:
                warnings.append(
                    f"FILTER OPERATOR NOT TRANSLATED ({where}): {_describe_filter(rf)}. Only the "
                    f"Classic wrappers seen in real files are mapped ({', '.join(sorted(FILTER_OPERATORS))}); "
                    "this one has never been observed, so it is reported rather than guessed at. "
                    "The condition is NOT applied - the workflow runs for rows Classic would have "
                    "skipped."
                )
                continue
            if operator in ("gt", "ge", "lt", "le") and not (rf.value or "").strip():
                warnings.append(
                    f"FILTER NOT TRANSLATED ({where}): {_describe_filter(rf)}"
                    + (" with IncludeBlank=\"true\"" if rf.include_blank else "") +
                    f". A {rf.operator} against an empty value reads as self-contradictory "
                    "(\"greater than nothing, but also include blanks\") and Classic's meaning for "
                    "it is not confirmed; rendered literally it would compare every value against "
                    "zero and drop every non-numeric row. The condition is NOT applied - settle it "
                    "on the Classic side first."
                )
                continue
            if operator in ("gt", "ge", "lt", "le") and not is_numeric_filter_value(rf.value):
                # The gate compares $number()/Number() on both sides, so a
                # non-numeric literal makes it fail on EVERY row and the call
                # never runs - silently. A real CustomerA file set has hundreds
                # of GE/GT '!' filters; '!' is the lowest printable character,
                # so it very likely means "not blank" under a TEXT comparison,
                # but that reading is not confirmed, so it is not mapped.
                warnings.append(
                    f"FILTER NOT TRANSLATED ({where}): {_describe_filter(rf)}. This path compares "
                    f"a {rf.operator} numerically, and {rf.value!r} is not a number, so rendered "
                    "literally the call would never run. The condition is NOT applied - the call "
                    "runs for rows Classic would have skipped."
                    + (" A value of '!' is very likely Classic's 'not blank' idiom (the lowest "
                       "printable character, under a text comparison); if so, it needs a "
                       "not-blank gate added by hand." if (rf.value or "").strip() == "!" else "")
                )
                continue
            if rf.include_blank:
                warnings.append(
                    f"IncludeBlank IGNORED ({where}): {_describe_filter(rf)} carries "
                    "IncludeBlank=\"true\", whose meaning is not confirmed. The comparison was "
                    "translated; whatever IncludeBlank adds to it was not."
                )
            if not rf.is_hidden:
                warnings.append(
                    f"FILTER VALUE IS NOW FIXED ({where}): {_describe_filter(rf)} is not hidden in "
                    "Classic, so whoever ran it could change the value. On this path the condition "
                    "is compiled into the Code step, so the value is baked in - expose it as a "
                    "trigger field by hand if it needs to stay editable."
                )
            gates.append(Gate(
                left=_filter_value(rf, position),
                op=operator,
                right={"const": rf.value},
                case_insensitive=rf.is_lower,
            ))
        return gates

    # --- the calls -----------------------------------------------------
    calls = []
    critical_calls = []
    for position, txn in enumerate(row_txns):
        ref = ref_by_ir_id[txn.ir_id]
        where = f"{txn.program}.{txn.transaction}"
        gates = []

        # <BasedOnTransaction> first: it is the coarsest condition, and
        # putting it ahead of the row filters makes the generated
        # "gate not met" message name the chain break rather than a filter.
        if txn.based_on:
            producer_ref = ref_by_ir_id.get(txn.based_on)
            if producer_ref is None:
                warnings.append(
                    f"DEPENDENCY NOT TRANSLATED ({where}): it runs only when an earlier call "
                    + ("FAILED" if txn.based_on_failed else "SUCCEEDED")
                    + ", but that call is not part of this pipeline (it is an <Export> call, or the "
                    "parser could not resolve it). This call currently runs whenever its own "
                    "filters allow."
                )
            else:
                gates.append(Gate(
                    left={"callStatus": producer_ref},
                    op="eq",
                    right={"const": CALL_STATUS_FAILED if txn.based_on_failed else CALL_STATUS_OK},
                ))

        gates.extend(_gates_for(txn.row_filters, position, where))

        record = {}
        for input_field in txn.inputs:
            if input_field.needs_review:
                warnings.append(
                    f"INPUT NEEDS CHECKING ({where}.{input_field.name}): {input_field.needs_review}"
                )
            kind = input_field.source_kind
            if kind == "CONSTANT":
                record[input_field.name] = {"const": input_field.value}
            elif kind == "EXCEL_COLUMN":
                record[input_field.name] = _row_value(input_field.value)
            elif kind == "CLIENT":
                name = input_field.value or input_field.name
                record[input_field.name] = {"trigger": name}
                trigger_fields.setdefault(name, TriggerField(
                    name=name,
                    label=input_field.label or name,
                    mandatory=False,
                ))
            elif kind == "API_CHAIN":
                record[input_field.name] = _api_chain_value(
                    input_field, where, ref_by_ir_id, export_field_column,
                    reads_by_ref, _row_value, warnings,
                )
            else:  # pragma: no cover - the IR has no fifth kind
                raise ClassicPipelineError(
                    f"{where}.{input_field.name}: unknown IR source kind {kind!r}"
                )

        if txn.is_critical is not None:
            # Collected, not warned per call: a real block marks nearly
            # every detail IsCritical, and ten copies of the same sentence
            # buries the warnings that differ.
            critical_calls.append(f"{where}={str(txn.is_critical).lower()}")
        if txn.max_rows is not None:
            warnings.append(
                f"MaxRows NOT TRANSLATED ({where}): the source caps this call at {txn.max_rows} "
                "rows. On this path the row count is whatever the spreadsheet sends."
            )
        if txn.needs_review:
            warnings.append(f"NEEDS CHECKING ({where}): {txn.needs_review}")

        calls.append(CodeCall(
            ref=ref,
            program=txn.program,
            transaction=txn.transaction,
            record=record,
            gates=gates,
            once_per_group=txn.runs_once_per_group,
            message_column=txn.message_column or None,
            # The chain is expressed by the gates, so one call failing must
            # not silently swallow the rest of the row: a later call that
            # genuinely depends on this one carries a callStatus gate and
            # skips itself, and one that does not should still run - which
            # is what Classic does.
            continue_on_error=True,
        ))

    # Fields other calls read from a call, so M3 is asked for them.
    # Restricting the answer to the <Cell> fields is also what Classic
    # wrote to the sheet, so the output stays close to the source.
    for txn, call in zip(row_txns, calls):
        wanted = {cell.field_name for cell in txn.cells}
        wanted |= reads_by_ref.get(call.ref, set())
        call.selected_columns = sorted(wanted)

    # --- block-level gates: Classic's <Import>-level RowFilter ---------
    row_gates = _gates_for(wf.row_filters, 0, "block level")
    for gate in row_gates:
        if "call" in gate.left:
            # _filter_value found an M3 writer for a BLOCK-level filter.
            # position 0 makes that impossible (nothing is earlier than the
            # first call), so this is unreachable - kept as an assertion
            # rather than a silent mis-scope.
            raise ClassicPipelineError(
                "A block-level filter resolved to a call result, which cannot be evaluated before "
                "the first call has run."
            )

    step = M3RowPipelineStep(
        calls=calls,
        base_url=base_url,
        connection_id=connection_id,
        row_gates=row_gates,
        group_columns=list(wf.group_columns),
        group_result_scope=GROUP_RESULT_SCOPE,
        column_headers=column_headers,
        debug=debug,
        name=f"{wf.label} (M3, per row)",
        # The input rows come back with the <Cell> columns written in, in
        # the shape they arrived - not a per-call log, which floods the
        # sheet it is written back into.
        output="sheet",
        sheet_cells=[
            {"call": call.ref, "field": cell.field_name, "column": cell.column}
            for txn, call in zip(row_txns, calls)
            for cell in txn.cells
        ],
        message_column=wf.message_column or None,
        input_start_row=wf.input_start_row if (wf.input_start_row or 0) > 1 else None,
        header_row=header_row if (wf.input_start_row or 0) > 1 else None,
    )

    if critical_calls:
        warnings.append(
            "IsCritical NOT TRANSLATED: the source marks " + ", ".join(critical_calls) +
            ". The name suggests \"abort on failure\", but whether that aborts the row, the block "
            "or the whole run is not confirmed - and the difference decides whether a half-written "
            "row is left behind. The chain here is driven by the translated <BasedOnTransaction> "
            "gates alone, so a failure stops the calls that depend on it and nothing else."
        )

    group_calls = [c for c in calls if c.once_per_group]
    if group_calls or wf.group_columns:
        columns = ", ".join(wf.group_columns) or "(none declared)"
        scope_note = (
            "its result is carried to every row of the group"
            if GROUP_RESULT_SCOPE == "group"
            else "its result reaches the group's first row only, so later rows behave as if it "
                 "never ran"
        )
        warnings.append(
            "GROUPING: the source groups rows by column(s) " + columns +
            " and runs " + str(len(group_calls)) + " call(s) once per group. The generated Code "
            "step fires those on a group's first row, detecting a break against the PREVIOUS row "
            "only (so the rows must arrive SORTED by those columns - confirmed Classic "
            "behaviour), and " + scope_note + ". CONFIRMED 2026-09-11 against a real run: with "
            "two rows sharing a group key, the once-per-group call's carried result reached the "
            "second row's gates correctly - see classic_to_pipeline.GROUP_RESULT_SCOPE."
        )

    if uses_row_values[0]:
        warnings.append(
            "ROW ADDRESSING: Classic addresses values by spreadsheet COLUMN LETTER, and this "
            "conversion keeps those letters as the row property names" +
            (" except where a column -> header map was supplied" if column_headers else "") +
            ". CONFIRMED 2026-09-11 against a real VXL-triggered run: a spreadsheet row does "
            "arrive keyed by column letter, unlike every widget-triggered example on record "
            "(keyed by header name). The generated step still looks up the letter, then any "
            "mapped header, then case-insensitively, and reports the row it really got in "
            "_diagnostics.rowKeys - worth a glance on a new sheet layout, not a leap of faith."
        )

    if wf.message_column:
        warnings.append(
            f"MessageColumn {wf.message_column}: each row's failed/errored calls are written there "
            "as '<call>: <message>' joined by ' | ', and blank when every call that ran succeeded. "
            "Classic's own wording in that column is not reproduced."
        )

    per_call_columns = sorted({c.message_column for c in calls if c.message_column})
    if per_call_columns:
        warnings.append(
            f"PER-CALL MESSAGE COLUMNS ({', '.join(per_call_columns)}): each call that declares its own "
            "MessageColumn writes its failure text (M3's error message and code) to that column, blank "
            "when it succeeded, was gated off, or did not run. Calls sharing a column are joined with "
            "' | ' in call order - what Classic does when several calls share one is NOT confirmed. "
            "The block-level column still receives every call's failure."
        )

    if any(txn.cells for txn in row_txns):
        warnings.append(
            "CELL COLUMNS WRITTEN BY LETTER (unconfirmed on a table): the Code step returns the "
            "input rows unchanged in shape, with each <Cell> field written under its column's key "
            "- the letter, or its columnHeaders name. The sheet must be formatted as an Excel TABLE "
            "for VXL to know where to read and write. Not yet confirmed on a table: whether its rows "
            "arrive keyed by header name rather than letter, whether a column blank on every input "
            "row is written back, and that VXL's trimming of empty leading columns has not shifted "
            "the letters. Check _diagnostics.rowKeys on the first run."
        )

    if wf.input_start_row and wf.input_start_row > 1:
        header = header_row or wf.input_start_row - 1
        warnings.append(
            f"INPUT START ROW {wf.input_start_row}: VXL sends the sheet's whole used range from row 1, "
            f"so the generated step skips rows 1-{wf.input_start_row - 1} (never called) and reads the "
            f"table's column names from sheet row {header}. It returns only the rows BELOW that header "
            f"(rows between it and {wf.input_start_row} unchanged), keyed by those names, so VXL writes "
            "into the table's own columns. The Excel TABLE's header must therefore be on that row"
            + ("" if header_row else " - the one directly above the Classic data start; pass header_row "
               "to use another") +
            ". Row 1 of the sheet must not be empty (a title is enough): VXL drops empty leading "
            "rows, which shifts every row - the step refuses to run if its header row looks shifted."
        )
    if wf.source_sheet_name or wf.target_sheet_name or wf.input_start_row == 1:
        warnings.append(
            "SHEET NAME NOT TRANSLATED: the source names sheet(s) "
            f"(source={wf.source_sheet_name!r} target={wf.target_sheet_name!r}). This path has no "
            "Excel step - VXL Live sends the rows it has selected, so which sheet is decided there."
        )

    if not base_url:
        warnings.append(
            "NO ION API BASE URL: the generated Code step needs the target tenant's ION API root "
            "(including the tenant segment) to build the bulk execute endpoint. Nothing can derive "
            "it - region and tenant segment are both per-tenant - so building Live JSON from this "
            "pipeline will fail until one is given. Take it from a real REST API step in the "
            "target tenant."
        )

    warnings.append(
        "TRIGGER FIELD TYPES DEFAULT TO STRING: this path does no M3 field-metadata lookup (that "
        "is an M3 API step feature), so nothing validates field names, lengths or types. A wrong "
        "field name surfaces at run time, in M3's own error."
    )

    pipeline = Pipeline(
        label=wf.label,
        steps=[step],
        description=f"Converted from Classic XML block {wf.label!r} onto the generic pipeline path.",
        trigger_fields=list(trigger_fields.values()),
        has_spreadsheet_input=True,
        labels=[VXL_LABEL],
    )
    return pipeline, warnings


def _api_chain_value(input_field, where, ref_by_ir_id, export_field_column,
                     reads_by_ref, row_value, warnings) -> dict:
    """
    A Classic `*PROGRAM.Transaction.FIELD` input, on this path.

    Straightforward when the producer is another per-row call: read that
    call's result for this row. When the producer is an `<Export>` call it
    is not in the pipeline at all - but the classifier only redirected the
    value to API_CHAIN because the Export block writes that same field to a
    spreadsheet column, so the value really is on the sheet and the column
    is the right source.
    """
    ref_info = input_field.ref or {}
    producer_ir_id = ref_info.get("producer_ir_id")
    field_name = ref_info.get("name") or input_field.value

    producer_ref = ref_by_ir_id.get(producer_ir_id)
    if producer_ref:
        reads_by_ref.setdefault(producer_ref, set()).add(field_name)
        return {"call": f"{producer_ref}.{field_name}"}

    column = export_field_column.get((producer_ir_id, field_name))
    if column:
        warnings.append(
            f"CHAINED VALUE READ FROM THE SHEET ({where}.{input_field.name}): Classic takes it "
            f"from an <Export> call's {field_name} output, which that block writes to column "
            f"{column}. The Export call is not part of this pipeline, so the value is read from "
            f"column {column} instead - the same value, as long as the Export half has been run "
            "first."
        )
        return row_value(column)

    warnings.append(
        f"CHAINED VALUE UNRESOLVED ({where}.{input_field.name}): Classic takes it from "
        f"{input_field.value!r}, whose producing call could not be found in this block. It is sent "
        "as an EMPTY value - fix it by hand before running this workflow."
    )
    return {"const": ""}
