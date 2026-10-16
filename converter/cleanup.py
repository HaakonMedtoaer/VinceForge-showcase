"""
Delete workflows this tool created, picked from the run history.

Test imports leave duplicates behind in the tenant (and a re-import under a
new base name makes more), which makes it easy to open the wrong one. This
removes chosen ones - and ONLY ones this tool can show it created, because
a wrong delete cannot be undone.

Every workflow goes through the same checks, and any failed check refuses
that one workflow (never the whole batch):

  1. It is in the run history as a SUCCESSFUL workflow creation with a real
     WORKFLOW- id. Groups and tables are not offered: no delete endpoint is
     confirmed for either (and a deleted table's name stays locked for up to
     6 hours).
  2. The token's own tenant is the tenant the row was created in. The token,
     not any id we send, decides which tenant a call acts on.
  3. GET the workflow: a 404 means it is already gone (the row is just
     stamped); otherwise its workflowName or workflowAlias must still equal
     the name we created it under, so an id that somehow points at something
     else is not deleted.
  4. DELETE, then GET again. `DELETE /workflows/{id}` is docs-sourced, not
     captured (see liveapi.delete_workflow), so the only proof of deletion
     accepted is that follow-up GET answering 404.

A deleted row stays in the history, stamped `deletedAt`, since it is still
a true record of what was created.
"""

import datetime

from history import load_history, mark_deleted, resource_name
from liveapi import delete_workflow, get_workflow
from metadata import tenant_from_token, token_expiry_warning


def is_deletable(entry: dict) -> bool:
    """A history row this tool may offer to delete."""
    return (entry.get("kind") in (None, "workflow")
            and bool(entry.get("ok"))
            and str(entry.get("workflowId") or "").startswith("WORKFLOW-")
            and not entry.get("deletedAt"))


def delete_created_workflows(workflow_ids: list, bearer_token: str, history_file: str = None,
                             token_tenant_hint: str = "") -> dict:
    """{"ok", "error"?, "results": [{workflowId, name, status, ok, error?}]}

    `token_tenant_hint` is the tenant a token is known to act on when the token
    itself names none - an API Client's access token carries no tenant claim
    (2026-10-01). The caller must have established it server-side (the token was
    minted for that tenant's preset); a claim in the token always wins."""
    kwargs = {"history_file": history_file} if history_file else {}
    if not bearer_token:
        return {"ok": False, "error": "A bearer token is required to delete workflows.", "results": []}
    expired = token_expiry_warning(bearer_token)
    if expired:
        return {"ok": False, "error": expired, "results": []}
    token_tenant = tenant_from_token(bearer_token) or (token_tenant_hint or None)

    rows = {}
    for entry in load_history(**kwargs):
        if is_deletable(entry):
            rows.setdefault(entry["workflowId"], entry)

    results, gone = [], []
    for workflow_id in dict.fromkeys(workflow_ids):      # de-duplicated, order kept
        entry = rows.get(workflow_id)
        result = {"workflowId": workflow_id, "name": resource_name(entry) if entry else None}
        results.append(result)

        if entry is None:
            result.update(ok=False, status="refused", error=(
                "Not in the run history as a workflow this tool created (or already deleted) - "
                "only those can be deleted from here."))
            continue
        if not token_tenant or token_tenant != entry.get("tenantId"):
            result.update(ok=False, status="refused", error=(
                "The token belongs to tenant " + str(token_tenant) + ", but this workflow was "
                "created in " + str(entry.get("tenantId")) + ". Use a token for that tenant."))
            continue

        before = get_workflow(workflow_id, bearer_token)
        if before.get("status") == 404:
            result.update(ok=True, status="already gone")
            gone.append(workflow_id)
            continue
        if not before.get("ok"):
            result.update(ok=False, status="refused", error=(
                "Couldn't read the workflow before deleting it: " + str(before.get("error"))))
            continue
        live = before.get("body") if isinstance(before.get("body"), dict) else {}
        names = {live.get("workflowName"), live.get("workflowAlias")}
        if result["name"] not in names:
            result.update(ok=False, status="refused", error=(
                "In Live this id is named " + repr(live.get("workflowAlias") or live.get("workflowName")) +
                ", not " + repr(result["name"]) + " as created - not deleting something that "
                "may not be what the history says it is."))
            continue

        deleted = delete_workflow(workflow_id, bearer_token)
        result["deleteStatus"] = deleted.get("status")
        if not deleted.get("ok"):
            result.update(ok=False, status="delete failed", error=str(deleted.get("error")))
            continue
        after = get_workflow(workflow_id, bearer_token)
        if after.get("status") == 404:
            result.update(ok=True, status="deleted")
            gone.append(workflow_id)
        else:
            result.update(ok=False, status="not confirmed", error=(
                "DELETE answered " + str(deleted.get("status")) + ", but the workflow can still be "
                "read (GET " + str(after.get("status")) + "). Check it in Live."))

    if gone:
        mark_deleted(gone, datetime.datetime.now(datetime.timezone.utc).isoformat(), **kwargs)
    return {"ok": all(r.get("ok") for r in results), "results": results}
