"""
Self-test for runs.py, the read-only Runs inspector - stdlib-only,
run-it-directly style, same as the other selftest_*.py files:

    python selftest_runs.py

The fixtures are HAND-BUILT in the shape of the real execution records
captured 2026-09-28 (RUN/filter-debug/, gitignored), with every id replaced.
They check the reading, not the platform.

What's checked, all of it the kind of thing that goes wrong silently:

  - the step trace comes out in RUN order, not Live's latest-first order, and
    each step's rowsIn is the previous step's rowsOut - a reversed trace
    would put every "dropped rows" note on the wrong step;
  - M3's per-row `<txnId>_MSG_COL` messages are tallied, blank ones included;
  - a listing with count < total is flagged partial, not presented as the
    whole history;
  - an expired token is refused before any call is made;
  - the tenant sweep pages the workflow list, skips `numRuns: 0` workflows
    unless asked, sorts the worst first, and STOPS on a 401 rather than
    returning a column of identical errors.

The network layer (liveapi) is monkeypatched and always restored.
"""

import base64
import json
import sys
import time

import liveapi
import runs

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def _token(exp_offset: int = 3600) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return part({"alg": "none"}) + "." + part({"exp": int(time.time()) + exp_offset,
                                               "custom:tenantId": "TENANT-test"}) + ".sig"


def _record(failed=False):
    rows = [{"A": 9, "B": 10, "C": 50}] * 5
    return {
        "executionId": "WFEXEC-test", "workflowId": "WORKFLOW-test", "failed": failed, "error": None,
        "startTime": "2026-09-28T08:00:00Z", "endTime": "2026-09-28T08:00:02Z",
        "payload": {
            "params": {"m3EnvironmentName": "DEV"},
            "trigger": {"header": {"Filter_A": "9"}, "body": rows},
            "result": {"history": [      # latest step FIRST, as Live returns it
                {"stepId": "api_1", "success": True, "duration": 900, "recordsProcessed": 3, "files": [],
                 "data": [{"A": 9, "T1_MSG_COL": ""}, {"A": 9, "T1_MSG_COL": "Customer 9 does not exist"},
                          {"A": 9, "T1_MSG_COL": "Customer 9 does not exist"}]},
                {"stepId": "generic_filter_1", "success": True, "duration": 4, "recordsProcessed": 5,
                 "files": [], "data": rows[:3]},
            ]},
        },
    }


def test_trace_is_in_run_order_with_row_flow():
    trace = runs.summarize_execution(_record())
    ids = [s["stepId"] for s in trace["steps"]]
    check(ids == ["generic_filter_1", "api_1"], f"trace not in run order: {ids}")
    first, second = trace["steps"]
    check(first["rowsIn"] == 5 and first["rowsOut"] == 3, f"filter row flow wrong: {first}")
    check(second["rowsIn"] == 3, f"api step rowsIn should be the filter's rowsOut: {second}")
    check(trace["inputRows"] == 5 and trace["m3Environment"] == "DEV", "input summary wrong")
    check(any("generic_filter_1: 5 row(s) in, 3 out" in n for n in trace["notes"]),
          f"no dropped-rows note on the filter: {trace['notes']}")


def test_m3_messages_are_tallied_including_blank():
    trace = runs.summarize_execution(_record())
    tally = trace["steps"][1]["messages"]
    check(tally == {"T1_MSG_COL": {"Customer 9 does not exist": 2, "": 1}}, f"tally wrong: {tally}")
    check(any("2 row(s) carry an M3 message" in n for n in trace["notes"]), "no M3-message note")


def test_failed_step_is_named():
    record = _record(failed=True)
    record["payload"]["result"]["history"][0]["success"] = False
    trace = runs.summarize_execution(record)
    check(trace["failedStep"] == "api_1", f"failedStep wrong: {trace['failedStep']}")


def test_partial_listing_is_flagged():
    listing = runs.summarize_execution_list({"count": 2, "total": 40, "items": [
        {"executionId": "WFEXEC-old", "startTime": "2026-09-01T00:00:00Z", "failed": False},
        {"executionId": "WFEXEC-new", "startTime": "2026-09-28T00:00:00Z", "failed": True},
    ]})
    check(listing["partial"], "count < total not flagged partial")
    check(listing["warnings"], "no warning on a partial listing")
    check(listing["executions"][0]["executionId"] == "WFEXEC-new", "listing not newest first")
    whole = runs.summarize_execution_list({"count": 1, "total": 1, "items": [{"executionId": "x"}]})
    check(not whole["partial"] and not whole["warnings"], "a complete listing was flagged")


def test_expired_token_is_refused_before_any_call():
    calls = []
    original = liveapi.list_executions
    liveapi.list_executions = lambda *a: calls.append(a) or {"ok": True, "body": {}}
    try:
        try:
            runs.list_runs("WORKFLOW-test", _token(-3600))
            failures.append("an expired token was not refused")
        except runs.RunsError as error:
            check("EXPIRED" in str(error), f"wrong refusal: {error}")
        check(not calls, "a call was made with an expired token")
    finally:
        liveapi.list_executions = original


def _patch_sweep(workflow_pages, executions_for):
    saved = (liveapi._request, liveapi.list_executions)
    pages = iter(workflow_pages)

    def fake_request(method, url, token, body=None, timeout=None):
        assert method == "GET" and "/v1/workflows/?limit=" in url, url
        return {"ok": True, "status": 200, "body": next(pages), "error": None}

    liveapi._request = fake_request
    liveapi.list_executions = lambda workflow_id, token: executions_for(workflow_id)
    return saved


def _restore(saved):
    liveapi._request, liveapi.list_executions = saved


def _ok(items, total=None):
    return {"ok": True, "status": 200, "error": None,
            "body": {"count": len(items), "total": len(items) if total is None else total, "items": items}}


def test_sweep_pages_skips_idle_and_sorts_worst_first():
    page1 = {"items": [{"workflowId": "WORKFLOW-good", "workflowName": "Good", "stats": {"numRuns": 2}},
                       {"workflowId": "WORKFLOW-idle", "workflowName": "Idle", "stats": {"numRuns": 0}}],
             "lastKey": "k1"}
    page2 = {"items": [{"workflowId": "WORKFLOW-bad", "workflowName": "Bad"}]}   # no stats: still asked
    asked = []

    def executions_for(workflow_id):
        asked.append(workflow_id)
        if workflow_id == "WORKFLOW-bad":
            return _ok([{"executionId": "e1", "startTime": "2026-09-28", "failed": True, "error": "boom"}])
        return _ok([{"executionId": "e2", "startTime": "2026-09-27", "failed": False}], total=9)

    saved = _patch_sweep([page1, page2], executions_for)
    try:
        result = runs.sweep_tenant(_token(), workers=1)
    finally:
        _restore(saved)
    check(result["workflowsInTenant"] == 3, f"second page not read: {result['workflowsInTenant']}")
    check(sorted(asked) == ["WORKFLOW-bad", "WORKFLOW-good"], f"idle not skipped / stats-less not asked: {asked}")
    check(result["workflowsSkippedIdle"] == 1, "idle count wrong")
    check(result["workflows"][0]["workflowId"] == "WORKFLOW-bad", "worst not sorted first")
    check(result["summary"]["lastRunFailed"] == 1, f"summary wrong: {result['summary']}")
    check(any("more executions than the list returned" in w for w in result["warnings"]),
          "partial per-workflow listing not warned")
    check(result["complete"], "a clean sweep marked incomplete")


def test_sweep_match_filters_by_name():
    page = {"items": [{"workflowId": "WORKFLOW-a", "workflowName": "CustomerA - Create RO"},
                      {"workflowId": "WORKFLOW-b", "workflowName": "Other"}]}
    saved = _patch_sweep([page], lambda wid: _ok([]))
    try:
        result = runs.sweep_tenant(_token(), match="customera", workers=1)
    finally:
        _restore(saved)
    check([r["workflowId"] for r in result["workflows"]] == ["WORKFLOW-a"], "match did not filter")
    check(result["summary"]["neverRan"] == 1, "empty listing not counted as never ran")


def test_sweep_stops_on_401():
    page = {"items": [{"workflowId": f"WORKFLOW-{i}", "workflowName": str(i)} for i in range(20)]}
    saved = _patch_sweep([page], lambda wid: {"ok": False, "status": 401, "body": None, "error": "HTTP 401"})
    try:
        result = runs.sweep_tenant(_token(), workers=1)
    finally:
        _restore(saved)
    check(not result["complete"], "a 401 sweep was marked complete")
    check(len(result["workflows"]) < 20, f"sweep kept going after a 401: {len(result['workflows'])} rows")
    check(any("stopped early" in w for w in result["warnings"]), "no early-stop warning")


def test_large_output_is_read_through_vl_redirect():
    # Read from the Excel add-in's code 2026-10-13: a big output answers /data
    # with a vl-redirect header, fetched with a plain GET and NO auth header.
    saved = (liveapi._request, liveapi._plain_get)
    seen = []

    def fake_request(method, url, token, body=None, timeout=None):
        seen.append(("auth", url))
        return {"ok": True, "status": 204, "body": "", "error": None,
                "headers": {"vl-redirect": "https://files.example/out.json?sig=x"}}

    def fake_plain_get(url, timeout=None):
        seen.append(("plain", url))
        return {"ok": True, "status": 200, "body": [{"A": 1}, {"A": 2}], "error": None}

    liveapi._request, liveapi._plain_get = fake_request, fake_plain_get
    try:
        result = liveapi.get_execution_data("WORKFLOW-x", "WFEXEC-y", "token")
    finally:
        liveapi._request, liveapi._plain_get = saved
    check(result.get("redirected") and result["body"] == [{"A": 1}, {"A": 2}],
          f"the redirected output was not returned: {result}")
    check(seen == [("auth", "https://api.vince.live/v1/workflows/WORKFLOW-x/results/WFEXEC-y/data"),
                   ("plain", "https://files.example/out.json?sig=x")],
          f"wrong calls: {seen}")


def test_small_output_needs_no_redirect():
    saved = (liveapi._request, liveapi._plain_get)
    liveapi._request = lambda *a, **k: {"ok": True, "status": 200, "body": [{"A": 1}], "error": None,
                                        "headers": {}}
    liveapi._plain_get = lambda *a, **k: failures.append("followed a redirect that was not there")
    try:
        result = liveapi.get_execution_data("WORKFLOW-x", "WFEXEC-y", "token")
    finally:
        liveapi._request, liveapi._plain_get = saved
    check(result["body"] == [{"A": 1}] and not result.get("redirected"),
          f"an inline output was changed: {result}")


def main():
    tests = (
        test_trace_is_in_run_order_with_row_flow,
        test_m3_messages_are_tallied_including_blank,
        test_failed_step_is_named,
        test_partial_listing_is_flagged,
        test_expired_token_is_refused_before_any_call,
        test_sweep_pages_skips_idle_and_sorts_worst_first,
        test_sweep_match_filters_by_name,
        test_sweep_stops_on_401,
        test_large_output_is_read_through_vl_redirect,
        test_small_output_needs_no_redirect,
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
    print(f"All {len(tests)} runs self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
