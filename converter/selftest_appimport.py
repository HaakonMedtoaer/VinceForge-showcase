"""
Self-test for appimport.py's HTTP-facing workflow logic - stdlib-only,
run-it-directly style, same as the other selftest_*.py files:

    python selftest_appimport.py

Added 2026-09-16, alongside a real, confirmed platform fact: `POST
/workflows` rejects a duplicate workflow name with
`HTTP 409: Workflow "X" already exists!` rather than silently allowing it
(what this project previously assumed, and what CLAUDE.md/
docs/vince-app-import.md said before this same day). Re-running an app
import against a tenant that already has some of its workflows is
therefore the NORMAL case, not a failure - `_import_workflows` now treats
that specific 409 shape as a skip (same treatment `_import_tables` already
gives an existing table), and `_create_groups` has to account for a
skipped member carrying no `workflowId` (there is no confirmed way to look
up an existing workflow's id by name).

What's checked, against real appimport.py functions with the network layer
replaced by fakes (no real HTTP, no real bearer token needed):

  - `_is_duplicate_name_conflict` only fires on a 409 whose body actually
    says "already exists" - any other 409, or any other status, is still
    a real failure.
  - `_import_workflows` marks a duplicate-name 409 as `ok: True,
    skipped: True`, not `ok: False` - it must not count as a failure.
  - `_create_groups` excludes a skipped member's (missing) workflowId from
    the group it creates, and warns by name about which member(s) it could
    not add, rather than silently shrinking the group with no explanation.

Network and history-log calls are monkeypatched for the duration of each
test and always restored in a `finally`, so nothing here ever makes a real
HTTP call or writes to the real RUN/webui_history.json.
"""

import sys
import tempfile

import appimport
from source_app_folder import AppBundle, AppWorkflow

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def _fake_workflow_data(name: str) -> dict:
    return {
        "entity": "Workflow",
        "workflowName": name,
        "definition": {
            "workflowConfig": {"name": name, "clientConfig": {"title": name, "fields": []}, "steps": []},
            "stepConfig": {},
        },
    }


TARGET = {"connectionId": "CONNECTION-" + "a" * 32, "tenantId": "TENANT-" + "b" * 32,
          "environmentId": "ENVIRONMENT-" + "c" * 32}


class _Patched:
    """Swap module-level names in appimport.py for the duration of a `with` block."""

    def __init__(self, **replacements):
        self.replacements = replacements
        self.originals = {}

    def __enter__(self):
        for name in self.replacements:
            self.originals[name] = getattr(appimport, name)
            setattr(appimport, name, self.replacements[name])
        return self

    def __exit__(self, *exc_info):
        for name, original in self.originals.items():
            setattr(appimport, name, original)


def test_is_duplicate_name_conflict_is_narrow():
    check(appimport._is_duplicate_name_conflict(
        {"status": 409, "error": 'HTTP 409: Workflow "Create Specific Area" already exists!'}),
        "a real duplicate-name 409 should be recognised")
    check(not appimport._is_duplicate_name_conflict(
        {"status": 409, "error": "HTTP 409: Some other conflict entirely"}),
        "a 409 that is NOT about an existing name must not be treated as one")
    check(not appimport._is_duplicate_name_conflict(
        {"status": 400, "error": "HTTP 400: Item size to update has exceeded the maximum allowed size"}),
        "a non-409 status must never be treated as a duplicate-name conflict")
    check(not appimport._is_duplicate_name_conflict({"status": None, "error": "Connection refused"}),
          "a network-level failure (no status at all) must not be treated as a duplicate-name conflict")


def test_import_workflows_skips_rather_than_fails_on_duplicate_name():
    bundle = AppBundle(root=tempfile.mkdtemp(), name="test-app", workflows=[
        AppWorkflow(path="workflows/a.json", name="Existing Workflow", source_workflow_id=None,
                    data=_fake_workflow_data("Existing Workflow")),
    ])

    def fake_post_workflow(body, bearer_token):
        return {"ok": False, "status": 409,
                "error": 'HTTP 409: Workflow "Existing Workflow" already exists!', "body": None}

    with _Patched(post_workflow=fake_post_workflow, append_history=lambda entry: None):
        results = appimport._import_workflows(bundle, TARGET, "fake-token", tempfile.mkdtemp(), dry_run=False)

    check(len(results) == 1, f"expected 1 result, got {len(results)}")
    result = results[0]
    check(result.get("ok") is True, f"a duplicate-name conflict must be ok=True (skipped), got {result.get('ok')!r}")
    check(result.get("skipped") is True, f"expected skipped=True, got {result.get('skipped')!r}")
    check(result.get("workflowId") is None, "a skipped workflow has no real id - nothing to fabricate one from")
    check(any("already exists" in w for w in result.get("warnings", [])),
          f"expected a warning explaining the skip, got {result.get('warnings')}")


def test_import_workflows_still_fails_on_a_real_conflict():
    bundle = AppBundle(root=tempfile.mkdtemp(), name="test-app", workflows=[
        AppWorkflow(path="workflows/a.json", name="Broken Workflow", source_workflow_id=None,
                    data=_fake_workflow_data("Broken Workflow")),
    ])

    def fake_post_workflow(body, bearer_token):
        return {"ok": False, "status": 400,
                "error": "HTTP 400: Item size to update has exceeded the maximum allowed size", "body": None}

    with _Patched(post_workflow=fake_post_workflow, append_history=lambda entry: None):
        results = appimport._import_workflows(bundle, TARGET, "fake-token", tempfile.mkdtemp(), dry_run=False)

    check(results[0].get("ok") is False, "a genuine failure must still be reported as ok=False, not silently skipped")
    check(results[0].get("skipped") is not True, "a genuine failure must not be marked skipped")


def test_create_groups_excludes_skipped_members_and_warns_by_name():
    bundle = AppBundle(root=tempfile.mkdtemp(), name="test-app", workflows=[
        AppWorkflow(path="workflows/a.json", name="File - Block A", source_workflow_id=None,
                    data=_fake_workflow_data("File - Block A"), group_key="file.xml"),
        AppWorkflow(path="workflows/b.json", name="File - Block B", source_workflow_id=None,
                    data=_fake_workflow_data("File - Block B"), group_key="file.xml"),
        AppWorkflow(path="workflows/c.json", name="File - Block C", source_workflow_id=None,
                    data=_fake_workflow_data("File - Block C"), group_key="file.xml"),
    ])
    # Two real creates, one skip (already existed - no workflowId available).
    workflow_results = [
        {"ok": True, "workflowId": "WORKFLOW-" + "1" * 32},
        {"ok": True, "workflowId": "WORKFLOW-" + "2" * 32},
        {"ok": True, "skipped": True, "workflowId": None},
    ]

    captured = {}

    def fake_create_group(group_name, workflow_ids, tenant_id, bearer_token):
        captured["workflow_ids"] = workflow_ids
        return {"ok": True, "status": 201, "body": {"groupId": "GROUP-" + "9" * 32}}

    with _Patched(create_group=fake_create_group, append_history=lambda entry: None):
        results = appimport._create_groups(bundle, workflow_results, {"file.xml": "File"},
                                           TARGET, "fake-token", dry_run=False)

    check(len(results) == 1, f"expected 1 group result, got {len(results)}")
    group = results[0]
    check(group.get("ok") is True, f"2 real ids should still be enough to create a group, got {group}")
    check(captured.get("workflow_ids") == ["WORKFLOW-" + "1" * 32, "WORKFLOW-" + "2" * 32],
          f"the skipped member's (missing) id must not reach create_group, got {captured.get('workflow_ids')!r}")
    check(any("File - Block C" in w for w in group.get("warnings", [])),
          f"expected a warning naming the skipped member that could not be added, got {group.get('warnings')}")


def test_design_file_is_linked_the_way_the_builder_does_it():
    # Confirmed 2026-09-29: the server does NOT link an uploaded design
    # file. The builder GETs the workflow, sets the two fields and PUTs the
    # definition back - exactly and only those two fields.
    import copy
    import os
    from source_app_folder import AppDesignFile
    root = tempfile.mkdtemp()
    with open(os.path.join(root, "sheet.xlsx"), "wb") as f:
        f.write(b"stand-in")
    workflow = AppWorkflow(path="workflows/a.json", name="A", source_workflow_id=None,
                           data=_fake_workflow_data("A"))
    workflow.design_file = AppDesignFile(path="sheet.xlsx", filename="sheet.xlsx", stem="sheet", size=8)
    bundle = AppBundle(root=root, name="test-app", workflows=[workflow])

    key = "WORKFLOW-1/designFile/sheet.xlsx"
    stored = {"tenantId": "TENANT-x", "workflowId": "WORKFLOW-1", "stats": {"numRuns": 0},
              "definition": {"workflowConfig": {"hasExcelDesignFile": False, "name": "A"},
                             "stepConfig": {"code_1": {"content": "x"}}}}
    before = copy.deepcopy(stored["definition"])
    puts = []

    def fake_get(workflow_id, token):
        return {"ok": True, "status": 200, "body": copy.deepcopy(stored), "error": None}

    def fake_put(tenant_id, workflow_id, definition, token):
        puts.append((tenant_id, workflow_id, copy.deepcopy(definition)))
        stored["definition"] = copy.deepcopy(definition)
        return {"ok": True, "status": 200, "body": {}, "error": None}

    def fake_upload(workflow_id, filename, data, token):
        return {"ok": True, "key": key, "steps": [{"step": "put", "ok": True, "status": 200}]}

    with _Patched(get_workflow=fake_get, update_workflow=fake_put, upload_design_file=fake_upload):
        outcome = appimport._upload_design_file(bundle, workflow, "WORKFLOW-1", "token")

    check(outcome.get("status") == "uploaded and linked" and outcome.get("ok"),
          f"expected the file to end up linked, got {outcome}")
    check(len(puts) == 1 and puts[0][:2] == ("TENANT-x", "WORKFLOW-1"),
          f"expected one PUT carrying the workflow's own tenant and id, got {[p[:2] for p in puts]}")
    if puts:
        expected = copy.deepcopy(before)
        expected["workflowConfig"].update(hasExcelDesignFile=True, excelDesignFileKey=key)
        check(puts[0][2] == expected,
              "the PUT changed more than the two design-file fields - the builder changes only those")


def test_import_workflows_reports_progress_per_workflow():
    names = ["One", "Two", "Three"]
    bundle = AppBundle(root=tempfile.mkdtemp(), name="test-app", workflows=[
        AppWorkflow(path="workflows/%s.json" % n, name=n, source_workflow_id=None, data=_fake_workflow_data(n))
        for n in names
    ])
    seen = []

    def fake_post_workflow(body, bearer_token):
        return {"ok": True, "status": 200, "body": {"workflowId": "WORKFLOW-" + "d" * 32}, "error": None}

    with _Patched(post_workflow=fake_post_workflow, append_history=lambda entry: None):
        appimport._import_workflows(bundle, TARGET, "fake-token", tempfile.mkdtemp(), dry_run=False,
                                    progress=seen.append)

    counts = [(p["done"], p["total"], p["item"]) for p in seen]
    check(counts == [(0, 3, "One"), (1, 3, "Two"), (2, 3, "Three"), (3, 3, "")],
          f"expected one report before each workflow and a final 3/3, got {counts}")

    def broken(state):
        raise RuntimeError("a UI callback blew up")

    with _Patched(post_workflow=fake_post_workflow, append_history=lambda entry: None):
        results = appimport._import_workflows(bundle, TARGET, "fake-token", tempfile.mkdtemp(), dry_run=False,
                                              progress=broken)
    check(len(results) == 3 and all(r.get("ok") for r in results),
          "a failing progress callback must never break the import itself")


def test_clone_remaps_chained_input_values():
    """A chained input's `value` ("<id>.output.X") must follow the new ids."""
    import source_live_json
    txn = lambda i, inp: {"id": i, "executionOrder": 1, "input": inp, "output": {}}
    data = {"definition": {
        "workflowConfig": {"name": "W", "clientConfig": {"fields": []}, "steps": []},
        "stepConfig": {"m3_api_1": {
            "xAaa": txn("xAaa", {}),
            "xBbb": txn("xBbb", {"ITNO": {"source": "API", "value": "xAaa.output.ITNO",
                                          "ref": {"apiId": "xAaa", "name": "ITNO", "type": "OUTPUT"}}}),
        }}}}
    out = source_live_json.clone_and_retarget(data, "C", "T", "E", preserve_name=True)
    new = out["definition"]["stepConfig"]["m3_api_1"]
    consumer = next(t for t in new.values() if t["input"])["input"]["ITNO"]
    producer_id = consumer["ref"]["apiId"]
    check(producer_id in new and producer_id != "xAaa", "ref.apiId must point at a new id")
    check(consumer["value"] == f"{producer_id}.output.ITNO",
          f"value must follow the new id, got {consumer['value']!r}")


def test_clone_remaps_expand_target_sheet_and_field_references():
    """
    An Expand workflow (seen in an earlier converter's CustomerB JSON) writes through
    excel_1.targetSheet, and the builder records chains in
    workflowConfig.apiFieldReferences - both name transaction ids, and both
    were left on the OLD ids, with no clientConfig tolerated either.
    """
    import source_live_json
    col = lambda ref: {"fieldId": ref, "field": {"fieldId": ref}, "primaryHeader": {"fieldId": ref}}
    data = {"definition": {
        "workflowConfig": {"name": "W", "steps": [],
                           "apiFieldReferences": {"xBbb.input.ITNO": "xAaa.output.ITNO",
                                                  "xGone.input.ITNO": "xAaa.output.ITNO"}},
        "stepConfig": {
            "m3_api_1": {"xAaa": {"id": "xAaa", "input": {}, "output": {}},
                         "xBbb": {"id": "xBbb", "input": {}, "output": {}}},
            "excel_1": {"sourceSheet": {"fields": {"columns": {"B": col("xAaa.input.PRRF")}}},
                        "targetSheet": {"fields": {"columns": {"D": col("xBbb.output.ITDS")}}}}}}}
    out = source_live_json.clone_and_retarget(data, "C", "T", "E", preserve_name=True)
    definition = out["definition"]
    ids = set(definition["stepConfig"]["m3_api_1"])
    target = definition["stepConfig"]["excel_1"]["targetSheet"]["fields"]["columns"]["D"]
    check(target["fieldId"].split(".")[0] in ids and target["field"]["fieldId"] == target["fieldId"],
          f"targetSheet column still names an old id: {target}")
    refs = definition["workflowConfig"]["apiFieldReferences"]
    check(len(refs) == 1 and all(k.split(".")[0] in ids and v.split(".")[0] in ids for k, v in refs.items()),
          f"apiFieldReferences not remapped (or a dangling entry kept): {refs}")


def test_imported_label_only_when_the_sheet_feeds_an_input():
    # Name kept for the test list; the label is unconditional since 2026-10-16.
    from envelope import with_imported_label, IMPORTED_LABEL
    def cfg(src):
        return {"m3_api_1": {"t1": {"input": {"ITNO": {"source": src}}, "output": {}}}}
    check(with_imported_label(["VXL"], {"hasSpreadSheetInput": True}, cfg("EXCEL")) == ["VXL", IMPORTED_LABEL],
          "a sheet-fed workflow should carry the Imported label")
    check(with_imported_label([], {}, cfg("CLIENT")) == [IMPORTED_LABEL],
          "a native export (no sheet input) must carry the Imported label too")
    check(with_imported_label([IMPORTED_LABEL], {}, cfg("CONSTANT")) == [IMPORTED_LABEL],
          "the Imported label must not be duplicated")


def test_spreadsheet_flag_cleared_when_no_sheet_input():
    from envelope import clear_unused_spreadsheet_flag
    native = {"m3_api_1": {"t1": {"input": {"ITNO": {"source": "CLIENT"}}, "output": {}}}}
    wc = {"hasSpreadSheetInput": True}
    clear_unused_spreadsheet_flag(wc, native)
    check(wc["hasSpreadSheetInput"] is False, "native + no EXCEL input must clear hasSpreadSheetInput")
    fed = {"m3_api_1": {"t1": {"input": {"ITNO": {"source": "EXCEL"}}, "output": {}}}}
    wc = {"hasSpreadSheetInput": True}
    clear_unused_spreadsheet_flag(wc, fed)
    check(wc["hasSpreadSheetInput"] is True, "a sheet-fed input keeps the flag")
    wc = {"hasSpreadSheetInput": True}
    clear_unused_spreadsheet_flag(wc, {"code_1": {"type": "CODE"}})
    check(wc["hasSpreadSheetInput"] is True, "a Code-step workflow keeps the flag")


def main():
    tests = (
        test_is_duplicate_name_conflict_is_narrow,
        test_import_workflows_skips_rather_than_fails_on_duplicate_name,
        test_import_workflows_still_fails_on_a_real_conflict,
        test_create_groups_excludes_skipped_members_and_warns_by_name,
        test_design_file_is_linked_the_way_the_builder_does_it,
        test_import_workflows_reports_progress_per_workflow,
        test_clone_remaps_chained_input_values,
        test_clone_remaps_expand_target_sheet_and_field_references,
        test_imported_label_only_when_the_sheet_feeds_an_input,
        test_spreadsheet_flag_cleared_when_no_sheet_input,
    )
    for test in tests:
        try:
            test()
        except Exception as error:      # a crash is a failure, not a reason to stop
            failures.append(f"{test.__name__} raised: {type(error).__name__}: {error}")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"All {len(tests)} appimport self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
