"""
Self-tests for cleanup.py - deleting workflows this tool created. Run it
directly. Network and token checks are mocked, and a temporary history file
is used, so this never touches Live or the real RUN/webui_history.json.

A wrong delete cannot be undone, so the subject here is REFUSAL: every
check that should stop a delete is exercised, plus the one thing accepted
as proof of deletion (a 404 on the follow-up GET).
"""

import json
import os
import sys
import tempfile

import cleanup
from history import load_history, resource_name

failures = []

TENANT = "TENANT-" + "a" * 32
OTHER_TENANT = "TENANT-" + "b" * 32


def check(condition, message):
    if not condition:
        failures.append(message)


def _history(rows):
    path = os.path.join(tempfile.mkdtemp(), "history.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return path


def _row(workflow_id, name, **extra):
    row = {"timestamp": "2026-09-29T12:00:00+00:00", "source": "app-import", "app": "VL",
           "kind": "workflow", "workflowName": "VL / " + name + " (workflow)",
           "tenantId": TENANT, "ok": True, "workflowId": workflow_id}
    row.update(extra)
    return row


class _Live:
    """A fake tenant: {workflow id: name}, recording every DELETE."""

    def __init__(self, workflows, delete_works=True):
        self.workflows = dict(workflows)
        self.deleted = []
        self.delete_works = delete_works

    def get(self, workflow_id, token):
        if workflow_id not in self.workflows:
            return {"ok": False, "status": 404, "body": None, "error": "HTTP 404"}
        name = self.workflows[workflow_id]
        return {"ok": True, "status": 200, "error": None,
                "body": {"workflowId": workflow_id, "workflowName": name, "workflowAlias": name}}

    def delete(self, workflow_id, token):
        self.deleted.append(workflow_id)
        if self.delete_works:
            self.workflows.pop(workflow_id, None)
        return {"ok": True, "status": 200, "body": None, "error": None}


def _run(live, ids, history_file, token_tenant=TENANT, hint=""):
    saved = (cleanup.get_workflow, cleanup.delete_workflow, cleanup.tenant_from_token,
             cleanup.token_expiry_warning)
    cleanup.get_workflow, cleanup.delete_workflow = live.get, live.delete
    cleanup.tenant_from_token = lambda token: token_tenant
    cleanup.token_expiry_warning = lambda token: None
    try:
        return cleanup.delete_created_workflows(ids, "token", history_file=history_file,
                                                token_tenant_hint=hint)
    finally:
        (cleanup.get_workflow, cleanup.delete_workflow, cleanup.tenant_from_token,
         cleanup.token_expiry_warning) = saved


def test_a_token_without_a_tenant_claim_uses_the_server_vouched_hint():
    # An API Client's access token names no tenant (seen 2026-10-01).
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({"WORKFLOW-1": "A"})
    result = _run(live, ["WORKFLOW-1"], path, token_tenant=None)
    check(_status(result, "WORKFLOW-1")["status"] == "refused" and live.deleted == [],
          f"a token with no tenant and no hint was allowed to delete: {result}")
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({"WORKFLOW-1": "A"})
    result = _run(live, ["WORKFLOW-1"], path, token_tenant=None, hint=TENANT)
    check(_status(result, "WORKFLOW-1")["status"] == "deleted", f"the vouched hint was not used: {result}")
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({"WORKFLOW-1": "A"})
    result = _run(live, ["WORKFLOW-1"], path, token_tenant=None, hint=OTHER_TENANT)
    check(_status(result, "WORKFLOW-1")["status"] == "refused" and live.deleted == [],
          f"a hint for another tenant was allowed to delete: {result}")
    # a claim in the token always wins over the hint
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({"WORKFLOW-1": "A"})
    result = _run(live, ["WORKFLOW-1"], path, token_tenant=OTHER_TENANT, hint=TENANT)
    check(_status(result, "WORKFLOW-1")["status"] == "refused" and live.deleted == [],
          f"the hint overrode a tenant claim in the token: {result}")


def _status(result, workflow_id):
    return next(r for r in result["results"] if r["workflowId"] == workflow_id)


def test_a_created_workflow_is_deleted_and_stamped():
    path = _history([_row("WORKFLOW-1", "S08 - Create")])
    live = _Live({"WORKFLOW-1": "S08 - Create"})
    result = _run(live, ["WORKFLOW-1"], path)
    check(_status(result, "WORKFLOW-1")["status"] == "deleted", f"expected deleted, got {result}")
    check(live.deleted == ["WORKFLOW-1"], f"expected exactly one DELETE, got {live.deleted}")
    check(load_history(path)[0].get("deletedAt"), "the history row was not stamped deletedAt")
    check(not cleanup.is_deletable(load_history(path)[0]), "a deleted row is still offered for deletion")


def test_only_workflows_from_the_history_can_be_deleted():
    path = _history([_row("WORKFLOW-1", "A"),
                     _row("GROUP-1", "A", kind="group"),
                     _row("WORKFLOW-2", "B", ok=False)])
    live = _Live({"WORKFLOW-1": "A", "WORKFLOW-2": "B", "WORKFLOW-9": "Hand-built", "GROUP-1": "A"})
    result = _run(live, ["WORKFLOW-9", "GROUP-1", "WORKFLOW-2"], path)
    check(all(r["status"] == "refused" for r in result["results"]),
          f"an id not created by this tool (or a group, or a failed create) was not refused: {result}")
    check(live.deleted == [], f"something was deleted anyway: {live.deleted}")


def test_a_token_for_another_tenant_is_refused():
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({"WORKFLOW-1": "A"})
    result = _run(live, ["WORKFLOW-1"], path, token_tenant=OTHER_TENANT)
    check(_status(result, "WORKFLOW-1")["status"] == "refused" and live.deleted == [],
          f"a token for a different tenant was allowed to delete: {result}")


def test_a_workflow_whose_name_changed_is_not_deleted():
    path = _history([_row("WORKFLOW-1", "S08 - Create")])
    live = _Live({"WORKFLOW-1": "Something else entirely"})
    result = _run(live, ["WORKFLOW-1"], path)
    check(_status(result, "WORKFLOW-1")["status"] == "refused" and live.deleted == [],
          f"a workflow whose name no longer matches the history was deleted: {result}")


def test_already_gone_is_not_an_error():
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({})
    result = _run(live, ["WORKFLOW-1"], path)
    check(_status(result, "WORKFLOW-1")["status"] == "already gone" and live.deleted == [],
          f"a workflow already missing in Live should just be stamped: {result}")
    check(load_history(path)[0].get("deletedAt"), "an already-gone workflow was not stamped")


def test_a_delete_that_did_not_take_is_reported_not_trusted():
    path = _history([_row("WORKFLOW-1", "A")])
    live = _Live({"WORKFLOW-1": "A"}, delete_works=False)
    result = _run(live, ["WORKFLOW-1"], path)
    check(_status(result, "WORKFLOW-1")["status"] == "not confirmed",
          f"a DELETE answering 200 while the workflow is still readable was trusted: {result}")
    check(not load_history(path)[0].get("deletedAt"), "an unconfirmed delete was stamped deletedAt")


def test_resource_name_unwraps_old_app_import_rows():
    check(resource_name(_row("WORKFLOW-1", "S08 - Create")) == "S08 - Create",
          "the app-import display name was not unwrapped")
    check(resource_name({"workflowName": "Check Data 6"}) == "Check Data 6",
          "a single-create row's name was altered")
    check(resource_name({"resourceName": "X", "workflowName": "VL / Y (workflow)"}) == "X",
          "resourceName should win when present")


def main():
    tests = (
        test_a_created_workflow_is_deleted_and_stamped,
        test_only_workflows_from_the_history_can_be_deleted,
        test_a_token_for_another_tenant_is_refused,
        test_a_token_without_a_tenant_claim_uses_the_server_vouched_hint,
        test_a_workflow_whose_name_changed_is_not_deleted,
        test_already_gone_is_not_an_error,
        test_a_delete_that_did_not_take_is_reported_not_trusted,
        test_resource_name_unwraps_old_app_import_rows,
    )
    for test in tests:
        try:
            test()
        except Exception as error:
            failures.append(f"{test.__name__} raised: {type(error).__name__}: {error}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"All {len(tests)} cleanup self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
