"""
The POST /workflows top-level envelope - everything outside `definition`.

Lifted out of build.py so the M3-API path and the generic-pipeline path
(build_pipeline.py) can't drift apart on the load-bearing details below.
Every comment here is a captured fact, not a preference; see
docs/vince-live-api-reference-prompt.md for how each was established.
"""

from typing import Optional

from ids import new_workflow_id

# Every workflow this tool creates gets this label, so a VinceForge-created
# workflow is distinguishable from one built by hand in Live's UI. Same
# mechanism as the confirmed VXL label (pipeline_ir.VXL_LABEL) - a plain
# entry in the top-level `labels` array - just a second, always-on one.
IMPORTED_LABEL = "Imported"


def reads_spreadsheet(workflow_config: dict, step_config: dict) -> bool:
    """Does the workflow take any INPUT from a spreadsheet?

    The Imported label makes Live treat a workflow as a spreadsheet import,
    and saving it then fails ("no import field mapped in the Excel step") when
    nothing reads from the sheet. So the label is only for workflows that do.
    Native M3 API steps are judged by their real inputs (a transaction input
    with source EXCEL); a workflow with no native API step (Code/Transform
    pipelines) falls back to workflowConfig.hasSpreadSheetInput.
    """
    native = False
    for step in (step_config or {}).values():
        if not isinstance(step, dict):
            continue
        for txn in step.values():
            if isinstance(txn, dict) and isinstance(txn.get("input"), dict):
                native = True
                if any(isinstance(i, dict) and i.get("source") == "EXCEL"
                       for i in txn["input"].values()):
                    return True
    if native:
        return False
    return bool((workflow_config or {}).get("hasSpreadSheetInput"))


def clear_unused_spreadsheet_flag(workflow_config: dict, step_config: dict) -> None:
    """Native workflow with no sheet-fed input must not claim spreadsheet input.

    workflowConfig.hasSpreadSheetInput makes Live treat the workflow as an
    import workflow, and saving then fails when no import field is mapped in
    the Excel step. Only ever turned OFF here, and only for a native M3 API
    workflow (judged by reads_spreadsheet), never switched on.
    """
    if workflow_config and workflow_config.get("hasSpreadSheetInput")             and not reads_spreadsheet(workflow_config, step_config):
        workflow_config["hasSpreadSheetInput"] = False


def with_imported_label(labels: list, workflow_config: dict, step_config: dict) -> list:
    """Every workflow VinceForge creates carries the Imported label.

    Unconditional again since 2026-10-16 (Haakon: the native exports of a
    real import came out untagged). From 2026-10-08 it was only added when
    the sheet fed an input, because a tagged workflow failed to save - but
    the re-import that day showed the "import workflow" behaviour stayed
    with the label gone, pointing at workflowConfig.hasSpreadSheetInput
    instead, which clear_unused_spreadsheet_flag now turns off. The
    arguments are kept so every caller stays as it is.
    """
    labels = list(labels or [])
    if IMPORTED_LABEL not in labels:
        labels.append(IMPORTED_LABEL)
    return labels


def workflow_envelope(name: str, tenant_id: str, connection_id: str, environment_id: str,
                      workflow_config: dict, step_config: dict,
                      description: str = "",
                      labels: Optional[list] = None,
                      extra_tags: Optional[dict] = None) -> dict:
    tags = {
        "connectionId": [connection_id],
        "apiEnvironment": environment_id,
        "environments": [environment_id],
        "primaryEnvironment": environment_id,
    }
    if extra_tags:
        tags.update(extra_tags)

    clear_unused_spreadsheet_flag(workflow_config, step_config)
    labels = with_imported_label(labels, workflow_config, step_config)

    return {
        "entity": "Workflow",
        "description": description,
        # hasError is a CLIENT-computed flag, not something the server
        # infers. Confirmed by diffing our creation payload against a
        # real "open workflow, change nothing, hit Save" PUT request:
        # the only difference was this flag flipping from true to false.
        # Since we're bypassing Live's own UI validation entirely, we set
        # it ourselves.
        "hasError": False,
        "tenantId": tenant_id,
        # Syntactically valid but disposable - Live assigns its own id
        # regardless, so always read the real one back from the response.
        "workflowId": new_workflow_id(),
        "workflowName": name,
        "workflowAlias": name,
        "active": True,
        # "STANDARD", not "EXPRESS" - sending EXPRESS at creation time
        # produces a broken, half-provisioned workflow.
        "workflowType": "STANDARD",
        "labels": labels,
        "tags": tags,
        "definition": {
            "workflowConfig": workflow_config,
            "stepConfig": step_config,
        },
    }
