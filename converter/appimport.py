"""
App import: take a folder describing a whole app and land it in a target
tenant - Custom Tables and workflows created via the API, plus an App
Builder prompt generated for the part that has no API.

Two entry points, both used by the web UI and the CLI:

  plan_app_import()   scan only. No HTTP, no token needed, nothing created.
                      This is what the UI shows before you commit.
  run_app_import()    scan, convert Classic XML, then create. Tables first,
                      then workflows.

## Classic XML in an app folder

A folder may hold Classic "VinceExcelConfig" exports alongside its JSON.
Each is converted to a workflow POST body and written into the folder as
`workflows/<stem>.generated.json` before anything is sent, then imported
exactly like a workflow that arrived as JSON - see app_classic.py, which
owns that stage and the reasoning behind it. The .xml files themselves are
never modified and never sent anywhere.

## Ordering

Tables are created before workflows, deliberately. A workflow's Table
Updater step references a table **by name**, so a workflow that writes to
one of the app's own tables needs that table to exist first. Names (unlike
ids) survive a move between tenants, which is why this ordering is enough
and no reference rewriting is involved.

Groups are created after workflows, for the same reason a design file's
upload happens after its workflow's creation: a Group (Live's real "bundle
these workflows so they open together" feature - `liveapi.create_group`)
references workflows by their real, server-assigned id, which does not
exist until the POST above comes back. Every source Classic XML file whose
blocks produced 2+ successfully-created workflows becomes one Group, named
from the same `base_names` value used to compose those workflows' names -
see `_create_groups`.

## What this does NOT do

* **Write table rows.** No confirmed API exists for it (see liveapi.py).
  Seed rows found in the folder are saved to the run artifacts and
  reported, never sent.
* **Remap cross-workflow references.** Workflow ids change on import, and
  no confirmed field holds a workflow-to-workflow reference, so guessing
  where to rewrite one would be exactly the kind of invention this
  project's rules forbid. The scan detects and reports them instead.
* **Update anything that already exists.** A table that's already in the
  target tenant is left alone (its `primaryKeys` are immutable anyway);
  workflows are always created new, since Live assigns its own id.

## Artifacts

Every run writes a timestamped directory under RUN/app-imports/ holding
the plan, the exact POST bodies sent, the generated prompt, any
un-importable rows, and the results. A dry run writes the same thing
minus the responses, so "what would this send" is always inspectable.

The prompt is ALSO written into the app folder itself as
app-builder-prompt.generated.md, so it lives next to the app it describes
instead of only under RUN/. Two rules make that safe:

  * scan_app_folder skips that filename, so a generated prompt is never
    read back in as App Builder source (which would inline it into the
    next one, and so on).
  * a DRY RUN never writes it. A dry-run prompt carries placeholder
    statuses and no real workflow ids, and quietly replacing a good prompt
    from a real import with that would be a nasty surprise. Dry runs still
    write their prompt under RUN/, where it can be read without doing any
    damage.
"""

import datetime
import json
import os
import re

from app_classic import convert_classic_xml, convert_pipeline_specs
from app_prompt import build_app_prompt
from history import append_history
from liveapi import (create_group, create_table_meta, get_workflow, post_workflow,
                     table_row_count, update_workflow, upload_design_file)
from source_app_folder import GENERATED_PROMPT_NAME, match_design_files, scan_app_folder
from source_live_json import clone_and_retarget

ARTIFACTS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "app-imports")

# CLAUDE.md: a malformed connection/tenant/environment id does NOT fail
# validation at creation time - it silently breaks M3 API step rendering in
# the builder UI. So check the shape up front and warn loudly, but don't
# block: the check is a shape heuristic from real ids, not a documented rule.
ID_PATTERNS = {
    "connectionId": re.compile(r"^CONNECTION-[A-Za-z0-9]{16,}$"),
    "tenantId": re.compile(r"^TENANT-[A-Za-z0-9]{16,}$"),
    "environmentId": re.compile(r"^ENVIRONMENT-[A-Za-z0-9]{16,}$"),
}


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "app")).strip("-").lower() or "app"


def _artifacts_dir(app_name: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.abspath(os.path.join(ARTIFACTS_ROOT, _slug(app_name) + "-" + stamp))
    os.makedirs(path, exist_ok=True)
    return path


def _write_artifact(directory: str, relative_name: str, content) -> str:
    path = os.path.join(directory, relative_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(content, str):
            f.write(content)
        else:
            json.dump(content, f, indent=2)
    return path


def check_target_ids(connection_id: str, tenant_id: str, environment_id: str) -> list:
    values = {"connectionId": connection_id, "tenantId": tenant_id, "environmentId": environment_id}
    warnings = []
    for key, pattern in ID_PATTERNS.items():
        value = (values.get(key) or "").strip()
        if not value:
            warnings.append("No " + key + " set - the import will target nothing usable.")
        elif not pattern.match(value):
            warnings.append(
                key + " doesn't look like a Live id (" + value[:24] + "...). A malformed id passes creation "
                "validation but silently breaks M3 API step rendering in the builder - check it before relying "
                "on the imported workflows."
            )
    return warnings


def _scan_with_matches(root: str, design_matches: dict = None):
    """scan_app_folder, with a person's hand-picked design-file pairings applied."""
    bundle = scan_app_folder(root)
    if design_matches:
        bundle.design_matches = dict(design_matches)
        match_design_files(bundle)
    return bundle


def plan_app_import(root: str, design_matches: dict = None) -> dict:
    """Scan a folder and return the import plan. No HTTP, no token, no writes to Live."""
    bundle = _scan_with_matches(root, design_matches)
    return {"ok": True, "plan": bundle.summary()}


def _probe_table_exists(table_name: str, bearer_token: str):
    """
    Is this table already in the target tenant?

    There is no confirmed GET on `/v1/custom-tables/meta/{tableName}` - only
    POST and PATCH are documented - so this uses the confirmed
    `aggregated/{tableName}` endpoint instead. Returns True (200 - really
    there), False (404 - really not), or None for any other status, which is
    treated as "unknown" and NOT as "absent": on unknown we still attempt the
    creation and let the API's own response decide, rather than acting on an
    inference about a status code nobody has confirmed. The raw response
    comes back too, so the caller can tell an unknown-but-authenticated
    probe apart from a plain auth failure.
    """
    response = table_row_count(table_name, bearer_token)
    if response["ok"]:
        return True, response
    if response.get("status") == 404:
        return False, response
    return None, response


def _artifact_name(prefix: str, label: str, index: int, suffix: str) -> str:
    """
    Unique per resource. The index is always present because app import
    preserves names verbatim, so two resources in one folder really can
    share a name - and an artifact silently overwriting another would be a
    nasty way to find that out.
    """
    return prefix + "/" + str(index + 1).zfill(2) + "-" + _slug(label) + suffix


def _report(progress, phase: str, done: int, total: int, item: str = "") -> None:
    """Tell the caller how far the run is ("Creating workflows", 2, 6). Only
    for showing a person that something is happening - a failing callback
    must never break an import, so it is swallowed."""
    if progress is None:
        return
    try:
        progress({"phase": phase, "done": done, "total": total, "item": item})
    except Exception:
        pass


def _convert_classic(bundle, connection_id: str, tenant_id: str, environment_id: str,
                     bearer_token: str, artifacts: str, warnings: list,
                     ion_api_base_url: str = "", base_names: dict = None,
                     header_row: int = None) -> list:
    """
    Convert the folder's Classic XML files and fold them into the bundle.

    Runs on a dry run too, and writes the JSON into the app folder either
    way - app_classic.py's docstring says why that is safe here when it
    isn't for the generated prompt. The built body goes to the run
    artifacts and is stripped from the returned result: it is a whole
    workflow, and echoing one per XML back to the UI would dwarf everything
    else in the response.

    `base_names` (keyed by the XML's app-relative path) is passed straight
    through to `app_classic.convert_classic_xml` - see its own docstring.
    """
    if not bundle.classic_xml and not bundle.pipeline_specs:
        return []

    results = []
    if bundle.classic_xml:
        results = convert_classic_xml(bundle, connection_id, tenant_id, environment_id, bearer_token,
                                      ion_api_base_url=ion_api_base_url, base_names=base_names,
                                      header_row=header_row)
    # Hand-written pipeline specs (e.g. an m3Expand replacing a Classic
    # <Expand>) are built here too, with the same target - never imported
    # pre-built; see app_classic.convert_pipeline_specs.
    results += convert_pipeline_specs(bundle, connection_id, tenant_id, environment_id,
                                      ion_api_base_url=ion_api_base_url)
    for index, result in enumerate(results):
        body = result.pop("body", None)
        if body is not None:
            result["postBodyArtifact"] = _write_artifact(
                artifacts, _artifact_name("classic-xml", result.get("label") or "workflow", index, ".json"), body)
        if result.get("ok") is False:
            warnings.append(result["path"] + ": " + str(result.get("status")) + " - " +
                            str(result.get("error")))
        # Only the critical subset is promoted to the run-level list; the
        # rest stays on the file, which is reported right next to it. See
        # app_classic.convert_classic_xml on why.
        for warning in result.pop("criticalWarnings", []):
            warnings.append(result["path"] + ": " + warning)
    return results


def _import_tables(bundle, target: dict, bearer_token: str, artifacts: str, dry_run: bool,
                   progress=None) -> list:
    results = []
    total = len(bundle.tables)
    for index, table in enumerate(bundle.tables):
        _report(progress, "Creating tables", index, total, table.table_name)
        result = {
            "tableName": table.table_name,
            "path": table.path,
            "primaryKeys": table.meta.get("primaryKeys", []),
            "updateType": table.meta.get("updateType"),
            "columns": sorted((table.meta.get("schema") or {}).get("properties", {}).keys()),
            "rowsNotImported": len(table.rows),
            "warnings": list(table.warnings),
        }
        _write_artifact(artifacts, _artifact_name("tables", table.table_name, index, ".meta.json"), table.meta)
        if table.rows:
            result["rowsArtifact"] = _write_artifact(
                artifacts, _artifact_name("tables", table.table_name, index, ".rows-NOT-imported.json"), table.rows)

        if dry_run:
            result.update(ok=None, status="planned (dry run - nothing sent)")
            results.append(result)
            continue

        exists, probe = _probe_table_exists(table.table_name, bearer_token)
        if exists is True:
            result.update(ok=True, skipped=True,
                          status="already exists in target tenant - left untouched (primaryKeys are immutable)")
            results.append(result)
            _log(bundle, result, "table", target)
            continue
        if exists is None and probe.get("status") not in (401, 403):
            # A 401/403 isn't an ambiguous existence answer, it's a dead
            # token - the creation call right below will say so far more
            # clearly than a warning about the probe would.
            result["warnings"].append(
                "Couldn't confirm whether the table already exists - attempted creation anyway and let the "
                "API decide."
            )

        response = create_table_meta(table.meta, bearer_token)
        if response["ok"]:
            result.update(ok=True, status="created", httpStatus=response.get("status"))
        else:
            error = response.get("error") or "unknown error"
            if response.get("status") == 400:
                error += (" | Note: docs/vince-custom-tables-guide.md records a known flaky 400 on this "
                          "endpoint whose validationError names workflow properties - the identical body has "
                          "succeeded on a re-run. Deliberately not auto-retried; re-run the import to test it.")
            result.update(ok=False, status="failed", httpStatus=response.get("status"), error=error)
        results.append(result)
        _log(bundle, result, "table", target)
    _report(progress, "Creating tables", total, total)
    return results


def _is_duplicate_name_conflict(response: dict) -> bool:
    """
    CONFIRMED 2026-09-16, from a real POST /workflows 409: Live enforces
    workflow-NAME uniqueness per tenant, answering
    `HTTP 409: Workflow "X" already exists!` on a collision - overturning
    what this project previously assumed (CLAUDE.md and
    docs/vince-app-import.md used to say duplicate names silently pile up
    with nothing deduping them; that was wrong, corrected the same day
    this was confirmed). Narrow on purpose: only a 409 whose body actually
    says "already exists" counts as this - any other 409, or any other
    status, is still a real failure to report as one, not something to
    paper over.
    """
    if response.get("status") != 409:
        return False
    return "already exists" in (response.get("error") or "").lower()


def _import_workflows(bundle, target: dict, bearer_token: str, artifacts: str, dry_run: bool,
                      progress=None) -> list:
    results = []
    seen_names = {}
    total = len(bundle.workflows)
    for index, workflow in enumerate(bundle.workflows):
        _report(progress, "Creating workflows", index, total, workflow.name)
        result = {
            "name": workflow.name,
            "path": workflow.path,
            "sourceWorkflowId": workflow.source_workflow_id,
            "warnings": list(workflow.warnings),
        }

        seen_names[workflow.name] = seen_names.get(workflow.name, 0) + 1
        if seen_names[workflow.name] > 1:
            result["warnings"].append(
                "Another workflow in this same app folder already uses this name. Names are preserved as-is on "
                "app import, so both will exist in the target tenant under the same name."
            )

        try:
            body = clone_and_retarget(workflow.data, target["connectionId"], target["tenantId"],
                                      target["environmentId"], preserve_name=True)
        except Exception as e:
            result.update(ok=False, status="failed to build POST body",
                          error=type(e).__name__ + ": " + str(e))
            results.append(result)
            _log(bundle, result, "workflow", target)
            continue

        result["postBodyArtifact"] = _write_artifact(
            artifacts, _artifact_name("workflows", workflow.name, index, ".post.json"), body)

        if dry_run:
            result.update(ok=None, status="planned (dry run - nothing sent)")
            if workflow.design_file is not None:
                result["designFile"] = {
                    "path": workflow.design_file.path,
                    "filename": workflow.design_file.filename,
                    "size": workflow.design_file.size,
                    "ok": None,
                    "status": "planned - uploads after creation (the storage key needs the real "
                              "workflow id, so there is nothing to dry-run against)",
                }
            results.append(result)
            continue

        response = post_workflow(body, bearer_token)
        if response["ok"]:
            created = response.get("body") or {}
            # Live assigns its own workflowId regardless of what was sent
            # (see ids.new_workflow_id) - always read it back, never assume.
            workflow_id = created.get("workflowId") if isinstance(created, dict) else None
            result.update(ok=True, status="created", httpStatus=response.get("status"),
                          workflowId=workflow_id)
            if workflow.design_file is not None:
                result["designFile"] = _upload_design_file(bundle, workflow, workflow_id, bearer_token)
        elif _is_duplicate_name_conflict(response):
            # Live rejects the duplicate outright rather than allowing it -
            # see _is_duplicate_name_conflict. Re-running an import against
            # a tenant that already has this workflow is therefore the
            # NORMAL case, not an error: same treatment _import_tables
            # already gives an existing table.
            result.update(ok=True, skipped=True, httpStatus=response.get("status"),
                          status="already exists in target tenant - left untouched")
            result["warnings"].append(
                "A workflow named " + repr(workflow.name) + " already exists in this tenant. Live enforces "
                "unique workflow names, so this run left the existing one alone rather than failing. It has "
                "NOT been added to any Group this run creates - there is no confirmed way to look up an "
                "existing workflow's id by name, so add it to the group by hand in Live if needed."
            )
        else:
            result.update(ok=False, status="failed", httpStatus=response.get("status"),
                          error=response.get("error"))
        results.append(result)
        _log(bundle, result, "workflow", target)
    _report(progress, "Creating workflows", total, total)
    return results


def _create_groups(bundle, workflow_results: list, base_names: dict, target: dict,
                   bearer_token: str, dry_run: bool, progress=None) -> list:
    """
    Bundle sibling workflows from the same source Classic XML file into a
    Live Group, so they open together in VXL Live - see
    `liveapi.create_group` for the confirmed request/response this is
    built from.

    Bucketed by `AppWorkflow.group_key`, positionally paired with
    `workflow_results` (both walk `bundle.workflows` in the same order -
    see `_import_workflows`). Only a bucket with >= 2 members even HAS a
    workflow name to group with; and only >= 2 *successfully created* ones
    get a real group - a single surviving workflow, or a source XML that
    only ever produced one block, has nothing to group. A group-creation
    failure is a loud warning, not a hard failure of the run, matching how
    design-file upload and metadata lookups are treated elsewhere here.
    """
    buckets = {}
    for workflow, result in zip(bundle.workflows, workflow_results):
        if workflow.group_key:
            buckets.setdefault(workflow.group_key, []).append((workflow, result))

    results = []
    groupable = [key for key, members in buckets.items() if len(members) >= 2]
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        _report(progress, "Creating groups", groupable.index(key), len(groupable), key)
        group_name = (base_names or {}).get(key) or os.path.splitext(os.path.basename(key))[0]
        result = {
            "groupName": group_name,
            "sourcePath": key,
            "memberCount": len(members),
            "members": [workflow.name for workflow, _ in members],
        }

        if dry_run:
            result.update(ok=None, status="planned (dry run - nothing sent)")
            results.append(result)
            continue

        workflow_ids = [r.get("workflowId") for _, r in members if r.get("ok") and r.get("workflowId")]
        # A SKIPPED member (already existed - see _is_duplicate_name_conflict)
        # is `ok: True` but has no workflowId, because there is no confirmed
        # way to look up an existing workflow's id by name. It's therefore
        # silently absent from workflow_ids above; call that out explicitly
        # rather than let a smaller-than-expected group pass without
        # explanation, or - worse - a run where every member was skipped
        # read identically to one where every member genuinely failed.
        skipped_names = [workflow.name for workflow, r in members if r.get("skipped")]
        if skipped_names:
            result["warnings"] = [
                "Already existed and could not be added to this group (no confirmed way to look up an "
                "existing workflow's id by name): " + ", ".join(skipped_names) + ". Add it to the group "
                "by hand in Live if it needs to be there."
            ]
        if len(workflow_ids) < 2:
            result.update(ok=False, status="skipped", error=(
                "Fewer than 2 of this file's workflows were created successfully - nothing to group."))
            results.append(result)
            continue

        response = create_group(group_name, workflow_ids, target["tenantId"], bearer_token)
        if response["ok"]:
            created = response.get("body") or {}
            group_id = created.get("groupId") if isinstance(created, dict) else None
            result.update(ok=True, status="created", httpStatus=response.get("status"), groupId=group_id)
        else:
            result.update(ok=False, status="failed", httpStatus=response.get("status"),
                          error=response.get("error"))
        results.append(result)
        _log(bundle, result, "group", target)
    if groupable:
        _report(progress, "Creating groups", len(groupable), len(groupable))
    return results


def _upload_design_file(bundle, workflow, workflow_id, bearer_token: str) -> dict:
    """
    Upload a workflow's Excel design file, immediately after its creation.

    It has to be here rather than in a phase of its own: the storage key
    embeds the workflow id, and Live assigns that itself at creation, so
    the id is not known until the POST above comes back.

    ## Upload, then LINK - the server does not do the second half

    CONFIRMED 2026-09-29: after the upload the workflow still has
    `hasExcelDesignFile: false` and no `excelDesignFileKey` (4 of 4 real
    CustomerA uploads). The builder links it itself: it GETs the workflow,
    sets exactly those two fields in `definition.workflowConfig`, and saves
    with `PUT /workflows` (captured - see liveapi.update_workflow). This
    does the same, then GETs once more to check it took. Each stage that
    fails says which, and still carries the key a manual fix would need.
    """
    design = workflow.design_file
    outcome = {"path": design.path, "filename": design.filename, "size": design.size}

    if not workflow_id:
        outcome.update(ok=False, status="not uploaded",
                       error="The creation response carried no workflowId, and the storage key needs it.")
        return outcome

    full = os.path.join(bundle.root, design.path.replace("/", os.sep))
    try:
        with open(full, "rb") as f:
            data = f.read()
    except PermissionError as e:
        # Seen for real (2026-09-29, 2 CustomerA sheets): Excel locks an open
        # workbook against reading, and that is by far the likeliest cause.
        outcome.update(ok=False, status="not uploaded", error=(
            "Couldn't read the file - it is locked, most likely because it is open in Excel "
            "(or OneDrive is syncing it). Close it and re-import, or attach it by hand. (" + str(e) + ")"))
        return outcome
    except OSError as e:
        outcome.update(ok=False, status="not uploaded", error="Couldn't read the file: " + str(e))
        return outcome

    upload = upload_design_file(workflow_id, design.filename, data, bearer_token)
    outcome["steps"] = upload.get("steps")
    if not upload["ok"]:
        outcome.update(ok=False, status="upload failed", error=upload.get("error"))
        return outcome

    outcome.update(key=upload["key"])

    readback = get_workflow(workflow_id, bearer_token)
    body = readback.get("body") if readback.get("ok") else None
    config = (body or {}).get("definition", {}).get("workflowConfig", {}) if isinstance(body, dict) else {}
    if config.get("hasExcelDesignFile") and config.get("excelDesignFileKey"):
        outcome.update(ok=True, status="uploaded and linked",
                       linkedKey=config.get("excelDesignFileKey"))
        return outcome

    if not readback.get("ok") or not isinstance(body, dict) or "definition" not in body:
        outcome.update(ok=False, status="uploaded but NOT linked", error=(
            "The file is in storage, but the workflow couldn't be read back to link it (" +
            str(readback.get("error")) + "). Attach it by hand in the builder."))
        return outcome

    # The builder's own edit, on the definition exactly as GET returned it.
    definition = body["definition"]
    definition.setdefault("workflowConfig", {})
    definition["workflowConfig"]["hasExcelDesignFile"] = True
    definition["workflowConfig"]["excelDesignFileKey"] = upload["key"]
    saved = update_workflow(body.get("tenantId"), workflow_id, definition, bearer_token)
    outcome["steps"] = (outcome.get("steps") or []) + [
        {"step": "link", "ok": saved["ok"], "status": saved.get("status")}]
    if not saved["ok"]:
        outcome.update(ok=False, status="uploaded but NOT linked", error=(
            "The file is in storage, but saving the link onto the workflow failed: " +
            str(saved.get("error")) + ". Attach it by hand in the builder."))
        return outcome

    check = get_workflow(workflow_id, bearer_token)
    config = ((check.get("body") or {}).get("definition", {}).get("workflowConfig", {})
              if check.get("ok") and isinstance(check.get("body"), dict) else {})
    if config.get("excelDesignFileKey") == upload["key"] and config.get("hasExcelDesignFile"):
        outcome.update(ok=True, status="uploaded and linked", linkedKey=upload["key"])
    else:
        outcome.update(ok=True, status="uploaded, link saved but unverified", error=(
            "PUT /workflows answered " + str(saved.get("status")) + ", but reading the workflow back " +
            ("failed: " + str(check.get("error")) if not check.get("ok") else
             "shows hasExcelDesignFile=" + repr(config.get("hasExcelDesignFile")) +
             ", excelDesignFileKey=" + repr(config.get("excelDesignFileKey"))) + "."))
    return outcome


def _log(bundle, result: dict, kind: str, target: dict) -> None:
    """One run-history row per resource, so app imports show up in the same list as single-workflow creates."""
    label = result.get("name") or result.get("tableName") or result.get("groupName") or ""
    append_history({
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "app-import",
        "app": bundle.name,
        "kind": kind,
        "workflowName": bundle.name + " / " + label + " (" + kind + ")",
        "resourceName": label,   # the name as created - cleanup checks it against Live
        "connectionId": target.get("connectionId"),
        "tenantId": target.get("tenantId"),
        "environmentId": target.get("environmentId"),
        "ok": bool(result.get("ok")),
        "httpStatus": result.get("httpStatus"),
        "workflowId": result.get("workflowId") or result.get("groupId"),
        "error": result.get("error") or (result.get("status") if not result.get("ok") else None),
    })


def _write_prompt_into_app_folder(root: str, prompt: str, warnings: list):
    """
    Drop the prompt next to the app it describes. Returns the path, or None
    if it couldn't be written - a failure here must not sink an import that
    otherwise succeeded, since the prompt is also safely under RUN/.
    """
    path = os.path.join(os.path.abspath(root), GENERATED_PROMPT_NAME)
    try:
        if os.path.exists(path):
            warnings.append("Replaced the existing " + GENERATED_PROMPT_NAME + " in the app folder.")
        with open(path, "w", encoding="utf-8") as f:
            f.write(prompt)
        return path
    except OSError as e:
        warnings.append("Couldn't write " + GENERATED_PROMPT_NAME + " into the app folder: " + str(e) +
                        " (it's still in the run artifacts).")
        return None


def _group_warnings_by_resource(warnings: list) -> dict:
    """
    `warnings` (the run-level list) is a flat mix of strings, most already
    prefixed "<resource path>: <message>" (classic XML criticals, design
    file pairing issues) and a few that aren't (bundle-level notes, the
    dry-run/prompt notes above). Split on the FIRST ": " to recover the
    resource a message belongs to - ungrouped ones land under "General".

    Also DEDUPES within each resource, preserving first-seen order. A
    multi-block Classic file's file-level notes (block count, unrepresented
    filter operators, pipeline routing) start every block's own critical
    list (see app_classic.convert_classic_xml), so they land here once per
    BLOCK, not once per file - a real CustomerA customer file
    (Creation_CentralRef, 8 blocks) repeats the same 3 file-level notes
    8 times each. That's real duplication, not just volume, and it's the
    biggest single reason a folder-scale run's warning list read as
    "excessive" rather than just long. Deduping only affects this grouped
    SUMMARY (and the warnings.txt artifact built from it) - the raw,
    undeduped list is still `warnings` itself and still lands in
    results.json in full, so nothing is actually lost, only shown once.

    This is what turns a folder-scale run (dozens of resources, each
    contributing a paragraph or two) into something scannable: one
    collapsible group per resource instead of one flat wall of text. See
    `run_app_import`'s docstring and CLAUDE.md's App import section for why
    this got added - a real CustomerA customer folder (21 workflows) made the
    old flat list genuinely unreadable.
    """
    groups = {}
    for warning in warnings:
        resource, sep, message = warning.partition(": ")
        key, text = (resource, message) if sep else ("General", warning)
        groups.setdefault(key, [])
        if text not in groups[key]:
            groups[key].append(text)
    return groups


def _warnings_artifact_text(warnings: list) -> str:
    """Plain-text rendering of every run-level warning, grouped by resource and deduped - the full (but not repeated) detail the UI summarizes rather than dumps inline."""
    if not warnings:
        return "No warnings for this run.\n"
    groups = _group_warnings_by_resource(warnings)
    deduped_total = sum(len(messages) for messages in groups.values())
    lines = [f"{deduped_total} distinct warning(s) ({len(warnings)} raw, before de-duping repeats "
             "across a multi-block file's blocks), grouped by resource:", ""]
    for resource, messages in groups.items():
        lines.append(f"== {resource} ({len(messages)}) ==")
        for message in messages:
            lines.append("  - " + message)
        lines.append("")
    return "\n".join(lines)


def run_app_import(root: str, connection_id: str, tenant_id: str, environment_id: str,
                   bearer_token: str = "", dry_run: bool = False,
                   write_prompt_to_folder: bool = True, ion_api_base_url: str = "",
                   base_names: dict = None, design_matches: dict = None, progress=None,
                   header_row: int = None) -> dict:
    """
    Import a whole app folder into the target tenant.

    Tables are created first (workflows reference them by name), then
    workflows, then Live Groups for any source Classic XML file whose
    blocks became >= 2 workflows, then the App Builder prompt is rendered
    from what actually happened. Individual failures do not abort the run -
    a partial import with a clear per-resource report is more useful than a
    half-done one that stops at the first error, and every resource here is
    independent.

    `ion_api_base_url` is a separate, explicit, optional argument - not
    pulled from a stored Target Tenant preset - because it is genuinely
    per-customer: different systems mean different ION API roots even
    within the same connection/tenant/environment ids. It is only used by
    Classic XML blocks that
    `source_classic_xml.unrepresentable_filter_operators` routes onto the
    generic-pipeline path; omitting it just means those specific blocks
    fall back to native with a loud warning, same as before this routing
    existed - nothing else is affected.

    `base_names`, keyed by a Classic XML's app-relative path, is the base
    name the caller (a person, via the CLI prompt or the web UI's per-row
    field) chose for that file - see `app_classic.convert_classic_xml` for
    how it composes into each block's workflow name, and `_create_groups`
    for how it becomes the resulting Group's name too. A file with no entry
    falls back to its own stem in both places.

    `design_matches` maps a design file's app-relative path to the Classic
    XML or workflow JSON it belongs to ("" = do not upload) - a person's
    answer for the ones `source_app_folder.match_design_files` could not
    pair on its own. See that function for the automatic rules.

    `progress`, if given, is called with {"phase", "done", "total", "item"}
    as the run moves along (the web UI polls it to show "2/6 workflows").
    """
    _report(progress, "Scanning the folder", 0, 0)
    bundle = _scan_with_matches(root, design_matches)
    warnings = list(bundle.warnings)

    if not dry_run:
        if not bearer_token:
            return {"ok": False, "error": "A bearer token is required to import an app into Live.",
                    "plan": bundle.summary()}
        warnings.extend(check_target_ids(connection_id, tenant_id, environment_id))
    elif bundle.classic_xml:
        # A dry run normally skips this check because it sends nothing. It
        # can't here: converting an XML BAKES the three ids into a file
        # written to disk, so a malformed id produces a saved workflow that
        # renders broken in the builder long after the "dry" run.
        warnings.extend(check_target_ids(connection_id, tenant_id, environment_id))

    target = {"connectionId": connection_id, "tenantId": tenant_id, "environmentId": environment_id}
    artifacts = _artifacts_dir(bundle.name)
    _write_artifact(artifacts, "plan.json", bundle.summary())

    # Before tables and workflows both: this appends to bundle.workflows, so
    # a converted XML is imported by the same code as any other workflow.
    if bundle.classic_xml:
        _report(progress, "Converting Classic XML", 0, len(bundle.classic_xml))
    classic_results = _convert_classic(bundle, connection_id, tenant_id, environment_id,
                                       bearer_token, artifacts, warnings, ion_api_base_url,
                                       base_names, header_row)

    # Runs after conversion: a design file can pair with a workflow that
    # only exists once its Classic XML has been converted.
    for design in bundle.design_files:
        for warning in design.warnings:
            warnings.append(design.path + ": " + warning)
        if design.target and not design.workflow_path:
            warnings.append(
                design.path + ": matched to " + design.target + ", but no workflow was built "
                "from it - not uploaded.")

    table_results = _import_tables(bundle, target, bearer_token, artifacts, dry_run, progress)
    workflow_results = _import_workflows(bundle, target, bearer_token, artifacts, dry_run, progress)
    group_results = _create_groups(bundle, workflow_results, base_names, target, bearer_token, dry_run,
                                   progress)
    if not dry_run:
        _report(progress, "Writing the App Builder prompt", 0, 0)

    # A dry run creates nothing in Live, so a prompt built from it would be
    # built from placeholders (no real workflow ids, statuses that never
    # happened) - not worth generating at all, not just worth withholding
    # from the app folder as before. `app-scan` (or this same call with
    # dry_run) already previews what WOULD happen; the prompt is for after
    # something real exists to point App Builder at.
    prompt = None
    prompt_path = None
    folder_prompt_path = None
    if dry_run:
        warnings.append(
            "Dry run - no App Builder prompt was generated. It needs real workflow ids and table "
            "statuses to be worth anything; run for real to get one."
        )
    else:
        prompt = build_app_prompt(bundle, table_results, workflow_results,
                                  connection_id, tenant_id, environment_id)
        prompt_path = _write_artifact(artifacts, "app-builder-prompt.md", prompt)
        if write_prompt_to_folder:
            folder_prompt_path = _write_prompt_into_app_folder(bundle.root, prompt, warnings)

    summary = {
        "designFilesUploaded": sum(1 for r in workflow_results
                                   if (r.get("designFile") or {}).get("ok")),
        "designFilesFailed": sum(1 for r in workflow_results
                                 if (r.get("designFile") or {}).get("ok") is False),
        "xmlConverted": sum(1 for r in classic_results if r.get("ok") and not r.get("skipped")),
        "xmlSkipped": sum(1 for r in classic_results if r.get("skipped")),
        "xmlFailed": sum(1 for r in classic_results if r.get("ok") is False),
        "xmlWithMissingMetadata": sum(1 for r in classic_results if r.get("missingMetadataFields")),
        "xmlOnPipelinePath": sum(1 for r in classic_results if r.get("buildPath") == "pipeline"),
        "tablesCreated": sum(1 for r in table_results if r.get("ok") and not r.get("skipped")),
        "tablesSkipped": sum(1 for r in table_results if r.get("skipped")),
        "tablesFailed": sum(1 for r in table_results if r.get("ok") is False),
        "workflowsCreated": sum(1 for r in workflow_results if r.get("ok") and not r.get("skipped")),
        "workflowsSkipped": sum(1 for r in workflow_results if r.get("skipped")),
        "workflowsFailed": sum(1 for r in workflow_results if r.get("ok") is False),
        "groupsCreated": sum(1 for r in group_results if r.get("ok")),
        "groupsFailed": sum(1 for r in group_results if r.get("ok") is False),
        "rowsNotImported": sum(len(t.rows) for t in bundle.tables),
    }
    warnings_path = _write_artifact(artifacts, "warnings.txt", _warnings_artifact_text(warnings))
    response = {
        "ok": (summary["tablesFailed"] == 0 and summary["workflowsFailed"] == 0
               and summary["xmlFailed"] == 0),
        "dryRun": dry_run,
        "app": bundle.name,
        "plan": bundle.summary(),
        "classicXml": classic_results,
        "tables": table_results,
        "workflows": workflow_results,
        "groups": group_results,
        "summary": summary,
        "warnings": warnings,
        "warningGroups": [{"resource": resource, "messages": messages}
                          for resource, messages in _group_warnings_by_resource(warnings).items()],
        "warningsPath": warnings_path,
        "prompt": prompt,
        "promptPath": prompt_path,
        "folderPromptPath": folder_prompt_path,
        "artifactsDir": artifacts,
    }
    _write_artifact(artifacts, "results.json", {k: v for k, v in response.items() if k != "prompt"})
    return response
