"""
BUILD stage - shared by every source. Turns a fully-resolved ir.Workflow
into the workflowConfig + stepConfig structure Live's API expects.

This is almost a verbatim port of the original converter's
build_live_workflow(): all the hard-won structural knowledge (hasError is
client-computed, workflowType must be STANDARD, the <column>-1 dual-role
mechanism, the Generic Filter uniform/non-uniform safety check, the
synthetic-output-column fix, etc.) is unchanged. What changed is only what
build.py no longer has to do, because the IR arrives pre-resolved:

  - No _classify_source() here anymore - every InputField already carries
    a resolved source_kind. That interpretation was Classic-syntax-
    specific and now lives in source_classic_xml.py (see ir.py's module
    docstring for why).
  - No seen_so_far / name-based document-order resolution - cross-
    transaction references (based_on, API_CHAIN) already point at another
    Transaction via ir_id; that resolution happened in whichever source
    parser built the IR.
  - No export_transactions/import_transactions split - "does this
    transaction contribute Excel output columns" is just "does it have
    cells", which works the same way for every source.

What's still exactly build.py's job: generating Live's real per-workflow
instance IDs (ir_id is only ever an internal handle, never sent to Live)
and laying out Live's JSON shape.
"""

import re
from typing import Optional

from envelope import workflow_envelope
from ids import new_id
from ir import Workflow
from metadata import field_metadata


# Classic operator wrapper -> Live GENERIC_FILTER operator value, for every
# operator whose JSON value has been CAPTURED from a real UI-built workflow
# (RUN/captures/generic-filter-all-operators.json, 2026-09-28 - every
# operator the UI offers, one per column; EQ/NE were also confirmed earlier
# by "Filter tests - Input and output"). Live's UI has four more with no
# Classic wrapper we know of: isBlank, isNotBlank, stringContains,
# stringNotContains (the two blank ones are saved as source "CONSTANT",
# value "''", and get no trigger field).
CAPTURED_FILTER_OPERATORS = {
    "EQ": "equal", "NE": "notEqual",
    "LT": "lessThan", "LE": "lessThanInclusive",
    "GT": "greaterThan", "GE": "greaterThanInclusive",
}

# The subset build.py actually EMITS - every captured operator with a
# Classic wrapper. The range ones were held back until two real runs of the
# UI-built test workflow settled what the capture could not
# (RUN/captures/generic-filter-constant-and-run.json, 2026-09-28):
#   - dataType "NUMBER" compares NUMERICALLY (C=50 passed lessThan "100";
#     B=10 failed greaterThan "10"), so a range is emitted as NUMBER.
#   - the UI's left-deep nesting is what _nest_filter_conditions writes.
# Being in this dict is NOT enough on its own for a range operator - see
# native_filter_problem: its value must be numeric, because a STRING range
# (lexical? "9" > "10") is still untested, and so is NUMBER against a value
# that is not a number.
REPRESENTABLE_FILTER_OPERATORS = dict(CAPTURED_FILTER_OPERATORS)

# Operators emitted with dataType "NUMBER". EQ/NE stay "STRING", unchanged
# from before: NUMBER equality would make "010" equal "10", which is a
# behaviour change nothing asked for.
RANGE_FILTER_OPERATORS = {"GT", "GE", "LT", "LE"}

# Plain decimal numbers only: what a NUMBER comparison was run against
# (integers) plus a decimal point. No exponents, signs-with-spaces,
# thousands separators or "nan" - float() accepts those, Live may not.
_NUMERIC_FILTER_VALUE = re.compile(r"-?\d+(\.\d+)?")


def is_numeric_filter_value(value: Optional[str]) -> bool:
    return bool(_NUMERIC_FILTER_VALUE.fullmatch((value or "").strip()))


def native_filter_problem(operator: str, value: Optional[str]) -> Optional[str]:
    """
    Why one Classic RowFilter cannot go into a native Generic Filter, or
    None when it can. Takes the raw operator and value rather than a
    RowFilter so peek_classic_xml's read-only scan can ask the exact same
    question from bare XML attributes - one rule, not two copies drifting.
    """
    if operator not in REPRESENTABLE_FILTER_OPERATORS:
        return f"operator {operator} has no captured Generic Filter value"
    if operator in RANGE_FILTER_OPERATORS and not is_numeric_filter_value(value):
        return (f"{operator} on the non-numeric value {value!r} - a range is only emitted as a "
                "NUMBER comparison, and a text range (possibly lexical) is not confirmed by a run")
    return None


def _filter_data_type(rf) -> str:
    return "NUMBER" if rf.operator in RANGE_FILTER_OPERATORS else "STRING"


_FILTER_OPERATOR_SYMBOLS = {"EQ": "==", "NE": "!=", "GT": ">", "GE": ">=", "LT": "<", "LE": "<="}


def _describe_filter(rf) -> str:
    """One row filter as a human-readable condition, for a review note."""
    symbol = _FILTER_OPERATOR_SYMBOLS.get(rf.operator, rf.operator)
    notes = []
    if rf.is_lower:
        notes.append("case-insensitive per IsLower (meaning unconfirmed)")
    if rf.include_blank:
        notes.append("IncludeBlank=true (meaning unconfirmed, not applied)")
    suffix = ", " + ", ".join(notes) if notes else ""
    return f"column {rf.column} {symbol} {rf.value!r}{suffix} [{rf.label or 'unlabeled'}]"


# M3 transaction-name prefixes that only ever READ. Used for exactly one
# thing - deciding how loudly to report a grouped StartRun call - and never
# to shape any JSON. The migration summary names Add*/Upd*/Del* as the
# dangerous writing case; this inverts that into an allowlist so an
# unfamiliar prefix lands on the SAFE side of the warning (reported as
# possibly-writing) instead of being waved through as harmless.
_READ_ONLY_TRANSACTION_PREFIXES = ("Get", "Lst", "List", "Chk", "Sel", "Search", "Exist")


def _add_review_note(entry: dict, note: str) -> None:
    """
    Attach a review note to a transaction entry without clobbering one
    already there - a transaction can legitimately collect more than one
    (e.g. an unresolved dependency AND a non-uniform filter), and a plain
    assignment silently dropped whichever came first.
    """
    existing = entry.get("_needs_review")
    entry["_needs_review"] = f"{existing} | {note}" if existing else note


def _warn(warnings: Optional[list], message: str) -> None:
    """
    Report something the conversion could not represent.

    Out-of-band rather than an `_needs_review` key in the JSON: these are
    sheet-layout concerns belonging to excel_1, and while Live tolerates a
    stray key inside an m3_api transaction entry, nothing confirms it does
    in the Excel step's config - and that config drives real rendering.
    """
    print(f"  Note: {message}")
    if warnings is not None:
        warnings.append(message)


def _nest_filter_conditions(conditions: list) -> dict:
    """
    Lay Generic Filter conditions out the way Live's own UI saves them:
    left-deep, one level per condition after the second -
    {"all": [{"all": [{"all": [c1, c2]}, c3]}, c4]}. Confirmed by two real
    UI-built captures of ten conditions each
    (RUN/captures/generic-filter-all-operators.json and
    generic-filter-datatypes-and-or.json). One or two conditions come out as
    a single flat "all", identical to the earlier "Filter tests" capture;
    a flat list of three or more was never seen, so it is not emitted.

    Each condition's logicalType is the connector to the condition AFTER
    it: the level joining "everything up to X" with X's successor is "any"
    when X is "OR" (the last condition's logicalType joins nothing). Only
    AND is emitted here - Classic's RowFilters are all conjunctive.
    """
    node = {"all": conditions[:2]}
    for condition in conditions[2:]:
        node = {"all": [node, condition]}
    return node


def build_live_workflow(wf: Workflow, connection_id: str, tenant_id: str, environment_id: str,
                          metadata_overrides: Optional[dict] = None,
                          warnings: Optional[list] = None) -> dict:
    step_id = "m3_api_1"  # single M3 API step containing all transactions,
                          # matching what we observed Live do for multi-
                          # transaction workflows

    # One real Live instance ID per transaction *occurrence* (not per
    # (program, transaction) pair - the same transaction can legitimately
    # appear more than once, e.g. an initial attempt and a fallback retry,
    # and keying by name alone would silently collapse those into one).
    id_by_ir_id = {t.ir_id: new_id() for t in wf.transactions}

    # All column letters already spoken for by a real output mapping -
    # used to allocate a harmless, unused column for any output field we
    # have to synthesize (see below).
    all_used_columns = {cell.column for txn in wf.transactions for cell in txn.cells}

    # Column -> the transaction that WRITES it (Classic's <Cell>). Live's
    # Generic Filter sits between trigger_1 and the M3 API step and
    # filters raw spreadsheet rows, so it can never read a column an M3
    # call produces - see the filter section below, which needs this.
    column_written_by = {}
    for txn in wf.transactions:
        for cell in txn.cells:
            column_written_by.setdefault(cell.column, txn.program + "." + txn.transaction)

    step_config_transactions = {}
    client_field_info = {}   # field name -> {length, dataType, apiId}, for building trigger fields
    client_field_first_declared = {}  # field name -> real id of the FIRST transaction to use it as CLIENT
    constants = {}           # workflowConfig.constants
    # Collected here, applied to excel_1 after the main loop. Confirmed via
    # a real diagnostic workflow ("Claude test INPUT") that an
    # EXCEL/COLUMN-sourced INPUT field needs its own entry in
    # excel_1.sourceSheet.fields.columns, shaped exactly like an output
    # entry but with fieldId "<txnId>.input.<field>" instead of
    # "<txnId>.output.<field>".
    input_column_candidates = []  # list of (column, field_name, real_txn_id, meta)

    for order, txn in enumerate(wf.transactions, start=1):
        real_id = id_by_ir_id[txn.ir_id]
        input_dict = {}

        for input_field in txn.inputs:
            meta = field_metadata(txn.program, txn.transaction, input_field.name, metadata_overrides, "I")

            entry = {
                "name": input_field.name,
                "descr": input_field.label or meta["descr"],
                "type": meta["type"],
                "length": meta["length"],
                "mandatory": meta["mandatory"],
                "fieldType": "I",
                "isMapped": True,
                "hasGrouping": False,
            }

            if input_field.source_kind == "CONSTANT":
                const_key = f"CONSTANT_{input_field.name}"
                constants[const_key] = input_field.value
                entry["source"] = "CONSTANT"
                entry["value"] = const_key

            elif input_field.source_kind == "EXCEL_COLUMN":
                entry["source"] = "EXCEL"
                entry["sourceType"] = "COLUMN"
                entry["value"] = input_field.value
                input_column_candidates.append((input_field.value, input_field.name, real_id, meta))

            elif input_field.source_kind == "CLIENT":
                entry["source"] = "CLIENT"
                entry["value"] = input_field.value
                if input_field.name not in client_field_first_declared:
                    # First time this field name is used as CLIENT - this
                    # becomes the canonical declaration. No ref needed;
                    # matches the real example where the first occurrence
                    # carries no ref at all.
                    client_field_first_declared[input_field.name] = real_id
                else:
                    # A later occurrence of the same field name. WITHOUT
                    # this ref, Live has no way to know these are meant to
                    # be one shared trigger value - confirmed by a real
                    # test where four repeated occurrences with no ref
                    # became four separate fields instead of one shared
                    # field.
                    entry["ref"] = {
                        "apiId": client_field_first_declared[input_field.name],
                        "type": "INPUT",
                        "name": input_field.name,
                    }
                # Overwrite on each occurrence - the one real example we
                # have shows the trigger field's "apiId" pointing at the
                # LAST transaction to use that field as CLIENT, not the
                # first.
                client_field_info[input_field.name] = {
                    "length": meta["length"],
                    "dataType": meta["type"],
                    "apiId": real_id,
                }

            elif input_field.source_kind == "API_CHAIN":
                entry["source"] = "API"
                if input_field.ref:
                    producer_real_id = id_by_ir_id[input_field.ref["producer_ir_id"]]
                    fname = input_field.ref["name"]
                    entry["value"] = f"{producer_real_id}.output.{fname}"
                    entry["ref"] = {"apiId": producer_real_id, "name": fname, "type": "OUTPUT"}
                else:
                    entry["value"] = input_field.value

            if input_field.needs_review:
                entry["_needs_review"] = input_field.needs_review

            input_dict[input_field.name] = entry

        output_dict = {}
        for cell in txn.cells:
            meta = field_metadata(txn.program, txn.transaction, cell.field_name, metadata_overrides, "O")
            output_dict[cell.field_name] = {
                "name": cell.field_name,
                "descr": meta["descr"],
                "type": "NUMERIC" if cell.data_type == "decimal" else meta["type"],
                "length": meta["length"],
                "mandatory": meta["mandatory"],
                "fieldType": "O",
                "isMapped": True,
                "hasGrouping": False,
                "source": "EXCEL",
                "sourceType": "COLUMN",
                "value": cell.column,
            }

        txn_entry = {
            "id": real_id,
            "stepId": step_id,
            "programName": txn.program,
            "transactionName": txn.transaction,
            "name": txn.transaction,
            "source": "M3",
            "connectionId": connection_id,
            "executionOrder": order,
            "extendedGrouping": [],
            "input": input_dict,
            "output": output_dict,
            # "type" was ABSENT from every transaction our earlier drafts
            # generated, and its absence is the strongest suspect for why
            # a real test workflow opened with its M3 API step completely
            # inert. Every transaction in a real, UI-built, correctly-
            # rendering workflow we've captured has this field. The earlier
            # captures all used "ADD" regardless of verb, but it is NOT
            # always "ADD": "Claude test - API fields fail test.txt" (repo
            # root, gitignored) has "LIST" and "GET", and VinceGenerator's
            # all-steps capture has CRS610MI/AddText as "LIST" - so it does
            # not simply follow the transaction name either. What the value
            # controls is unconfirmed. "ADD" is kept because it is what the
            # workflows this builder was verified against used; do NOT
            # infer LIST/GET/ADD/UPD from the transaction name until one
            # UI-built workflow per value has been captured and run.
            "type": "ADD",
        }

        if txn.max_rows is not None:
            # Matches the one real example we have of a list-style
            # transaction: it carried maxRows, defaultInputConfig, and
            # hasSpreadSheetInput alongside it.
            txn_entry["maxRows"] = txn.max_rows
            txn_entry["defaultInputConfig"] = {}
            txn_entry["hasSpreadSheetInput"] = False

        if txn.based_on:
            txn_entry["basedOnTransaction"] = id_by_ir_id[txn.based_on]
            txn_entry["runOnFailure"] = bool(txn.based_on_failed)

        if txn.needs_review:
            txn_entry["_needs_review"] = txn.needs_review

        step_config_transactions[real_id] = txn_entry

    # POST-PROCESSING FIX, confirmed by a real execution error:
    # "SqliteError: no such column: <id>_MMS301MI_LstStockTakeDet.CONO".
    # A chained API reference can point to a field that was never given
    # an explicit output/Cell mapping (e.g. CONO, which M3 always returns
    # but which wasn't one of the columns in the sample file). Such a
    # field would be present as a reference elsewhere but never declared
    # where it's produced. Live's execution engine needs it declared on
    # the producer to resolve the reference at runtime. Fixed by scanning
    # every chained reference after the fact and synthesizing a minimal
    # output declaration (on a harmless, unused column letter) for
    # anything missing.
    next_synthetic_column_index = [0]

    def _next_synthetic_column():
        while True:
            idx = next_synthetic_column_index[0]
            next_synthetic_column_index[0] += 1
            letter = chr(ord("A") + idx) if idx < 26 else None
            if letter is None:
                raise RuntimeError("Ran out of single-letter columns for synthetic output fields - "
                                    "this workflow has more than 26 distinct columns; extend "
                                    "_next_synthetic_column to support multi-letter columns.")
            if letter not in all_used_columns:
                all_used_columns.add(letter)
                return letter

    # Needed by the fix below to look up a producing Transaction's
    # (program, transaction) for a metadata lookup, keyed by real id.
    txn_by_real_id = {id_by_ir_id[t.ir_id]: t for t in wf.transactions}

    for txn_entry in list(step_config_transactions.values()):
        for input_field_entry in txn_entry.get("input", {}).values():
            ref = input_field_entry.get("ref")
            if not ref or ref.get("type") != "OUTPUT":
                continue
            producer_id = ref["apiId"]
            field_name = ref["name"]
            producer_entry = step_config_transactions.get(producer_id)
            if producer_entry is None or field_name in producer_entry.get("output", {}):
                continue

            all_used_columns.update(col for col, _, _, _ in input_column_candidates)

            producer_txn = txn_by_real_id[producer_id]
            meta = field_metadata(producer_txn.program, producer_txn.transaction, field_name, metadata_overrides, "O")
            column = _next_synthetic_column()
            producer_entry.setdefault("output", {})[field_name] = {
                "name": field_name,
                "descr": meta["descr"],
                "type": meta["type"],
                "length": meta["length"],
                "mandatory": meta["mandatory"],
                "fieldType": "O",
                "isMapped": True,
                "hasGrouping": False,
                "source": "EXCEL",
                "sourceType": "COLUMN",
                "value": column,
            }

    # --- Generic Filter step (Classic's row filters, both levels) ---
    # Confirmed real shape via a manually-built Live test workflow
    # ("Filter tests - Input and output"): Live has a GENERIC_FILTER step
    # type that sits between trigger_1 and the M3 API step, filtering raw
    # Excel row data BEFORE any M3 transaction runs. This is a strong
    # structural match for RowFilterEQ/RowFilterNE - both gate on a raw
    # import column's value. (A second, DIFFERENT feature, "M3 Filter" /
    # m3_filter_1, filters a TRANSACTION'S OUTPUT rows after the API
    # call; not used here.)
    #
    # THE CATCH: Generic Filter runs once, before the ENTIRE M3 API step -
    # it cannot gate one transaction while letting a different one in the
    # same step run unconditionally. That makes Classic's two filter
    # levels map very differently:
    #
    #   Workflow.row_filters (Classic's <Import>-level filter, a sibling
    #   of the ImportDetails) gates the whole block, so it is an EXACT
    #   semantic match - it always goes straight into the step.
    #
    #   Transaction.row_filters (Classic's per-<ImportDetail> filter) is
    #   only safe to auto-translate when every transaction in the workflow
    #   shares the exact same ones (including the trivial single-
    #   transaction case). That decision needs visibility across ALL
    #   transactions, so it is made once, here, after the main loop.
    #
    # Either level can also carry a filter the native step cannot be given
    # (an operator with no captured value, or a range on a non-numeric
    # value - see native_filter_problem). Those are never approximated;
    # they are reported and the gate is left off.
    # A THIRD way a filter can be untranslatable, independent of the
    # operator and of uniformity: the column it reads is not spreadsheet
    # input at all, but one an M3 call WRITES (Classic's <Cell>). Generic
    # Filter runs BEFORE the M3 API step, on raw row data, so that column
    # is empty at the only moment the filter is evaluated - the gate would
    # pass everything or block everything, and which one is not something
    # to discover in a customer's tenant.
    #
    # This was invisible while ImportDetail <Cell> elements went unparsed:
    # such a column looked exactly like an ordinary input column. Both real
    # customer files are this shape (C05 gates on P and Q, Existing Price
    # Lists on BK and BM), which is also why the "real unsolved case" in
    # the migration summary was never really about uniformity - splitting
    # transactions across several M3 API steps would not have helped, since
    # no placement of a Generic Filter can see an M3 call's output.
    #
    # It matters at the BLOCK level too, and there it is a genuine bug fix
    # rather than a better message: a block gate goes straight into
    # generic_filter_1 with no uniformity check, so one on an M3-written
    # column used to be emitted and silently filter on an empty cell.
    def _representable(filters):
        return all(native_filter_problem(rf.operator, rf.value) is None for rf in filters)

    def _api_written(filters):
        return [rf for rf in filters if rf.column in column_written_by]

    def _describe_api_columns(filters) -> str:
        return ", ".join(rf.column + " (written by " + column_written_by[rf.column] + ")"
                         for rf in _api_written(filters))

    generic_filter_rowfilters = []

    # --- Level 1: the block-level gate. ---
    block_api = _api_written(wf.row_filters)
    block_ok = [rf for rf in wf.row_filters
                if native_filter_problem(rf.operator, rf.value) is None and rf not in block_api]
    block_bad = [rf for rf in wf.row_filters if native_filter_problem(rf.operator, rf.value)]
    generic_filter_rowfilters.extend(block_ok)
    if block_api:
        _warn(warnings,
              "BLOCK-LEVEL FILTER ON AN M3-WRITTEN COLUMN: the source gates this whole workflow "
              "on " + " AND ".join(_describe_filter(rf) for rf in block_api) + ", but column(s) " +
              _describe_api_columns(wf.row_filters) + " hold an M3 call's output, not "
              "spreadsheet input. Live's Generic Filter runs BEFORE the M3 API step and only "
              "sees raw row data, so it cannot read those columns - the condition was NOT "
              "applied and the workflow runs for every row. This needs the generic pipeline "
              "path (docs/vince-generic-m3-pipeline.md), whose gates can read an earlier "
              "call's output.")
    if block_bad:
        _warn(warnings,
              "UNREPRESENTABLE BLOCK-LEVEL FILTER: the source gates this whole workflow on " +
              " AND ".join(_describe_filter(rf) for rf in block_bad) + ", but " +
              "; ".join(native_filter_problem(rf.operator, rf.value) for rf in block_bad) +
              ", so that condition is NOT applied. The created workflow will run for rows the "
              "source would have skipped. Handle it manually, or build this workflow on the "
              "generic pipeline path (docs/vince-generic-m3-pipeline.md), whose gates compare "
              "any value.")

    # --- Level 2: the per-transaction gates. ---
    filtered_txns = [(t, id_by_ir_id[t.ir_id]) for t in wf.transactions if t.row_filters]
    shared = filtered_txns[0][0].row_filters if filtered_txns else []
    is_uniform = bool(filtered_txns) and len(filtered_txns) == len(wf.transactions) and all(
        t.row_filters == shared for t, _ in filtered_txns
    )

    if is_uniform and _representable(shared) and not _api_written(shared):
        # Safe, uniform case - every transaction wants the same gate, and
        # every operator in it has a confirmed Live equivalent.
        generic_filter_rowfilters.extend(shared)
    elif filtered_txns:
        # Unsafe case - flag each affected transaction individually.
        for txn, real_id in filtered_txns:
            conditions = " AND ".join(_describe_filter(rf) for rf in txn.row_filters)
            if _api_written(txn.row_filters):
                reason = (
                    "This gate reads column(s) " + _describe_api_columns(txn.row_filters) +
                    " - an M3 call's output, not spreadsheet input. Live's Generic Filter runs "
                    "BEFORE the M3 API step and sees only raw row data, so it can never read "
                    "them, whatever the operator and however the transactions are split"
                )
            elif not _representable(txn.row_filters):
                reason = (
                    "This gate cannot be given to Live's Generic Filter at all (" +
                    "; ".join(p for p in (native_filter_problem(rf.operator, rf.value)
                                          for rf in txn.row_filters) if p) + ")"
                )
            else:
                reason = (
                    "Live DOES have a native Generic Filter step for this, but it applies to "
                    "the WHOLE M3 API step, not a single transaction - and this filter is not "
                    "shared identically by every transaction in this workflow, so auto-"
                    "translating it here would incorrectly also gate transactions that should "
                    "run unconditionally"
                )
            note = (
                "UNSUPPORTED CONDITIONAL LOGIC (RowFilter): this transaction should only "
                "run when " + conditions + ". " + reason + ". This transaction currently runs "
                "UNCONDITIONALLY in Live" +
                (", apart from the workflow-level Generic Filter, which does apply to it"
                 if generic_filter_rowfilters else "") +
                ". Needs manual handling, a converter enhancement that can split transactions "
                "across multiple M3 API steps so each gets its own Generic Filter, or the "
                "generic pipeline path (docs/vince-generic-m3-pipeline.md), which gates each "
                "call independently."
            )
            _add_review_note(step_config_transactions[real_id], note)

    # --- Grouping: reported, never translated. -------------------------
    # Classic's <ImportHeading GroupColumns="B C E O"> groups rows and runs
    # its <StartRun> call once per group. What this path emits runs every
    # transaction once per row. Live DOES document a "Group by" option on the
    # M3 API step (product docs, "Group by" under M3 API), and every captured
    # transaction carries the fields it presumably drives (`hasGrouping` per
    # field, `extendedGrouping` per transaction) - but in every capture they
    # are false/[], so neither their populated JSON nor their runtime
    # semantics (once per group? previous-row break like Classic, or all
    # equal values?) is confirmed. Until one hand-built Group-by workflow is
    # captured and run, there is nothing confirmed to translate into, and the
    # divergence is stated instead of being quietly emitted as a per-row call.
    # The confirmed Classic semantics being diverged from are in
    # ir.Workflow.group_columns.
    group_txns = [t for t in wf.transactions if t.runs_once_per_group]
    if wf.group_columns or group_txns:
        columns = ", ".join(wf.group_columns) or "(none declared)"
        _warn(warnings,
              f"GROUPING NOT REPRESENTED: the source groups rows by column(s) {columns} "
              f"(GroupColumns) and runs {len(group_txns)} call(s) once per group (StartRun). "
              "Live's M3 API step has no confirmed group equivalent (a 'Group by' option is "
              "documented, but its JSON and runtime behaviour are unconfirmed), so every "
              "transaction here runs once per row and the grouping was NOT translated. Confirmed Classic behaviour this diverges "
              "from: the call fires on a group's first row only, writes its output cell to that "
              "row alone with no fill-down, and a per-row filter reading that column therefore "
              "passes on the first row of a group and fails on the rest. The generic pipeline "
              "path (docs/vince-generic-m3-pipeline.md) is where this shape can be expressed.")

    for txn in group_txns:
        reads_only = txn.transaction.startswith(_READ_ONLY_TRANSACTION_PREFIXES)
        if reads_only:
            consequence = (
                f"{txn.program}.{txn.transaction} looks read-only, so each row gets the same "
                "answer Classic got for its group - equivalent, just with more HTTP calls. What "
                "is NOT equivalent: Classic writes this call's output cell only to a group's "
                "FIRST row, so in Live that column carries a value on every row where Classic "
                "left it blank"
            )
        else:
            consequence = (
                f"{txn.program}.{txn.transaction} is NOT a recognised read-only transaction "
                "name, so it may WRITE. If it does, running it per row instead of per group "
                "creates one record PER ROW where Classic created one per group. Do not run "
                "this workflow until that has been checked by hand"
            )
        _add_review_note(step_config_transactions[id_by_ir_id[txn.ir_id]],
                         "RUNS ONCE PER GROUP (StartRun), NOT PER ROW: Classic fires this call "
                         "once per group of rows (grouped by " +
                         (", ".join(wf.group_columns) or "unstated columns") +
                         "); this workflow has no confirmed group equivalent and will run it once per row. " +
                         consequence + ".")

    # Every filter becomes a trigger field keyed "Filter_<column>", so two
    # filters on one column would collide into a single field and silently
    # lose one. Identical duplicates (the same filter appearing at both
    # levels) just collapse; a genuine conflict is reported.
    deduped = []
    for rf in generic_filter_rowfilters:
        if rf in deduped:
            continue
        clash = next((seen for seen in deduped if seen.column == rf.column), None)
        if clash is not None:
            _warn(warnings,
                  "Two different filters both target column " + rf.column + " (" +
                  _describe_filter(clash) + " and " + _describe_filter(rf) +
                  "). Live's Generic Filter keys its fields by column, so only the first is "
                  "applied - the second condition is NOT enforced.")
            continue
        deduped.append(rf)
    generic_filter_rowfilters = deduped

    # A filter Classic HIDES from whoever runs it becomes a CONSTANT
    # condition: the literal in both value and defaultValue, and no trigger
    # field or workflowConfig.inputs entry - exactly the shape of the
    # captured, run-confirmed fixed filter
    # (RUN/captures/generic-filter-constant-and-run.json). A visible one
    # stays an editable CLIENT field defaulting to the source value.
    def _filter_condition(rf) -> dict:
        condition = {
            "clientKey": f"Filter_{rf.column}",
            "operator": REPRESENTABLE_FILTER_OPERATORS[rf.operator],
            "dataType": _filter_data_type(rf),
            "logicalType": "AND",  # OR exists (captured); Classic filters are all AND
            "from": "Row",
            "defaultValue": rf.value,
            "fact": rf.column,
        }
        if rf.is_hidden:
            condition.update(source="CONSTANT", value=rf.value)
        else:
            condition.update(source="CLIENT", value=f"{{{{ header.Filter_{rf.column} }}}}")
        return condition

    editable_filters = [rf for rf in generic_filter_rowfilters if not rf.is_hidden]
    review = []
    if editable_filters:
        review.append(
            "Filter(s) on " + ", ".join(rf.column for rf in editable_filters) + " are EDITABLE "
            "trigger inputs (source=CLIENT) defaulting to the source value, because the source "
            "shows them to whoever runs it - anyone running this workflow in Live can change them.")
    hidden_numeric = [rf.column for rf in generic_filter_rowfilters
                      if rf.is_hidden and _filter_data_type(rf) == "NUMBER"]
    if hidden_numeric:
        review.append(
            "Fixed (CONSTANT) filter(s) on " + ", ".join(hidden_numeric) + " compare as NUMBER - "
            "CONSTANT and NUMBER are each confirmed, but no capture has the two together.")
    if any(_filter_data_type(rf) == "NUMBER" for rf in generic_filter_rowfilters):
        review.append(
            "A NUMBER range is confirmed on numeric cells only; what it does with a blank or "
            "non-numeric cell is untested.")
    review.append("A case-insensitive comparison flag is not applied here.")

    generic_filter_step_config = None
    generic_filter_trigger_fields = []
    if generic_filter_rowfilters:
        generic_filter_step_config = {
            **_nest_filter_conditions([_filter_condition(rf) for rf in generic_filter_rowfilters]),
            "_needs_review": " ".join(review),
        }
        # elementPosition resets to 1 within this group - unconfirmed
        # whether Live expects positions unique per-group or globally
        # across all trigger fields, but per-group ordering is the more
        # natural reading of what "position within this form section"
        # would mean.
        generic_filter_trigger_fields = [
            {
                "fieldId": f"Filter_{rf.column}",
                "fieldName": rf.column,
                "label": rf.column,  # real example used the bare column
                                      # letter, not a nicer label -
                                      # unconfirmed whether one can be
                                      # supplied instead.
                "placeHolder": "Specify  to filter",  # matches the exact
                                                       # (oddly double-
                                                       # spaced) real
                                                       # example for a
                                                       # generic filter
                                                       # field with no
                                                       # description set.
                "dataType": _filter_data_type(rf),
                "length": "15",
                "mandatory": True,
                "type": "text",
                "formGroupId": 2,
                "elementPosition": position,
                "value": rf.value,
            }
            for position, rf in enumerate(editable_filters, start=1)
        ]

    # --- Trigger fields (client-sourced inputs) ---
    # formGroupId and elementPosition are NOT cosmetic extras - a field
    # missing formGroupId has no group to render under (we only declare
    # one group, "fields", with groupId 1, plus "filters"/groupId 2 when
    # Generic Filter fields exist), which is the most likely explanation
    # for a real test workflow showing zero trigger fields despite the
    # fields existing in the underlying data.
    trigger_fields = [
        {
            "fieldId": name,
            "fieldName": name,
            "label": name,
            "placeHolder": name,
            "dataType": info["dataType"] if info["dataType"] != "UNKNOWN" else "STRING",
            "length": info["length"],
            "mandatory": True,
            "type": "text",
            "formGroupId": 1,
            "elementPosition": position,
            "apiId": info["apiId"],
        }
        for position, (name, info) in enumerate(client_field_info.items(), start=1)
    ]
    trigger_fields.extend(generic_filter_trigger_fields)

    trigger_form_groups = [{"groupId": 1, "groupName": "fields"}]
    if editable_filters:
        trigger_form_groups.append({"groupId": 2, "groupName": "filters"})

    workflow_inputs = {name: {"dataType": "STRING", "validation": {"required": True}}
                       for name in client_field_info}
    for rf in editable_filters:
        workflow_inputs[f"Filter_{rf.column}"] = {"dataType": _filter_data_type(rf),
                                                  "validation": {"required": True}}

    m3_api_step_tree = {
        "stepId": step_id, "type": "API", "name": "M3 API",
        "retryAttempts": 0, "target": "workflow-api",
        "childSteps": [
            {
                "stepId": "excel_1", "type": "EXCEL", "name": "Excel",
                "retryAttempts": 0, "target": "workflow-excel",
                "childSteps": [],
            }
        ],
    }
    # Insert generic_filter_1 between trigger_1 and the M3 API step only
    # when we have a safe, uniform set of row filters to represent.
    trigger_child_steps = [m3_api_step_tree]
    if generic_filter_rowfilters:
        trigger_child_steps = [
            {
                "stepId": "generic_filter_1", "type": "GENERIC_FILTER", "name": "Generic Filter",
                "retryAttempts": 0, "target": "workflow-generic-filter",
                "childSteps": [m3_api_step_tree],
            }
        ]

    workflow_config = {
        "name": wf.label,
        "constants": constants,
        "inputs": workflow_inputs,
        "connectionContext": {"M3": connection_id},
        "hasSpreadSheetInput": bool(any(t.cells for t in wf.transactions) or input_column_candidates),
        "hasExcelDesignFile": False,
        "hasTemplateFile": False,
        "workflowTags": {},
        "apiFieldReferences": {},
        "contexts": {},
        "clientConfig": {
            "title": wf.label,
            "description": "Description",
            "buttonText": "Execute",
            "formGroups": trigger_form_groups,
            "fields": trigger_fields,
        },
        "steps": [
            {
                "stepId": "trigger_1", "type": "TRIGGER", "name": "Trigger",
                "target": "workflow-trigger",
                "childSteps": trigger_child_steps,
            }
        ],
    }

    # --- Excel step: build real column definitions from every
    # transaction's own cells. In the Classic-XML case that now means an
    # Export block's cells AND the ImportDetail/StartRun-level ones, which
    # went unparsed until 2026-09-10 - a cell is "this call's output goes
    # to this column" wherever Classic writes it, which is exactly what an
    # excel_1 column entry expresses, so they all land here the same way.
    excel_columns = {}
    for txn in wf.transactions:
        for cell in txn.cells:
            real_id = id_by_ir_id[txn.ir_id]
            meta = field_metadata(txn.program, txn.transaction, cell.field_name, metadata_overrides, "O")
            datatype = "NUMERIC" if cell.data_type == "decimal" else meta["type"]
            field_ref = f"{real_id}.output.{cell.field_name}"
            excel_columns[cell.column] = {
                "fieldId": field_ref,
                "type": "API",
                "stepId": step_id,
                "field": {
                    "fieldId": field_ref,
                    "fieldName": cell.field_name,
                    "type": "API",
                    "datatype": datatype,
                },
                "primaryHeader": {"type": "API", "value": meta["descr"], "fieldId": field_ref},
                "secondaryHeader": {"type": "API", "value": cell.field_name, "fieldId": field_ref},
            }

    # Merge in input-side EXCEL/COLUMN fields. Confirmed via a real
    # diagnostic capture ("Claude test INPUT", extended with a second
    # transaction sharing columns B/C): a column already occupied by an
    # export/output mapping gets a SECOND entry for the input mapping,
    # keyed "<column>-1" - not merged into or replacing the original.
    # The "-1" entry's secondaryHeader is a copy of the plain entry's
    # secondaryHeader in the real example (not derived fresh from the
    # input field) - we mirror that exactly rather than guess at why.
    columns_with_dash1_added = set()
    for column, field_name, real_txn_id, meta in input_column_candidates:
        field_ref = f"{real_txn_id}.input.{field_name}"
        input_entry = {
            "fieldId": field_ref,
            "type": "API",
            "stepId": step_id,
            "field": {
                "fieldId": field_ref,
                "fieldName": field_name,
                "type": "API",
                "datatype": meta["type"],
            },
            "primaryHeader": {"type": "API", "value": meta["descr"], "fieldId": field_ref},
            "secondaryHeader": {"type": "API", "value": field_name, "fieldId": field_ref},
        }
        if column not in excel_columns:
            excel_columns[column] = input_entry
        elif excel_columns[column]["fieldId"] != field_ref and column not in columns_with_dash1_added:
            input_entry["secondaryHeader"] = dict(excel_columns[column]["secondaryHeader"])
            excel_columns[f"{column}-1"] = input_entry
            columns_with_dash1_added.add(column)

    # Live's excel_1 has ONE dataRow and ONE sheetName for the whole grid,
    # while Classic states them separately per direction: OutputStartRow /
    # TargetSheetName on the Export side, InputStartRow / SourceSheetName
    # on <Import>. Read both - an import-only workflow has only the latter,
    # and ignoring it silently produced a workflow that read from row 4
    # when the file said row 7.
    if wf.output_start_row and wf.input_start_row and wf.output_start_row != wf.input_start_row:
        _warn(warnings,
              f"Classic declares different start rows for reading (InputStartRow="
              f"{wf.input_start_row}) and writing (OutputStartRow={wf.output_start_row}). Live's "
              f"Excel step has a single dataRow for both, so {wf.output_start_row} was used and the "
              "input side will be off. This shape cannot be represented faithfully - check the "
              "sheet layout by hand.")
    data_row_declared = wf.output_start_row or wf.input_start_row

    if wf.target_sheet_name and wf.source_sheet_name and wf.target_sheet_name != wf.source_sheet_name:
        _warn(warnings,
              f"Classic reads from sheet {wf.source_sheet_name!r} and writes to "
              f"{wf.target_sheet_name!r}. Live's Excel step names a single sheet, so "
              f"{wf.target_sheet_name!r} was used.")
    sheet_name = wf.target_sheet_name or wf.source_sheet_name or "Sheet1"

    if wf.message_column:
        _warn(warnings,
              f"Classic writes a per-row status message to column {wf.message_column} "
              "(MessageColumn). No equivalent is confirmed in Live's Excel step, so nothing was "
              "generated for it - the converted workflow will not write those messages.")

    if excel_columns:
        used_columns = sorted(c for c in excel_columns.keys() if "-" not in c)
        start_column, end_column = used_columns[0], used_columns[-1]
    else:
        start_column = end_column = "A"

    # The two rows immediately above the data row hold the primary (label)
    # and secondary (field name) headers in every real example captured.
    data_row = data_row_declared or 4
    secondary_header_row = data_row - 1
    primary_header_row = data_row - 2

    if primary_header_row < 1:
        # A Classic file may legitimately start data at row 1 or 2, leaving
        # nowhere for the two header rows Live expects. Clamping keeps the
        # numbers valid; it cannot keep the layout faithful, so say so
        # rather than emitting row 0 or -1 and letting the builder deal
        # with it.
        _warn(warnings,
              f"Data starts at row {data_row}, which leaves no room for the two header rows Live's "
              f"Excel step expects above it. Header rows were clamped to 1 and "
              f"{max(secondary_header_row, 1)}; check the sheet layout by hand.")
        primary_header_row = 1
        secondary_header_row = max(secondary_header_row, 1)

    step_config = {
        step_id: step_config_transactions,
        "trigger_1": {
            "title": wf.label,
            "description": "Description",
            "buttonText": "Execute",
            "triggerType": "CLIENT",
            "formGroups": trigger_form_groups,
            "fields": trigger_fields,
            "tags": {},
        },
        "excel_1": {
            "sourceSheet": {
                "fields": {
                    "sheetName": sheet_name,
                    "startColumn": start_column,
                    "endColumn": end_column,
                    "primaryHeaderRow": primary_header_row,
                    "secondaryHeaderRow": secondary_header_row,
                    "dataRow": data_row,
                    "primaryHeaderStyle": {},
                    "secondaryHeaderStyle": {},
                    "dataRowStyle": {},
                    "columns": excel_columns,
                },
                "cells": {"contents": {}, "styleInfo": {}},
                "sortOrder": [],
            }
        },
    }
    if generic_filter_step_config is not None:
        step_config["generic_filter_1"] = generic_filter_step_config

    return workflow_envelope(
        name=wf.label,
        tenant_id=tenant_id,
        connection_id=connection_id,
        environment_id=environment_id,
        workflow_config=workflow_config,
        step_config=step_config,
    )
