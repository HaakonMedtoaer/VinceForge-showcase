"""
Vince Classic -> Vince Live workflow converter
================================================

WHAT THIS DOES
--------------
Reads a Vince Classic "VinceExcelConfig" XML file (the kind you export from
Classic) and builds the JSON structure that Vince Live's workflow API
(POST /workflows) expects, based on real payloads we captured from Live's
own GET /workflows/{id} responses.

HOW TO READ THIS FILE
----------------------
It's organised in three stages, matching how we talked through the problem:

  1. PARSE    - turn the Classic XML into a simple, easy-to-inspect Python
                structure (an "intermediate representation", or IR). This
                stage knows nothing about Live.
  2. BUILD    - turn that IR into Live's JSON shape (workflowConfig +
                stepConfig). This stage knows nothing about Classic XML.
  3. ASSEMBLE - glue the two stages together and write the result to disk.

Keeping PARSE and BUILD separate means: if Classic ever changes its export
format, you only touch stage 1. If Live changes its API shape, you only
touch stage 2.

WHAT'S STILL A GAP (see "KNOWN GAPS" near the bottom)
------------------------------------------------------
Live's JSON stores rich metadata per field - data type, length, whether
it's mandatory, a human label (e.g. "type": "NUMERIC", "length": "17",
"mandatory": true, "descr": "Physical inventory quantity"). Your Classic
XML doesn't carry any of that - it only has field names and where the
value comes from. That metadata has to come from somewhere: Infor M3's
own API catalog (the same thing Live's UI reads from when you pick a
transaction and see the field list appear). This script currently uses a
small hand-filled lookup table as a stand-in, and clearly flags any field
it doesn't have metadata for, rather than guessing.
"""

import json
import os
import random
import string
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# Persisted next to this script so the numbering survives between runs.
# Keeps a count per base workflow label, e.g. {"List Lines": 3} means
# "List Lines", "List Lines 2", and "List Lines 3" have already been
# generated, so the next run produces "List Lines 4".
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "converter_state.json")


def _next_labeled_name(base_label: str) -> str:
    """
    Append an incrementing number to a workflow name each time this
    script is run for the same base label, so repeated test runs get
    distinct, easy-to-track names in the Live UI instead of colliding
    or requiring you to rename manually (e.g. "List Lines", then
    "List Lines 2", "List Lines 3", ...).
    """
    state = {}
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}

    count = state.get(base_label, 0) + 1
    state[base_label] = count

    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    return base_label if count == 1 else f"{base_label} {count}"


# ============================================================
# STAGE 1: PARSE - Classic XML -> intermediate representation
# ============================================================

@dataclass
class ClassicInputField:
    """One <InputField> from the Classic XML."""
    name: str                  # e.g. "WHLO"
    raw_value: str              # e.g. "*Field1", "*A", "*MMS301MI.LstStockTakeDet.WHLO", "1"
    label: Optional[str] = None
    source: Optional[str] = None  # e.g. "Constant" if explicitly set in XML


@dataclass
class ClassicCell:
    """One <Cell> from an Export block (Excel column mapping)."""
    column: str                # e.g. "A"
    field_name: str             # e.g. "WHLO" (stripped of the leading *)
    data_type: Optional[str] = None  # e.g. "decimal"


@dataclass
class ClassicRowFilter:
    """
    One <RowFilter> condition, always wrapped in either <RowFilterEQ>
    (run only if column == value) or <RowFilterNE> (run only if
    column != value). Confirmed via a real example in classic_v1.xml:

        <RowFilterEQ>
            <RowFilter Column="BD" Value="OK" Label="Deliverymethod filter"
                       IsHidden="true" IsLower="false" />
        </RowFilterEQ>

    IsHidden and IsLower are carried through as-is but not yet
    interpreted - their exact meaning (IsLower: case-insensitive
    comparison? something else?) is unconfirmed.
    """
    operator: str        # "EQ" or "NE"
    column: str           # Excel column letter this condition reads, e.g. "BD"
    value: str            # comparison value, e.g. "OK"
    label: Optional[str] = None
    is_hidden: bool = False
    is_lower: bool = False


@dataclass
class ClassicTransaction:
    """One <M3Api> block - a single program/transaction call."""
    program: str                # e.g. "MMS301MI"
    transaction: str            # e.g. "LstStockTakeDet"
    inputs: list[ClassicInputField] = field(default_factory=list)
    cells: list[ClassicCell] = field(default_factory=list)  # only meaningful for Export blocks

    # Dependency info, only present on Import's <ImportDetail> transactions
    based_on_transaction: Optional[str] = None   # e.g. "MMS301MI.UpdStockTake"
    based_on_failed: Optional[bool] = None       # True/False from TransactionFailed="..."

    # Conditional-execution info, only present on Import's <ImportDetail>
    # transactions. See ClassicRowFilter - currently parsed and carried
    # through, but NOT YET represented in Live's output JSON (see
    # build_live_workflow and KNOWN GAPS at the bottom).
    row_filters: list[ClassicRowFilter] = field(default_factory=list)

    # Only present on the Export block's <LstApi>, e.g. MaxRows="100000".
    # Live's own examples show a matching maxRows on the corresponding
    # list-style transaction, so we carry this through rather than
    # hardcoding a guessed default.
    max_rows: Optional[int] = None


@dataclass
class ClassicWorkflow:
    """The whole parsed Classic file."""
    label: str
    export_transactions: list[ClassicTransaction] = field(default_factory=list)
    import_transactions: list[ClassicTransaction] = field(default_factory=list)
    target_sheet_name: Optional[str] = None   # from <Export TargetSheetName="...">
    output_start_row: Optional[int] = None    # from <LstApi OutputStartRow="...">, i.e. where data rows begin


def _parse_m3api(m3api_el, cells_el_list=None) -> ClassicTransaction:
    txn = ClassicTransaction(
        program=m3api_el.get("Program"),
        transaction=m3api_el.get("Transaction"),
    )
    for input_el in m3api_el.findall("InputField"):
        txn.inputs.append(ClassicInputField(
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
            field_name = value.lstrip("*")  # "*WHLO" -> "WHLO"
            txn.cells.append(ClassicCell(
                column=cell_el.get("Column"),
                field_name=field_name,
                data_type=cell_el.get("Type"),
            ))
    return txn


def _parse_row_filters(detail_el) -> list[ClassicRowFilter]:
    """
    Parse every <RowFilterEQ>/<RowFilterNE> block found anywhere inside
    an <ImportDetail> element.

    Searched recursively (".//") rather than as direct children, since
    we haven't confirmed whether these sit as a sibling of <M3Api> or
    nested inside it - a recursive search finds them either way without
    needing to guess the exact placement.

    Each wrapper can contain one or more <RowFilter> child elements; we
    treat multiple children within one wrapper as AND'd conditions
    (unconfirmed - every real example seen so far has exactly one child
    per wrapper).
    """
    filters: list[ClassicRowFilter] = []
    for wrapper_tag, operator in (("RowFilterEQ", "EQ"), ("RowFilterNE", "NE")):
        for wrapper_el in detail_el.findall(f".//{wrapper_tag}"):
            for rf_el in wrapper_el.findall("RowFilter"):
                filters.append(ClassicRowFilter(
                    operator=operator,
                    column=rf_el.get("Column"),
                    value=rf_el.get("Value"),
                    label=rf_el.get("Label"),
                    is_hidden=(rf_el.get("IsHidden", "").lower() == "true"),
                    is_lower=(rf_el.get("IsLower", "").lower() == "true"),
                ))
    return filters


def parse_classic_xml(path: str) -> ClassicWorkflow:
    tree = ET.parse(path)
    root = tree.getroot()

    export_el = root.find("Export")
    import_el = root.find("Import")

    label = (export_el.get("Label") if export_el is not None
             else import_el.get("Label") if import_el is not None
             else "Untitled")

    wf = ClassicWorkflow(label=label)

    if export_el is not None:
        wf.target_sheet_name = export_el.get("TargetSheetName")

    # --- Export block: LstApi (primary) + GetApi (chained enrichment) ---
    if export_el is not None:
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

        get_api_el = export_el.find("GetApi")
        if get_api_el is not None:
            m3api_el = get_api_el.find("M3Api")
            cells = get_api_el.findall("Cell")
            wf.export_transactions.append(_parse_m3api(m3api_el, cells))

    # --- Import block: one or more ImportDetail steps, in document order ---
    if import_el is not None:
        for detail_el in import_el.findall("ImportDetail"):
            m3api_el = detail_el.find("M3Api")
            txn = _parse_m3api(m3api_el)
            txn.row_filters = _parse_row_filters(detail_el)
            wf.import_transactions.append(txn)

    return wf


# ============================================================
# STAGE 2: BUILD - intermediate representation -> Live JSON
# ============================================================

def _new_id() -> str:
    """
    Generate an ID in the same shape Live uses for transaction instances,
    e.g. "xN9MwyGBpt". We don't know Live's exact generation algorithm,
    but the shape (leading 'x' + 10 mixed-case alphanumeric chars) matches
    every example we've captured, and Live has not indicated IDs need to
    follow any particular scheme - just be unique within the workflow.
    """
    chars = string.ascii_letters + string.digits
    return "x" + "".join(random.choice(chars) for _ in range(10))


def _new_workflow_id() -> str:
    """
    Generate a workflowId in Live's format: "WORKFLOW-" followed by a
    32-character hex string. Confirmed against real examples, e.g.
    "WORKFLOW-0998db275da791c3670ab16dbfa5b320" - this is a standard
    UUID4 with the dashes stripped out.

    IMPORTANT: this satisfies Live's validation (the field must be
    present), but Live does NOT actually use this value - it assigns
    its own workflowId regardless of what's sent, confirmed by a real
    test where the created workflow's actual ID differed from what we
    generated. Any code that needs to reference "the workflow just
    created" must read the ID back from the API response, not assume
    it matches this generated value.
    """
    return "WORKFLOW-" + uuid.uuid4().hex


# --- Minimal field metadata lookup -----------------------------------
# THIS IS THE KNOWN GAP. Live's JSON wants type/length/mandatory/descr
# per field. We don't have a source for this yet (see KNOWN GAPS below).
# Fill in real entries as you discover them (e.g. from a captured Live
# "list available fields for MMS301MI.LstStockTakeDet" network call),
# or replace this with a real lookup against an M3 metadata source.
#
# Key: (program, transaction, field_name) -> metadata dict
M3_FIELD_METADATA = {
    ("MMS301MI", "LstStockTakeDet", "WHLO"): dict(type="STRING", length="3", mandatory=True, descr="Warehouse"),
    ("MMS301MI", "LstStockTakeDet", "CONO"): dict(type="NUMERIC", length="3", mandatory=True, descr="Company"),
    # ... add more as you confirm them
}


def _field_metadata(program: str, transaction: str, field_name: str, overrides: Optional[dict] = None) -> dict:
    key = (program, transaction, field_name)
    if overrides and key in overrides:
        return overrides[key]
    if key in M3_FIELD_METADATA:
        return M3_FIELD_METADATA[key]
    # Explicit placeholder rather than a silent guess - easy to grep for.
    return dict(type="UNKNOWN", length="0", mandatory=False,
                descr=f"TODO: metadata not found for {program}.{transaction}.{field_name}")


# M3 field type codes -> Live's own type vocabulary. Confirmed against
# real captured data: A (alphanumeric), N (numeric), D (date) are the
# only codes observed so far. D is mapped to NUMERIC as a reasonable
# guess (M3 typically stores dates as numeric YYYYMMDD), NOT confirmed
# against a real Live-created field of type D.
_M3_TYPE_CODE_MAP = {"A": "STRING", "N": "NUMERIC", "D": "NUMERIC"}


def fetch_live_metadata(connection_id: str, program: str, transaction: str, bearer_token: str) -> dict:
    """
    Query Live's own M3 metadata endpoint for a program/transaction's
    input and output fields, and return them in the same shape
    _field_metadata() returns - i.e. ready to use as an "overrides" dict
    keyed by (program, transaction, field_name).

    This endpoint was discovered by capturing a full HAR of Live's own
    workflow builder in action - it's exactly what powers the "pick a
    transaction, see its fields populate" behaviour in the UI, and
    directly resolves the field-metadata gap (previously a hand-
    maintained stub table with "UNKNOWN" placeholders).

    Requires a valid bearer token (the same short-lived kind captured
    from the browser elsewhere in this workflow) - this makes a real
    network call, so it will fail once the token expires.
    """
    overrides = {}
    for direction in ("I", "O"):
        details = f"FIELDS#{program}#{transaction}#{direction}"
        url = f"https://api.vince.live/metadata/{connection_id}?details={urllib.parse.quote(details, safe='')}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"  Warning: metadata lookup failed for {program}.{transaction} ({direction}): HTTP {e.code}")
            continue
        except urllib.error.URLError as e:
            print(f"  Warning: metadata lookup failed for {program}.{transaction} ({direction}): {e}")
            continue

        for item in data.get("items", []):
            for f in item.get("data", []):
                name = f.get("name")
                if not name:
                    continue
                overrides[(program, transaction, name)] = {
                    "type": _M3_TYPE_CODE_MAP.get(f.get("type"), "UNKNOWN"),
                    "length": f.get("length", "0"),
                    "mandatory": f.get("mandatory") == "1",
                    "descr": f.get("descr", name),
                }
    return overrides


def fetch_metadata_for_workflow(wf: "ClassicWorkflow", connection_id: str, bearer_token: str) -> dict:
    """
    Fetch live field metadata for every distinct (program, transaction)
    pair used anywhere in a parsed Classic workflow. Returns a single
    merged overrides dict, ready to pass into build_live_workflow().
    """
    overrides = {}
    seen_pairs = set()
    for txn in wf.export_transactions + wf.import_transactions:
        pair = (txn.program, txn.transaction)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        print(f"  Fetching metadata for {txn.program}.{txn.transaction}...")
        overrides.update(fetch_live_metadata(connection_id, txn.program, txn.transaction, bearer_token))
    return overrides


def _classify_source(raw_value: str, explicit_source: Optional[str],
                      id_by_txn_key: dict, field_name: Optional[str] = None,
                      export_column_to_field: Optional[dict] = None) -> dict:
    """
    Work out a Live-style input descriptor from a Classic *Value string.

    Returns a dict with at least: source, value, and (if relevant) ref.

    Classic patterns we handle:
      "1"                              -> Source not given but looks like a literal -> CONSTANT
      (Source="Constant" explicitly)   -> CONSTANT
      "*A", "*B", ...                  -> single-letter column ref. If that
                                           column is ALSO the Export block's
                                           output for the SAME field name,
                                           this is really the same value
                                           flowing from the earlier API call
                                           via the spreadsheet - redirect to
                                           a direct API-chain reference
                                           instead (confirmed correct: in
                                           the sample file, every such
                                           column matches by name except
                                           one genuine input-only column).
                                           Otherwise, a genuine EXCEL/COLUMN
                                           reference.
      "*Field1", "*Field2", "*Field3"  -> Classic's fixed reference cells,
                                           confirmed -> CLIENT trigger input.
      "*PROGRAM.Transaction.FIELD"     -> chained value from another transaction's output -> API
    """
    if explicit_source and explicit_source.lower() == "constant":
        return {"source": "CONSTANT", "value": raw_value}

    if not raw_value.startswith("*"):
        # No leading '*' and no explicit Source -> treat as a literal constant
        return {"source": "CONSTANT", "value": raw_value}

    ref = raw_value[1:]  # strip leading '*'

    if ref.startswith("Field"):
        # CONFIRMED (and corrected) via a real execution error:
        # "SqliteError: no such column: x__contextHeader.Field3". The
        # runtime trigger header is keyed by the REAL M3 field name
        # (e.g. "CONO"), not Classic's placeholder ("Field3"). Our
        # earlier theory that "Live rewrites this on save" was
        # half-right: it's the UI's OWN save logic that recomputes the
        # correct value, not something the server does automatically -
        # so a workflow created via the API and never manually resaved
        # keeps the literal wrong value and fails at execution time.
        # We now generate the correct value ourselves from the start.
        return {"source": "CLIENT", "value": field_name or ref}

    if "." in ref:
        # e.g. "MMS301MI.LstStockTakeDet.WHLO" -> chained API reference
        program, transaction, fname = ref.split(".")
        producing_id = id_by_txn_key.get((program, transaction))
        if producing_id is None:
            return {"source": "API", "value": ref, "_needs_review": f"Could not resolve producing transaction for {ref}"}
        return {
            "source": "API",
            "value": f"{producing_id}.output.{fname}",
            "ref": {"apiId": producing_id, "name": fname, "type": "OUTPUT"},
        }

    # Single letter (or short code) -> spreadsheet column reference.
    # Check first whether this is really a same-value passthrough from an
    # earlier export call, rather than a genuine Excel-sourced input.
    if export_column_to_field and ref in export_column_to_field:
        producing_id, exported_field_name = export_column_to_field[ref]
        if exported_field_name == field_name:
            return {
                "source": "API",
                "value": f"{producing_id}.output.{exported_field_name}",
                "ref": {"apiId": producing_id, "name": exported_field_name, "type": "OUTPUT"},
            }

    return {"source": "EXCEL", "sourceType": "COLUMN", "value": ref}


def build_live_workflow(wf: ClassicWorkflow, connection_id: str, tenant_id: str, environment_id: str, metadata_overrides: Optional[dict] = None) -> dict:
    """
    Build the workflowConfig + stepConfig structure Live's API expects.
    This mirrors the shape we captured from real GET /workflows/{id}
    responses; fields not required for creation (workflowArn, stats,
    definitionAsl, etc.) are intentionally left out - those appear to be
    generated by Live's backend, not sent by the client.
    """
    step_id = "m3_api_1"  # single M3 API step containing all transactions,
                          # matching what we observed Live do for multi-
                          # transaction workflows

    all_txns = wf.export_transactions + wf.import_transactions

    # IMPORTANT: give every transaction a unique ID *per occurrence*, not
    # per (program, transaction) pair. Classic workflows can legitimately
    # call the same transaction more than once (e.g. UpdStockTake appears
    # twice in the stock-take example: once as the initial attempt, once
    # after a fallback Add succeeds). Keying by name alone would silently
    # collapse those into a single step and lose one of them.
    txn_ids = [_new_id() for _ in all_txns]

    # id -> ClassicTransaction, needed later to look up (program,
    # transaction) for a given producing transaction id when synthesizing
    # a missing output field declaration.
    id_to_txn = {txn_id: txn for txn, txn_id in zip(all_txns, txn_ids)}

    # All column letters already spoken for by a real Export mapping -
    # used to allocate a harmless, unused column for any output field we
    # have to synthesize (see below).
    all_used_columns = {cell.column for txn in wf.export_transactions for cell in txn.cells}

    # Column letter -> (producing export transaction id, field name).
    # Used to detect when a Classic "*<letter>" input reference is really
    # a same-value passthrough from an earlier export call, rather than a
    # genuine spreadsheet-sourced input - see _classify_source.
    export_txn_ids_early = txn_ids[:len(wf.export_transactions)]
    export_column_to_field = {}
    for txn, txn_id in zip(wf.export_transactions, export_txn_ids_early):
        for cell in txn.cells:
            export_column_to_field[cell.column] = (txn_id, cell.field_name)

    # References in Classic XML (both chained API values like
    # "*MMS301MI.LstStockTakeDet.WHLO" and dependencies via
    # BasedOnTransaction) are written by transaction *name*, not by a
    # unique ID. When a name is reused, Classic's own top-to-bottom
    # document order is what disambiguates which occurrence is meant -
    # a reference always means "the nearest matching transaction defined
    # earlier in the file". We reproduce that by building this lookup
    # incrementally as we walk through transactions in order, rather than
    # building the whole thing up front.
    seen_so_far = {}

    step_config_transactions = {}
    client_field_info = {}   # field name -> {length, dataType, apiId}, for building trigger fields
    client_field_first_declared = {}  # field name -> id of the FIRST transaction to use it as CLIENT
    constants = {}           # workflowConfig.constants
    # Collected here, applied to excel_1 after the main loop. Confirmed via
    # a real diagnostic workflow ("Claude test INPUT") that an
    # EXCEL/COLUMN-sourced INPUT field needs its own entry in
    # excel_1.sourceSheet.fields.columns, shaped exactly like an output
    # entry but with fieldId "<txnId>.input.<field>" instead of
    # "<txnId>.output.<field>".
    input_column_candidates = []  # list of (column, field_name, txn_id, program, transaction)

    for order, (txn, txn_id) in enumerate(zip(all_txns, txn_ids), start=1):
        input_dict = {}

        for input_field in txn.inputs:
            classification = _classify_source(input_field.raw_value, input_field.source, seen_so_far,
                                               field_name=input_field.name,
                                               export_column_to_field=export_column_to_field)
            meta = _field_metadata(txn.program, txn.transaction, input_field.name, metadata_overrides)

            entry = {
                "name": input_field.name,
                "descr": input_field.label or meta["descr"],
                "type": meta["type"],
                "length": meta["length"],
                "mandatory": meta["mandatory"],
                "fieldType": "I",
                "isMapped": True,
                "hasGrouping": False,
                **classification,
            }
            input_dict[input_field.name] = entry

            if classification.get("source") == "EXCEL" and classification.get("sourceType") == "COLUMN":
                input_column_candidates.append((classification["value"], input_field.name, txn_id, meta))

            if classification.get("source") == "CONSTANT":
                const_key = f"CONSTANT_{input_field.name}"
                constants[const_key] = classification["value"]
                entry["value"] = const_key

            if classification.get("source") == "CLIENT":
                if input_field.name not in client_field_first_declared:
                    # First time this field name is used as CLIENT - this
                    # becomes the canonical declaration. No ref needed;
                    # matches the real example where the first occurrence
                    # (e.g. LstStockTakeDet's WHLO) carries no ref at all.
                    client_field_first_declared[input_field.name] = txn_id
                else:
                    # A later occurrence of the same field name (e.g. the
                    # same *Field3 used across four different Classic
                    # transactions). WITHOUT this ref, Live has no way to
                    # know these are meant to be one shared trigger value
                    # - confirmed by a real test where four CONO
                    # occurrences with no ref became four separate fields
                    # (CONO, CONO_1, CONO_2, CONO_3) instead of one shared
                    # field. Value stays as-is (Live rewrites it
                    # regardless); only the ref is added.
                    entry["ref"] = {
                        "apiId": client_field_first_declared[input_field.name],
                        "type": "INPUT",
                        "name": input_field.name,
                    }

                # Overwrite on each occurrence - the one real example we
                # have shows the trigger field's "apiId" pointing at the
                # LAST transaction to use that field as CLIENT, not the
                # first. We don't know for certain why (possibly just
                # whichever one was last edited in the UI), but matching
                # the observed pattern is the safest bet until we learn
                # otherwise.
                client_field_info[input_field.name] = {
                    "length": meta["length"],
                    "dataType": meta["type"],
                    "apiId": txn_id,
                }

        output_dict = {}
        for cell in txn.cells:
            meta = _field_metadata(txn.program, txn.transaction, cell.field_name, metadata_overrides)
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
            "id": txn_id,
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
            # inert (no transactions visible, no way to add one). Every
            # transaction in a real, UI-built, correctly-rendering
            # workflow we've captured has this field. All of our captured
            # examples used "ADD" here regardless of whether the M3
            # transaction itself lists, gets, updates, or adds - so we
            # do NOT try to infer LIST/GET/ADD/UPD from the transaction
            # name (an earlier version of this script did, incorrectly -
            # see KNOWN GAPS). "ADD" is used as a fixed default because
            # it's the only value we've seen in a multi-transaction
            # example; still unconfirmed for single-transaction workflows.
            "type": "ADD",
        }

        if txn.max_rows is not None:
            # Matches the one real example we have of a list-style
            # transaction: it carried maxRows (taken directly from
            # Classic's own <LstApi MaxRows="..."> attribute),
            # defaultInputConfig, and hasSpreadSheetInput alongside it.
            txn_entry["maxRows"] = txn.max_rows
            txn_entry["defaultInputConfig"] = {}
            txn_entry["hasSpreadSheetInput"] = False

        # NOTE: txn.row_filters is intentionally NOT handled here.
        # Confirmed (via a real manually-built Live workflow, "Filter
        # tests - Input and output") that Live DOES have a native
        # mechanism for this - a GENERIC_FILTER step that runs before
        # the M3 API step, filtering on raw Excel row data. But because
        # that step runs ONCE for the whole M3 API step, it can only
        # safely represent row_filters that apply UNIFORMLY to every
        # transaction in the workflow - not filters that are only
        # attached to some transactions (e.g. an authority-check call
        # that always runs, feeding a column that gates a later,
        # different transaction). That decision needs visibility across
        # ALL transactions, so it's made once, after this loop - see
        # "Generic Filter" below.

        if txn.based_on_transaction:
            ref_program, ref_transaction = txn.based_on_transaction.split(".")
            ref_id = seen_so_far.get((ref_program, ref_transaction))
            if ref_id:
                txn_entry["basedOnTransaction"] = ref_id
                txn_entry["runOnFailure"] = bool(txn.based_on_failed)
            else:
                txn_entry["_needs_review"] = f"Could not resolve dependency target {txn.based_on_transaction}"

        step_config_transactions[txn_id] = txn_entry

        # Record this transaction as the most recent occurrence of its
        # name, AFTER processing it - so it can never resolve as its own
        # dependency, and so later occurrences of the same name correctly
        # shadow it for anything that comes after.
        seen_so_far[(txn.program, txn.transaction)] = txn_id

    # POST-PROCESSING FIX, confirmed by a real execution error:
    # "SqliteError: no such column: <id>_MMS301MI_LstStockTakeDet.CONO".
    # A chained API reference (source=API, ref.type=OUTPUT) can point to
    # a field that Classic never chose to export to a visible spreadsheet
    # column (e.g. CONO, which M3 always returns but which wasn't one of
    # the columns in the sample file). Our own output-building only ever
    # covered fields with an explicit Export <Cell> mapping, so such a
    # field was silently missing from its producing transaction's own
    # "output" dict - present as a reference elsewhere, but never
    # declared where it's produced. Live's execution engine needs it
    # declared on the producer to resolve the reference at runtime.
    # Fixed by scanning every chained reference after the fact and
    # synthesizing a minimal output declaration (on a harmless, unused
    # column letter) for anything missing.
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

    for txn_entry in list(step_config_transactions.values()):
        for input_field in txn_entry.get("input", {}).values():
            ref = input_field.get("ref")
            if not ref or ref.get("type") != "OUTPUT":
                continue
            producer_id = ref["apiId"]
            field_name = ref["name"]
            producer_entry = step_config_transactions.get(producer_id)
            if producer_entry is None or field_name in producer_entry.get("output", {}):
                continue  # already declared, or producer not found (shouldn't happen)

            # Make sure we don't hand out a column already claimed by an
            # input-only Excel reference (e.g. STDI/"P") - all_used_columns
            # was only seeded from Export cells, so top it up here with
            # every column any input field actually ended up using.
            all_used_columns.update(col for col, _, _, _ in input_column_candidates)

            producer_txn = id_to_txn[producer_id]
            meta = _field_metadata(producer_txn.program, producer_txn.transaction, field_name, metadata_overrides)
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

    # --- Generic Filter step (Classic RowFilterEQ/RowFilterNE) ---
    # Confirmed real shape via a manually-built Live test workflow
    # ("Filter tests - Input and output", captured 2026-08-04): Live has
    # a GENERIC_FILTER step type that sits between trigger_1 and the M3
    # API step, filtering raw Excel row data BEFORE any M3 transaction
    # runs, e.g.:
    #   generic_filter_1 stepConfig: {"all": [
    #       {"clientKey": "Filter_BD", "operator": "equal", "fact": "BD",
    #        "from": "Row", "source": "CLIENT", "defaultValue": "OK",
    #        "value": "{{ header.Filter_BD }}", "dataType": "STRING",
    #        "logicalType": "AND"},
    #       ...
    #   ]}
    # This is a strong structural match for RowFilterEQ/RowFilterNE -
    # both gate on a raw import column's value. (The same capture also
    # confirmed a second, DIFFERENT feature - "M3 Filter" / m3_filter_1
    # - which filters a TRANSACTION'S OUTPUT rows after the API call;
    # CUNM/CONO were confirmed as real output fields of the transaction
    # it was tested against. That one doesn't correspond to anything in
    # Classic we've seen and is not used here.)
    #
    # THE CATCH: Generic Filter runs once, before the ENTIRE M3 API
    # step - it cannot gate one transaction while letting a different
    # one in the same step run unconditionally. So this is only safe to
    # auto-translate when every transaction in the workflow shares the
    # exact same row_filters (including the trivial single-transaction
    # case). If only some transactions carry row_filters - e.g. an
    # authority-check transaction that always runs, feeding a column
    # that gates a later, different transaction (the real shape of
    # classic_v1.xml, per the project summary) - applying Generic
    # Filter would silently change behaviour, so that case stays
    # flagged per-transaction instead of guessed at.
    filtered_txns = [(t, tid) for t, tid in zip(all_txns, txn_ids) if t.row_filters]
    generic_filter_rowfilters: list[ClassicRowFilter] = []

    if filtered_txns and len(filtered_txns) == len(all_txns) and all(
        t.row_filters == filtered_txns[0][0].row_filters for t, _ in filtered_txns
    ):
        # Safe, uniform case - every transaction wants the same gate.
        generic_filter_rowfilters = filtered_txns[0][0].row_filters
    else:
        # Unsafe case - flag each affected transaction individually.
        for txn, txn_id in filtered_txns:
            conditions = []
            for rf in txn.row_filters:
                op_symbol = "==" if rf.operator == "EQ" else "!="
                lower_note = ", case-insensitive per IsLower (meaning unconfirmed)" if rf.is_lower else ""
                conditions.append(
                    f"column {rf.column} {op_symbol} {rf.value!r}{lower_note} [{rf.label or 'unlabeled'}]"
                )
            note = (
                "UNSUPPORTED CONDITIONAL LOGIC (RowFilter): Classic only runs this "
                "transaction when " + " AND ".join(conditions) + ". Live DOES have a "
                "native Generic Filter step for this, but it applies to the WHOLE "
                "M3 API step, not a single transaction - and this filter is not "
                "shared identically by every transaction in this workflow, so "
                "auto-translating it here would incorrectly also gate transactions "
                "Classic always ran unconditionally. This transaction currently "
                "runs UNCONDITIONALLY in Live. Needs manual handling, or a converter "
                "enhancement that can split transactions across multiple M3 API "
                "steps so each gets its own Generic Filter."
            )
            step_config_transactions[txn_id]["_needs_review"] = note

    generic_filter_step_config = None
    generic_filter_trigger_fields = []
    if generic_filter_rowfilters:
        generic_filter_step_config = {
            "all": [
                {
                    "clientKey": f"Filter_{rf.column}",
                    "operator": "equal" if rf.operator == "EQ" else "notEqual",
                    "dataType": "STRING",  # only confirmed for string-like columns so far
                    "logicalType": "AND",  # Classic gives us no confirmed OR case
                    "from": "Row",
                    "defaultValue": rf.value,
                    "source": "CLIENT",
                    "fact": rf.column,
                    "value": f"{{{{ header.Filter_{rf.column} }}}}",
                }
                for rf in generic_filter_rowfilters
            ],
            "_needs_review": (
                "Generic Filter fields are created as EDITABLE trigger inputs "
                "(source=CLIENT) with Classic's value as the default - the only "
                "confirmed pattern available. Classic marked these filters "
                "IsHidden=\"true\" (meant to be fixed and invisible to whoever runs "
                "the workflow), which this does not replicate: anyone running this "
                "workflow in Live can see and change the filter value(s) at "
                "execution time. No confirmed fixed/non-editable variant exists yet. "
                "Classic's IsLower attribute is also not applied here (its meaning "
                "is unconfirmed)."
            ),
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
                                      # letter, not Classic's own Label -
                                      # unconfirmed whether a nicer label
                                      # can be supplied instead.
                "placeHolder": "Specify  to filter",  # matches the exact
                                                       # (oddly double-
                                                       # spaced) real
                                                       # example for a
                                                       # generic filter
                                                       # field with no
                                                       # description set.
                "dataType": "STRING",
                "length": "15",
                "mandatory": True,
                "type": "text",
                "formGroupId": 2,
                "elementPosition": position,
                "value": rf.value,
            }
            for position, rf in enumerate(generic_filter_rowfilters, start=1)
        ]

    # --- Trigger fields (client-sourced inputs) ---
    # formGroupId and elementPosition are NOT cosmetic extras - a field
    # missing formGroupId has no group to render under (we only declare
    # one group, "fields", with groupId 1, plus "filters"/groupId 2 when
    # Generic Filter fields exist - confirmed via the same real capture),
    # which is the most likely explanation for a real test workflow
    # showing zero trigger fields despite the fields existing in the
    # underlying data.
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
    if generic_filter_rowfilters:
        trigger_form_groups.append({"groupId": 2, "groupName": "filters"})

    workflow_inputs = {name: {"dataType": "STRING", "validation": {"required": True}}
                       for name in client_field_info}
    for rf in generic_filter_rowfilters:
        workflow_inputs[f"Filter_{rf.column}"] = {"dataType": "STRING", "validation": {"required": True}}

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
    # when we have a safe, uniform set of row filters to represent -
    # confirmed real step type/target/name via the same capture.
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
        "hasSpreadSheetInput": bool(any(t.cells for t in all_txns) or input_column_candidates),
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

    # --- Excel step: build real column definitions from the Export
    # block's <Cell> mappings (LstApi + GetApi). We deliberately do NOT
    # use the <Cell> elements that appear as siblings of <M3Api> inside
    # <ImportDetail> - in the sample file, one of those reuses a column
    # letter ("S") that the Export block already assigned to a different
    # field ("BREM" vs "STAG"). We don't yet understand what that
    # ImportDetail-level Cell actually represents, so rather than guess,
    # we leave it out entirely - see KNOWN GAPS at the bottom.
    excel_columns = {}
    export_txn_ids = txn_ids[:len(wf.export_transactions)]  # export txns are always first in all_txns
    for txn, txn_id in zip(wf.export_transactions, export_txn_ids):
        for cell in txn.cells:
            meta = _field_metadata(txn.program, txn.transaction, cell.field_name, metadata_overrides)
            datatype = "NUMERIC" if cell.data_type == "decimal" else meta["type"]
            field_ref = f"{txn_id}.output.{cell.field_name}"
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
    # We assign the plain slot to export/output (since Export is always
    # processed first in Classic's document order) and "-1" to input;
    # the one real example we have shows the opposite assignment
    # (input got the plain slot there), so which side gets which slot
    # may not be load-bearing - flagged here in case it turns out to
    # matter.
    columns_with_dash1_added = set()
    for column, field_name, txn_id, meta in input_column_candidates:
        field_ref = f"{txn_id}.input.{field_name}"
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
            # Column not yet claimed by an export/output mapping - plain slot.
            excel_columns[column] = input_entry
        elif excel_columns[column]["fieldId"] != field_ref and column not in columns_with_dash1_added:
            # Genuine dual-role column, and we haven't already added the
            # "-1" for it (avoids piling up duplicates when multiple
            # import transactions happen to read the same column).
            input_entry["secondaryHeader"] = dict(excel_columns[column]["secondaryHeader"])
            excel_columns[f"{column}-1"] = input_entry
            columns_with_dash1_added.add(column)

    if excel_columns:
        used_columns = sorted(c for c in excel_columns.keys() if "-" not in c)
        start_column, end_column = used_columns[0], used_columns[-1]
        # Classic's OutputStartRow tells us where data rows begin; the
        # two rows immediately above it are consistently used for the
        # primary (label) and secondary (field name) headers in every
        # real example we've captured.
        data_row = wf.output_start_row or 4
        secondary_header_row = data_row - 1
        primary_header_row = data_row - 2
    else:
        start_column = end_column = "A"
        data_row, secondary_header_row, primary_header_row = 4, 3, 2

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
                    "sheetName": wf.target_sheet_name or "Sheet1",
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
                # NOTE: cells.contents is left empty - this is where a
                # fixed, single-value reference (as opposed to a per-row
                # column) would go, e.g. Classic's Field1/2/3 IF they
                # turn out to need this representation. We tested CLIENT
                # trigger fields for these instead and confirmed they
                # render correctly, so this is deliberately left empty
                # rather than guessed at further.
                "cells": {"contents": {}, "styleInfo": {}},
                "sortOrder": [],
            }
        },
    }
    if generic_filter_step_config is not None:
        step_config["generic_filter_1"] = generic_filter_step_config

    return {
        "entity": "Workflow",
        "description": "",
        # THE FIX: hasError is a CLIENT-computed flag, not something the
        # server infers. Confirmed by diffing our creation payload against
        # a real "open workflow, change nothing, hit Save" PUT request:
        # the only difference was this flag flipping from true to false.
        # Live's own UI sends "true" at creation (a blank workflow really
        # is incomplete) and only sends "false" once its own validation
        # decides the configuration looks complete. Since we're
        # bypassing that UI validation entirely, we set it ourselves.
        "hasError": False,
        "tenantId": tenant_id,
        "workflowId": _new_workflow_id(),
        "workflowName": wf.label,
        "workflowAlias": wf.label,
        "active": True,
        # CORRECTED via a captured real creation request from Live's own
        # UI: "STANDARD", not "EXPRESS". Our earlier attempt sent EXPRESS
        # directly at creation and produced a broken, half-provisioned
        # workflow (existed enough to block re-creating the same ID, but
        # unreachable via GET and invisible in the workflow list).
        # EXPRESS appears only in an already-running production workflow
        # we compared against separately - likely something a workflow
        # transitions to later, not a valid value at creation time.
        "workflowType": "STANDARD",
        "labels": [],
        "tags": {
            "connectionId": [connection_id],
            "apiEnvironment": environment_id,
            "environments": [environment_id],
            "primaryEnvironment": environment_id,
        },
        "definition": {
            "workflowConfig": workflow_config,
            "stepConfig": step_config,
        },
    }


# ============================================================
# STAGE 3: ASSEMBLE
# ============================================================

def convert(classic_xml_path: str, connection_id: str, tenant_id: str, environment_id: str, output_path: str,
            bearer_token: Optional[str] = None):
    wf = parse_classic_xml(classic_xml_path)
    wf.label = _next_labeled_name(wf.label)

    metadata_overrides = None
    if bearer_token:
        print("Fetching live field metadata from Live's own metadata endpoint...")
        metadata_overrides = fetch_metadata_for_workflow(wf, connection_id, bearer_token)
        print(f"  Got metadata for {len(metadata_overrides)} fields.")
    else:
        print("No bearer token provided - using static metadata table only (fields not in it will show as UNKNOWN).")

    live_json = build_live_workflow(wf, connection_id, tenant_id, environment_id, metadata_overrides)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(live_json, f, indent=2)
    print(f"Wrote {output_path}")

    # Surface anything flagged for review, front and center.
    flagged = json.dumps(live_json)
    if "_needs_review" in flagged:
        print("\n⚠️  Some fields were flagged for manual review - search the output for \"_needs_review\".")


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (6, 7):
        print("Usage: python classic_to_live_converter.py <classic.xml> <connection_id> <tenant_id> <environment_id> <output.json> [bearer_token]")
        print("  bearer_token is optional - if provided, fetches real M3 field metadata from Live instead of using placeholders.")
        sys.exit(1)
    token = sys.argv[6] if len(sys.argv) == 7 else None
    convert(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], token)


# ============================================================
# KNOWN GAPS - things we deliberately have NOT solved yet
# ============================================================
#
# 1. FIELD METADATA (type/length/mandatory/descr)
#    Classic XML doesn't carry this; Live's JSON wants it. We need a
#    source of truth for "what does field WHLO on MMS301MI.LstStockTakeDet
#    look like". Two realistic options:
#      a) Live appears to have system-managed workflows for this
#         (M3MetadataImport, M3ProgramsLookup - discovered in a workflow
#         listing, not yet investigated). Capturing the network calls
#         Live's own UI makes when you pick a transaction and its field
#         list populates would very likely reveal how to query this
#         directly, rather than hand-maintaining it.
#      b) Failing that, build a static lookup table by hand, covering
#         only the programs/transactions your workflows actually use.
#    Until resolved, M3_FIELD_METADATA below is the stand-in, and
#    unresolved fields are clearly marked "UNKNOWN" rather than guessed.
#    NOTE: workflow creation and rendering have both tolerated "UNKNOWN"
#    without complaint so far - this gap has not yet been shown to block
#    anything. It may only matter once a workflow is actually executed
#    against real M3 data (untested - see gap 4).
#
# 2. THE *FieldN* MAPPING (Classic's fixed reference cells) - RESOLVED
#    We mapped *Field1/2/3 to CLIENT (manual trigger) inputs. This has
#    been confirmed by an actual test: the trigger screen correctly
#    showed all three fields after the formGroupId/elementPosition/apiId
#    fix. The alternative hypothesis (a fixed EXCEL/CELL reference, like
#    the single-cell "A3" pattern seen in one hand-built example) does
#    not appear to be necessary, at least for this file.
#
# 3. THE excel_1 STEP'S COLUMN DEFINITIONS - PARTIALLY RESOLVED
#    Now generated from the Export block's <Cell> mappings (LstApi +
#    GetApi), which are the only unambiguous source of column truth.
#    NOT yet handled: the <Cell> elements that appear as siblings of
#    <M3Api> inside <ImportDetail> (e.g. "<Cell Value='*STAG'
#    Column='S'/>" under the UpdStockTake ImportDetail). In the sample
#    file, this reuses column letter "S", which the Export block already
#    assigned to a different field ("BREM") - a real conflict we don't
#    understand yet. Left unparsed rather than guessed at. Worth asking
#    directly: what does a Cell inside ImportDetail (as opposed to inside
#    LstApi/GetApi) actually control in Classic?
#
# 4. NOT YET TESTED END TO END
#    Workflow creation and UI rendering are now confirmed working
#    end-to-end for structure (trigger, M3 API steps, dependencies,
#    chained references, Excel columns). Actually clicking Execute and
#    running the workflow against real M3 data has NOT been tried yet.
#    That's the next real test, and the one most likely to surface
#    whether gap 1 (field metadata) actually matters in practice.
#
# 5. ROWFILTEREQ / ROWFILTERNE (conditional execution) - PARSED AND
#    PARTIALLY TRANSLATED, via Live's "Generic Filter" step
#    Confirmed real Classic shape (classic_v1.xml):
#        <RowFilterEQ>
#          <RowFilter Column="BD" Value="OK" Label="Deliverymethod filter"
#                     IsHidden="true" IsLower="false" />
#        </RowFilterEQ>
#        <RowFilterNE>
#          <RowFilter Column="BA" Value="x" Label="x" IsHidden="true"
#                     IsLower="true" />
#        </RowFilterNE>
#    Parsed into ClassicTransaction.row_filters and carried through the
#    IR.
#
#    REVISED UNDERSTANDING: an earlier version of this script treated
#    this as entirely unsupported in Live. That was wrong - confirmed
#    via a manually-built Live test workflow ("Filter tests - Input and
#    output") that a native GENERIC_FILTER step exists, sitting between
#    trigger_1 and the M3 API step, filtering raw Excel row data before
#    any transaction runs. Structurally, this is a real match for
#    RowFilterEQ/RowFilterNE. (The same capture also confirmed a
#    different feature, "M3 Filter"/m3_filter_1, which filters a
#    TRANSACTION'S OUTPUT rows after the API call - not a match for
#    anything seen in Classic so far, and not used here.)
#
#    WHAT'S NOW HANDLED: when every transaction in a workflow shares
#    the exact same row_filters (including the common single-
#    transaction case), build_live_workflow() emits a real
#    generic_filter_1 step, wires it into the step chain, and adds the
#    necessary CLIENT trigger fields (Filter_<Column>) in their own
#    form group.
#
#    WHAT'S STILL DEFERRED, deliberately: Generic Filter runs once,
#    before the WHOLE M3 API step - it cannot gate one transaction
#    while letting a different one in the same step run
#    unconditionally. classic_v1.xml's real shape (an always-run
#    authority-check transaction feeding a column that gates a later,
#    different transaction) is exactly this unsafe case, so it is NOT
#    auto-translated - each affected transaction gets a `_needs_review`
#    entry instead, explaining why. Solving that properly would need
#    the converter to split a workflow's transactions across multiple
#    M3 API steps (each with its own Generic Filter) - not yet built.
#
#    ALSO UNCONFIRMED (flagged via a `_needs_review` note on the
#    generic_filter_1 stepConfig itself, not just in this comment):
#      - Classic marks these filters IsHidden="true" (fixed, invisible
#        to whoever runs the workflow). Every real Live example we have
#        uses "source": "CLIENT", which creates an EDITABLE trigger
#        field instead - a real UX/behavioural difference, not just a
#        missing detail. No confirmed fixed/non-editable variant exists
#        yet.
#      - Classic's IsLower attribute (case-insensitive comparison?) has
#        no confirmed counterpart and is not applied.
#      - "dataType": "STRING" and "length": "15" on the generated
#        fields are only confirmed for string-like columns in the one
#        real test we have - no confirmed NUMERIC example yet.
#      - Whether multiple <RowFilter> children within one wrapper
#        should combine as AND is assumed, not confirmed (every real
#        Classic example seen so far has exactly one child per wrapper).
#    Also unconfirmed (low priority): whether the wrapper is a sibling
#    of <M3Api> or nested inside it within <ImportDetail> - the parser
#    searches recursively so it doesn't matter for parsing correctness.