"""
PARSE stage for Classic XML: turns a Classic "VinceExcelConfig" export into
the shared IR (ir.Workflow).

Two things happen here, both Classic-specific and unchanged in substance
from the original single-file converter - only their location has moved:

  1. Structural parsing (_Raw* dataclasses, _parse_m3api, _parse_row_filters)
     - reading the XML shape. Private to this module; nothing outside it
     needs to know Classic uses "<Cell Value='*WHLO'>" or similar.

     <Cell> elements are read at every level Classic puts them: an
     <Export> block's LstApi/GetApi, an <ImportDetail>, and a
     <StartRun>. The last two were unread until 2026-09-10 - see
     _parse_group_heading and the ImportDetail loop for why that mattered
     beyond the missing Excel columns.

     Grouping (<ImportHeading GroupColumns="..."> and its <StartRun>) is
     parsed as of 2026-09-10 too, into Workflow.group_columns plus a
     Transaction flagged runs_once_per_group. Parsed and REPORTED, never
     translated: what build.py emits runs every transaction once per row
     (Live's documented "Group by" is unconfirmed), so it flags it instead.

     Filters are read at BOTH of Classic's two levels - per
     <ImportDetail> (gates one call) and per <Import> block (a sibling of
     the details, gates everything) - and the operator wrapper name is
     read as an open set rather than matched against EQ/NE. Both of those
     were fixes for the same failure mode: a filter the parser had no
     slot for disappeared silently, and the converted workflow then ran
     for rows Classic would have skipped.

  2. Source classification (_classify_source) - resolving Classic's raw
     "*"-prefixed value syntax down to one of the IR's four source kinds
     (CONSTANT / EXCEL_COLUMN / CLIENT / API_CHAIN). This used to run
     inside the old BUILD stage, but it's really Classic-syntax
     interpretation, not "how does Live's JSON need to look" - Live JSON
     and a worded prompt both state their sources explicitly and need no
     such inference. So it moved here: by the time a Transaction leaves
     this module, its inputs are already fully resolved and build.py does
     no source-syntax interpretation of its own.

EVERY <Export> and <Import> block is read, as of 2026-09-10. They are
grouped into UNITS - one per workflow the file becomes - because Live
carries a single sheet, data row and filter gate per workflow while
Classic states those per block, and real files disagree across blocks. See
_parse_raw_units for the grouping rule and the evidence behind it.
`parse_classic_xml_units` is the only entry point and always returns a
list - see its docstring for why there is no single-workflow variant.

Before that, only the FIRST of each was read (root.find(), not findall()),
so a real customer file with a "Check Data" block and a separate "Create
RO" block silently lost 4 of its 6 M3 calls.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from build import native_filter_problem
from ir import Cell, InputField, RowFilter, Transaction, Workflow


# --- Stage 1: structural parse -----------------------------------------

@dataclass
class _RawInputField:
    name: str
    raw_value: str
    label: Optional[str] = None
    source: Optional[str] = None


@dataclass
class _RawCell:
    column: str
    field_name: str
    data_type: Optional[str] = None


@dataclass
class _RawRowFilter:
    operator: str
    column: str
    value: str
    label: Optional[str] = None
    is_hidden: bool = False
    is_lower: bool = False
    include_blank: bool = False


@dataclass
class _RawTransaction:
    program: str
    transaction: str
    inputs: list = field(default_factory=list)
    cells: list = field(default_factory=list)
    based_on_transaction: Optional[str] = None
    based_on_failed: Optional[bool] = None
    row_filters: list = field(default_factory=list)
    max_rows: Optional[int] = None
    runs_once_per_group: bool = False   # came from <ImportHeading><StartRun>
    is_critical: Optional[bool] = None  # <ImportDetail/@IsCritical>, read to be reported
    message_column: Optional[str] = None  # <M3Api/@MessageColumn> - this call's own message column


@dataclass
class _RawWorkflow:
    label: str
    export_transactions: list = field(default_factory=list)
    import_transactions: list = field(default_factory=list)
    target_sheet_name: Optional[str] = None
    output_start_row: Optional[int] = None
    source_sheet_name: Optional[str] = None
    input_start_row: Optional[int] = None
    message_column: Optional[str] = None
    row_filters: list = field(default_factory=list)   # <Import>-level, gates the whole block
    group_columns: list = field(default_factory=list)  # <ImportHeading GroupColumns="B C E O">


def _bool_attr(el, name: str) -> Optional[bool]:
    """A Classic true/false attribute, or None when it isn't there at all."""
    raw = el.get(name)
    if raw is None:
        return None
    return raw.strip().lower() == "true"


def _parse_m3api(m3api_el, cells_el_list=None) -> _RawTransaction:
    txn = _RawTransaction(
        program=m3api_el.get("Program"),
        transaction=m3api_el.get("Transaction"),
        message_column=m3api_el.get("MessageColumn") or None,
    )
    for input_el in m3api_el.findall("InputField"):
        txn.inputs.append(_RawInputField(
            name=input_el.get("Name"),
            raw_value=input_el.get("Value"),
            label=input_el.get("Label"),
            source=input_el.get("Source"),
        ))

    based_on_el = m3api_el.find("BasedOnTransaction")
    if based_on_el is not None:
        txn.based_on_transaction = based_on_el.get("TransactionName")
        txn.based_on_failed = based_on_el.get("TransactionFailed", "").lower() == "true"

    if cells_el_list:
        for cell_el in cells_el_list:
            value = cell_el.get("Value", "")
            # A Cell without "*" writes a CONSTANT (CustomerB: Column="A"
            # Value="1"), not an M3 field. It used to become an output field
            # named "1". An Excel step only writes M3 output, so it is
            # dropped here and reported by peek_classic_xml.
            if not value.startswith("*"):
                continue
            field_name = value.lstrip("*")  # "*WHLO" -> "WHLO"
            txn.cells.append(_RawCell(
                column=cell_el.get("Column"),
                field_name=field_name,
                data_type=cell_el.get("Type"),
            ))
    return txn


FILTER_WRAPPER_PREFIX = "RowFilter"


def _is_filter_wrapper(el) -> bool:
    """
    True for <RowFilterEQ>, <RowFilterNE>, <RowFilterGT>, ... - any wrapper
    naming an operator, but not the bare <RowFilter> child inside one.

    Matched by PREFIX rather than against a fixed EQ/NE list on purpose.
    The list was the fixed one until a real customer file
    (RUN/Existing Price Lists.xml) turned up a third wrapper, GT; treating
    the vocabulary as closed is what made that one vanish silently instead
    of being reported. An operator we can't represent is still an operator
    we must see.
    """
    tag = el.tag
    return isinstance(tag, str) and tag.startswith(FILTER_WRAPPER_PREFIX)         and tag != FILTER_WRAPPER_PREFIX


def _read_filter_wrapper(wrapper_el) -> list:
    """
    The <RowFilter> children of one operator wrapper, as _RawRowFilters.

    Multiple children within one wrapper are treated as AND'd conditions
    (unconfirmed - but both real files seen so far do use two children in
    a single <RowFilterEQ> to mean "both must hold").
    """
    operator = wrapper_el.tag[len(FILTER_WRAPPER_PREFIX):]
    return [
        _RawRowFilter(
            operator=operator,
            column=rf_el.get("Column"),
            value=rf_el.get("Value"),
            label=rf_el.get("Label"),
            is_hidden=(rf_el.get("IsHidden", "").lower() == "true"),
            is_lower=(rf_el.get("IsLower", "").lower() == "true"),
            include_blank=(rf_el.get("IncludeBlank", "").lower() == "true"),
        )
        for rf_el in wrapper_el.findall("RowFilter")
    ]


def _parse_row_filters(detail_el) -> list:
    """
    Every operator wrapper found anywhere inside an <ImportDetail> - the
    PER-CALL filter level, gating just that transaction.

    Searched recursively rather than as direct children, since we haven't
    confirmed whether these sit as a sibling of <M3Api> or nested inside
    it - a recursive search finds them either way without needing to guess
    the exact placement. It stays inside this one detail, so it cannot
    reach the block-level filter that is a sibling of the details.
    """
    filters = []
    for el in detail_el.iter():
        if _is_filter_wrapper(el):
            filters.extend(_read_filter_wrapper(el))
    return filters


def _parse_block_row_filters(import_el) -> list:
    """
    The <Import>-level filter level: operator wrappers that are DIRECT
    children of <Import>, siblings of the <ImportDetail> elements, gating
    the whole block.

    Classic has these two levels and they mean genuinely different things.
    Only the per-detail one was ever read here, so a block-level filter was
    dropped without a word - and a converted workflow then ran for rows
    Classic would have skipped. Real example: "Existing Price Lists" gates
    its entire "Add Price List" block on BH == "OK".

    Direct children ONLY, deliberately: a recursive search from <Import>
    would also sweep up every per-detail filter and re-report each one as
    if it gated everything.
    """
    filters = []
    for el in list(import_el):
        if _is_filter_wrapper(el):
            filters.extend(_read_filter_wrapper(el))
    return filters


def _parse_group_heading(import_el) -> tuple:
    """
    An `<Import>`'s `<ImportHeading GroupColumns="B C E O">` and the
    `<StartRun>` call inside it, as (group_columns, start_run_txns).

    The StartRun transactions come back flagged `runs_once_per_group`, in
    document order, to be prepended to the block's ImportDetail calls -
    Classic puts `<ImportHeading>` before the details, and later details
    reference the StartRun call BY NAME through `<BasedOnTransaction>`
    (both real files do this), so it has to be in place before `_to_ir`
    walks the list building `seen_so_far`.

    A StartRun's `<Cell>` is read exactly like an ImportDetail's - a
    sibling of its `<M3Api>`, which is where both real files put it. It
    need NOT have one: "Existing Price Lists"'s `MNS150MI.SelUsers` has no
    Cell at all and reaches later calls only through success/failure.

    `GroupColumns` is space-separated in both real files ("B C E O");
    split on ANY whitespace, since nothing confirms the separator is
    exactly one space. Only ONE `<ImportHeading>` per block has ever been
    observed, so this reads one - `peek_classic_xml` counts them and
    reports a file carrying more, rather than this dropping the extras in
    silence.
    """
    heading_el = import_el.find("ImportHeading")
    if heading_el is None:
        return [], []

    group_columns = (heading_el.get("GroupColumns") or "").split()

    start_run_txns = []
    for start_run_el in heading_el.findall("StartRun"):
        m3api_el = start_run_el.find("M3Api")
        if m3api_el is None:
            continue
        txn = _parse_m3api(m3api_el, start_run_el.findall("Cell"))
        txn.runs_once_per_group = True
        # <ImportHeading> carries IsCritical, not the <StartRun> inside it.
        txn.is_critical = _bool_attr(heading_el, "IsCritical")
        start_run_txns.append(txn)

    return group_columns, start_run_txns


ENRICHMENT_TAGS = ("GetApi", "LookupApi")

# Top-level blocks the parser converts. Anything else at the top level is
# NOT converted and is reported by peek_classic_xml - above all <Expand>
# (read one sheet, write another), which Live is treated as not having: a
# workflow reads and writes only the sheet it runs on (product manager,
# 2026-10-16). It has to be rebuilt by hand as a Code step on its target
# sheet that fetches its own source data; see converter/expand_code.py.
CONVERTED_BLOCK_TAGS = ("Export", "Import")
EXPAND_TAG = "Expand"


def _sheets_pair(export_el, import_el) -> bool:
    """
    May the first <Export> and first <Import> share one workflow? Only when
    they work on the same sheet. Both are paired because an Export usually
    writes the rows its Import then reads back - but CustomerB's "Sales Price
    List" has its first Export on "OIS017 (List header)" and its first
    Import on "OIS021 (Lines)", and pairing those made one workflow out of
    two sheets. A block that names no sheet keeps the old pairing: there is
    nothing to say it differs.
    """
    target = (export_el.get("TargetSheetName") or "").strip()
    source = (import_el.get("SourceSheetName") or "").strip()
    return not target or not source or target == source


def _block_units(root) -> list:
    """
    The converted top-level blocks of a Classic file, grouped into units
    (lists of elements), one workflow each, in document order. The ONE
    place that decides the grouping, so the read-only scan and the real
    parse can never disagree on how many workflows a file becomes.
    """
    exports = root.findall("Export")
    imports = root.findall("Import")
    first_export = exports[0] if exports else None
    first_import = imports[0] if imports else None
    paired = (first_export is not None and first_import is not None
              and _sheets_pair(first_export, first_import))

    units = []
    if paired:
        units.append([first_export, first_import])
    for el in list(root):
        if el.tag not in CONVERTED_BLOCK_TAGS:
            continue
        if paired and (el is first_export or el is first_import):
            continue
        units.append([el])
    return units


def _parse_export_block(export_el, wf: _RawWorkflow) -> None:
    """One <Export> block: LstApi (primary) + GetApi (chained enrichment)."""
    wf.target_sheet_name = export_el.get("TargetSheetName")

    lst_api_el = export_el.find("LstApi")
    if lst_api_el is not None:
        m3api_el = lst_api_el.find("M3Api")
        cells = lst_api_el.findall("Cell")
        txn = _parse_m3api(m3api_el, cells)
        max_rows_attr = lst_api_el.get("MaxRows")
        if max_rows_attr is not None:
            txn.max_rows = int(max_rows_attr)
        wf.export_transactions.append(txn)

        output_start_row_attr = lst_api_el.get("OutputStartRow")
        if output_start_row_attr is not None:
            wf.output_start_row = int(output_start_row_attr)

    # EVERY enrichment call, in document order. Until 2026-10-16 only the
    # first <GetApi> was read: CustomerB's "Export basic prices" has three
    # (GetItmBasic, GetAlias, MMS015MI.Get) and silently lost the last two,
    # columns K and L with them. <LookupApi> is the same shape (seen inside
    # <Expand> blocks), so it is read here too should an Export carry one.
    for child in export_el:
        if child.tag not in ENRICHMENT_TAGS:
            continue
        m3api_el = child.find("M3Api")
        if m3api_el is None:
            continue
        wf.export_transactions.append(_parse_m3api(m3api_el, child.findall("Cell")))


def _parse_import_block(import_el, wf: _RawWorkflow) -> None:
    """One <Import> block: its sheet settings, gates, grouping and details."""
    # These live on <Import> itself, not on the ImportDetail children.
    # Missing them meant an import-only workflow silently fell back to
    # build.py's dataRow default of 4, so a file declaring
    # InputStartRow="7" produced a workflow reading from the wrong row.
    wf.source_sheet_name = import_el.get("SourceSheetName")
    input_start_row_attr = import_el.get("InputStartRow")
    if input_start_row_attr is not None:
        wf.input_start_row = int(input_start_row_attr)
    wf.message_column = import_el.get("MessageColumn")

    wf.row_filters = _parse_block_row_filters(import_el)

    # Before the details, matching document order - see
    # _parse_group_heading for why that ordering is load-bearing.
    wf.group_columns, start_run_txns = _parse_group_heading(import_el)
    wf.import_transactions.extend(start_run_txns)

    for detail_el in import_el.findall("ImportDetail"):
        m3api_el = detail_el.find("M3Api")
        # An ImportDetail's own <Cell>s: this call's OUTPUT written to a
        # spreadsheet column, structurally identical to an Export
        # block's cells. They went unread until 2026-09-10, which cost
        # more than the missing Excel columns: a RowFilter names a
        # COLUMN, and the only way to know which earlier call produces
        # that column is the Cell that writes it, so the whole
        # mixed-filter case was unresolvable without them.
        txn = _parse_m3api(m3api_el, detail_el.findall("Cell"))
        txn.row_filters = _parse_row_filters(detail_el)
        # Read purely so it can be reported - see ir.Transaction.is_critical
        # for why it is deliberately not translated into behaviour.
        txn.is_critical = _bool_attr(detail_el, "IsCritical")
        wf.import_transactions.append(txn)


def _parse_raw_units(path: str) -> list:
    """
    Every <Export>/<Import> block in the file, grouped into UNITS - one
    per workflow the file should become.

    Why a file can't always be one workflow. Live's excel_1 carries a
    single sheetName and a single dataRow, and a single generic_filter_1
    gates the whole thing. Classic states those PER BLOCK, and real files
    disagree across blocks: C05_MMS100's two <Import>s declare
    InputStartRow 3 and 4 and MessageColumn AA and AF, and "Existing Price
    Lists" gates its "Add Price List" block on BH == "OK" while its
    "Update Price List" block has no gate at all. One Live workflow cannot
    express either pair, so a multi-block file becomes several workflows -
    which is also what the scan's warning has always told people to do by
    hand ("split the extra block(s) into their own XML files").

    The grouping rule, and the one arbitrary-looking part of it: the FIRST
    <Export> and the FIRST <Import> stay together in unit 1, because that
    pairing is the shape every confirmed conversion so far was built and
    validated on - an Export whose cells feed the Import's inputs through
    _classify_source's column redirect. Preserving it means a file with at
    most one of each converts byte-identically to before. Every remaining
    block becomes its own unit, in document order. Since 2026-10-16 the
    pair is only kept when both blocks work on the same sheet
    (_sheets_pair); otherwise every block is its own unit. <Expand> and
    any other unknown block is not converted at all - peek_classic_xml
    reports it.

    An extra <Import> parsed alone has no export_transactions, so a
    "*<letter>" input in it resolves to EXCEL_COLUMN rather than
    API_CHAIN. That is the correct reading, not a degradation: the earlier
    block already wrote that column to the sheet in its own run, so at
    this point the value genuinely does come from the spreadsheet.
    """
    root = ET.parse(path).getroot()

    units = []
    # A file's block order is the order a person runs them, so the unpaired
    # blocks keep it rather than exports-first.
    for blocks in _block_units(root):
        label = next((el.get("Label") for el in blocks if el.get("Label")), None)
        wf = _RawWorkflow(label=label or "Untitled")
        for el in blocks:
            if el.tag == "Export":
                _parse_export_block(el, wf)
            else:
                _parse_import_block(el, wf)
        units.append(wf)

    return units or [_RawWorkflow(label="Untitled")]


# --- Stage 2: classify raw values, assign IR identity -------------------

def _classify_source(raw_value: str, explicit_source: Optional[str], field_name: str,
                      id_by_txn_key: dict, export_column_to_field: dict) -> dict:
    """
    Work out a resolved IR source for a Classic *Value string. Returns a
    dict suitable for **-expanding into InputField(name=..., label=..., **this).

    Classic patterns handled - logic unchanged from the original converter,
    only the output vocabulary changed (IR source_kind instead of Live's
    "source"/"sourceType"):
      "1"                              -> no leading '*', no explicit Source -> CONSTANT
      (Source="Constant" explicitly)   -> CONSTANT
      "*A", "*B", ...                  -> column ref, redirected to
                                           API_CHAIN when that column is
                                           ALSO the Export block's own
                                           output for the SAME field name
                                           (the same value flowing from an
                                           earlier API call via the
                                           spreadsheet, not a genuine
                                           Excel-sourced input)
      "*Field1", "*Field2", "*Field3"  -> CLIENT trigger input, using the
                                           REAL M3 field name rather than
                                           Classic's placeholder - confirmed
                                           via a real execution error (see
                                           ir.py's module docstring)
      "*PROGRAM.Transaction.FIELD"     -> API_CHAIN, resolved against
                                           whichever occurrence of that
                                           transaction name appeared most
                                           recently earlier in the document
    """
    if explicit_source and explicit_source.lower() == "constant":
        return {"source_kind": "CONSTANT", "value": raw_value}

    # Classic's Source="UserID" means "the M3 user id of whoever is running
    # this", filled in automatically; its Value is the literal "*USERID",
    # not a column. Nothing in Live is confirmed to supply that, so it
    # degrades to a CLIENT trigger field - the closest confirmed mechanism,
    # an editable input - and says so.
    #
    # Without this branch it fell through to the "*<letter>" case below and
    # was read as a spreadsheet column literally named "USERID", which then
    # reached excel_1 as a bogus "USERID" column key. That stayed invisible
    # only because the sole real examples sit inside a <StartRun>, which
    # went unparsed until 2026-09-10.
    if explicit_source and explicit_source.lower() == "userid":
        return {"source_kind": "CLIENT", "value": field_name,
                "needs_review": (
                    "Classic filled this automatically from the running user's M3 user id "
                    '(Source="UserID"). No confirmed Live equivalent exists, so it was created '
                    "as an editable trigger field instead - whoever runs the workflow has to "
                    "supply the user id by hand."
                )}

    if explicit_source:
        # Any OTHER explicit Source. Classic uses the attribute precisely to
        # mark values that are not spreadsheet columns (Constant, UserID are
        # the two seen), so falling through would invent a column out of
        # whatever Value happens to say - the same failure as USERID above.
        return {"source_kind": "CLIENT", "value": field_name,
                "needs_review": (
                    f'Classic sets Source="{explicit_source}" on this field, which this '
                    "converter does not recognise (only Constant and UserID are known). It was "
                    "created as an editable trigger field; check what that source is meant to "
                    "supply before running the workflow."
                )}

    if not raw_value.startswith("*"):
        return {"source_kind": "CONSTANT", "value": raw_value}

    ref = raw_value[1:]

    if ref.startswith("Field"):
        return {"source_kind": "CLIENT", "value": field_name}

    if "." in ref:
        program, transaction, fname = ref.split(".")
        producer_ir_id = id_by_txn_key.get((program, transaction))
        if producer_ir_id is None:
            return {"source_kind": "API_CHAIN", "value": ref,
                    "needs_review": f"Could not resolve producing transaction for {ref}"}
        return {"source_kind": "API_CHAIN", "ref": {"producer_ir_id": producer_ir_id, "name": fname}}

    if ref in export_column_to_field:
        producer_ir_id, exported_field_name = export_column_to_field[ref]
        if exported_field_name == field_name:
            return {"source_kind": "API_CHAIN",
                     "ref": {"producer_ir_id": producer_ir_id, "name": exported_field_name}}

    return {"source_kind": "EXCEL_COLUMN", "value": ref}


def _to_ir_row_filters(raw_filters: list) -> list:
    return [RowFilter(operator=rf.operator, column=rf.column, value=rf.value,
                      label=rf.label, is_hidden=rf.is_hidden, is_lower=rf.is_lower,
                      include_blank=rf.include_blank)
            for rf in raw_filters]


def _to_ir(raw: _RawWorkflow) -> Workflow:
    all_raw_txns = raw.export_transactions + raw.import_transactions
    ir_ids = [f"c{i}" for i in range(1, len(all_raw_txns) + 1)]
    # An <Export> call is not driven by an input row - it RETURNS rows. The
    # M3-API path doesn't care, the pipeline path does; see
    # ir.Transaction.per_input_row.
    export_count = len(raw.export_transactions)

    # Column letter -> (producing export transaction's ir_id, field name),
    # for the *<letter> -> API_CHAIN redirect. Export transactions are
    # always first in all_raw_txns, matching Classic's document order.
    export_ir_ids = ir_ids[: len(raw.export_transactions)]
    export_column_to_field = {}
    for txn, ir_id in zip(raw.export_transactions, export_ir_ids):
        for cell in txn.cells:
            export_column_to_field[cell.column] = (ir_id, cell.field_name)

    # References in Classic XML (chained API values and
    # BasedOnTransaction) are written by transaction *name*, not by a
    # unique ID. When a name is reused, Classic's own top-to-bottom
    # document order disambiguates which occurrence is meant - a
    # reference always means "the nearest matching transaction defined
    # earlier in the file". Built incrementally while walking in order,
    # so it can never resolve a transaction as its own dependency.
    seen_so_far = {}
    transactions = []

    for position, (raw_txn, ir_id) in enumerate(zip(all_raw_txns, ir_ids)):
        inputs = []
        for raw_input in raw_txn.inputs:
            classification = _classify_source(
                raw_input.raw_value, raw_input.source, raw_input.name,
                seen_so_far, export_column_to_field,
            )
            inputs.append(InputField(name=raw_input.name, label=raw_input.label, **classification))

        cells = [Cell(column=c.column, field_name=c.field_name, data_type=c.data_type)
                 for c in raw_txn.cells]
        row_filters = _to_ir_row_filters(raw_txn.row_filters)

        based_on = None
        needs_review = None
        if raw_txn.based_on_transaction:
            ref_program, ref_transaction = raw_txn.based_on_transaction.split(".")
            based_on = seen_so_far.get((ref_program, ref_transaction))
            if based_on is None:
                needs_review = f"Could not resolve dependency target {raw_txn.based_on_transaction}"

        transactions.append(Transaction(
            ir_id=ir_id,
            program=raw_txn.program,
            transaction=raw_txn.transaction,
            inputs=inputs,
            cells=cells,
            based_on=based_on,
            based_on_failed=raw_txn.based_on_failed,
            row_filters=row_filters,
            max_rows=raw_txn.max_rows,
            needs_review=needs_review,
            runs_once_per_group=raw_txn.runs_once_per_group,
            per_input_row=(position >= export_count),
            is_critical=raw_txn.is_critical,
            message_column=raw_txn.message_column,
        ))

        # Recorded AFTER processing, so later occurrences of the same
        # name correctly shadow this one for anything that comes after.
        seen_so_far[(raw_txn.program, raw_txn.transaction)] = ir_id

    return Workflow(
        label=raw.label,
        transactions=transactions,
        target_sheet_name=raw.target_sheet_name,
        output_start_row=raw.output_start_row,
        source_sheet_name=raw.source_sheet_name,
        input_start_row=raw.input_start_row,
        message_column=raw.message_column,
        row_filters=_to_ir_row_filters(raw.row_filters),
        group_columns=list(raw.group_columns),
    )


def parse_classic_xml_units(path: str) -> list:
    """
    Every workflow a Classic file should become, in document order - see
    _parse_raw_units for how blocks are grouped and why a multi-block file
    cannot be one workflow.

    Always a list, never a bare Workflow, even for the single-block file
    that is the common case: the earlier one-workflow-per-file signature
    is exactly what let three separate callers each silently drop whole
    blocks, so there is deliberately no way to ask for "the workflow" any
    more. A caller that can only handle one takes [0] and says so.
    """
    return [_to_ir(raw) for raw in _parse_raw_units(path)]


def compose_workflow_name(base_name: str, function_label: str) -> str:
    """
    "<base name> - <function>" - the user picks the base name (suggested
    from the source file, theirs to override), every block/unit still
    appends its own function label so siblings from one file stay
    distinguishable. Falls back gracefully when either half is missing or
    the two are identical (a single-block file where the block's own label
    already matches the base name shouldn't repeat itself).
    """
    base_name = (base_name or "").strip()
    function_label = (function_label or "").strip()
    if not base_name:
        return function_label or "Untitled"
    if not function_label or function_label == base_name:
        return base_name
    return f"{base_name} - {function_label}"


def unrepresentable_filter_operators(workflow: Workflow) -> list:
    """
    Every RowFilter operator on this unit (block-level or per-transaction)
    used by at least one filter the NATIVE path cannot express - decided per
    FILTER by `build.native_filter_problem`, not per operator, because since
    2026-09-28 a range operator is native when its value is numeric (a
    NUMBER comparison, confirmed by a run) and not when it isn't. So "GE"
    here means "some GE filter in this block has a non-numeric value" (or
    an operator with no captured value at all).

    Lives here, not in app_classic.py, so `peek_classic_xml` can predict a
    block's routing at SCAN time (before any target ids exist) with the
    exact same check app_classic.convert_classic_xml uses to actually
    build it - two independently-drifting copies of "does this block need
    the pipeline path" is exactly the kind of thing that quietly goes
    stale. app_classic.py imports this rather than defining its own.

    This is the "narrow" auto-routing trigger, added 2026-09-16 after a
    real CustomerA customer file set turned up hundreds of GE/LE/LT filters:
    route a block onto the generic-pipeline path ONLY when it needs an
    operator native genuinely cannot do. Deliberately does NOT also trigger
    on the other existing native gaps (non-uniform per-transaction filters,
    a filter on an M3-written column, grouping) - those stay exactly as
    reported/`_needs_review` as before. Widening this is a separate,
    later decision, not a side effect of this one.
    """
    filters = list(workflow.row_filters)
    for txn in workflow.transactions:
        filters.extend(txn.row_filters)
    return sorted({rf.operator for rf in filters if native_filter_problem(rf.operator, rf.value)})


# --- Recognising a Classic XML file without converting it ----------------

CLASSIC_ROOT_TAG = "VinceExcelConfig"


def _classic_root(text: str):
    """
    The root element of `text` if it is a Classic export, else None.

    The BOM strip is load-bearing: real Classic exports are UTF-8 *with* a
    BOM, and while ET.parse(path) reads bytes and copes, ET.fromstring on a
    str that starts with \ufeff raises "XML or text declaration not at start
    of entity". The scan already has the file as text, so it lands here.
    """
    if not text:
        return None
    stripped = text.lstrip("\ufeff").lstrip()
    if not stripped.startswith("<"):
        return None
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError:
        return None
    if root.tag == CLASSIC_ROOT_TAG:
        return root
    # Tolerate a differently-named root as long as the shape is
    # unmistakably Classic: an Export/Import block containing M3 calls.
    # This is a shape check on a file we already own the parser for, not an
    # inference about anything on Live's side.
    if (root.find("Export") is not None or root.find("Import") is not None) \
            and root.find(".//M3Api") is not None:
        return root
    return None


def looks_like_classic_xml(text: str) -> bool:
    return _classic_root(text) is not None


def peek_classic_xml(text: str, path: Optional[str] = None) -> Optional[dict]:
    """
    Identify a Classic XML without converting it: its label, how many M3
    calls it holds, and how many workflows it will become.

    Returns None when the text isn't a Classic export. Used by the app-folder
    scan, which must stay read-only and target-free - conversion needs
    connection ids and a token, and happens later in app_classic.py.

    The block count matters more than it looks. Until 2026-09-10 only the
    first <Export>/<Import> was read at all and the rest vanished, which on
    a one-off conversion you would probably notice but in a folder of 80
    files being migrated for a customer is exactly the kind of thing that
    reaches production unnoticed. Every block is read now, so what this
    reports is no longer a loss but a FAN-OUT: how many separate workflows
    one file is about to create, said out loud before anything is created.

    `path`, when given, buys ONE more thing this text-only scan cannot:
    which specific block(s) will auto-route onto the generic-pipeline path
    (added 2026-09-16 - see `unrepresentable_filter_operators`), predicted
    with a REAL parse (`parse_classic_xml_units`) rather than a re-derived
    approximation, so it can never disagree with what conversion actually
    does. Still read-only and target-free - routing needs no connection id,
    no token, nothing tenant-specific, only the file itself. Optional and
    best-effort: the caller may only have text (a paste, a fixture), and a
    parse failure here is swallowed rather than breaking the scan - real
    parse errors still surface loudly later, at conversion time.
    """
    root = _classic_root(text)
    if root is None:
        return None

    exports = root.findall("Export")
    imports = root.findall("Import")
    first_export = exports[0] if exports else None
    first_import = imports[0] if imports else None

    label = (first_export.get("Label") if first_export is not None
             else first_import.get("Label") if first_import is not None
             else "Untitled")

    total_calls = len(root.findall(".//M3Api"))
    # Only calls in a block that converts. Until 2026-10-16 this was simply
    # total_calls, which on CustomerB's files claimed 17 of 17 calls read while
    # the three <Expand> blocks (and two GetApis) vanished.
    read_calls = sum(len(block.findall(".//M3Api"))
                     for block in root if block.tag in CONVERTED_BLOCK_TAGS)
    units = _block_units(root)
    block_count = len(units)

    warnings = []

    # Blocks that are NOT converted, said before anything is created.
    expands = [el for el in root if el.tag == EXPAND_TAG]
    if expands:
        described = "; ".join(
            repr(el.get("Label") or "unlabelled") + " (reads sheet " +
            repr(el.get("SourceSheetName") or "?") + ", writes sheet " +
            repr(el.get("TargetSheetName") or "?") + ", " +
            str(len(el.findall(".//M3Api"))) + " M3 call(s))"
            for el in expands)
        warnings.append(
            str(len(expands)) + " <Expand> block(s) NOT CONVERTED: " + described + ". Live has "
            "no Expand - a workflow reads and writes only the sheet it runs on - so each has to be "
            "rebuilt as a Code step on its TARGET sheet that fetches its own source data from M3 "
            "(converter/expand_code.py, `cli.py pipeline` with an m3Expand step).")
    unknown = sorted({el.tag for el in root
                      if isinstance(el.tag, str) and el.tag not in CONVERTED_BLOCK_TAGS
                      and el.tag != EXPAND_TAG and el.findall(".//M3Api")})
    if unknown:
        warnings.append(
            "Block type(s) " + ", ".join("<" + t + ">" for t in unknown) + " are not understood "
            "and NOT CONVERTED - their M3 calls are not in any workflow. Check this file by hand.")
    constant_cells = [(cell.get("Column"), cell.get("Value", ""))
                      for block in exports + imports for cell in block.iter("Cell")
                      if not cell.get("Value", "").startswith("*")]
    if constant_cells:
        warnings.append(
            "Constant cell(s) NOT WRITTEN: " + ", ".join(
                (column or "?") + "=" + repr(value) for column, value in constant_cells) +
            ". Classic writes a fixed value there; an Excel step only writes M3 output, so put "
            "the value in the design file instead.")
    if exports and imports and not _sheets_pair(exports[0], imports[0]):
        warnings.append(
            "The first <Export> (" + repr(exports[0].get("Label") or "unlabelled") + ", sheet " +
            repr(exports[0].get("TargetSheetName")) + ") and the first <Import> (" +
            repr(imports[0].get("Label") or "unlabelled") + ", sheet " +
            repr(imports[0].get("SourceSheetName")) + ") work on different sheets, so they "
            "become separate workflows rather than one.")
    if block_count > 1:
        warnings.append(
            "This file has " + str(len(exports)) + " <Export> and " + str(len(imports)) +
            " <Import> block(s), so it converts to " + str(block_count) + " SEPARATE workflows, "
            "one per block (Live carries a single sheet, data row and filter gate per workflow, "
            "while Classic states those per block). All " + str(block_count) + " are created, "
            "each named after its own block Label - check that is what you want."
        )
    if read_calls == 0:
        warnings.append("No <M3Api> calls found in a convertible block - conversion will produce "
                        "an empty workflow.")

    # Filter operators Live's Generic Filter has no confirmed equivalent
    # for. Reported HERE, from the read-only scan, and not only from
    # build.py's warnings, so they show in the app-import plan and the
    # confirmation box - i.e. before anything reaches the tenant. At one
    # file you would read the conversion warnings anyway; at eighty, a
    # workflow that quietly lost its gate and now runs for rows the source
    # skipped is exactly what gets missed.
    # Same per-filter rule as the build (build.native_filter_problem), read
    # straight off the XML attributes.
    unrepresentable = sorted({
        rf.operator
        for block in exports + imports
        for el in block.iter() if _is_filter_wrapper(el)
        for rf in _read_filter_wrapper(el)
        if native_filter_problem(rf.operator, rf.value)
    })
    # Grouping, reported from the scan for the same reason unrepresentable
    # operators are: a grouped file converts into a workflow that runs the
    # StartRun call once per ROW instead of once per group, which is a
    # behavioural divergence (and a dangerous one if that call writes), not
    # a cosmetic one. build.py warns too, but that is after conversion -
    # this shows in the app-import plan and the confirmation box first.
    headings = [el for block in exports + imports
                for el in block.iter() if el.tag == "ImportHeading"]
    if headings:
        # Deduped, order preserved: every block in a file tends to declare
        # the SAME group columns (both <Import>s of "Existing Price Lists"
        # say "B C D E"), and repeating them once per block read as
        # "B, C, D, E, B, C, D, E".
        group_columns = list(dict.fromkeys(
            " ".join(h.get("GroupColumns", "") for h in headings).split()))
        start_run_calls = sum(len(h.findall("StartRun/M3Api")) for h in headings)
        warnings.append(
            "This file groups rows by column(s) " +
            (", ".join(group_columns) or "(none declared)") + " and runs " +
            str(start_run_calls) + " call(s) once per group (ImportHeading/StartRun). Live's M3 "
            "API step has no confirmed group equivalent, so those call(s) will run once per ROW instead - the "
            "same answer for a read, but one record per row rather than per group if any of them "
            "WRITES. Check them before running the result. (A file-level note: which block the "
            "grouping is in is named in that workflow's own conversion warnings.)"
        )
    if len(headings) > max(block_count, 1):
        warnings.append(
            "This file declares " + str(len(headings)) + " <ImportHeading> elements across " +
            str(block_count) + " block(s); the parser reads one per block. Only one per block "
            "has ever been observed - check this file by hand."
        )

    if unrepresentable:
        warnings.append(
            "Uses filter operator(s) " + ", ".join(unrepresentable) + " in a form Live's Generic "
            "Filter step cannot be given natively (a range is only emitted as a NUMBER comparison, "
            "so it needs a numeric value; a text range is not confirmed by a run). "
            "Those conditions will NOT be applied, so the created workflow runs for rows the "
            "source would have skipped. The affected transactions are flagged individually in "
            "the converted JSON; the generic pipeline path "
            "(docs/vince-generic-m3-pipeline.md) can express them."
        )

    # Per-block routing PREDICTION - see the docstring's `path` paragraph.
    # A real parse, not a re-derived guess, so this can never disagree with
    # what app_classic.convert_classic_xml actually decides.
    routed_blocks = []
    if path is not None and unrepresentable:
        try:
            units = parse_classic_xml_units(path)
        except Exception:
            units = []   # best-effort - a real parse failure surfaces loudly at conversion time
        for unit in units:
            ops = unrepresentable_filter_operators(unit)
            if ops:
                routed_blocks.append({"label": unit.label, "operators": ops})
        if routed_blocks:
            named = "; ".join(
                repr(b["label"] or "unlabelled") + " (" + ", ".join(b["operators"]) + ")"
                for b in routed_blocks
            )
            warnings.append(
                str(len(routed_blocks)) + " of " + str(max(block_count, 1)) + " block(s) will "
                "auto-route onto the generic-pipeline path (needs the target's ION API root - "
                "--ion-api-base-url / the web UI's ION API root field): " + named + ". Blocks not "
                "listed stay on the native path as usual."
            )

    return {
        "label": label,
        "transactionCount": read_calls,
        "totalTransactionCount": total_calls,
        "exportBlocks": len(exports),
        "importBlocks": len(imports),
        "blockCount": block_count,
        "pipelineRoutedBlocks": routed_blocks,
        "warnings": warnings,
    }
