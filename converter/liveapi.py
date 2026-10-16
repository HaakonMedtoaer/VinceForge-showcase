"""
Thin HTTP client for the confirmed api.vince.live surfaces this tool talks
to: the Workflows API, the Groups API, and the Custom Tables API. Stdlib
urllib only, same as the rest of the project.

ONLY confirmed endpoints live here. Sources for each:
  - Workflows POST/GET       docs/vince-live-api-reference-prompt.md section 1
  - Workflows list (paged)   VincePrompter's docs/appbuilder-api-confirmed.md
  - Execution results        docs/vince-live-api-reference-prompt.md section 1e
  - Groups POST              a real captured request/response, 2026-09-16 -
                              see the docstring on create_group() below
  - Custom Tables meta/agg   docs/vince-custom-tables-guide.md (tested live)

Deliberately absent: any endpoint for WRITING custom-table rows. The
Custom Tables guide documents creating a table's meta and reading its data,
and states rows are loaded by a workflow's Table Updater step - there is no
confirmed direct row-write API. app import therefore reports row payloads
as not-imported rather than guessing an endpoint. If a real one ever gets
confirmed (by hitting it and reading what actually comes back), it belongs
here alongside the rest.

Extracted out of webui.py so appimport.py and cli.py can make the same
calls without importing the web UI.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.vince.live"
TIMEOUT = 30


def _request(method: str, url: str, bearer_token: str, body: dict = None, timeout: int = TIMEOUT) -> dict:
    """
    One shared request/response shape for every call in this module:
      {"ok": bool, "status": int|None, "body": parsed-json-or-text, "error": str|None,
       "headers": {lowercased name: value}}   (headers on a 2xx only)

    A non-2xx is returned as ok=False with the server's own body intact
    rather than raised - Live's validation errors are the most useful
    signal this project has, so they must reach the caller verbatim.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "*/*"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return {"ok": True, "status": resp.status, "body": _maybe_json(raw), "error": None,
                    "headers": {k.lower(): v for k, v in resp.headers.items()}}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        parsed = _maybe_json(raw)
        summary = parsed if isinstance(parsed, str) else json.dumps(parsed)
        return {"ok": False, "status": e.code, "body": parsed, "error": f"HTTP {e.code}: {summary[:400]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": None, "body": None, "error": str(e)}


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# --- Workflows API --------------------------------------------------------

def get_workflow(workflow_id: str, bearer_token: str) -> dict:
    return _request("GET", f"{BASE_URL}/workflows/{urllib.parse.quote(workflow_id, safe='')}", bearer_token)


def post_workflow(workflow_body: dict, bearer_token: str) -> dict:
    return _request("POST", f"{BASE_URL}/workflows", bearer_token, workflow_body)


def delete_workflow(workflow_id: str, bearer_token: str) -> dict:
    """
    `DELETE /workflows/{workflowId}` - CONFIRMED 2026-09-29 by use: 16 CustomerA
    test workflows deleted from the web UI, each followed by a GET that
    answered 404 (history rows stamped deletedAt 13:16-13:17 UTC). Sourced
    from Vince's own API docs (via the vince-live-rest-api skill).
    cleanup.delete_created_workflows still checks with a GET afterwards and
    only reports a workflow deleted on that 404.
    """
    return _request("DELETE", f"{BASE_URL}/workflows/{urllib.parse.quote(workflow_id, safe='')}", bearer_token)


def update_workflow(tenant_id: str, workflow_id: str, definition: dict, bearer_token: str) -> dict:
    """
    Save a changed `definition` over an existing workflow - the builder's
    own Save. CONFIRMED 2026-09-29 from a HAR of the builder attaching a
    design file (a HAR capture of the builder):

        PUT https://api.vince.live/workflows
        {"tenantId": ..., "workflowId": ..., "definition": {...}}  -> 200

    The builder sent the `definition` exactly as GET returned it, with only
    the fields it changed edited - so start from a fresh GET, not from a
    locally built body (GET and POST shapes differ; see get_workflow).
    Also confirmed 2026-10-16: trimming workflowConfig.clientConfig.fields and
    workflowConfig.inputs on a test workflow (200, the GET after showed the
    change and the recompiled definitionAsl carried it).
    """
    body = {"tenantId": tenant_id, "workflowId": workflow_id, "definition": definition}
    return _request("PUT", f"{BASE_URL}/workflows", bearer_token, body)


# The workflow LIST silently truncates at 200 - `count: 200` in a tenant with
# 1,990 workflows, no `total`, no error. `limit` is the page size and
# `startKey` (fed the previous page's `lastKey`) the cursor; `size`/`pageSize`/
# `offset`/`page`/`nextToken` are all ignored. Confirmed against a real tenant
# by the sibling VincePrompter project (its docs/appbuilder-api-confirmed.md),
# not here - a sweep built on the unpaginated list there got the answer badly
# wrong, which is the reason this is the only way this module lists workflows.
WORKFLOW_PAGE_SIZE = 200


def list_workflows(bearer_token: str, max_pages: int = 100) -> dict:
    """Every workflow in the token's tenant, paged. Returns the usual
    _request shape with `body` replaced by the full item list, plus `pages`.

    Stops, rather than loops, if a page brings nothing new - a server that
    ignores the cursor would otherwise hand back page 1 forever.
    """
    items, seen, key, pages = [], set(), None, 0
    while pages < max_pages:
        url = f"{BASE_URL}/v1/workflows/?limit={WORKFLOW_PAGE_SIZE}"
        if key:
            url += "&startKey=" + urllib.parse.quote(key, safe="")
        response = _request("GET", url, bearer_token)
        if not response["ok"]:
            response["body"] = items
            response["pages"] = pages
            return response
        pages += 1
        body = response["body"] if isinstance(response["body"], dict) else {}
        fresh = [i for i in body.get("items") or [] if i.get("workflowId") not in seen]
        for item in fresh:
            seen.add(item.get("workflowId"))
        items.extend(fresh)
        key = body.get("lastKey")
        if not key or not fresh:
            break
    return {"ok": True, "status": 200, "body": items, "error": None, "pages": pages,
            "truncated": pages >= max_pages and bool(key)}


# --- Execution results (runs) ----------------------------------------------
#
# CONFIRMED 2026-09-28 - see docs/vince-live-api-reference-prompt.md §1e.
# The workflow-less `/v1/workflows/results/<exec>` from the public docs 404s.
# Read-only on purpose: nothing here can start, stop or change a run.
#
# The list answers {count, total, items}. Whether it pages - and with which
# parameter - is NOT confirmed, so no paging parameter is sent: runs.py
# compares count against total and says so when they differ, instead of
# assuming the workflow list's `limit`/`startKey` works here too.

def list_executions(workflow_id: str, bearer_token: str) -> dict:
    return _request("GET", f"{BASE_URL}/v1/workflows/{urllib.parse.quote(workflow_id, safe='')}/results",
                    bearer_token)


def get_execution(workflow_id: str, execution_id: str, bearer_token: str) -> dict:
    """The whole execution record: payload.trigger (the run's real input) and
    payload.result.history (one entry per step, LATEST step first)."""
    return _request("GET", f"{BASE_URL}/v1/workflows/{urllib.parse.quote(workflow_id, safe='')}"
                           f"/results/{urllib.parse.quote(execution_id, safe='')}", bearer_token)


def get_execution_data(workflow_id: str, execution_id: str, bearer_token: str) -> dict:
    """The final output rows ([] when nothing came out).

    A large output is stored as a file (result.dataFileKey) instead of in the
    record. Read from the Excel add-in's own code 2026-10-13
    (docs/vince-excel-addin-internals.md §5): /data then answers with a
    `vl-redirect` header, and the add-in fetches that URL with a plain GET and
    NO auth header. Done the same way here; the result carries
    "redirected": True. Not yet run against Live by this tool."""
    response = _request("GET", f"{BASE_URL}/v1/workflows/{urllib.parse.quote(workflow_id, safe='')}"
                               f"/results/{urllib.parse.quote(execution_id, safe='')}/data", bearer_token)
    redirect = (response.get("headers") or {}).get("vl-redirect")
    if not response["ok"] or not redirect:
        return response
    followed = _plain_get(redirect)
    followed["redirected"] = True
    return followed


def _plain_get(url: str, timeout: int = TIMEOUT) -> dict:
    """GET a presigned URL: no Authorization header (the signature is in the
    query string, and an extra auth header can break it). Same result shape
    as _request."""
    req = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return {"ok": True, "status": resp.status, "body": _maybe_json(raw), "error": None}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": e.code, "body": _maybe_json(raw), "error": f"HTTP {e.code}: {raw[:400]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": None, "body": None, "error": str(e)}


# --- Groups API -------------------------------------------------------------
#
# CONFIRMED 2026-09-16 - a real POST/response captured against api.vince.live
# (the user created a group named "PIPO" from Live's own "Groups" UI page and
# shared the request/response verbatim):
#
#   POST https://api.vince.live/groups
#   {"groupName":"PIPO","groupAlias":"PIPO",
#    "entities":[{"type":"Workflows","id":"WORKFLOW-...","sortX":0,"sortY":1},
#                {"type":"Workflows","id":"WORKFLOW-...","sortX":0,"sortY":2},
#                {"type":"Workflows","id":"WORKFLOW-...","sortX":0,"sortY":3}],
#    "tenantId":"TENANT-..."}
#   ->
#   {"groupId":"GROUP-...","tenantId":"...","entities":[...],
#    "createdBy":"USER-...","entity":"Group","created":"...","modified":"..."}
#
# The public product doc (vincesoftware.com/product-documentation/groups,
# read as raw HTML, not just the AI-summarized fetch) confirms what the
# feature IS: a named Group holds a list of workflow entities and roles;
# selecting it in VXL Live runs every workflow in it "in sequence." No
# `roles` field appeared in the captured request (the doc's own UI has a
# roles dropdown) - left as an open, unconfirmed question. This function
# does not touch roles.
def create_group(group_name: str, workflow_ids: list, tenant_id: str, bearer_token: str) -> dict:
    entities = [
        {"type": "Workflows", "id": workflow_id, "sortX": 0, "sortY": index + 1}
        for index, workflow_id in enumerate(workflow_ids)
    ]
    body = {
        "groupName": group_name,
        "groupAlias": group_name,
        "entities": entities,
        "tenantId": tenant_id,
    }
    return _request("POST", f"{BASE_URL}/groups", bearer_token, body)


# --- Custom Tables API ----------------------------------------------------

def create_table_meta(meta: dict, bearer_token: str) -> dict:
    """
    POST /v1/custom-tables/meta - confirmed working. `tenantId` is NOT sent
    in the body; the server fills it from the authenticated session.

    Known flaky behaviour, documented in docs/vince-custom-tables-guide.md:
    the exact same valid body has been rejected with a 400 whose
    validationError names workflow properties (definition/tenantId/
    workflowAlias/workflowId) and then succeeded unchanged on a later
    attempt. This function deliberately does NOT auto-retry - the guide
    explicitly says not to build retry-suppression around a root cause
    nobody has confirmed. The caller surfaces the error with a pointer to
    that note so a human decides whether to re-run.
    """
    return _request("POST", f"{BASE_URL}/v1/custom-tables/meta", bearer_token, meta)


def table_row_count(table_name: str, bearer_token: str) -> dict:
    """
    GET /v1/custom-tables/aggregated/{tableName} -> {"count": N, ...}.
    Confirmed endpoint (used to validate bulk loads).

    app import also uses it as an existence probe, because there is no
    confirmed GET on .../meta/{tableName} - only POST and PATCH are
    documented for meta. A 200 here means the table is really there; a 404
    means it isn't. Any OTHER status is treated as "unknown", not as
    "absent" - see appimport._probe_table_exists.
    """
    return _request("GET", f"{BASE_URL}/v1/custom-tables/aggregated/{urllib.parse.quote(table_name, safe='')}",
                    bearer_token)


# --- Reading a table's DEFINITION - probed, not assumed ------------------
#
# There is no confirmed endpoint for reading a Custom Table's meta.
# docs/vince-custom-tables-guide.md confirms POST (create) and PATCH
# (update) on the meta path, and reads only for DATA (/data/, /search/,
# /aggregated/) - none of which return primaryKeys / schema / updateType,
# which is exactly what rebuilding a table elsewhere needs.
#
# So this probes, in most-plausible-first order, and VALIDATES what comes
# back against the confirmed creation-response shape before accepting it.
# That last part is the point: the project's own methodology says a guess
# that happens to return 200 is not proof it was right. A 200 carrying
# something that isn't this table's meta is rejected here, and every
# attempt is reported so the real answer can be written down for good.
#
# The first candidate is the plausible one rather than a shot in the dark:
# PATCH on exactly this path IS confirmed, so a GET on it is the single
# likeliest place a read lives. The second is a fallback. If a real
# endpoint gets confirmed, collapse this list to it and delete the probe.
META_READ_CANDIDATES = (
    ("GET", "/v1/custom-tables/meta/{name}"),
    ("GET", "/v1/custom-tables/meta?tableName={name}"),
)

# The keys the confirmed creation response carries. A body claiming to be a
# table meta has to have at least one of them, or it isn't one.
_META_MARKERS = ("primaryKeys", "schema", "updateType", "displayOrder")


def _find_matching_meta(body, table_name: str):
    """
    Pull this table's meta out of a response body, or return None.

    Accepts the three shapes a read could plausibly come back as - the meta
    object itself, a bare list of metas, or a {"items": [...]} envelope
    (the shape the confirmed data endpoint uses) - and in every case
    requires the entry to actually name THIS table and to carry at least
    one real meta field. Anything else is not a match, however healthy its
    status code was.
    """
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        candidates = body["items"]
    elif isinstance(body, list):
        candidates = body
    elif isinstance(body, dict):
        candidates = [body]
    else:
        return None

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        names = {candidate.get("tableName"), candidate.get("tableAlias")}
        if table_name in names and any(marker in candidate for marker in _META_MARKERS):
            return candidate
    return None


def get_table_meta(table_name: str, bearer_token: str) -> dict:
    """
    Read one table's definition, by probing (see META_READ_CANDIDATES).

    Returns {"ok", "endpoint", "body", "attempts"}. `attempts` records every
    candidate tried with its status and why it was accepted or rejected -
    paste it into the docs when a real endpoint gets confirmed, so the next
    person doesn't have to re-probe.
    """
    encoded = urllib.parse.quote(table_name, safe="")
    attempts = []

    for method, template in META_READ_CANDIDATES:
        path = template.replace("{name}", encoded)
        response = _request(method, BASE_URL + path, bearer_token)
        meta = _find_matching_meta(response.get("body"), table_name) if response["ok"] else None

        if response["ok"] and meta is None:
            verdict = ("responded, but the body is not this table's meta - "
                       "no entry naming it with any of " + ", ".join(_META_MARKERS))
        elif response["ok"]:
            verdict = "accepted - body carries this table's definition"
        else:
            verdict = response.get("error") or "failed"

        attempts.append({"endpoint": method + " " + template.replace("{name}", table_name),
                         "status": response.get("status"), "accepted": meta is not None,
                         "verdict": verdict})

        if meta is not None:
            return {"ok": True, "endpoint": method + " " + path, "body": meta, "attempts": attempts}

    return {"ok": False, "endpoint": None, "body": None, "attempts": attempts,
            "error": "No endpoint returned this table's definition. None is confirmed to exist - "
                     "see the attempts list for exactly what each one did."}


def search_table(table_name: str, bearer_token: str, size: int = 1000,
                 scroll_minutes: int = 5, scroll_id: str = None) -> dict:
    """
    GET /v1/custom-tables/search/{tableName} - the paginated read.

    Documented in docs/vince-custom-tables-guide.md but explicitly marked
    NOT tested there, so callers must treat the response shape as unknown
    rather than assume `items`/`scrollId` are present. Once a `scrollId` is
    passed, the guide says every other parameter is ignored, which is why
    this sends either the scroll id or the page settings, never both.
    """
    if scroll_id:
        query = urllib.parse.urlencode({"scrollId": scroll_id})
    else:
        query = urllib.parse.urlencode({"size": size, "scrollMinutes": scroll_minutes})
    return _request("GET", BASE_URL + "/v1/custom-tables/search/" +
                    urllib.parse.quote(table_name, safe="") + "?" + query, bearer_token)


# --- Design file (an Excel sheet attached to a workflow) -----------------
#
# Confirmed 2026-09-10 from a real builder upload: request-header capture,
# the upload-url response, and a GET of the same workflow before and after
# (RUN/*design file*). Full write-up in
# docs/vince-live-api-reference-prompt.md, "Design file".
#
# Two steps, and the bytes never touch api.vince.live:
#   1. GET  /v1/files/tenant/upload-url/<key>  -> a presigned descriptor
#   2. PUT  the bytes to the descriptor's own `url` on files.vince.live
#
# The key embeds the workflow id, and Live assigns its own id at creation
# regardless of what is POSTed - so a design file can only be uploaded
# AFTER the workflow exists and its real id has been read back.

DESIGN_FILE_KIND = "designFile"


def design_file_key(workflow_id: str, filename: str) -> str:
    """
    The storage key for a workflow's design file: `<workflowId>/designFile/<filename>`.

    Provided for building the request path. Do NOT use it to populate
    `excelDesignFileKey` - read that back from the upload-url response's
    own `filename`, which is confirmed to be byte-identical and is the
    server's own spelling of the key.
    """
    return workflow_id + "/" + DESIGN_FILE_KIND + "/" + filename


def get_file_upload_url(key: str, bearer_token: str) -> dict:
    """
    Ask for a presigned PUT descriptor for a storage key.

    The tenant comes from the token, not from the caller: the returned
    `url` carries a TENANT-<id> prefix that was never sent. The response
    also carries `expiresAt` (about five minutes out), so request the URL
    immediately before uploading rather than up front for a whole batch.
    """
    path = "/v1/files/tenant/upload-url/" + urllib.parse.quote(key, safe="/")
    return _request("GET", BASE_URL + path, bearer_token)


def put_file_bytes(presigned_url: str, data: bytes, content_type: str = "*/*",
                   timeout: int = 120) -> dict:
    """
    Upload the bytes to a presigned URL.

    Deliberately NOT routed through `_request`: this call carries no
    Authorization header (the signature is in the query string, and adding
    a bearer token to a presigned S3 URL is a way to get a 400), sends raw
    bytes rather than JSON, and normally answers 200 with an empty body.
    The longer default timeout is because this one moves a real file.
    """
    request = urllib.request.Request(presigned_url, data=data, method="PUT",
                                     headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "body": _maybe_json(raw) if raw else None,
                    "error": None}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": e.code, "body": _maybe_json(raw),
                "error": f"HTTP {e.code}: {raw[:400]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": None, "body": None, "error": str(e)}


def upload_design_file(workflow_id: str, filename: str, data: bytes, bearer_token: str) -> dict:
    """
    Both steps: get a presigned URL for this workflow's design file, then
    upload the bytes to it.

    Returns {"ok", "key", "uploadUrl", "expiresAt", "error", "steps"} -
    `key` is the server's own `filename` from the descriptor, which is what
    `workflowConfig.excelDesignFileKey` is set to. `steps` records the
    status of each half so a failure says which one broke.
    """
    steps = []
    descriptor = get_file_upload_url(design_file_key(workflow_id, filename), bearer_token)
    steps.append({"step": "upload-url", "ok": descriptor["ok"], "status": descriptor.get("status")})
    if not descriptor["ok"]:
        return {"ok": False, "key": None, "error": "Couldn't get an upload URL: " +
                str(descriptor.get("error")), "steps": steps}

    body = descriptor.get("body")
    if not isinstance(body, dict) or not body.get("url") or not body.get("filename"):
        return {"ok": False, "key": None, "steps": steps,
                "error": "The upload-url response wasn't the confirmed shape (needs `url` and "
                         "`filename`) - got: " + json.dumps(body)[:300]}

    # `method`/`headers` in the descriptor are instructions for THIS call.
    # Honour them rather than hardcoding, but keep the confirmed defaults:
    # a descriptor that ever comes back saying POST should fail loudly here
    # instead of being silently PUT anyway.
    method = (body.get("method") or "PUT").upper()
    if method != "PUT":
        return {"ok": False, "key": None, "steps": steps,
                "error": "Upload descriptor asked for method " + method +
                         ", but only PUT is confirmed. Not guessing - capture this case first."}
    content_type = (body.get("headers") or {}).get("Content-Type", "*/*")

    upload = put_file_bytes(body["url"], data, content_type)
    steps.append({"step": "put", "ok": upload["ok"], "status": upload.get("status")})
    if not upload["ok"]:
        return {"ok": False, "key": None, "steps": steps,
                "error": "Upload failed: " + str(upload.get("error")),
                "expiresAt": body.get("expiresAt")}

    return {"ok": True, "key": body["filename"], "uploadUrl": body["url"],
            "expiresAt": body.get("expiresAt"), "error": None, "steps": steps}
