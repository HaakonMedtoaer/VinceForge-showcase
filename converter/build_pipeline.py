"""
BUILD stage for the GENERIC pipeline path: pipeline_ir.Pipeline -> Live
workflow JSON (POST /workflows shape).

Sibling of build.py, not a replacement. build.py emits the confirmed
M3-API shape (trigger -> [generic filter] -> m3_api -> excel); this emits
a chain of Transform / REST API / Code steps that reaches M3 over ION
API's bulk execute endpoint instead - the shape Vince's own production
customer workflows use, and the only one that can gate one M3 call while
another runs unconditionally.

Structural facts relied on here, all confirmed:
  * type / name / target per step type, and that a wrong `type` is a real
    400 from POST /workflows.
  * stepId conventions transform_N / rest_api_N / code_N, and that they
    are load-bearing - a Transform addresses siblings by exact stepId
    (`$context.data.all.rest_api_1.body.results`).
  * Linear nesting via a single-element `childSteps` at each level, from
    one root TRIGGER step.
  * `childSteps: []` is a valid, saveable terminal state - a workflow does
    NOT have to end in excel_1.
  * excel_1 must NOT appear here: it can only bind to m3_api_1 transaction
    instance ids, and attaching it to a non-M3 chain broke saving in the
    builder UI for real. Spreadsheet in/out instead comes from the VXL
    label plus an export-shaped final step.
  * retryAttempts: 0 on every step except trigger_1, which omits it.
  * A trigger field with no owning M3 transaction carries no `apiId`.

One thing this file will NOT do: inject a diagnostic key into a
Transform / REST API / Code stepConfig. Their save-time validation is
loose enough that malformed content has been observed to silently reset,
so warnings come back out-of-band from pipeline_warnings() instead of
riding along in the JSON the way build.py's `_needs_review` does.
"""

import json
from typing import Optional

from envelope import workflow_envelope
from m3_rest import render_bulk_rest_config, render_bulk_transform, render_row_pipeline_js
from expand_code import expand_warnings, render_expand_js
from pipeline_ir import (
    CodeStep,
    M3BulkStep,
    M3ExpandStep,
    M3RowPipelineStep,
    Pipeline,
    PipelineSpecError,
    RestApiStep,
    TransformStep,
    VXL_LABEL,
    parse_pipeline_spec,
)

# Confirmed type / name / target per Live step type. A closed list - a
# value not in here has never been captured, and a wrong `type` fails
# POST validation outright.
_TRANSFORM_SHAPE = ("TRANSFORMER_MORPH", "Transform", "workflow-transform", "transform")
_REST_SHAPE = ("GENERIC_API", "Rest API", "workflow-rest-api", "rest_api")
_CODE_SHAPE = ("GENERIC_CODE", "Code", "workflow-code", "code")


def build_pipeline_workflow(pipeline: Pipeline, connection_id: str, tenant_id: str,
                            environment_id: str) -> dict:
    """
    Two passes on purpose. Pass one lays out every Live step and assigns
    its stepId; pass two renders the configs. It has to be that way
    because a step's content can reference a LATER-numbered sibling's id
    (`{"step": "<ref>.FIELD"}` resolving to `rest_api_3`), and because an
    m3Bulk step expands into two Live steps, so ids can't be known while
    the list is still being walked.
    """
    nodes = []            # (step_id, type, name, target) in chain order
    owners = []           # parallel list: (ir_step, role) for pass two
    counters = {}
    step_ids = {}         # spec ref -> the REST stepId that carries its results

    def allocate(shape, name: Optional[str], ir_step, role: str) -> str:
        step_type, default_name, target, prefix = shape
        counters[prefix] = counters.get(prefix, 0) + 1
        step_id = f"{prefix}_{counters[prefix]}"
        nodes.append({
            "stepId": step_id,
            "type": step_type,
            "name": name or default_name,
            "retryAttempts": 0,
            "target": target,
            "childSteps": [],
        })
        owners.append((ir_step, role, step_id))
        return step_id

    for step in pipeline.steps:
        if isinstance(step, TransformStep):
            allocate(_TRANSFORM_SHAPE, step.name, step, "transform")
        elif isinstance(step, RestApiStep):
            allocate(_REST_SHAPE, step.name, step, "rest")
        elif isinstance(step, (CodeStep, M3RowPipelineStep, M3ExpandStep)):
            allocate(_CODE_SHAPE, step.name, step, "code")
        elif isinstance(step, M3BulkStep):
            # The confirmed pair: a Transform that builds {"payload": ...}
            # then a REST API step that posts it.
            allocate(_TRANSFORM_SHAPE,
                     step.transform_name or f"Build {step.program} payload",
                     step, "bulk-transform")
            rest_id = allocate(_REST_SHAPE, step.name or f"{step.program} (M3)", step, "bulk-rest")
            if step.ref:
                if step.ref in step_ids:
                    raise PipelineSpecError(f"Duplicate m3Bulk ref {step.ref!r}")
                step_ids[step.ref] = rest_id
        else:
            raise PipelineSpecError(f"No confirmed Live step type for {type(step).__name__}")

    # Pass two: configs, now that every stepId (and every ref) is known.
    step_config = {}
    for ir_step, role, step_id in owners:
        if role == "transform":
            step_config[step_id] = {"template": ir_step.template, "trimAll": True}
        elif role == "code":
            step_config[step_id] = _code_config(ir_step, connection_id)
        elif role == "rest":
            step_config[step_id] = _rest_config(ir_step)
        elif role == "bulk-transform":
            step_config[step_id] = {
                "template": render_bulk_transform(ir_step, step_ids),
                "trimAll": True,
            }
        elif role == "bulk-rest":
            step_config[step_id] = _rest_content(render_bulk_rest_config(ir_step, connection_id))
        else:  # pragma: no cover
            raise AssertionError(f"unknown role {role!r}")

    # Linear nesting: each step is the single child of the one before it.
    # No confirmed example has more than one child at any level.
    for parent, child in zip(nodes, nodes[1:]):
        parent["childSteps"] = [child]

    trigger_fields = [_trigger_field(f, position)
                      for position, f in enumerate(pipeline.trigger_fields, start=1)]
    # A real generic chain captured from Live carried formGroups: [] with
    # no fields; the M3 path's group 1 is named "fields". Declare the
    # group only when something goes in it - a field with no matching
    # group has been observed to render nowhere at all.
    form_groups = [{"groupId": 1, "groupName": "fields"}] if trigger_fields else []

    title = pipeline.trigger_title or pipeline.label
    client_config = {
        "title": title,
        "description": pipeline.trigger_description,
        "buttonText": pipeline.button_text,
        "formGroups": form_groups,
        "fields": trigger_fields,
    }

    workflow_config = {
        "name": pipeline.label,
        "constants": dict(pipeline.constants),
        "inputs": {
            f.name: {"dataType": f.data_type if f.data_type != "UNKNOWN" else "STRING",
                     "validation": {"required": bool(f.mandatory)}}
            for f in pipeline.trigger_fields
        },
        # Kept even with no M3 API step: it is how a workflow declares
        # which stored connection it belongs to, and it is present in
        # every real generic workflow captured.
        "connectionContext": {"M3": connection_id},
        "hasSpreadSheetInput": bool(pipeline.has_spreadsheet_input),
        "hasExcelDesignFile": False,
        "hasTemplateFile": False,
        "workflowTags": {},
        "apiFieldReferences": {},
        "contexts": {},
        "clientConfig": client_config,
        "steps": [
            {
                "stepId": "trigger_1",
                "type": "TRIGGER",
                "name": "Trigger",
                "target": "workflow-trigger",
                "childSteps": [nodes[0]] if nodes else [],
            }
        ],
    }

    step_config["trigger_1"] = {
        "title": title,
        "description": pipeline.trigger_description,
        "buttonText": pipeline.button_text,
        "triggerType": "CLIENT",
        "formGroups": form_groups,
        "fields": trigger_fields,
        "tags": {},
    }

    return workflow_envelope(
        name=pipeline.label,
        tenant_id=tenant_id,
        connection_id=connection_id,
        environment_id=environment_id,
        workflow_config=workflow_config,
        step_config=step_config,
        description=pipeline.description,
        # Confirmed: the VXL marker is a plain top-level label.
        labels=pipeline.labels,
    )


def _code_config(step, connection_id: str) -> dict:
    if isinstance(step, M3RowPipelineStep):
        return {"content": render_row_pipeline_js(step, connection_id)}
    if isinstance(step, M3ExpandStep):
        return {"content": render_expand_js(step.spec, connection_id, step.base_url)}
    return {"content": step.content}


def _rest_content(config: dict) -> dict:
    """
    Wrap a REST API config the way Live itself stores it.

    CONFIRMED from a GET of a hand-built workflow: a GENERIC_API step's
    stepConfig is `{"content": "<the config as a JSON STRING>"}`, not the
    config's keys sitting flat in stepConfig. The compiled state machine
    for that workflow parses the string back out and passes
    `stepConfig: {"content": {...}}` to
    `vl-sls-connection-generic-rest-api-prod-proxy`, so a flat stepConfig
    would leave the proxy with no content at all.

    This also explains the previously-unresolved observation that a POSTed
    `endpoint`/`bodyPath` came back correctly from the API but rendered as
    empty placeholders in the builder: the builder reads them out of
    `content`.

    Note the asymmetry - a TRANSFORMER_MORPH step's config really is flat
    (`{"template": ..., "trimAll": true}`), confirmed in the same GET.
    indent=2 matches the UI's own formatting of the string.
    """
    return {"content": json.dumps(config, indent=2)}


def _rest_config(step: RestApiStep) -> dict:
    config = {
        "endpoint": step.endpoint,
        "method": step.method,
        "headers": dict(step.headers),
    }
    # version first in the real examples ("append to top for easier
    # readability"), though key order carries no meaning in JSON.
    if step.version is not None:
        config = {"version": step.version, **config}
    if step.body_path is not None:
        config["bodyPath"] = step.body_path
    if step.body is not None:
        # Literal-body mode. Confirmed real, with {{ body.x }} /
        # {{ header.x }} substituted at execution time.
        config["body"] = step.body
    if step.body_path is None and step.body is None:
        # Neither set: a GET with no body is a real, captured shape
        # ("Export data from REST API to VXL Live" omits both).
        pass
    if step.for_each:
        config["forEach"] = step.for_each
    if step.skip_if_expression:
        config["skipIfExpression"] = step.skip_if_expression
    # connectionId is omitted ENTIRELY when unset - confirmed to be what
    # "Use connection: off" looks like. Empty string or null is not the
    # same thing and has never been captured.
    if step.connection_id:
        config["connectionId"] = step.connection_id
    return _rest_content(config)


def _trigger_field(field, position: int) -> dict:
    """
    A CLIENT trigger field with no owning M3 transaction.

    Shape taken from the confirmed Generic Filter trigger field - the only
    captured example of one: same keys, no `apiId`, `value` carrying the
    default.
    """
    entry = {
        "fieldId": field.name,
        "fieldName": field.name,
        "label": field.label or field.name,
        "placeHolder": field.place_holder or field.label or field.name,
        "dataType": field.data_type if field.data_type != "UNKNOWN" else "STRING",
        "length": str(field.length),
        "mandatory": bool(field.mandatory),
        "type": "text",
        "formGroupId": field.form_group_id,
        "elementPosition": position,
    }
    if field.default_value is not None:
        entry["value"] = field.default_value
    return entry


def pipeline_warnings(pipeline: Pipeline) -> list:
    """
    Things worth saying out loud about a built pipeline, returned rather
    than injected into the JSON (see the module docstring for why).
    """
    warnings = []

    if pipeline.has_spreadsheet_input and VXL_LABEL not in pipeline.labels:
        warnings.append(
            "This pipeline reads spreadsheet input but carries no VXL label. A workflow with no M3 "
            "API + Excel step pair needs the VXL label to be visible in the VXL Live add-in at all "
            '- add "vxl": true.'
        )

    # The scaffold ships a "<TENANT>" placeholder in baseUrl on purpose.
    # A workflow POSTed with that still in it saves fine and then fails at
    # run time, which is the worst way to find out.
    for step in pipeline.steps:
        target = getattr(step, "base_url", None) or getattr(step, "endpoint", "")
        if target and ("<" in target or ">" in target):
            warnings.append(
                f"An endpoint still carries a placeholder: {target!r}. Replace it with the target "
                "tenant's real ION API root before creating this in Live - it will save happily and "
                "then fail at execution."
            )

    bulk_steps = [s for s in pipeline.steps if isinstance(s, M3BulkStep)]
    for step in bulk_steps:
        if step.skip_if_expression:
            warnings.append(
                f"m3Bulk {step.program}: skipIfExpression is set. The property NAME is confirmed "
                "(from the ticket that implemented it), but its expression syntax and truthiness "
                "semantics are documented nowhere - verify against a real run before relying on it. "
                "A gate/filterExpression does the same job in the Transform, where the semantics are "
                "plain JSONata."
            )
        if len(step.calls) > 1 and any(
            "step" in value and ":" not in str(value["step"]).split(".")[0]
            for later in bulk_steps
            for call in later.calls
            for value in call.record.values()
        ):
            warnings.append(
                f"m3Bulk {step.program} sends {len(step.calls)} transactions in one call, and some "
                "later step reads its results without naming a transaction. "
                "`...body.results.records.FIELD` flattens records across ALL transactions in the "
                'payload - use {"step": "<ref>:<Transaction>.FIELD"} to pick one out.'
            )
            break

    if len(bulk_steps) > 1:
        warnings.append(
            "More than one M3 call: results are correlated by position when a later step reads an "
            "earlier one's records (that is what the real customer workflows do, via `records[$i]`). "
            "A gate or filter that drops rows between two calls breaks that alignment - carry the "
            "key field (e.g. ORNO) into the payload and join on it rather than trusting the index."
        )

    for step in pipeline.steps:
        if isinstance(step, M3ExpandStep):
            warnings.extend(expand_warnings(step.spec))
        if isinstance(step, M3RowPipelineStep):
            warnings.append(
                "This pipeline uses a Code-step row pipeline. That is the escape hatch, not the "
                "idiom: it is invisible in the builder UI, it spends wall-clock on sequential HTTP "
                "(watch the 29s synchronous-execution ceiling), and every real Vince workflow does "
                "this with Transform + REST API instead. Keep it only if a later call's input truly "
                "depends on an earlier call's output per row."
            )
            if step.debug:
                warnings.append(
                    "Row pipeline has debug: true. CONFIRMED HAZARD, 2026-09-11 (WFEXEC-7684f109...): "
                    "a Manual-trigger run bound to a spreadsheet range writes the step's return value "
                    "back into the SAME source sheet - and since debug:true's shape is "
                    "{records, _diagnostics, _aborted} rather than a bare array, the write-back can't "
                    "expand it into a table, so it dumps the whole thing as one giant JSON string into "
                    "a single cell (observed: cell A2). That cell is then read back in as row 1's data "
                    "on the NEXT run - a self-poisoning feedback loop, not a cosmetic export-shape "
                    "issue. Turn debug off (and clear any cell it already wrote into) before any run "
                    "that reuses this sheet - keep it on only for a one-off diagnostic run you intend "
                    "to inspect and then clean up by hand."
                )
        if isinstance(step, RestApiStep):
            if step.for_each:
                warnings.append(
                    f"REST API step '{step.name or step.endpoint}' uses forEach. It is documented but "
                    "marked BETA with inconsistent variable naming, its own doc's example says "
                    '"version": 3 while the instructions say 2, and no captured example exists. Test '
                    "it directly before building on it."
                )
            if step.skip_if_expression:
                warnings.append(
                    f"REST API step '{step.name or step.endpoint}' uses skipIfExpression - name "
                    "confirmed, syntax unconfirmed (see above)."
                )

    return warnings


def build_pipeline_spec(spec: dict, connection_id: str, tenant_id: str,
                        environment_id: str) -> tuple:
    """Convenience for callers holding raw spec JSON. Returns (workflow, warnings)."""
    pipeline = parse_pipeline_spec(spec)
    return (build_pipeline_workflow(pipeline, connection_id, tenant_id, environment_id),
            pipeline_warnings(pipeline))
