"""
Live JSON as a source. Two separate things live here, deliberately NOT
sharing one code path:

  1. parse_live_json() - a GET /workflows/{id} response -> the shared IR.
     Close to trivial, as expected: Live already states its sources/refs
     explicitly, so there's no inference to do, just a direct mapping.
     This is the generically useful building block - needed for any
     future "regenerate one step" or diff work - but it is NOT used by
     clone_and_retarget() below.

  2. clone_and_retarget() - the operation actually requested for this
     first pass. It works directly on the raw Live JSON, NOT through
     IR -> build.py. Reason: Live JSON already IS the target shape, and
     the IR is deliberately narrower than it (build.py synthesizes
     things like `descr` from metadata lookups rather than preserving
     whatever the source workflow actually had). Round-tripping a full
     clone through IR/BUILD would risk quietly losing or rewriting
     fields BUILD doesn't know how to reconstruct. A straight ID-remap +
     targeting-field swap on the original JSON is lower-risk and is all
     "clone with new IDs, pointed at a different tenant/connection/
     environment" actually needs.
"""

import copy
from typing import Optional

from build import CAPTURED_FILTER_OPERATORS
from envelope import IMPORTED_LABEL, with_imported_label, clear_unused_spreadsheet_flag
from ids import new_id, new_workflow_id
from ir import Cell, InputField, RowFilter, Transaction, Workflow
from source_classic_xml import compose_workflow_name
from state import next_labeled_name


def _find_api_step_id(steps: list) -> Optional[str]:
    """Walk workflowConfig.steps/childSteps looking for the API-type step."""
    for step in steps:
        if step.get("type") == "API":
            return step.get("stepId")
        found = _find_api_step_id(step.get("childSteps", []))
        if found:
            return found
    return None


# --- 1. Live JSON -> IR ---------------------------------------------------

# Live Generic Filter operator value -> the IR's Classic-style operator, for
# every operator whose JSON is captured (build.CAPTURED_FILTER_OPERATORS).
_CLASSIC_OPERATOR_BY_LIVE = {live: classic for classic, live in CAPTURED_FILTER_OPERATORS.items()}


def _filter_conditions(node: dict) -> list:
    """
    Every condition in a Generic Filter step, in order. Live's UI nests them
    left-deep ({"all": [{"all": [a, b]}, c]}) - reading only the top level,
    as this used to, turned a nested block into a condition with no operator
    or column. Refuses what the IR cannot carry rather than guessing: an OR
    ("any" block, or logicalType "OR") and any operator with no Classic
    equivalent (isBlank, stringContains, ...).
    """
    if "any" in node:
        raise ValueError("Generic Filter uses OR ('any'), which the IR's row filters cannot express")
    conditions = []
    for item in node.get("all", []):
        if "all" in item or "any" in item:
            conditions.extend(_filter_conditions(item))
            continue
        if item.get("logicalType") == "OR":
            raise ValueError(f"Generic Filter condition on {item.get('fact')} is OR-joined, "
                             "which the IR's row filters cannot express")
        if item.get("operator") not in _CLASSIC_OPERATOR_BY_LIVE:
            raise ValueError(f"Generic Filter operator {item.get('operator')!r} on {item.get('fact')} "
                             "has no Classic/IR equivalent")
        conditions.append(item)
    return conditions


def parse_live_json(data: dict) -> Workflow:
    workflow_config = data["definition"]["workflowConfig"]
    step_config = data["definition"]["stepConfig"]
    constants = workflow_config.get("constants", {})

    api_step_id = _find_api_step_id(workflow_config.get("steps", [])) or "m3_api_1"
    txn_entries = step_config.get(api_step_id, {})

    # Reverse-engineer Generic Filter, if present. It is a single step
    # gating the WHOLE M3 API step in Live's real shape, which is exactly
    # what Workflow.row_filters means, so it lands there rather than being
    # copied onto every transaction. (It used to be fanned out per
    # transaction because the IR had no block-level slot; build.py then
    # had to recognise the uniform case to fold it back into one step. A
    # round trip now carries it at its real level and cannot degrade into
    # per-transaction review notes just because a transaction was added.)
    generic_row_filters = []
    generic_filter = step_config.get("generic_filter_1")
    if generic_filter:
        for condition in _filter_conditions(generic_filter):
            generic_row_filters.append(RowFilter(
                operator=_CLASSIC_OPERATOR_BY_LIVE[condition.get("operator")],
                column=condition.get("fact"),
                value=condition.get("defaultValue"),
                # A CONSTANT condition is a fixed, non-editable filter - Classic's IsHidden.
                is_hidden=condition.get("source") == "CONSTANT",
            ))

    transactions = []
    for real_id, entry in sorted(txn_entries.items(), key=lambda kv: kv[1].get("executionOrder", 0)):
        inputs = []
        for name, inp in entry.get("input", {}).items():
            source = inp.get("source")
            if source == "CONSTANT":
                literal = constants.get(inp.get("value"), inp.get("value"))
                inputs.append(InputField(name=name, source_kind="CONSTANT", value=literal, label=inp.get("descr")))
            elif source == "EXCEL":
                inputs.append(InputField(name=name, source_kind="EXCEL_COLUMN", value=inp.get("value"),
                                          label=inp.get("descr")))
            elif source == "CLIENT":
                # A "ref" here just links repeated occurrences back to
                # Live's own real ids - build.py re-derives that linkage
                # itself from repeated (source_kind=CLIENT, name), so it
                # isn't carried through.
                inputs.append(InputField(name=name, source_kind="CLIENT", value=inp.get("value") or name,
                                          label=inp.get("descr")))
            elif source == "API":
                ref = inp.get("ref") or {}
                producer_ir_id = ref.get("apiId")  # Live's real id, reused directly as ir_id below
                if producer_ir_id:
                    inputs.append(InputField(name=name, source_kind="API_CHAIN", label=inp.get("descr"),
                                              ref={"producer_ir_id": producer_ir_id, "name": ref.get("name")}))
                else:
                    inputs.append(InputField(name=name, source_kind="API_CHAIN", value=inp.get("value"),
                                              label=inp.get("descr"),
                                              needs_review=f"API-sourced field {name!r} has no ref to resolve"))
            else:
                inputs.append(InputField(name=name, source_kind="CONSTANT", value=inp.get("value"),
                                          label=inp.get("descr"),
                                          needs_review=f"Unrecognized source {source!r} on field {name!r}"))

        cells = [
            Cell(column=out["value"], field_name=name, data_type=None)
            for name, out in entry.get("output", {}).items()
            if out.get("source") == "EXCEL" and out.get("sourceType") == "COLUMN"
        ]

        transactions.append(Transaction(
            ir_id=real_id,  # reuse Live's own instance id - a Live-JSON round trip needs no remapping
            program=entry.get("programName"),
            transaction=entry.get("transactionName"),
            inputs=inputs,
            cells=cells,
            based_on=entry.get("basedOnTransaction"),
            based_on_failed=entry.get("runOnFailure"),
            max_rows=entry.get("maxRows"),
        ))

    excel_fields = step_config.get("excel_1", {}).get("sourceSheet", {}).get("fields", {})

    return Workflow(
        label=data.get("workflowName") or workflow_config.get("name"),
        transactions=transactions,
        target_sheet_name=excel_fields.get("sheetName"),
        output_start_row=excel_fields.get("dataRow"),
        row_filters=generic_row_filters,
    )


# --- 2. Clone + retarget (direct JSON transform, no IR involved) ---------

def _remap_field_ref(value: Optional[str], id_map: dict) -> Optional[str]:
    """Rewrite a '<oldId>.input.X' / '<oldId>.output.X' style string."""
    if not value or "." not in value:
        return value
    old_id, rest = value.split(".", 1)
    return f"{id_map.get(old_id, old_id)}.{rest}"


def clone_and_retarget(data: dict, connection_id: str, tenant_id: str, environment_id: str,
                        preserve_name: bool = False, override_name: Optional[str] = None) -> dict:
    """
    Clone an existing Live workflow (a GET /workflows/{id} response),
    regenerate every transaction's instance ID plus the workflow ID, and
    repoint it at a different tenant/connection/environment. Everything
    else in the source JSON - field descriptions, labels, generic filter
    config, whatever else Live's own UI put there - is preserved exactly
    as captured, not regenerated.

    The workflow name is passed through state.next_labeled_name(), same
    as every other source, so repeated clones get distinct, trackable
    names instead of colliding.

    `preserve_name=True` opts out of that, keeping the source workflow's
    name exactly. Used by the app importer: an app is a coherent set, and
    its workflows have to arrive in the target tenant under the names the
    app's own docs, prompts and cross-references use - a silent
    "Pick List 2" would break that. The trade-off is real and deliberate:
    importing the same app twice into the same tenant produces two
    workflows with identical names, which the caller is expected to warn
    about rather than paper over.

    `override_name`, when given, replaces the base name a standalone clone
    is built from - the source workflow's own name becomes the "function"
    half appended after it (source_classic_xml.compose_workflow_name), e.g.
    override_name="Q4 Rollout" on a source named "Price Update" produces
    "Q4 Rollout - Price Update". Ignored when `preserve_name=True` (app
    import never passes it - see above).

    IMPORTANT, confirmed via a real POST 400 against a real GET response:
    GET and POST do NOT share a body shape. GET returns server-computed
    fields (stats, workflowArn, definitionAsl, created/modified/
    createdBy/modifiedBy, overview) that POST's own validation rejects -
    e.g. stats.lastExecutionId comes back null, and Live's creation
    schema wants a string, not null, for whatever it does accept. GET
    also OMITS description/labels entirely, even though creation requires
    them (matches the original converter's own confirmed required-field
    list). So this builds a fresh top-level document containing only the
    fields creation actually wants, rather than deep-copying and mutating
    the full GET response - the same "leave out fields Live's backend
    generates" rule the original single-file converter already followed
    for fresh-build workflows now applies here too.
    """
    source_definition = copy.deepcopy(data["definition"])
    workflow_config = source_definition["workflowConfig"]
    step_config = source_definition["stepConfig"]

    api_step_id = _find_api_step_id(workflow_config.get("steps", [])) or "m3_api_1"
    txn_entries = step_config.get(api_step_id, {})

    id_map = {old_id: new_id() for old_id in txn_entries}

    new_txn_entries = {}
    for old_id, entry in txn_entries.items():
        new_id_value = id_map[old_id]
        entry["id"] = new_id_value
        entry["connectionId"] = connection_id

        if entry.get("basedOnTransaction") in id_map:
            entry["basedOnTransaction"] = id_map[entry["basedOnTransaction"]]

        for input_entry in entry.get("input", {}).values():
            ref = input_entry.get("ref")
            if ref and ref.get("apiId") in id_map:
                ref["apiId"] = id_map[ref["apiId"]]
            # A chained input also carries the producer's id inside its
            # `value` string ("<id>.output.<FIELD>"). Leaving that on the
            # OLD id leaves the input pointing at nothing in the new workflow.
            if input_entry.get("source") == "API" and isinstance(input_entry.get("value"), str):
                input_entry["value"] = _remap_field_ref(input_entry["value"], id_map)

        new_txn_entries[new_id_value] = entry
    step_config[api_step_id] = new_txn_entries

    # The builder also records each chained input here, keyed
    # "<consumer>.input.<F>" -> "<producer>.output.<F>". Remap both sides;
    # an entry naming a transaction this workflow no longer has is dropped.
    references = workflow_config.get("apiFieldReferences")
    if isinstance(references, dict):
        workflow_config["apiFieldReferences"] = {
            _remap_field_ref(key, id_map): _remap_field_ref(value, id_map)
            for key, value in references.items()
            if str(key).split(".")[0] in id_map and str(value).split(".")[0] in id_map}

    # An Expand workflow's Excel step has a targetSheet as well (the sheet the
    # expanded rows land on), whose columns name transaction ids too.
    for side in ("sourceSheet", "targetSheet"):
        excel_fields = step_config.get("excel_1", {}).get(side, {}).get("fields", {})
        for column_entry in excel_fields.get("columns", {}).values():
            column_entry["fieldId"] = _remap_field_ref(column_entry.get("fieldId"), id_map)
            for section in ("field", "primaryHeader", "secondaryHeader"):
                if section in column_entry:
                    column_entry[section]["fieldId"] = _remap_field_ref(column_entry[section].get("fieldId"), id_map)

    for fields_source in (workflow_config.get("clientConfig", {}).get("fields", []),
                           step_config.get("trigger_1", {}).get("fields", [])):
        for trigger_field in fields_source:
            if trigger_field.get("apiId") in id_map:
                trigger_field["apiId"] = id_map[trigger_field["apiId"]]

    base_label = workflow_config.get("name") or data.get("workflowName")
    if preserve_name:
        new_label = base_label
    elif override_name:
        new_label = next_labeled_name(compose_workflow_name(override_name, base_label))
    else:
        new_label = next_labeled_name(base_label)
    workflow_config["name"] = new_label
    if "clientConfig" in workflow_config:  # absent on some builder-made workflows (Expand ones seen)
        workflow_config["clientConfig"]["title"] = new_label
    if "trigger_1" in step_config:
        step_config["trigger_1"]["title"] = new_label

    workflow_config["connectionContext"] = {"M3": connection_id}

    # Only the fields creation actually wants - see the docstring above.
    # Everything server-computed (stats, workflowArn, definitionAsl,
    # created/modified/createdBy/modifiedBy, overview) is dropped, and
    # description/labels get their confirmed creation defaults since GET
    # doesn't return them at all.
    clear_unused_spreadsheet_flag(workflow_config, step_config)
    labels = with_imported_label(data.get("labels", []), workflow_config, step_config)

    return {
        "entity": data.get("entity", "Workflow"),
        "description": data.get("description", ""),
        "hasError": data.get("hasError", False),
        "tenantId": tenant_id,
        "workflowId": new_workflow_id(),
        "workflowName": new_label,
        "workflowAlias": new_label,
        "active": data.get("active", True),
        "workflowType": data.get("workflowType", "STANDARD"),
        "labels": labels,
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
