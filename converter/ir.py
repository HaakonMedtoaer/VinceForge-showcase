"""
Intermediate representation (IR) shared by all input sources.

These are the generalized versions of the Classic* dataclasses from the
original single-file converter - same shapes, same meanings, no longer
tied to Classic's vocabulary so Live JSON and (later) worded prompts can
populate them too.

One deliberate departure from the original: InputField.source_kind is
already RESOLVED by the time a Transaction reaches build.py. The original
script's _classify_source() interpreted Classic's raw "*"-prefixed string
syntax ("*A", "*Field1", "*PROGRAM.Transaction.FIELD") inside the BUILD
stage. That interpretation is Classic-specific - Live JSON already states
its source/ref explicitly, and a worded prompt states dependencies
explicitly too - so it now lives in source_classic_xml.py instead. build.py
never looks at raw source syntax; it only ever sees one of the four kinds
below.

The four source kinds reuse Live's own vocabulary on purpose: Live JSON
parsing is then a direct passthrough with no inference needed, and it's
the natural shared vocabulary regardless of which source is producing it.

    CONSTANT      - a fixed literal value baked into the workflow
    EXCEL_COLUMN  - read from a spreadsheet column at run time
    CLIENT        - a user-supplied trigger field, filled in when the
                    workflow is run
    API_CHAIN     - the output of an earlier transaction in the same
                    workflow

Cross-transaction references (based_on, API_CHAIN) point at another
Transaction via ir_id - an identity assigned by whichever source parser
built the IR, NOT Live's real per-workflow instance id (build.py assigns
those, once, right before emitting Live JSON). For a Transaction parsed
FROM Live JSON, ir_id is simply Live's own real instance id reused as-is.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InputField:
    name: str                      # M3 field name, e.g. "WHLO"
    source_kind: str               # CONSTANT | EXCEL_COLUMN | CLIENT | API_CHAIN
    value: Optional[str] = None    # CONSTANT: literal value
                                    # EXCEL_COLUMN: column letter
                                    # CLIENT: canonical field name
                                    # API_CHAIN: unused (see ref)
    label: Optional[str] = None
    ref: Optional[dict] = None     # API_CHAIN only: {"producer_ir_id": ..., "name": ...}
    needs_review: Optional[str] = None  # source parser flags something it couldn't resolve


@dataclass
class Cell:
    """An Excel column mapping, e.g. a transaction's output field written to column A."""
    column: str
    field_name: str
    data_type: Optional[str] = None


@dataclass
class RowFilter:
    """
    One conditional-execution gate, e.g. Classic's RowFilterEQ/RowFilterNE.

    `operator` is an OPEN set, not an enum. Classic wraps its filters in
    per-operator elements (<RowFilterEQ>, <RowFilterNE>, <RowFilterGT>, and
    presumably others we have not met), and a real customer file turned up
    GT after EQ/NE had been treated as the whole vocabulary. So the parser
    records whatever wrapper it finds and lets the BUILD stage decide what
    it can represent - an unknown operator must be reported, never dropped
    and never coerced into a known one.

    What the native path can be given is decided per filter by
    build.native_filter_problem: EQ/NE always, GT/GE/LT/LE when the value is
    numeric (a NUMBER comparison). is_hidden decides CLIENT (editable) vs
    CONSTANT (fixed).
    """
    operator: str        # "EQ" | "NE" | "GT" | any other Classic wrapper suffix
    column: str
    value: str
    label: Optional[str] = None
    is_hidden: bool = False
    is_lower: bool = False
    # Classic's RowFilter/@IncludeBlank. Captured, never acted on: the one
    # real example sets it on a GT whose Value is empty, which reads
    # self-contradictory ("greater than nothing, but also include blanks").
    # Its meaning is unconfirmed - do not guess it.
    include_blank: bool = False


@dataclass
class Transaction:
    """One program/transaction call."""
    ir_id: str
    program: str
    transaction: str
    inputs: list[InputField] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)   # output fields written to Excel columns

    based_on: Optional[str] = None          # ir_id of another Transaction in the same Workflow
    based_on_failed: Optional[bool] = None

    row_filters: list[RowFilter] = field(default_factory=list)
    max_rows: Optional[int] = None
    needs_review: Optional[str] = None  # source parser flags something it couldn't resolve

    # False for a call that is NOT driven by an input row but RETURNS rows
    # - Classic's <Export> LstApi/GetApi, which queries M3 and writes the
    # answer into the spreadsheet. Everything else runs once per input row.
    #
    # build.py does not need the distinction (Live's M3 API step runs every
    # transaction the same way and excel_1 maps both directions), but the
    # generic-pipeline translator does: a row pipeline iterates input rows,
    # so a call with no input row to iterate has no place in one. Defaulting
    # to True keeps every other source unchanged.
    per_input_row: bool = True

    # Classic's IsCritical on the <ImportDetail>/<ImportHeading> that holds
    # this call. PARSED AND REPORTED, NEVER TRANSLATED: the name suggests
    # "abort on failure", but whether that aborts the row, the block or the
    # whole run is not confirmed, and the difference decides whether a
    # half-written row is left behind. Do not map it to behaviour until a
    # real Classic run settles it - just make sure nobody loses it silently.
    is_critical: Optional[bool] = None

    # Classic's <M3Api MessageColumn>: the sheet column THIS call's outcome is
    # written to (the block-level one is Workflow.message_column). Translated
    # only on the generic-pipeline path; build.py reports it.
    message_column: Optional[str] = None

    # Runs ONCE PER GROUP of rows rather than once per row - Classic's
    # <ImportHeading><StartRun>. See Workflow.group_columns for what
    # defines a group and for the confirmed firing semantics. No Live
    # equivalent is confirmed: Live documents a "Group by" option on the M3
    # API step (hasGrouping / extendedGrouping), but it is empty in every
    # capture and its semantics are unknown, so build.py does not use it and
    # says so rather than quietly emitting an ordinary per-row call.
    runs_once_per_group: bool = False


@dataclass
class Workflow:
    """The whole parsed source, regardless of where it came from."""
    label: str
    transactions: list[Transaction] = field(default_factory=list)
    # Export side: where results are WRITTEN.
    target_sheet_name: Optional[str] = None
    output_start_row: Optional[int] = None
    # Import side: where input rows are READ from. Classic keeps these
    # separate; Live's excel_1 has a single sheetName/dataRow for the whole
    # grid, so build.py has to reconcile them (and flag it when they
    # disagree, since that shape cannot be represented).
    source_sheet_name: Optional[str] = None
    input_start_row: Optional[int] = None
    # Classic writes a per-row status message to this column. No confirmed
    # Live equivalent, so it is carried here and reported, never guessed at.
    message_column: Optional[str] = None
    # BLOCK-level gates: filters that apply to the whole workflow rather
    # than to one transaction. Classic has both levels - a <RowFilterEQ>
    # inside an <ImportDetail> gates that one call, while one sitting as a
    # SIBLING of the details gates the entire <Import> block. Kept separate
    # from Transaction.row_filters because the two map differently: a
    # block-level filter is exactly GENERIC_FILTER's semantics (it also
    # runs once, before everything), whereas a per-transaction one is only
    # safe to translate when every transaction shares it.
    row_filters: list[RowFilter] = field(default_factory=list)
    # Columns whose combined value defines a GROUP of rows - Classic's
    # <ImportHeading GroupColumns="B C E O">. Empty when the source has no
    # grouping.
    #
    # Confirmed semantics (2026-09-10, by running a real Classic function
    # with grouped rows - see the migration summary's Grouping section):
    # a group breaks when these columns differ from the PREVIOUS row's, so
    # the sheet must be sorted by them; the runs_once_per_group call fires
    # on that first row only; its Cell output lands on that row alone with
    # no fill-down; and per-row filters reading that column therefore pass
    # on a group's first row and fail on the rest.
    #
    # Carried and reported, never translated: Live's M3 API step runs every
    # transaction per row, so none of the above is expressible on that path.
    group_columns: list[str] = field(default_factory=list)
