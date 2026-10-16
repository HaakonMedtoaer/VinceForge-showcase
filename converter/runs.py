"""
The "Runs" inspector: what a workflow's executions actually did, read from
Live's execution-results API (docs/vince-live-api-reference-prompt.md §1e).

Two scopes:

  * ONE workflow - its execution list, and for one execution a step-by-step
    trace in RUN order: records in, records out, success, duration, and a
    tally of M3's per-row messages. This is the only confirmed way to see
    where a run passed or dropped rows; before this it meant dumping raw
    JSON by hand (RUN/filter-debug/).
  * The whole TENANT - every workflow the token can see, each with its run
    count, last run and how many of its listed runs failed. The sweep that
    answers "which of this customer's workflows are actually broken" rather
    than "is this one broken".

## Strictly read-only

Every call here is a GET through liveapi's execution-results functions and
list_workflows. Nothing can start, stop or change a run, which is what makes
it safe to point at a customer's production tenant.

## What is confirmed and what is not

Confirmed (real GETs, 2026-09-28): the three result paths; the record's
`payload.trigger.header`/`body` as the run's input; `payload.result.history`
as one entry per step, LATEST FIRST, each `{stepId, success, duration,
recordsProcessed, data, files}`; M3's per-row message in `<txnId>_MSG_COL`.

NOT confirmed, and handled by saying so rather than guessing:
  * whether the results LIST pages. It answers `{count, total, items}`; when
    count < total the listing is reported as partial (`partial: true` plus a
    warning) - no paging parameter is sent, because the workflow list's
    `limit`/`startKey` is confirmed for that endpoint only.
  * the list items' `status` vocabulary - shown verbatim, never interpreted.
    `failed` (a boolean) is what counts a failure.
  * the workflow-list item's name/stats field names. Several candidates are
    tried (`_workflow_name`), and `stats.numRuns` is used only to SKIP
    workflows that report zero runs when the field is present - a workflow
    without it is always asked.
"""

import concurrent.futures
import datetime
import json
import os
from collections import Counter

import liveapi
from metadata import clean_token, tenant_from_token, token_expiry_warning

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "..", "RUN", "runs")

MSG_COL_SUFFIX = "_MSG_COL"

# Parallel GETs for a tenant sweep. Eight is what VincePrompter's own
# full-tenant scans use without trouble; more buys little against a
# ~1-hour token and risks looking like abuse on a customer tenant.
SWEEP_WORKERS = 8


class RunsError(Exception):
    pass


# --- one execution -----------------------------------------------------------

def _rows(value) -> list:
    return value if isinstance(value, list) else []


def message_tally(rows: list) -> dict:
    """`{<txnId>_MSG_COL: {message: count}}` over a step's output rows.
    Blank messages are counted as "" - on M3 API output an empty message is
    what a row with no complaint looks like, and dropping them would hide how
    many rows went through clean."""
    tally = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if key.endswith(MSG_COL_SUFFIX):
                tally.setdefault(key, Counter())[str(value if value is not None else "")] += 1
    return {key: dict(counter.most_common()) for key, counter in tally.items()}


def summarize_execution(record: dict) -> dict:
    """A run's input and its step trace, in RUN order.

    `rowsIn` of a step is the previous step's `rowsOut` (the trigger's own
    rows for the first step) - that pairing is what shows where rows were
    dropped. It is an inference from step order, which is why each step also
    keeps Live's own `recordsProcessed` beside it rather than replacing it.
    """
    if not isinstance(record, dict):
        raise RunsError("execution record is not a JSON object")
    payload = record.get("payload") or {}
    trigger = payload.get("trigger") or {}
    trigger_rows = _rows(trigger.get("body"))
    history = _rows((payload.get("result") or {}).get("history"))

    steps, previous_out = [], len(trigger_rows)
    for entry in reversed(history):              # Live lists the LATEST step first
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        rows_out = len(data) if isinstance(data, list) else None
        step = {
            "stepId": entry.get("stepId"),
            "success": entry.get("success"),
            "durationMs": entry.get("duration"),
            "recordsProcessed": entry.get("recordsProcessed"),
            "rowsIn": previous_out,
            "rowsOut": rows_out,
            "messages": message_tally(_rows(data)),
            "files": len(_rows(entry.get("files"))),
        }
        steps.append(step)
        if rows_out is not None:
            previous_out = rows_out

    failed_step = next((s["stepId"] for s in steps if s["success"] is False), None)
    return {
        "executionId": record.get("executionId"),
        "workflowId": record.get("workflowId"),
        "startTime": record.get("startTime"),    # absent on the captured records - the LIST has it
        "endTime": record.get("endTime"),
        "status": record.get("status"),
        "failed": record.get("failed"),
        "error": record.get("error"),
        "m3Environment": (payload.get("params") or {}).get("m3EnvironmentName"),
        "triggerHeader": trigger.get("header") or {},
        "inputRows": len(trigger_rows),
        # OBSERVED 2026-09-29 (WFEXEC-a2e7c05e...): a bigger run is stored
        # OUTSIDE the record - trigger.body is null and the rows sit in the
        # file named by payload.triggerFile, the output in
        # result.dataFileKey, and the step's own data is null. The OUTPUT is
        # read by inspect_run through /data's vl-redirect (how the Excel
        # add-in does it); the input file has no known read route (the
        # /v1/files/tenant/download-url/<key> presign answers 403), so it is
        # only named here.
        "inputFile": payload.get("triggerFile") or None,
        "inputSnapshotFile": (trigger.get("executionParams") or {}).get("inputSnapshotFileKey"),
        "outputFile": (payload.get("result") or {}).get("dataFileKey"),
        "steps": steps,
        "failedStep": failed_step,
        "notes": _trace_notes(record, steps, len(trigger_rows), payload.get("triggerFile")),
    }


def _trace_notes(record: dict, steps: list, input_rows: int, input_file: str = None) -> list:
    """Plain-language reading of a trace - only things the record itself
    shows, never a guess at why."""
    notes = []
    if record.get("failed"):
        notes.append("The run is marked failed" +
                     (": " + str(record.get("error")) if record.get("error") else " (no error text)."))
    if not steps:
        notes.append("No step history in the record - the run may not have started a step.")
    if input_rows == 0 and steps and input_file:
        notes.append("The input rows are not in the run record - Live stored them in a file (" +
                     input_file + "), and the output likewise, so row counts here read 0/None. "
                     "recordsProcessed is still the step's own count.")
    elif input_rows == 0 and steps:
        notes.append("The run had no input rows (trigger body empty), so every step saw nothing.")
    # A later step (Excel) passes the M3 step's message columns straight
    # through, so a column is noted once, at the step it first appears on.
    noted_columns = set()
    for step in steps:
        if step["success"] is False:
            notes.append(f"Step {step['stepId']} reports success=false.")
        if step["rowsOut"] is not None and step["rowsIn"] and step["rowsOut"] < step["rowsIn"]:
            notes.append(f"Step {step['stepId']}: {step['rowsIn']} row(s) in, {step['rowsOut']} out.")
        for column, counts in step["messages"].items():
            if column in noted_columns:
                continue
            noted_columns.add(column)
            complaints = sum(n for msg, n in counts.items() if msg)
            if complaints:
                notes.append(f"Step {step['stepId']}: {complaints} row(s) carry an M3 message in "
                             f"{column} - see the message tally.")
    return notes


def summarize_execution_list(body) -> dict:
    """The results list, newest first, with the partial-listing check."""
    body = body if isinstance(body, dict) else {}
    items = [i for i in _rows(body.get("items")) if isinstance(i, dict)]
    items.sort(key=lambda i: str(i.get("startTime") or ""), reverse=True)
    count, total = body.get("count"), body.get("total")
    listed = len(items)
    partial = isinstance(total, int) and total > listed
    warnings = []
    if partial:
        warnings.append(f"Live reports {total} execution(s) but listed {listed}. Paging this endpoint "
                        "is not confirmed, so older runs are not shown and failure counts cover the "
                        "listed runs only.")
    if body.get("lastKey"):
        warnings.append("The listing carries a lastKey, i.e. more exist - paging this endpoint is not "
                        "confirmed, so it is not followed.")
        partial = True
    return {
        "count": count if isinstance(count, int) else listed,
        "total": total if isinstance(total, int) else None,
        "partial": partial,
        "executions": [{
            "executionId": i.get("executionId"),
            "startTime": i.get("startTime"),
            "endTime": i.get("endTime"),
            "status": i.get("status"),
            "failed": i.get("failed"),
            "error": i.get("error"),
        } for i in items],
        "warnings": warnings,
    }


# --- entry points ------------------------------------------------------------

def _check_token(bearer_token: str) -> str:
    token = clean_token(bearer_token or "")
    if not token:
        raise RunsError("No bearer token.")
    expired = token_expiry_warning(token)
    if expired and "EXPIRED" in expired:
        raise RunsError(expired)
    return token


def _response_error(response: dict, what: str) -> str:
    status = response.get("status")
    if status == 401:
        return f"401 on {what} - the token has expired or is malformed."
    if status == 404:
        return f"404 on {what} - no such workflow/execution in the token's tenant."
    return f"{what} failed: {response.get('error')}"


def list_runs(workflow_id: str, bearer_token: str) -> dict:
    token = _check_token(bearer_token)
    response = liveapi.list_executions(workflow_id, token)
    if not response["ok"]:
        raise RunsError(_response_error(response, "the execution list"))
    return {"workflowId": workflow_id, **summarize_execution_list(response["body"])}


def inspect_run(workflow_id: str, execution_id: str, bearer_token: str, with_output: bool = True) -> dict:
    """One execution's trace, plus its raw record (and final output) so the
    caller can save exactly what Live said alongside our reading of it."""
    token = _check_token(bearer_token)
    response = liveapi.get_execution(workflow_id, execution_id, token)
    if not response["ok"]:
        raise RunsError(_response_error(response, "the execution record"))
    result = {"trace": summarize_execution(response["body"]), "raw": {"detail": response["body"]}}
    if with_output:
        output = liveapi.get_execution_data(workflow_id, execution_id, token)
        if output["ok"]:
            result["raw"]["data"] = output["body"]
            result["trace"]["finalOutputRows"] = len(output["body"]) if isinstance(output["body"], list) else None
            if output.get("redirected"):
                result["trace"]["notes"].append("The final output was stored as a file and read through "
                                                "the vl-redirect link Live returned.")
        else:
            result["trace"]["notes"].append("Could not read the final output: " + str(output.get("error")))
    return result


def _workflow_name(item: dict) -> str:
    for key in ("workflowName", "name", "workflowAlias"):
        if item.get(key):
            return str(item[key])
    return str(item.get("workflowId"))


def _num_runs(item: dict):
    stats = item.get("stats")
    value = stats.get("numRuns") if isinstance(stats, dict) else None
    return value if isinstance(value, int) else None


def _sweep_one(item: dict, token: str) -> dict:
    workflow_id = item.get("workflowId")
    row = {"workflowId": workflow_id, "name": _workflow_name(item), "numRuns": _num_runs(item),
           "listed": 0, "total": None, "partial": False, "failedListed": 0,
           "lastStart": None, "lastStatus": None, "lastFailed": None, "lastError": None,
           "lastExecutionId": None, "error": None}
    response = liveapi.list_executions(workflow_id, token)
    if not response["ok"]:
        row["error"] = _response_error(response, "the execution list")
        row["status"] = response.get("status")
        return row
    listing = summarize_execution_list(response["body"])
    executions = listing["executions"]
    row.update(listed=len(executions), total=listing["total"], partial=listing["partial"],
               failedListed=sum(1 for e in executions if e["failed"]))
    if executions:
        last = executions[0]
        row.update(lastStart=last["startTime"], lastStatus=last["status"], lastFailed=last["failed"],
                   lastError=last["error"], lastExecutionId=last["executionId"])
    return row


def sweep_tenant(bearer_token: str, match: str = "", include_idle: bool = False,
                 workers: int = SWEEP_WORKERS, progress=None) -> dict:
    """Every workflow in the token's tenant, with its run health.

    `match` keeps workflows whose name contains it (case-insensitive).
    Workflows whose list entry says `numRuns: 0` are skipped unless
    `include_idle` - asking 1,990 workflows when most never ran is most of
    the cost. A 401 part-way (the token expiring mid-sweep) stops the sweep
    and returns what was read, marked incomplete, rather than a column of
    identical errors.
    """
    token = _check_token(bearer_token)
    listing = liveapi.list_workflows(token)
    if not listing["ok"]:
        raise RunsError(_response_error(listing, "the workflow list"))
    workflows = listing["body"]
    warnings = []
    if listing.get("truncated"):
        warnings.append("The workflow list hit the page limit - the tenant has more workflows than "
                        "were read.")

    needle = (match or "").lower()
    chosen = [w for w in workflows if not needle or needle in _workflow_name(w).lower()]
    idle = [w for w in chosen if _num_runs(w) == 0]
    to_ask = chosen if include_idle else [w for w in chosen if _num_runs(w) != 0]

    rows, aborted = [], None
    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as pool:
        futures = {pool.submit(_sweep_one, w, token): w for w in to_ask}
        for future in concurrent.futures.as_completed(futures):
            if future.cancelled():
                continue
            row = future.result()
            rows.append(row)
            if progress:
                progress(len(rows), len(to_ask))
            if row.get("status") == 401 and not aborted:
                aborted = row["error"]
                for pending in futures:
                    pending.cancel()

    if aborted:
        warnings.append("Sweep stopped early: " + aborted + f" {len(rows)} of {len(to_ask)} "
                        "workflow(s) were read before it did.")
    partial = [r for r in rows if r["partial"]]
    if partial:
        warnings.append(f"{len(partial)} workflow(s) have more executions than the list returned; "
                        "their failure counts cover the listed runs only.")

    # Worst first - read errors, last run failed, any listed failure, the
    # rest - and most recent run first within each (two stable sorts).
    rows.sort(key=lambda r: str(r["lastStart"] or ""), reverse=True)
    rows.sort(key=_health_rank)
    return {
        "tenantId": tenant_from_token(token),
        "sweptAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "match": match,
        "workflowsInTenant": len(workflows),
        "workflowsMatched": len(chosen),
        "workflowsSkippedIdle": 0 if include_idle else len(idle),
        "workflowsAsked": len(to_ask),
        "complete": not aborted,
        "summary": {
            "lastRunFailed": sum(1 for r in rows if r["lastFailed"]),
            "anyListedFailure": sum(1 for r in rows if r["failedListed"]),
            "neverRan": sum(1 for r in rows if not r["error"] and r["listed"] == 0),
            "readErrors": sum(1 for r in rows if r["error"]),
        },
        "workflows": rows,
        "warnings": warnings,
    }


def _health_rank(row: dict) -> int:
    return 0 if row["error"] else 1 if row["lastFailed"] else 2 if row["failedListed"] else 3


# --- artifacts ---------------------------------------------------------------

def write_artifact(kind: str, content: dict, runs_dir: str = RUNS_DIR) -> str:
    """RUN/runs/<timestamp>-<kind>.json - gitignored: real tenant/user ids and
    real customer row data."""
    os.makedirs(runs_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(runs_dir, f"{stamp}-{kind}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=1)
    return os.path.normpath(path)
