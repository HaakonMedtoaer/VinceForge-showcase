"""
IR for a GENERIC step pipeline - a workflow built from Trigger +
Transform / REST API / Code steps only, with no M3 API step and no Excel
step, talking to M3 over ION API's bulk execute endpoint instead.

This is not a workaround. It is the shape Vince's own production customer
workflows use (see m3_rest.py for the provenance list): "Package Lost"
runs Trigger -> (Transform -> Rest API) x7 and performs real M3 writes,
"Claim register" ends in a Table Updater, "Display customer order lines"
feeds a widget. What this module adds is a way to DESCRIBE one and build
it, rather than clicking twelve steps together by hand.

Why a separate IR from ir.py: ir.py's Workflow/Transaction is shaped
around Live's M3 API step - a bag of program/transaction calls inside ONE
step, whose inputs Live itself binds to Excel columns and trigger fields.
A generic pipeline is the opposite shape: an ordered CHAIN of steps where
nothing is bound by Live at all - every binding is an expression we write
(JSONata in a Transform, JS in a Code step).

The two IRs share vocabulary where they overlap: a value fed into an M3
call still comes from one of the same four places ir.py names (CONSTANT /
EXCEL_COLUMN / CLIENT / API_CHAIN), spelled {"const": ...} / {"row": ...}
/ {"trigger": ...} / {"step": ...} here because a generic pipeline states
each one explicitly instead of inferring it from Classic's "*"-syntax.

WHAT PROBLEM THIS SOLVES
------------------------
Live's GENERIC_FILTER step runs once, before the whole M3 API step, so it
cannot gate one M3 call while another runs unconditionally - the shape
Classic functions use constantly (an authority check that always runs,
whose output gates a later update). Here a filter is a JSONata predicate
on the rows going into one specific call's payload, and it can read an
earlier call's own output. See M3BulkStep.
"""

from dataclasses import dataclass, field
from typing import Optional, Union


class PipelineSpecError(ValueError):
    """A hand-written pipeline spec that can't be built as given."""


# --- Value sources -----------------------------------------------------
# Exactly one of these keys, mirroring ir.py's four source kinds plus a
# raw escape hatch:
#   {"const": "901"}                a fixed literal
#   {"row": "OrderNumber"}          a property of the current input row
#   {"trigger": "CONO"}             a trigger field / request body value
#   {"step": "rest_api_1.CUNO"}     a field from an earlier step's records
#   {"jsonata": "'-' & ORQT"}       raw JSONata, for what the rest can't say
VALUE_KEYS = ("const", "row", "trigger", "step", "jsonata")

# The Code-step path resolves values at runtime in JS instead of JSONata,
# so it reads an earlier call by its ref rather than by Live stepId.
#
#   {"call": "authority.ALO1"}   a FIELD of an earlier call's first record,
#                                for THIS row
#   {"callStatus": "authority"}  that call's outcome for this row, as one of
#                                "ok" | "failed" | "skipped" | "notrun"
#
# callStatus is what makes Classic's <BasedOnTransaction> expressible, and
# it is only meaningful on this path: a row pipeline POSTs exactly ONE M3
# transaction per request, so the bulk response's confirmed
# `nrOfFailedTransactions` counter is a verdict on that one call. Inside a
# bulk payload carrying N transactions the same counter says only "some of
# them failed", which is why the m3Bulk path has no equivalent.
CODE_VALUE_KEYS = ("const", "row", "trigger", "call", "callStatus")

# The callStatus values a translated <BasedOnTransaction> compares against.
CALL_STATUS_OK = "ok"
CALL_STATUS_FAILED = "failed"

# Value keys that read an earlier CALL, and so cannot appear in a gate
# evaluated before any call has run (see M3RowPipelineStep.row_gates).
CALL_REFERENCE_KEYS = ("call", "callStatus")


def validate_value(value: object, where: str, allowed=VALUE_KEYS) -> dict:
    if not isinstance(value, dict) or len(value) != 1 or next(iter(value)) not in allowed:
        raise PipelineSpecError(f"{where}: expected exactly one of {allowed}, got {value!r}")
    return value


# --- Gates -------------------------------------------------------------
# Classic's RowFilterEQ/RowFilterNE map onto "eq"/"ne". The rest cost
# nothing to support because these compile to JSONata (or to JS in the
# Code path), not to Live's GENERIC_FILTER step - whose confirmed operator
# set is only equal/notEqual, and which can't be scoped to a single call.
GATE_OPERATORS = ("eq", "ne", "empty", "notEmpty", "contains", "gt", "ge", "lt", "le", "in", "notIn")


@dataclass
class Gate:
    """One condition. A row survives only if every gate on the call passes."""
    left: dict
    op: str
    right: Optional[dict] = None     # unused by empty/notEmpty
    case_insensitive: bool = False   # Classic's RowFilter IsLower="true".
                                     # GENERIC_FILTER has no confirmed
                                     # equivalent; in JSONata/JS it is a
                                     # real comparison option.

    _SPEC_KEYS = {"left": "left", "op": "op", "right": "right",
                  "caseInsensitive": "case_insensitive"}

    @classmethod
    def from_spec(cls, raw: dict, allowed=VALUE_KEYS) -> "Gate":
        unknown = set(raw) - set(cls._SPEC_KEYS)
        if unknown:
            raise PipelineSpecError(f"Unknown key(s) in gate: {sorted(unknown)}")
        gate = cls(**{cls._SPEC_KEYS[key]: value for key, value in raw.items()})
        gate.validate(allowed)
        return gate

    def validate(self, allowed=VALUE_KEYS) -> None:
        if self.op not in GATE_OPERATORS:
            raise PipelineSpecError(f"Unknown gate operator {self.op!r} - expected one of {GATE_OPERATORS}")
        validate_value(self.left, f"gate left ({self.op})", allowed)
        if self.op in ("empty", "notEmpty"):
            if self.right is not None:
                raise PipelineSpecError(f"Gate operator {self.op!r} takes no right-hand side")
        else:
            if self.right is None:
                raise PipelineSpecError(f"Gate operator {self.op!r} needs a right-hand side")
            validate_value(self.right, f"gate right ({self.op})", allowed)


# --- M3 bulk call ------------------------------------------------------

@dataclass
class BulkCall:
    """
    One M3 transaction inside a bulk payload's `transactions` array.

    Many of these ride in a single HTTP call - the whole point of the bulk
    endpoint (M3 needs eight transactions to read one order).
    """
    transaction: str                                   # e.g. "GetOrderHead"
    record: dict = field(default_factory=dict)         # M3 field -> value source
    selected_columns: list = field(default_factory=list)   # limit returned fields

    _SPEC_KEYS = {"transaction": "transaction", "record": "record",
                  "selectedColumns": "selected_columns"}

    @classmethod
    def from_spec(cls, raw: dict) -> "BulkCall":
        unknown = set(raw) - set(cls._SPEC_KEYS)
        if unknown:
            raise PipelineSpecError(
                f"Unknown key(s) in call {raw.get('transaction')!r}: {sorted(unknown)} "
                f"- expected some of {sorted(cls._SPEC_KEYS)}"
            )
        call = cls(**{cls._SPEC_KEYS[key]: value for key, value in raw.items()})
        if not call.transaction:
            raise PipelineSpecError("A bulk call needs a \"transaction\"")
        for name, value in call.record.items():
            validate_value(value, f"call {call.transaction!r} record field {name!r}")
        return call


@dataclass
class M3BulkStep:
    """
    One M3 bulk call, built the way every real Vince workflow builds it:
    a Transform that assembles {"payload": {...}} followed by a REST API
    step that posts it. This single IR step therefore expands to TWO Live
    steps (see build_pipeline.py).

    `gates` / `filter_expression` is the feature that makes this path
    worth having: they filter the rows that go into THIS call's payload,
    so one call can be gated while the next runs over everything - and a
    gate can read an earlier call's output via {"step": "..."}.
    """
    program: str                                  # e.g. "OIS100MI"
    calls: list = field(default_factory=list)     # list[BulkCall]

    # JSONata for the array of input rows. `$context.data.trigger.body` is
    # the confirmed real source for widget/VXL input; a previous step's
    # records is the other common one (see m3_rest.records_path).
    rows: str = "$context.data.trigger.body"
    gates: list = field(default_factory=list)     # list[Gate]
    filter_expression: Optional[str] = None       # raw JSONata predicate, wins over gates

    cono: Optional[int] = None                    # optional in the bulk body
    max_returned_records: Optional[int] = None    # optional in the bulk body

    base_url: Optional[str] = None                # ION API root incl. tenant segment
    connection_id: Optional[str] = None           # defaults to the workflow's own
    headers: Optional[dict] = None                # defaults to content-type: application/json
    skip_if_expression: Optional[str] = None      # v2 REST API skip; syntax UNCONFIRMED

    ref: Optional[str] = None                     # handle a later step can point at with
                                                  # {"step": "<ref>.FIELD"} instead of a
                                                  # hardcoded Live stepId
    name: Optional[str] = None                    # display name for the REST step
    transform_name: Optional[str] = None          # display name for the Transform step

    def __post_init__(self):
        if not self.program:
            raise PipelineSpecError("An m3Bulk step needs a \"program\"")
        self.calls = [c if isinstance(c, BulkCall) else BulkCall.from_spec(c) for c in self.calls]
        if not self.calls:
            raise PipelineSpecError(f"m3Bulk {self.program}: needs at least one entry in \"calls\"")
        self.gates = [g if isinstance(g, Gate) else Gate.from_spec(g) for g in self.gates]


# --- The Code-step escape hatch ---------------------------------------

@dataclass
class CodeCall:
    """
    One M3 bulk call made per row, sequentially, from inside a Code step.

    Only reach for this when a later call's INPUT depends on an earlier
    call's OUTPUT for that same row (so the calls can't be batched), or
    when you need per-row throttling or error handling. Anything
    set-shaped belongs in M3BulkStep.
    """
    ref: str                                      # handle for gates/records of later calls
    program: str
    transaction: str
    record: dict = field(default_factory=dict)    # M3 field -> value source (CODE_VALUE_KEYS)
    selected_columns: list = field(default_factory=list)
    cono: Optional[int] = None
    gates: list = field(default_factory=list)     # list[Gate]
    continue_on_error: bool = True
    throttle_ms: int = 0                          # the one real customer Code example
                                                  # waited 2100ms between calls
    keep_raw: bool = False

    # Fires on the FIRST row of each group only, groups being defined by
    # M3RowPipelineStep.group_columns - Classic's <ImportHeading><StartRun>.
    # With no group columns declared there is one group, so such a call runs
    # once for the whole run.
    once_per_group: bool = False

    # output "sheet" only: the column this call's failure text is written to
    # (Classic's <M3Api MessageColumn>). Calls sharing a column are joined.
    message_column: Optional[str] = None

    _SPEC_KEYS = {
        "ref": "ref", "program": "program", "transaction": "transaction", "record": "record",
        "selectedColumns": "selected_columns", "cono": "cono", "gates": "gates",
        "continueOnError": "continue_on_error", "throttleMs": "throttle_ms", "keepRaw": "keep_raw",
        "oncePerGroup": "once_per_group", "messageColumn": "message_column",
    }

    @classmethod
    def from_spec(cls, raw: dict) -> "CodeCall":
        unknown = set(raw) - set(cls._SPEC_KEYS)
        if unknown:
            raise PipelineSpecError(
                f"Unknown key(s) in call {raw.get('ref')!r}: {sorted(unknown)} "
                f"- expected some of {sorted(cls._SPEC_KEYS)}"
            )
        return cls(**{cls._SPEC_KEYS[key]: value for key, value in raw.items()})

    def __post_init__(self):
        if not self.ref or not self.ref.replace("_", "").isalnum():
            raise PipelineSpecError(
                f"Call ref {self.ref!r} must be a non-empty alphanumeric/underscore handle "
                "(it becomes a key in the generated JS)"
            )
        for name, value in self.record.items():
            validate_value(value, f"call {self.ref!r} record field {name!r}", CODE_VALUE_KEYS)
        self.gates = [g if isinstance(g, Gate) else Gate.from_spec(g, CODE_VALUE_KEYS)
                      for g in self.gates]


# How far a once-per-group call's result reaches. Classic's confirmed SHEET
# behaviour is "firstRow": the StartRun writes its output cell to the
# group's first row with no fill-down, so a per-row filter reading that
# column fails on rows 2..n. Whether Classic's own line calls therefore
# also stop at a group's first row is the one contradiction its source
# never resolved - classic_to_pipeline.GROUP_RESULT_SCOPE owns that choice
# and carries the test that settles it.
GROUP_RESULT_SCOPES = ("group", "firstRow")

# What a row pipeline returns:
#   "records"  - one record per CALL per row (_row/_call/_status/_message,
#                I_<input>, every returned field). A call log, not a sheet:
#                written back into a spreadsheet it floods it with columns.
#   "sheet"    - the INPUT rows passed straight back, same count, order and
#                keys, with `sheet_cells` and `message_column` written into
#                them. Output in the same shape the input arrived in, which
#                is what a VXL write-back into the source sheet expects -
#                what Classic's <Cell>/MessageColumn did. The Classic
#                translator always uses this.
#   "envelope" - {status, results, ...} for a workflow called over REST.
OUTPUT_MODES = ("records", "sheet", "envelope")


@dataclass
class M3RowPipelineStep:
    """Sequential per-row M3 caller, rendered into a single Code step."""
    calls: list = field(default_factory=list)     # list[CodeCall]
    rows_js: str = "trigger.body"                 # JS expression; confirmed real source
    # ^ CONFIRMED 2026-09-11 against a real VXL-triggered run (WFEXEC-08330923...):
    # trigger.body is a BARE ARRAY of row objects keyed by column LETTER, not
    # {data: [...]}. The old "trigger.body.data" default was never actually
    # confirmed for a spreadsheet trigger - it came from a widget-triggered
    # example - and silently produced zero rows (someArray.data is undefined).
    base_url: Optional[str] = None
    connection_id: Optional[str] = None
    output: str = "records"                       # "records" | "sheet" | "envelope" - see OUTPUT_MODES
    row_throttle_ms: int = 0
    stop_on_error: bool = False
    debug: bool = True                            # attach diagnostics; MUST be off before a
    # run whose sheet you intend to reuse - CONFIRMED 2026-09-11: when debug is on, the
    # spreadsheet write-back can't expand {records, _diagnostics, _aborted} into a table, so
    # it dumps the whole thing as one JSON string into a single cell of the SAME source sheet
    # - which then gets read back in as row 1's data on the next run. See build_pipeline.py's
    # warning for this step.
    #
    # A SECOND bug this same investigation caught, 2026-09-11: m3_rest.py used to pick between
    # two fixed return statements in PYTHON at generation time (`_RETURN_RECORDS_DEBUG` if
    # `step.debug` else `_RETURN_RECORDS`), so the `debug` VALUE sitting in the generated PLAN
    # object was pure data - the deployed JS never read it, so editing "debug": true -> false
    # directly in an already-pasted Live Code step and re-running did NOTHING. Fixed: the
    # runtime JS now reads `PLAN.debug` itself (`return PLAN.debug ? {...} : records;`), so
    # flipping this field in a pasted Code step and re-running actually works, no regeneration
    # needed. See `m3_rest._RETURN_RECORDS`.
    name: Optional[str] = None

    # Gates evaluated ONCE per row, before any call: Classic's
    # <Import>-level RowFilter, which skips the whole row rather than one
    # call. They may not reference a call - nothing has run yet.
    row_gates: list = field(default_factory=list)  # list[Gate]

    # Columns whose combined value defines a group, for once_per_group
    # calls. A group breaks when they differ from the PREVIOUS row's, which
    # is Classic's confirmed rule - and means the rows have to arrive
    # sorted by them.
    group_columns: list = field(default_factory=list)
    group_result_scope: str = "group"

    # Optional column-letter -> header-name map for {"row": ...} lookups.
    # Exists because whether a spreadsheet row arrives keyed by column
    # letter or by header name is NOT confirmed: the runtime tries the key
    # as given, then this map, and reports the real first row it saw.
    column_headers: dict = field(default_factory=dict)

    # output == "sheet" only: which call field lands in which column
    # ({"call": ref, "field": "STAT", "column": "P"}), and the column that
    # gets each row's failure messages. A column is looked up like a
    # {"row": ...} key (as given, then column_headers, then case-
    # insensitively) so it overwrites the key the row already uses; a
    # column the row does not carry at all is ADDED under that name.
    sheet_cells: list = field(default_factory=list)
    message_column: Optional[str] = None

    # output == "sheet" only. Observed on a real table run (2026-10-01,
    # MMS002_OpenWhs_1.5 - Open): VXL sends the sheet's whole used range
    # from ROW 1, keyed by column letter, and writes the result into the
    # TABLE by HEADER NAME starting at its first body row. So:
    #   input_start_row - 1-based sheet row of the first row to PROCESS (Classic's
    #       InputStartRow). Rows above it are never called.
    #   header_row      - 1-based sheet row holding the table's header cells.
    #       Default input_start_row - 1. Its cell values name the output keys,
    #       and the rows RETURNED start just below it, so they land on the
    #       table's body. Rows between the header and input_start_row pass
    #       through unchanged. Assumes rows[i] is sheet row i+1.
    # Neither set: the old behaviour (every row processed and returned, letter keys).
    input_start_row: Optional[int] = None
    header_row: Optional[int] = None

    def __post_init__(self):
        if self.output not in OUTPUT_MODES:
            raise PipelineSpecError(f"output must be one of {OUTPUT_MODES}, got {self.output!r}")
        if self.group_result_scope not in GROUP_RESULT_SCOPES:
            raise PipelineSpecError(
                f"groupResultScope must be one of {GROUP_RESULT_SCOPES}, "
                f"got {self.group_result_scope!r}"
            )
        self.row_gates = [g if isinstance(g, Gate) else Gate.from_spec(g, CODE_VALUE_KEYS)
                          for g in self.row_gates]
        for gate in self.row_gates:
            for value in (gate.left, gate.right):
                if value and any(key in value for key in CALL_REFERENCE_KEYS):
                    raise PipelineSpecError(
                        f"A row gate cannot read a call ({value!r}): row gates are evaluated "
                        "before any call has run for that row. Put it on the call that needs it."
                    )
        self.calls = [c if isinstance(c, CodeCall) else CodeCall.from_spec(c) for c in self.calls]
        refs = {c.ref for c in self.calls}
        for cell in self.sheet_cells:
            if not isinstance(cell, dict) or set(cell) != {"call", "field", "column"}:
                raise PipelineSpecError(
                    f"sheetCells entries must be {{call, field, column}}, got {cell!r}")
            if cell["call"] not in refs:
                raise PipelineSpecError(f"sheetCells names unknown call {cell['call']!r}")
        if (self.sheet_cells or self.message_column or any(c.message_column for c in self.calls)) \
                and self.output != "sheet":
            raise PipelineSpecError('sheetCells/messageColumn only apply to output "sheet"')
        for label, value in (("inputStartRow", self.input_start_row), ("headerRow", self.header_row)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise PipelineSpecError(f"{label} must be a whole number >= 1, got {value!r}")
        if (self.input_start_row or self.header_row) and self.output != "sheet":
            raise PipelineSpecError('inputStartRow/headerRow only apply to output "sheet"')
        if self.header_row and not self.input_start_row:
            raise PipelineSpecError("headerRow needs inputStartRow (the first row to process)")
        if self.header_row and self.header_row >= self.input_start_row:
            raise PipelineSpecError(
                f"headerRow ({self.header_row}) must be above inputStartRow ({self.input_start_row})")
        if not self.calls:
            raise PipelineSpecError("m3RowPipeline needs at least one call")
        refs = [c.ref for c in self.calls]
        if len(set(refs)) != len(refs):
            raise PipelineSpecError(f"Duplicate call refs: {refs}")
        # A gate or record field may only reference an EARLIER call - the
        # generated JS runs them in order, so a forward reference would
        # read undefined at runtime instead of failing loudly.
        seen = set()
        for call in self.calls:
            sources = list(call.record.values()) + [
                v for g in call.gates for v in (g.left, g.right) if v
            ]
            for value in sources:
                for key in CALL_REFERENCE_KEYS:
                    if key not in value:
                        continue
                    ref = str(value[key]).split(".")[0]
                    if ref not in seen:
                        raise PipelineSpecError(
                            f"call {call.ref!r} references {value[key]!r}, but {ref!r} is not an "
                            f"earlier call in this pipeline (earlier refs: {sorted(seen) or 'none'})"
                        )
            seen.add(call.ref)


# --- Plain steps -------------------------------------------------------

@dataclass
class TransformStep:
    """type TRANSFORMER_MORPH. `template` is a JSONata expression string."""
    template: str
    name: Optional[str] = None


@dataclass
class CodeStep:
    """type GENERIC_CODE. `content` is a whole Node module source string."""
    content: str
    name: Optional[str] = None


@dataclass
class RestApiStep:
    """
    type GENERIC_API. One call per execution unless `for_each` is set.

    Both body modes are confirmed real: `bodyPath` (a JSONata selector
    into the previous step's output - what every M3 example uses) and
    `body` (a literal object, with `{{ body.x }}` / `{{ header.x }}`
    substituted at execution). Exactly one of them should be set.
    """
    endpoint: str
    method: str = "post"
    headers: dict = field(default_factory=dict)
    body_path: Optional[str] = None
    body: Optional[dict] = None
    version: Optional[int] = None            # 2 unlocks forEach + skipIfExpression
    for_each: Optional[str] = None           # JSONpath to an array; beta
    skip_if_expression: Optional[str] = None  # name confirmed, syntax NOT
    connection_id: Optional[str] = None      # omitted entirely when unset
    name: Optional[str] = None

    def __post_init__(self):
        if self.body_path and self.body is not None:
            raise PipelineSpecError(
                f"restApi {self.endpoint!r}: set either \"bodyPath\" or \"body\", not both - they are "
                "two different modes (a selector into the previous step's output vs. a literal body)."
            )
        if (self.for_each or self.skip_if_expression) and self.version is None:
            self.version = 2   # both features exist only on v2; documented


@dataclass
class M3ExpandStep:
    """
    type GENERIC_CODE: Classic's <Expand> rebuilt on ONE sheet - nested M3
    list calls fanning out, lookups per record, rows keyed by the sheet
    table's header names. `spec` is validated and rendered by expand_code.py.
    """
    spec: dict
    base_url: Optional[str] = None
    name: Optional[str] = None


Step = Union[TransformStep, CodeStep, RestApiStep, M3BulkStep, M3RowPipelineStep, M3ExpandStep]


@dataclass
class TriggerField:
    """
    One CLIENT trigger input.

    Shape note: the confirmed trigger-field JSON for a field NOT tied to
    an M3 transaction comes from the Generic Filter case (a Filter_<col>
    field), which carries no `apiId`. Every other confirmed trigger field
    belongs to an M3 API step and does carry one.
    """
    name: str
    label: Optional[str] = None
    data_type: str = "STRING"
    length: str = "15"
    mandatory: bool = True
    default_value: Optional[str] = None
    place_holder: Optional[str] = None
    form_group_id: int = 1


# The VXL label is CONFIRMED to live in the top-level `labels` array:
# a GET of a hand-built, VXL-tagged workflow came back with
# `"labels": ["VXL"]`. The three speculative placements this used to carry
# (workflowTags / trigger_1.tags / envelope tags) are gone - the question
# is settled, so keeping the alternatives around would just be clutter
# pretending to be caution.
VXL_LABEL = "VXL"


@dataclass
class Pipeline:
    """A whole generic workflow: a trigger plus an ordered chain of steps."""
    label: str
    steps: list = field(default_factory=list)
    description: str = ""
    trigger_title: Optional[str] = None
    trigger_description: str = "Description"
    button_text: str = "Execute"
    trigger_fields: list = field(default_factory=list)
    has_spreadsheet_input: bool = False
    labels: list = field(default_factory=list)   # VXL_LABEL belongs here
    constants: dict = field(default_factory=dict)


# --- Spec (JSON) -> IR -------------------------------------------------

SPEC_MARKER = "vinceforge"
SPEC_VERSION = "pipeline/1"

STEP_KINDS = ("transform", "code", "restApi", "m3Bulk", "m3RowPipeline", "m3Expand")


def looks_like_pipeline_spec(obj: object) -> bool:
    return isinstance(obj, dict) and str(obj.get(SPEC_MARKER, "")).startswith("pipeline/")


def parse_pipeline_spec(spec: dict) -> Pipeline:
    """Validate a hand-written spec and turn it into a Pipeline."""
    if not looks_like_pipeline_spec(spec):
        raise PipelineSpecError(
            f'Not a pipeline spec - expected a "{SPEC_MARKER}": "{SPEC_VERSION}" key at the top level.'
        )
    if spec[SPEC_MARKER] != SPEC_VERSION:
        raise PipelineSpecError(
            f"Unsupported spec version {spec[SPEC_MARKER]!r} (this build understands {SPEC_VERSION!r})"
        )

    name = (spec.get("name") or "").strip()
    if not name:
        raise PipelineSpecError('A pipeline spec needs a "name".')

    trigger = spec.get("trigger") or {}
    fields = []
    for index, raw in enumerate(trigger.get("fields") or []):
        if not raw.get("name"):
            raise PipelineSpecError(f"trigger.fields[{index}] has no name")
        fields.append(TriggerField(
            name=raw["name"],
            label=raw.get("label"),
            data_type=raw.get("dataType", "STRING"),
            length=str(raw.get("length", "15")),
            mandatory=bool(raw.get("mandatory", True)),
            default_value=raw.get("defaultValue"),
            place_holder=raw.get("placeHolder"),
            form_group_id=int(raw.get("formGroupId", 1)),
        ))

    labels = list(spec.get("labels") or [])
    for label in labels:
        if not isinstance(label, str):
            raise PipelineSpecError(f'"labels" must be strings, got {label!r}')
    # "vxl": true is sugar for the one label that carries behaviour.
    if spec.get("vxl") and VXL_LABEL not in labels:
        labels.append(VXL_LABEL)

    # A pipeline-level baseUrl saves repeating the ION API root on every
    # M3 step; a step can still override it.
    default_base_url = spec.get("baseUrl")
    steps = [_parse_step(raw, index, default_base_url)
             for index, raw in enumerate(spec.get("steps") or [])]
    if not steps:
        raise PipelineSpecError('A pipeline spec needs at least one entry in "steps".')

    return Pipeline(
        label=name,
        steps=steps,
        description=spec.get("description", ""),
        trigger_title=trigger.get("title"),
        trigger_description=trigger.get("description", "Description"),
        button_text=trigger.get("buttonText", "Execute"),
        trigger_fields=fields,
        has_spreadsheet_input=bool(trigger.get("hasSpreadSheetInput", False)),
        labels=labels,
        constants=spec.get("constants") or {},
    )


def _parse_step(raw: dict, index: int, default_base_url: Optional[str]):
    kind = raw.get("kind")
    if kind not in STEP_KINDS:
        raise PipelineSpecError(
            f"steps[{index}]: unknown kind {kind!r} - expected one of {list(STEP_KINDS)}"
        )
    where = f"steps[{index}] ({kind})"

    if kind == "transform":
        template = raw.get("template")
        if not isinstance(template, str) or not template.strip():
            raise PipelineSpecError(f'{where}: needs a non-empty "template" string')
        return TransformStep(template=template, name=raw.get("name"))

    if kind == "code":
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PipelineSpecError(
                f'{where}: needs a non-empty "content" string (the whole Node module source)'
            )
        return CodeStep(content=content, name=raw.get("name"))

    if kind == "restApi":
        if not raw.get("endpoint"):
            raise PipelineSpecError(f'{where}: needs an "endpoint"')
        return RestApiStep(
            endpoint=raw["endpoint"],
            method=raw.get("method", "post"),
            headers=raw.get("headers") or {},
            body_path=raw.get("bodyPath"),
            body=raw.get("body"),
            version=raw.get("version"),
            for_each=raw.get("forEach"),
            skip_if_expression=raw.get("skipIfExpression"),
            connection_id=raw.get("connectionId"),
            name=raw.get("name"),
        )

    if kind == "m3Bulk":
        unknown = set(raw) - {
            "kind", "program", "calls", "rows", "gates", "filterExpression", "cono",
            "maxReturnedRecords", "baseUrl", "connectionId", "headers", "skipIfExpression",
            "ref", "name", "transformName",
        }
        if unknown:
            raise PipelineSpecError(f"{where}: unknown key(s) {sorted(unknown)}")
        return M3BulkStep(
            program=raw.get("program", ""),
            calls=raw.get("calls") or [],
            rows=raw.get("rows", "$context.data.trigger.body"),
            gates=raw.get("gates") or [],
            filter_expression=raw.get("filterExpression"),
            cono=raw.get("cono"),
            max_returned_records=raw.get("maxReturnedRecords"),
            base_url=raw.get("baseUrl") or default_base_url,
            connection_id=raw.get("connectionId"),
            headers=raw.get("headers"),
            skip_if_expression=raw.get("skipIfExpression"),
            ref=raw.get("ref"),
            name=raw.get("name"),
            transform_name=raw.get("transformName"),
        )

    if kind == "m3RowPipeline":
        unknown = set(raw) - {
            "kind", "calls", "rows", "baseUrl", "connectionId", "output", "rowThrottleMs",
            "stopOnError", "debug", "name", "rowGates", "groupColumns", "groupResultScope",
            "columnHeaders", "sheetCells", "messageColumn", "inputStartRow", "headerRow",
        }
        if unknown:
            raise PipelineSpecError(f"{where}: unknown key(s) {sorted(unknown)}")
        return M3RowPipelineStep(
            calls=raw.get("calls") or [],
            rows_js=raw.get("rows", "trigger.body"),
            base_url=raw.get("baseUrl") or default_base_url,
            connection_id=raw.get("connectionId"),
            output=raw.get("output", "records"),
            row_throttle_ms=int(raw.get("rowThrottleMs", 0)),
            stop_on_error=bool(raw.get("stopOnError", False)),
            debug=bool(raw.get("debug", True)),
            name=raw.get("name"),
            row_gates=raw.get("rowGates") or [],
            group_columns=list(raw.get("groupColumns") or []),
            group_result_scope=raw.get("groupResultScope", "group"),
            column_headers=dict(raw.get("columnHeaders") or {}),
            sheet_cells=list(raw.get("sheetCells") or []),
            message_column=raw.get("messageColumn"),
            input_start_row=raw.get("inputStartRow"),
            header_row=raw.get("headerRow"),
        )

    if kind == "m3Expand":
        from expand_code import ExpandSpecError, validate_expand_spec
        unknown = set(raw) - {"kind", "name", "baseUrl", "connectionId", "concurrency", "debug",
                              "params", "levels", "columns"}
        if unknown:
            raise PipelineSpecError(f"{where}: unknown key(s) {sorted(unknown)}")
        try:
            validate_expand_spec(raw, where)
        except ExpandSpecError as error:
            raise PipelineSpecError(str(error))
        return M3ExpandStep(spec=raw, base_url=raw.get("baseUrl") or default_base_url,
                            name=raw.get("name"))

    raise AssertionError("unreachable")  # pragma: no cover


def scaffold_spec() -> dict:
    """
    A starter spec for the CLI's pipeline-scaffold command and the web
    UI's Scaffold button. Shows the gated second call, since that is the
    case Live's native filtering cannot express and therefore the reason
    this path exists.
    """
    return {
        SPEC_MARKER: SPEC_VERSION,
        "name": "New generic pipeline",
        "description": "",
        "baseUrl": "https://mingle-ionapi.eu1.inforcloudsuite.com/<TENANT>",
        "vxl": True,
        "trigger": {
            "description": "Description",
            "buttonText": "Execute",
            "hasSpreadSheetInput": True,
        },
        "steps": [
            {
                "kind": "m3Bulk",
                "ref": "authority",
                "name": "Check authority",
                "program": "AUTCHKMI",
                "rows": "$context.data.trigger.body",
                "calls": [
                    {
                        "transaction": "ChkAuthority",
                        "record": {"PGNM": {"const": "MMS100"}, "ITNO": {"row": "ItemNumber"}},
                    }
                ],
            },
            {
                "kind": "m3Bulk",
                "name": "Update only what passed",
                "program": "MMS100MI",
                "rows": "$context.data.trigger.body",
                "gates": [
                    {"left": {"step": "authority.ALPG"}, "op": "eq", "right": {"const": "1"}}
                ],
                "calls": [
                    {
                        "transaction": "UpdItmBasic",
                        "record": {"ITNO": {"row": "ItemNumber"}, "ITDS": {"row": "Description"}},
                    }
                ],
            },
        ],
    }
