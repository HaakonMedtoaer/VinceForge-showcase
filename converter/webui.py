"""
Minimal local web UI for the workflow creator. Stdlib-only (no Flask/
FastAPI dependency) - matches the rest of this project, and keeps this to
one file with nothing to install. Explicitly scoped down: one page, paste
a source on one side, target/credentials on the other, Generate then
optionally Create in Live. Not meant to be a polished product - it's
either staying single-user or getting ported before anyone else touches
it, per project direction.

Colors are a best-effort approximation of Vince Live's own palette,
sampled from screenshots captured during this project (NOT from an
official style guide or design tokens) - swap the CSS variables at the
top of HTML_PAGE for exact values/assets when available. Same goes for
the header mark, which is a plain placeholder square, not Vince's real
logo - no logo asset was available to use.

Run: python webui.py
Then open http://127.0.0.1:8420
"""

import datetime
import json
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from appimport import plan_app_import, run_app_import
from build import build_live_workflow
from build_pipeline import build_pipeline_spec, build_pipeline_workflow, pipeline_warnings
from classic_to_pipeline import ClassicPipelineError, translate_classic_to_pipeline
from history import append_history as _append_history, load_history as _load_history
import cleanup
from liveapi import post_workflow
from metadata import (fetch_metadata_for_workflow, fill_from_catalog, missing_metadata_fields,
                      unmapped_mandatory_warning,
                      tenant_from_token, tenant_mismatch_warning, token_expiry_warning)
from pipeline_ir import PipelineSpecError, looks_like_pipeline_spec, scaffold_spec
from source_app_folder import looks_like_table_meta
from source_classic_xml import compose_workflow_name, parse_classic_xml_units, peek_classic_xml
from source_live_json import clone_and_retarget
from state import next_labeled_name
from live_fetch import fetch_table, fetch_workflow as fetch_workflow_from_live
from tenants import get_last_selected, load_tenants, remove_tenant, set_last_selected, upsert_tenant
from token_store import load_token, save_token
import api_client
import runs as runs_api
import threading

PORT = 8420

# How far each running app import has got, keyed by an id the browser makes
# up and sends along. The import request itself blocks until it is done, so
# the page polls /api/app/import/progress on the side to show "2/6". Only
# the latest few are kept - this is for a person watching, not a record.
_import_progress = {}
_import_progress_lock = threading.Lock()


def _set_import_progress(import_id: str, state: dict) -> None:
    if not import_id:
        return
    with _import_progress_lock:
        _import_progress[import_id] = dict(state, updatedAt=time.time())
        while len(_import_progress) > 20:
            _import_progress.pop(next(iter(_import_progress)))


def parse_header_row(value):
    """The Target panel's 'table header row' as an int >= 1, or None when blank."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError("Table header row must be a whole number of 1 or more (or blank).")
    return int(text)


def api_client_token(preset: str, tenant_id: str) -> dict:
    """
    A token from the preset's API Client, for when the bearer-token field is
    empty. Refused unless the tenant about to be targeted IS the preset's
    tenant: an API Client belongs to one tenant, and a token used against
    another tenant's ids fails silently (see CLAUDE.md, auth) - and a
    minted token may not name its tenant, so the usual claim check can't
    catch it. See api_client.py for what is still unconfirmed.
    """
    preset = (preset or "").strip()
    stored = load_tenants().get(preset) or {}
    if not stored:
        return {"ok": False, "error": "Pick a saved Target Tenant to use its API Client, or paste a bearer token."}
    if tenant_id and stored.get("tenantId") and tenant_id.strip() != stored["tenantId"]:
        return {"ok": False, "error": (
            "The Tenant ID in the Target panel is not the one saved in '" + preset + "', so its API "
            "Client would act on a different tenant. Re-select the preset, or paste a bearer token.")}
    minted = api_client.mint_token(preset)
    if not minted["ok"]:
        return minted
    return {"ok": True, "token": minted["token"], "expiresAt": minted["expiresAt"],
            "preset": preset, "tenantId": stored.get("tenantId", "")}

# The "Create in Live" log lives in history.py now - app imports append to
# the same file, and appimport.py can't import this module (this module
# imports it).


def detect_source_type(content: str) -> str:
    """
    Which source a pasted blob is.

    Note what is NOT here: "classic-pipeline". The same Classic XML can be
    converted either way, and the native path is the one to prefer, so
    choosing the generic path is a deliberate click rather than something
    auto-detect can decide from the text.
    """
    stripped = content.strip()
    if stripped.startswith("<?xml") or "<VinceExcelConfig" in stripped:
        return "classic-xml"
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return "prompt"
        # A pipeline spec is JSON too, so it has to be recognised before
        # the generic live-json path claims it.
        if looks_like_pipeline_spec(parsed):
            return "pipeline-spec"
        return "live-json"
    return "prompt"


def _missing_metadata_warning(result: dict) -> list:
    """
    Name the fields that still have no real metadata after a build.

    Worth surfacing before "Create in Live" rather than after: the
    placeholder lands in the workflow's Excel column headers, so the first
    time you see it is otherwise in the spreadsheet of a workflow that is
    already created in the tenant.
    """
    listed = missing_metadata_fields(result)
    if not listed:
        return []

    shown = ", ".join(listed[:6]) + (f" (+{len(listed) - 6} more)" if len(listed) > 6 else "")
    return [
        f"{len(listed)} field(s) have no metadata, so their Excel column header reads "
        f'"TODO: metadata not found ..." and their type is UNKNOWN: {shown}. '
        "First thing to check: the bearer token must belong to the SAME tenant as the connection "
        "id - a cross-tenant token does not error, it just makes the connection invisible and "
        "every lookup comes back empty. After that: the transaction name (AUTCHKMI has genuinely "
        "returned nothing before), or add real entries to metadata.M3_FIELD_METADATA."
    ]


def generate(payload: dict) -> dict:
    source_type = payload.get("sourceType") or "auto"
    content = payload.get("content", "")
    connection_id = payload.get("connectionId", "")
    tenant_id = payload.get("tenantId", "")
    environment_id = payload.get("environmentId", "")

    if not content.strip():
        return {"ok": False, "error": "Paste some source content first."}

    if source_type == "auto":
        source_type = detect_source_type(content)

    if source_type == "classic-xml":
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(content)
            tmp_path = f.name
        # Every <Export>/<Import> block, one workflow each - a Classic file
        # states its sheet, data row and gate per block and Live carries one
        # of each per workflow, so a multi-block file cannot be one workflow
        # (see source_classic_xml._parse_raw_units). The panel creates one at
        # a time, so all of them are built and the browser picks.
        units = parse_classic_xml_units(tmp_path)

        # Findings from the read-only scan, reported against every block.
        peeked = peek_classic_xml(content)
        scan_warnings = list((peeked or {}).get("warnings", []))
        bearer_token = payload.get("bearerToken", "").strip()
        mismatch = tenant_mismatch_warning(bearer_token, tenant_id)
        if mismatch:
            scan_warnings.append(mismatch)
        expired = token_expiry_warning(bearer_token)
        if expired:
            scan_warnings.append(expired)
        if not bearer_token:
            scan_warnings.append(
                "No bearer token, so no field metadata was fetched. Fields outside the small static "
                "table get \"TODO: metadata not found ...\" as their Excel column header and UNKNOWN "
                "as their type. Paste a token into Target and generate again."
            )

        base_name = (payload.get("baseName") or "").strip()

        built = []
        for wf in units:
            wf.label = next_labeled_name(compose_workflow_name(base_name, wf.label))
            warnings = list(scan_warnings)

            # Fetch real M3 field metadata, exactly as the CLI does. Skipping
            # this was a real, visible bug: every field fell back to the
            # two-entry static table, so a created workflow's Excel step showed
            # "TODO: metadata not found for BUS100MI.DelBudgetLines.DIVI" as its
            # column header and UNKNOWN as its type.
            metadata_overrides = None
            if bearer_token:
                try:
                    problems = []
                    metadata_overrides = fetch_metadata_for_workflow(
                        wf, connection_id, bearer_token, problems)
                    for problem in problems:
                        warnings.append(f"Metadata lookup: {problem}")
                except Exception as e:
                    # An expired token or an unreachable endpoint must not stop
                    # the conversion - it just costs you the real field names.
                    warnings.append(f"Field metadata lookup failed ({type(e).__name__}: {e}). "
                                    "Generated with the static table only - see the note below.")
                    metadata_overrides = None
            metadata_overrides = fill_from_catalog(wf, metadata_overrides, warnings)
            warnings.extend(unmapped_mandatory_warning(wf, metadata_overrides))

            result = build_live_workflow(wf, connection_id, tenant_id, environment_id,
                                         metadata_overrides, warnings)
            warnings.extend(_missing_metadata_warning(result))
            built.append({"name": wf.label, "result": result, "warnings": warnings})

        return {"ok": True, "detectedType": source_type,
                "result": built[0]["result"], "warnings": built[0]["warnings"],
                "units": built}

    if source_type == "classic-pipeline":
        # The SAME Classic file as above, converted onto the generic
        # pipeline path instead - the only way to keep a Classic function's
        # per-call conditions (see classic_to_pipeline). One workflow per
        # block, same fan-out as the native path, so the block picker works
        # unchanged. No metadata fetch: there is no M3 API step to validate
        # fields against on this path.
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(content)
            tmp_path = f.name
        units = parse_classic_xml_units(tmp_path)

        peeked = peek_classic_xml(content)
        scan_warnings = [
            warning for warning in (peeked or {}).get("warnings", [])
            # The native path's two "we cannot express this" notes are wrong
            # here - this path is exactly where those shapes DO work, and
            # repeating them would teach people to ignore the warnings.
            if "API step has no confirmed group equivalent" not in warning
            and "Live's Generic Filter step has no confirmed equivalent" not in warning
        ]
        base_url = (payload.get("ionApiBaseUrl") or "").strip()
        base_name = (payload.get("baseName") or "").strip()
        header_row = parse_header_row(payload.get("tableHeaderRow"))

        built = []
        refused = []
        for wf in units:
            wf.label = next_labeled_name(compose_workflow_name(base_name, wf.label))
            try:
                pipeline, warnings = translate_classic_to_pipeline(
                    wf, base_url=base_url or None, connection_id=connection_id,
                    header_row=header_row)
            except ClassicPipelineError as e:
                refused.append(f"{wf.label}: {e}")
                continue
            result = build_pipeline_workflow(pipeline, connection_id, tenant_id, environment_id)
            built.append({
                "name": wf.label,
                "result": result,
                "warnings": scan_warnings + warnings + pipeline_warnings(pipeline),
            })

        if not built:
            return {"ok": False, "detectedType": source_type,
                    "error": "Nothing in this file belongs on the generic pipeline path. "
                             + " ".join(refused)}
        for entry in built:
            entry["warnings"] = [f"BLOCK NOT CONVERTED - {note}" for note in refused] + entry["warnings"]

        return {"ok": True, "detectedType": source_type,
                "result": built[0]["result"], "warnings": built[0]["warnings"],
                "units": built}

    if source_type == "live-json":
        try:
            source = json.loads(content)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"Not valid JSON: {e}"}
        # A pipeline spec is JSON too. Auto-detect routes it correctly,
        # but the Live JSON tab can be picked by hand (or the server can
        # be running pre-pipeline code), and clone_and_retarget then dies
        # on a missing `definition` with a bare KeyError whose whole
        # message is "'definition'" - which tells nobody anything.
        if looks_like_pipeline_spec(source):
            return {"ok": False, "detectedType": "pipeline-spec",
                    "error": 'That is a generic pipeline spec, not a Live workflow export. Switch '
                             'to the "Pipeline spec" tab (or "Auto-detect") and generate again.'}
        # Fetching a table drops its definition in here too, and a table
        # meta run through the workflow path just dies on a missing
        # `definition` key. Say what it actually is instead.
        if looks_like_table_meta(source):
            return {"ok": False, "detectedType": "table-json",
                    "error": "That's a Custom Table definition, not a workflow. Use the \"Import an app "
                             "folder\" panel below - a table gets created as part of an app import."}
        if "definition" not in source:
            return {"ok": False, "detectedType": source_type,
                    "error": 'That JSON has no "definition" key, so it is not a '
                             "GET /workflows/{id} response. The Live JSON source expects a whole "
                             "workflow as Live returns it."}
        override_name = (payload.get("overrideName") or "").strip() or None
        result = clone_and_retarget(source, connection_id, tenant_id, environment_id,
                                    override_name=override_name)
        return {"ok": True, "detectedType": source_type, "result": result}

    if source_type == "pipeline-spec":
        try:
            spec = json.loads(content)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"Not valid JSON: {e}"}
        name_override = (payload.get("nameOverride") or "").strip()
        if name_override:
            spec["name"] = name_override
        try:
            result, warnings = build_pipeline_spec(spec, connection_id, tenant_id, environment_id)
        except PipelineSpecError as e:
            # A spec error is the user's own JSON being wrong, not a bug -
            # show it verbatim, they are written to be actionable.
            return {"ok": False, "detectedType": source_type, "error": str(e)}
        return {"ok": True, "detectedType": source_type, "result": result, "warnings": warnings}

    return {"ok": False, "detectedType": "prompt",
            "error": "Worded-prompt source isn't built yet - paste Classic XML, a Live JSON export, "
                     "or a generic pipeline spec instead."}


def fetch_workflow(workflow_id: str, bearer_token: str, app_folder: str = "") -> dict:
    """
    GET an existing workflow from the target tenant, by id. Identifies the
    tenant purely via the bearer token's own claims - no connectionId/
    tenantId/environmentId needed on this call, unlike create_in_live.

    `app_folder` comes from the UI's save-into-folder toggle: when set, the
    response is also written to <folder>/workflows/<name>.json, ready for
    app import to read back. When empty, nothing touches the disk.
    """
    return fetch_workflow_from_live(workflow_id, bearer_token, app_folder)


def token_info(bearer_token: str) -> dict:
    """
    What a token says about itself, for the pre-flight confirmation box.

    Purely local - it decodes the token's own claims and makes no HTTP call.
    Worth surfacing in the UI because the token, NOT the tenantId in the
    body, decides which tenant a create lands in, and a mismatched pair
    fails silently rather than with a 401 (see CLAUDE.md, auth).
    """
    token = (bearer_token or "").strip()
    if not token:
        return {"ok": True, "tenant": None, "hasToken": False}
    return {
        "ok": True,
        "hasToken": True,
        "tenant": tenant_from_token(token),
        "expiryWarning": token_expiry_warning(token),
    }


def create_in_live(result: dict, bearer_token: str) -> dict:
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "workflowName": (result or {}).get("workflowName"),
        "connectionId": (result or {}).get("tags", {}).get("connectionId", [None])[0],
        "tenantId": (result or {}).get("tenantId"),
        "environmentId": (result or {}).get("tags", {}).get("apiEnvironment"),
    }

    if not bearer_token:
        response = {"ok": False, "error": "No bearer token provided."}
        _append_history({**entry, "ok": False, "error": response["error"]})
        return response

    # Refuse rather than warn: the token decides which tenant this lands
    # in, NOT the tenantId in the body, so a mismatched pair silently
    # creates the workflow in the wrong customer's tenant. That is not
    # something to discover later from a warning nobody read.
    expired = token_expiry_warning(bearer_token)
    if expired:
        _append_history({**entry, "ok": False, "error": expired[:300]})
        return {"ok": False, "error": expired}

    mismatch = tenant_mismatch_warning(bearer_token, (result or {}).get("tenantId", ""))
    if mismatch:
        _append_history({**entry, "ok": False, "error": mismatch[:300]})
        return {"ok": False, "error": "Refusing to create: " + mismatch}

    response = post_workflow(result, bearer_token)
    body = response.get("body")
    if response["ok"]:
        _append_history({**entry, "ok": True, "httpStatus": response.get("status"),
                         "workflowId": body.get("workflowId") if isinstance(body, dict) else None})
        return {"ok": True, "status": response.get("status"), "body": body}

    # Keep the server's own body in the UI response - Live's validation
    # errors are the most useful signal this project has, so they go to the
    # screen verbatim, with only the history row truncated.
    _append_history({**entry, "ok": False, "httpStatus": response.get("status"),
                     "error": (body if isinstance(body, str) else json.dumps(body))[:300]})
    return {"ok": False, "status": response.get("status"), "body": body,
            "error": response.get("error")}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The whole page - including every bit of client JS - is only
            # re-fetched on an actual page load, never by the AJAX calls the
            # page itself makes afterward. This server gets restarted with
            # new code often (see CLAUDE.md's "restart after editing" note);
            # without this header a tab left open since before a restart can
            # silently keep running OLD JS against a server that has since
            # moved on, with no visible sign anything is stale.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/history":
            # `deletable` is decided here, by the same rule the delete
            # endpoint enforces, so the UI never offers what it would refuse.
            rows = [dict(entry, deletable=cleanup.is_deletable(entry)) for entry in _load_history()]
            self._send_json(200, {"ok": True, "history": rows})
        elif self.path == "/api/tenants":
            self._send_json(200, {"ok": True, "tenants": load_tenants(), "lastSelected": get_last_selected()})
        elif self.path == "/api/token":
            self._send_json(200, {"ok": True, **load_token()})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON body"})
            return

        if self.path == "/api/generate":
            try:
                result = generate(payload)
            except Exception as e:
                # Include the type: a bare str() of a KeyError is just the
                # quoted key name ("'definition'"), which reads like a
                # glitch rather than a diagnosis.
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._send_json(200, result)
            return

        if self.path == "/api/pipeline/scaffold":
            self._send_json(200, {"ok": True, "spec": scaffold_spec()})
            return

        if self.path == "/api/fetch-workflow":
            try:
                response = fetch_workflow(payload.get("workflowId"), payload.get("bearerToken"),
                                          payload.get("folder", ""))
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            self._send_json(200, response)
            return

        if self.path == "/api/token/info":
            try:
                response = token_info(payload.get("bearerToken", ""))
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            self._send_json(200, response)
            return

        if self.path == "/api/create":
            try:
                response = create_in_live(payload.get("result"), payload.get("bearerToken"))
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            self._send_json(200, response)
            return

        if self.path == "/api/table/fetch":
            try:
                response = fetch_table(
                    payload.get("tableName", ""),
                    payload.get("bearerToken", ""),
                    bool(payload.get("withRows")),
                    payload.get("folder", ""),
                )
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            self._send_json(200, response)
            return

        # The Runs inspector - GETs only, see runs.py.
        if self.path in ("/api/runs/list", "/api/runs/inspect", "/api/runs/sweep"):
            token = payload.get("bearerToken", "")
            try:
                if self.path == "/api/runs/list":
                    response = {"ok": True, **runs_api.list_runs(payload.get("workflowId", ""), token)}
                elif self.path == "/api/runs/inspect":
                    execution_id = payload.get("executionId", "")
                    result = runs_api.inspect_run(payload.get("workflowId", ""), execution_id, token)
                    response = {"ok": True, "trace": result["trace"],
                                "savedTo": runs_api.write_artifact("run-" + execution_id, result)}
                else:
                    result = runs_api.sweep_tenant(token, match=payload.get("match", ""),
                                                   include_idle=bool(payload.get("includeIdle")))
                    response = {"ok": True, **result, "savedTo": runs_api.write_artifact("sweep", result)}
            except runs_api.RunsError as e:
                response = {"ok": False, "error": str(e)}
            except Exception as e:
                response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._send_json(200, response)
            return

        if self.path == "/api/app/scan":
            try:
                response = plan_app_import(payload.get("folder", ""),
                                           payload.get("designMatches") or {})
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            self._send_json(200, response)
            return

        if self.path == "/api/workflows/delete":
            try:
                token = payload.get("bearerToken", "")
                # An API Client's token names no tenant; vouch for it from the
                # preset that minted it (never from anything the page sends).
                minted_for = api_client.preset_for_token(token)
                hint = (load_tenants().get(minted_for) or {}).get("tenantId", "") if minted_for else ""
                response = cleanup.delete_created_workflows(
                    list(payload.get("workflowIds") or []), token, token_tenant_hint=hint)
            except Exception as e:
                response = {"ok": False, "error": str(e), "results": []}
            self._send_json(200, response)
            return

        if self.path == "/api/app/import/progress":
            with _import_progress_lock:
                state = _import_progress.get(payload.get("importId", ""))
            self._send_json(200, {"ok": True, "progress": state})
            return

        if self.path == "/api/app/import":
            import_id = payload.get("importId", "")
            _set_import_progress(import_id, {"phase": "Starting", "done": 0, "total": 0, "item": ""})
            try:
                response = run_app_import(
                    payload.get("folder", ""),
                    payload.get("connectionId", ""),
                    payload.get("tenantId", ""),
                    payload.get("environmentId", ""),
                    payload.get("bearerToken", ""),
                    bool(payload.get("dryRun")),
                    payload.get("promptToFolder", True),
                    payload.get("ionApiBaseUrl", ""),
                    payload.get("baseNames") or {},
                    payload.get("designMatches") or {},
                    progress=lambda state: _set_import_progress(import_id, state),
                    header_row=parse_header_row(payload.get("tableHeaderRow")),
                )
            except Exception as e:
                response = {"ok": False, "error": str(e)}
            _set_import_progress(import_id, {"phase": "Done", "done": 0, "total": 0, "item": ""})
            self._send_json(200, response)
            return

        if self.path == "/api/apiclient/get":
            preset = payload.get("preset", "")
            summary = api_client.get_client_summary(preset)
            summary["warning"] = api_client.token_uri_warning(summary["tokenUri"]) if summary["tokenUri"] else None
            self._send_json(200, {"ok": True, **summary})
            return

        if self.path == "/api/apiclient/save":
            try:
                summary = api_client.save_client(payload.get("preset", ""), payload.get("clientId", ""),
                                                 payload.get("clientSecret", ""), payload.get("tokenUri", ""))
                summary["warning"] = api_client.token_uri_warning(summary["tokenUri"])
                self._send_json(200, {"ok": True, **summary})
            except ValueError as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return

        if self.path == "/api/apiclient/remove":
            try:
                api_client.remove_client(payload.get("preset", ""))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/apiclient/test":
            try:
                response = api_client.test_connection(payload.get("preset", ""))
            except Exception as e:
                response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._send_json(200, response)
            return

        if self.path == "/api/apiclient/token":
            try:
                response = api_client_token(payload.get("preset", ""), payload.get("tenantId", ""))
            except Exception as e:
                response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._send_json(200, response)
            return

        if self.path == "/api/tenants/save":
            try:
                tenants = upsert_tenant(
                    payload.get("name", ""),
                    payload.get("connectionId", ""),
                    payload.get("tenantId", ""),
                    payload.get("environmentId", ""),
                    payload.get("ionApiBaseUrl", ""),
                )
                # Saving (creating or editing) a preset is a strong signal
                # it's the one to come back to on the next reload - not
                # just whichever happened to load first.
                set_last_selected(payload.get("name", ""))
                self._send_json(200, {"ok": True, "tenants": tenants})
            except ValueError as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/tenants/delete":
            try:
                tenants = remove_tenant(payload.get("name", ""))
                api_client.remove_client(payload.get("name", ""))
                if get_last_selected() == (payload.get("name", "") or "").strip():
                    set_last_selected("")
                self._send_json(200, {"ok": True, "tenants": tenants})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/tenants/select":
            # Fired whenever the Target panel's dropdown changes, purely so
            # the NEXT page load remembers it - never blocks or affects the
            # current session either way, so a failure here is silent.
            try:
                set_last_selected(payload.get("name", ""))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            return

        if self.path == "/api/token/save":
            try:
                data = save_token(payload.get("token", ""))
                self._send_json(200, {"ok": True, **data})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            return

        self._send_json(404, {"ok": False, "error": "Not found"})

    def log_message(self, format, *args):
        pass  # keep the console quiet - errors already surface as JSON in the UI


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VinceForge</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    /* Brand colours from the "Vince" Design System in Claude (Vince Brand Guide
       2026): orange, teal, ink, the two light surfaces, peach, on-ink. The
       brand says orange and teal fail contrast as small text, so text that
       needs a teal/orange tone uses the -deep variants, which are DERIVED
       here for contrast, not brand tokens. */
    --orange: #f26b21;
    --teal: #4bb4b0;
    --ink: #1f363d;
    --surface-cool: #f8fcfd;
    --surface-warm: #f4efe9;
    --peach: #f8d5b7;
    --on-ink: #fcf1e7;
    --teal-deep: #1d6f6c;
    --teal-tint: #e3f3f2;
    --danger: #b3261e;
    --danger-deep: #8c1d17;
    --warn-bg: #fdf1e4;
    --warn-border: #efc9a2;
    --warn-text: #7a3d0c;

    --radius-sm: 6px;
    --radius-md: 12px;
    --radius-lg: 24px;
    --font-sans: "Manrope", system-ui, -apple-system, "Segoe UI", sans-serif;
    --font-mono: ui-monospace, "Cascadia Mono", Consolas, monospace;

    /* The old names, kept because inline styles in the JS below use them. */
    --vince-teal-dark: var(--teal-deep);
    --vince-teal: var(--teal);
    --vince-mint-bg: var(--surface-cool);
    --vince-accent: var(--orange);
    --vince-text: var(--ink);
    --vince-text-muted: #56696f;
    --vince-border: #d9e4e4;
    --vince-panel: #ffffff;
    --vince-error: var(--danger);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: var(--font-sans);
    font-size: 15px;
    line-height: 1.5;
    background: var(--surface-warm);
    color: var(--ink);
  }
  header {
    display: flex;
    align-items: center;
    gap: 14px;
    background: var(--ink);
    color: var(--on-ink);
    padding: 14px 24px;
  }
  header .mark {
    width: 34px; height: 34px;
    background: var(--orange);
    color: white;
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 800;
    font-size: 18px;
  }
  header h1 { font-size: 20px; font-weight: 800; margin: 0; }
  header .sub { font-size: 14px; opacity: 0.8; margin-left: 4px; }

  main {
    max-width: 1280px;
    margin: 28px auto;
    padding: 0 24px 64px;
    display: grid;
    grid-template-columns: 1.5fr 1fr;
    gap: 20px;
  }
  @media (max-width: 1000px) { main { grid-template-columns: 1fr; } }
  .panel {
    background: var(--vince-panel);
    border: 1px solid var(--vince-border);
    border-radius: var(--radius-md);
    padding: 20px 24px;
  }
  .panel h2 {
    font-size: 18px;
    font-weight: 700;
    color: var(--ink);
    margin: 0 0 16px;
    display: flex; align-items: center; gap: 10px;
  }
  .panel h2 .icon { color: var(--teal-deep); }
  .icon { width: 20px; height: 20px; flex: none; }
  .muted { color: var(--vince-text-muted); }

  /* ---- Buttons: one system for every button on the page ----
     Base = secondary (outlined). .primary = the main action of an area,
     .destructive = removes something from Live. Anything unclassed still
     gets the base look, so a new button can't come out browser-default. */
  button {
    font-family: inherit;
    font-size: 14.5px;
    font-weight: 600;
    line-height: 1.2;
    min-height: 40px;
    padding: 9px 18px;
    border-radius: var(--radius-md);
    border: 1.5px solid var(--ink);
    background: white;
    color: var(--ink);
    cursor: pointer;
    white-space: nowrap;
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    transition: background-color .12s, border-color .12s, color .12s;
  }
  button:hover { background: var(--teal-tint); }
  button:focus-visible { outline: 3px solid var(--teal); outline-offset: 2px; }
  button.primary { background: var(--ink); border-color: var(--ink); color: white; }
  button.primary:hover { background: #2c4d57; border-color: #2c4d57; }
  button.destructive { background: var(--danger); border-color: var(--danger); color: white; }
  button.destructive:hover { background: var(--danger-deep); border-color: var(--danger-deep); }
  button:disabled, button:disabled:hover {
    opacity: 0.4; cursor: not-allowed; background: white; color: var(--ink); border-color: var(--ink);
  }
  button.primary:disabled, button.primary:disabled:hover { background: var(--ink); color: white; }
  button.destructive:disabled, button.destructive:disabled:hover {
    background: var(--danger); border-color: var(--danger); color: white;
  }
  /* Compact size, for buttons inside table rows only. */
  .runs-table button, .modal-body table button, button.sm {
    min-height: 32px; padding: 5px 12px; font-size: 13.5px; border-radius: var(--radius-sm);
  }

  .source-tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .source-tabs button[data-type] {
    border: 1.5px solid var(--vince-border);
    color: var(--vince-text-muted);
    border-radius: 999px;
    min-height: 36px;
    padding: 6px 16px;
    font-size: 14px;
  }
  .source-tabs button[data-type]:hover { border-color: var(--teal); color: var(--ink); background: white; }
  .source-tabs button[data-type].active {
    background: var(--teal);
    border-color: var(--teal);
    color: var(--ink);
  }
  .source-tabs #scaffoldBtn { min-height: 36px; padding: 6px 16px; border-radius: 999px; }
  .detected-badge {
    font-size: 14px;
    color: var(--teal-deep);
    font-weight: 600;
    margin-bottom: 10px;
    min-height: 20px;
  }

  textarea {
    width: 100%;
    font-family: var(--font-mono);
    font-size: 14px;
    line-height: 1.45;
    color: var(--ink);
    border: 1px solid var(--vince-border);
    border-radius: var(--radius-sm);
    padding: 12px;
    resize: vertical;
  }
  #sourceContent { height: 380px; }
  #outputJson { height: 300px; background: var(--surface-cool); }

  label {
    display: block;
    font-size: 14px;
    font-weight: 600;
    color: var(--ink);
    margin: 14px 0 6px;
  }
  label:first-of-type { margin-top: 0; }
  input[type=text], input[type=password], select {
    width: 100%;
    min-height: 40px;
    padding: 8px 12px;
    border: 1px solid #c5d3d4;
    border-radius: var(--radius-sm);
    font-size: 14.5px;
    font-family: var(--font-mono);
    color: var(--ink);
    background: white;
  }
  select { font-family: var(--font-sans); font-weight: 600; }
  input[type=text]:focus, input[type=password]:focus, select:focus, textarea:focus {
    outline: none; border-color: var(--teal); box-shadow: 0 0 0 3px rgba(75,180,176,0.25);
  }
  input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--teal-deep); margin: 0; }
  .tenant-row { display: flex; gap: 8px; align-items: center; }
  .tenant-row select, .tenant-row input[type=text] { flex: 1; min-width: 0; }

  .hint { font-size: 14px; color: var(--vince-text-muted); margin-top: 8px; }

  /* Longer explanations stay out of the way until asked for. */
  details.help { margin-top: 8px; font-size: 14px; color: var(--vince-text-muted); }
  details.help summary {
    cursor: pointer; user-select: none; width: fit-content;
    font-weight: 600; color: var(--teal-deep); list-style: none;
    display: inline-flex; align-items: center; gap: 6px;
  }
  details.help summary::-webkit-details-marker { display: none; }
  details.help summary::before {
    content: "?"; display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 999px; font-size: 12px; font-weight: 800;
    background: var(--teal-tint); color: var(--teal-deep);
  }
  details.help[open] summary { margin-bottom: 6px; }
  details.help > div {
    background: var(--surface-cool); border-left: 3px solid var(--teal);
    border-radius: 0 var(--radius-sm) var(--radius-sm) 0; padding: 8px 12px;
  }

  .actions {
    grid-column: 1 / -1; display: flex; gap: 12px; align-items: center;
    background: white; border: 1px solid var(--vince-border);
    border-radius: var(--radius-md); padding: 14px 24px;
  }

  .status { font-size: 14.5px; margin-left: 4px; }
  .status.error { color: var(--danger); font-weight: 600; }
  .status.ok { color: var(--teal-deep); font-weight: 600; }

  .output-panel { grid-column: 1 / -1; }

  /* App import panel */
  .app-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .app-row input[type=text] { flex: 1; min-width: 280px; }
  .app-row label { margin: 0; display: flex; align-items: center; gap: 8px; white-space: nowrap; font-weight: 500; }
  #appPlan { margin-top: 16px; font-size: 14px; }
  #appPlan:empty { display: none; }
  .app-group { margin-bottom: 16px; }
  .app-group h3 {
    font-size: 13.5px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--vince-text-muted); margin: 0 0 8px;
  }
  .app-item {
    border: 1px solid var(--vince-border); border-left-width: 4px;
    border-radius: var(--radius-sm); padding: 9px 12px; margin-bottom: 8px; background: var(--surface-cool);
  }
  .app-item.ok { border-left-color: var(--teal); }
  .app-item.bad { border-left-color: var(--danger); }
  .app-item.planned { border-left-color: #b9c9c6; }
  .app-item .name { font-weight: 700; }
  .app-item .name .tag {
    font-weight: 700; font-size: 12px; letter-spacing: .03em; text-transform: uppercase;
    color: var(--ink); background: var(--peach); border-radius: var(--radius-sm);
    padding: 2px 7px; margin-left: 8px; vertical-align: middle;
  }
  .app-item .meta { color: var(--vince-text-muted); font-size: 13.5px; margin-top: 3px; }
  .app-item .warn { color: var(--warn-text); font-size: 13.5px; margin-top: 4px; }
  .app-item .err { color: var(--danger); font-size: 13.5px; margin-top: 4px; word-break: break-word; }
  .warn-details { margin-top: 4px; }
  .warn-details summary {
    color: var(--warn-text); font-size: 13.5px; cursor: pointer; font-weight: 600; user-select: none;
  }
  .warn-details .warn { margin-left: 4px; }
  .warn-group { border: 1px solid var(--vince-border); border-radius: var(--radius-sm); padding: 8px 12px; margin-bottom: 8px; }
  .warn-group summary { cursor: pointer; font-weight: 600; font-size: 14px; user-select: none; }
  .warn-group .warn { margin-top: 6px; }
  .app-summary {
    background: var(--teal-tint); border: 1px solid #bfe3e1;
    border-radius: var(--radius-sm); padding: 10px 14px; margin-bottom: 14px; font-weight: 700;
  }
  .app-files { color: var(--vince-text-muted); font-size: 13.5px; line-height: 1.6; }
  #appPromptWrap { display: none; margin-top: 10px; }
  #appPrompt { height: 240px; background: var(--surface-cool); }

  /* Header action (Run history) */
  header .spacer { flex: 1; }
  header .header-btn {
    background: transparent;
    border-color: rgba(252,241,231,0.45);
    color: var(--on-ink);
  }
  header .header-btn:hover { background: rgba(252,241,231,0.12); border-color: var(--on-ink); }

  /* Target, collapsed by default - the ids rarely change, the token does */
  .target-summary {
    display: flex; align-items: center; gap: 12px;
    border: 1px solid var(--vince-border);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    background: var(--surface-cool);
    margin-bottom: 14px;
  }
  .target-summary .who { font-weight: 700; font-size: 15px; }
  .target-summary .ids {
    font-family: var(--font-mono);
    font-size: 13px; color: var(--vince-text-muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
  }
  #targetFields[hidden] { display: none; }
  .api-client {
    margin-top: 18px; padding: 14px 16px; border: 1px solid var(--vince-border);
    border-radius: var(--radius-md); background: var(--surface-cool);
  }
  .api-client h3 { margin: 0 0 4px; font-size: 15.5px; font-weight: 700; }
  .api-client .row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; align-items: center; }
  .api-client .status { margin-left: 0; }
  #apiClientHint { color: var(--teal-deep); font-weight: 600; }
  #apiClientHint:empty { display: none; }

  /* App import progress */
  #appProgress { margin-top: 14px; }
  #appProgress[hidden] { display: none; }
  #appProgress .bar {
    height: 10px; border-radius: 999px; background: var(--teal-tint); overflow: hidden;
  }
  #appProgress .bar span {
    display: block; height: 100%; width: 0; background: var(--teal); border-radius: 999px;
    transition: width .3s;
  }
  #appProgress.indeterminate .bar span { width: 30%; animation: slide 1.2s ease-in-out infinite; }
  @keyframes slide { from { margin-left: -30%; } to { margin-left: 100%; } }
  #appProgress .text { margin-top: 8px; font-size: 14.5px; font-weight: 600; }
  #appProgress .item { font-weight: 500; color: var(--vince-text-muted); }
  #targetFields { margin-bottom: 14px; }

  /* The two fetches, side by side */
  .fetch-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .fetch-box {
    border: 1px solid var(--vince-border);
    border-radius: var(--radius-md);
    padding: 14px 16px;
    background: var(--surface-cool);
  }
  .fetch-box h3 {
    margin: 0 0 10px; font-size: 15.5px; font-weight: 700; color: var(--ink);
  }
  .fetch-box .tenant-row label { margin: 0; display: flex; align-items: center; gap: 6px; white-space: nowrap; font-weight: 500; }
  .fetch-shared {
    margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--vince-border);
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }
  .fetch-shared label { margin: 0; display: flex; align-items: center; gap: 8px; font-weight: 500; }
  .fetch-shared details.help { margin-top: 0; }
  #saveFolderHint {
    font-family: var(--font-mono);
    font-size: 13px; color: var(--vince-text-muted);
  }
  @media (max-width: 860px) { .fetch-grid { grid-template-columns: 1fr; } }

  /* Runs in Live */
  #runsOut { margin-top: 14px; overflow-x: auto; }
  .runs-table { width: 100%; border-collapse: collapse; font-size: 14px; }
  .runs-table th {
    text-align: left; color: var(--vince-text-muted); font-weight: 700; padding: 8px 10px;
    border-bottom: 1px solid var(--vince-border);
  }
  .runs-table td { padding: 8px 10px; border-bottom: 1px solid var(--vince-border); vertical-align: top; }
  .runs-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .runs-table .mono { font-family: var(--font-mono); font-size: 13px; }
  .runs-bad { color: var(--danger); font-weight: 700; }
  .runs-notes { font-size: 14px; margin: 8px 0; padding-left: 20px; }
  .runs-meta { font-size: 13.5px; color: var(--vince-text-muted); margin: 8px 0; word-break: break-all; }

  /* Modals */
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(31,54,61,0.55);
    display: flex; align-items: center; justify-content: center; padding: 30px; z-index: 50;
  }
  /* .modal-backdrop sets display:flex, which would otherwise beat [hidden]. */
  .modal-backdrop[hidden] { display: none !important; }
  .modal {
    background: var(--vince-panel); border-radius: var(--radius-lg);
    width: 100%; max-width: 1060px; max-height: 100%;
    display: flex; flex-direction: column; overflow: hidden;
    box-shadow: 0 16px 48px rgba(31,54,61,0.3);
  }
  .modal-head, .modal-foot {
    display: flex; align-items: center; gap: 10px;
    padding: 16px 24px; border-bottom: 1px solid var(--vince-border);
  }
  .modal-foot { border-bottom: none; border-top: 1px solid var(--vince-border); background: var(--surface-cool); }
  .modal-head h2 { margin: 0; font-size: 19px; display: flex; align-items: center; gap: 10px; }
  .modal-head .spacer, .modal-foot .spacer { flex: 1; }
  .modal-body { overflow: auto; padding: 12px 24px 14px; font-size: 15px; }
  .modal-body table { width: 100%; border-collapse: collapse; font-size: 14px; }
  .modal-body thead th {
    position: sticky; top: 0; background: var(--vince-panel);
    text-align: left; color: var(--vince-text-muted); padding: 8px; font-weight: 700;
  }
  /* Run history: tight rows, no wrapping; the Target column is as wide as its
     text and the modal body scrolls sideways for it. */
  #historyTable { width: max-content; min-width: 100%; font-size: 13px; }
  #historyTable th, #historyTable td { padding: 3px 8px; white-space: nowrap; line-height: 1.25; }
  #historyTable td.hist-detail { max-width: 340px; overflow: hidden; text-overflow: ellipsis; }
  #historyTable td.hist-target { min-width: 460px; color: var(--vince-text-muted); }
  #historyTable input[type=checkbox] { margin: 0; vertical-align: middle; }
  #historyPageInfo { font-size: 14px; color: var(--vince-text-muted); }
  .history-tools { flex-wrap: wrap; border-bottom: none; background: white; }
  .history-tools label { margin: 0; display: flex; align-items: center; gap: 8px; font-weight: 500; }
  #historyStatus .err { color: var(--danger); margin-top: 3px; word-break: break-word; }
  .output-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .needs-review {
    background: var(--warn-bg); border: 1px solid var(--warn-border); color: var(--warn-text);
    border-radius: var(--radius-sm); padding: 10px 14px; font-size: 14px; margin-top: 12px;
  }

  /* Pre-flight confirmation modal */
  #confirmModal .modal { max-width: 820px; }
  #confirmModal .modal-foot button, #promptModal .modal-foot button { min-width: 120px; min-height: 44px; }
  .confirm-target {
    background: var(--surface-cool); border: 1px solid var(--vince-border);
    border-radius: var(--radius-md); padding: 12px 16px; margin: 10px 0 16px;
  }
  .confirm-target dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 0; }
  .confirm-target dt { color: var(--vince-text-muted); font-size: 14px; font-weight: 600; }
  .confirm-target dd {
    margin: 0; font-family: var(--font-mono); font-size: 13.5px;
    word-break: break-all;
  }
  .confirm-flag {
    background: #fbe9e7; border: 1px solid #e6a9a3; color: var(--danger-deep);
    border-radius: var(--radius-sm); padding: 10px 14px; font-size: 14px; margin-bottom: 12px; font-weight: 600;
  }
  .confirm-note {
    background: var(--warn-bg); border: 1px solid var(--warn-border); color: var(--warn-text);
    border-radius: var(--radius-sm); padding: 10px 14px; font-size: 14px; margin-bottom: 12px;
  }
  .confirm-group { margin-bottom: 14px; }
  .confirm-group h3 { margin: 0 0 8px; font-size: 15.5px; }
  .confirm-group ul { margin: 0; padding-left: 20px; font-size: 14px; }
  .confirm-group li { margin-bottom: 4px; }
  .confirm-empty { color: var(--vince-text-muted); font-size: 14px; }
</style>
</head>
<body>

<header>
  <div class="mark">V</div>
  <h1>VinceForge</h1>
  <span class="sub">Classic XML / Live JSON / pipeline spec &rarr; Live workflow JSON</span>
  <span class="spacer"></span>
  <button class="header-btn" id="openHistoryBtn">
    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>
    Run history
  </button>
</header>

<main>
  <div class="panel">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z"/><path d="M8 9l-3 3 3 3M16 9l3 3-3 3M14 7l-4 10"/></svg>
      Source
    </h2>
    <div class="source-tabs" id="sourceTabs">
      <button data-type="auto" class="active">Auto-detect</button>
      <button data-type="classic-xml">Classic XML</button>
      <button data-type="classic-pipeline" title="Same Classic file, converted onto the generic pipeline path - keeps per-call filters and BasedOnTransaction, needs an ION API root">Classic XML &rarr; pipeline</button>
      <button data-type="live-json">Live JSON</button>
      <button data-type="pipeline-spec">Pipeline spec</button>
      <button data-type="prompt">Prompt (soon)</button>
      <button class="secondary" id="scaffoldBtn" title="Drop a starter generic-pipeline spec into the box below">Scaffold</button>
    </div>
    <div class="detected-badge" id="detectedBadge"></div>
    <textarea id="sourceContent" placeholder="Paste a Classic XML export, a Live GET /workflows/{id} JSON response, or a generic pipeline spec ({&quot;vinceforge&quot;: &quot;pipeline/1&quot;, ...}) here - or hit Scaffold for a starter spec."></textarea>
  </div>

  <div class="panel">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a5 5 0 0 1 5 5v3H7V7a5 5 0 0 1 5-5z"/><path d="M5 10h14v10H5z"/></svg>
      Target
    </h2>
    <div class="target-summary">
      <span class="who" id="targetName">-</span>
      <span class="ids" id="targetIds"></span>
      <button class="secondary" id="toggleTargetBtn">Change</button>
    </div>
    <div id="targetFields" hidden>
      <label>Target Tenant</label>
      <div class="tenant-row">
        <select id="tenantPreset"></select>
        <button class="secondary" id="addTenantBtn" title="Save the fields below as a new Target Tenant">+ Add</button>
        <button class="secondary" id="removeTenantBtn" title="Remove the selected Target Tenant">Remove</button>
      </div>
      <label>Connection ID</label>
      <input type="text" id="connectionId">
      <label>Tenant ID</label>
      <input type="text" id="tenantId">
      <label>Environment ID</label>
      <input type="text" id="environmentId">
      <label>ION API root <span class="muted" style="font-weight:500">(pipeline paths only)</span></label>
      <input type="text" id="ionApiBaseUrl" placeholder="https://mingle-ionapi.eu1.inforcloudsuite.com/YOUR_TENANT">
      <label>Table header row <span class="muted" style="font-weight:500">(pipeline Code step; blank = the row above the data start)</span></label>
      <input type="text" id="tableHeaderRow" inputmode="numeric" placeholder="e.g. 6" style="width:120px">
      <details class="help"><summary>About these fields</summary><div>
        Pick a saved Target Tenant, or edit the ids directly for an ad hoc target.
        "+ Add" saves the current values under a new (or existing, to overwrite) name.
        The ION API root is where a generated M3 bulk-execute endpoint is built from -
        region and tenant segment are both per-tenant, so copy it from a real REST API
        step in the target tenant. It is needed by the pipeline paths, and by any
        app-import Classic XML block with a non-numeric range filter.
      </div></details>
      <div class="api-client">
        <h3>API Client <span class="muted" style="font-weight:500">(used when no bearer token is pasted)</span></h3>
        <label for="apiClientId">Client ID</label>
        <input type="text" id="apiClientId" autocomplete="off">
        <label for="apiClientSecret">Client Secret</label>
        <input type="password" id="apiClientSecret" autocomplete="new-password">
        <label for="apiTokenUri">Token URI</label>
        <input type="text" id="apiTokenUri" placeholder="https://tenant-....auth.eu-west-1.amazoncognito.com/oauth2/token">
        <div class="row">
          <button class="primary" id="apiClientSaveBtn">Save</button>
          <button id="apiClientTestBtn">Test connection</button>
          <button id="apiClientRemoveBtn">Remove</button>
          <span class="status" id="apiClientStatus"></span>
        </div>
        <details class="help"><summary>How this works</summary><div>
          Create an API Client in Live (Administration &rarr; Access Management &rarr; API Clients) and
          copy its Client ID, Secret and Token URI here. It is saved with the selected Target Tenant
          preset (in RUN/api_clients.json on this machine; the secret is never sent back to the page).
          When the bearer token field is empty, VinceForge asks the Token URI for a token itself, and
          only while the Target ids still match that preset. <strong>Not yet verified</strong> that
          api.vince.live accepts an API Client token - press Test connection once to find out.
        </div></details>
      </div>
    </div>
    <label>Bearer token <span class="muted" style="font-weight:500">(needed for every call to Live)</span></label>
    <input type="password" id="bearerToken" placeholder="eyJ...">
    <div class="hint" id="apiClientHint"></div>
    <div class="hint" id="tokenPastedInfo">
      Short-lived - expect a 401 after an hour or so and paste a fresh one.
    </div>
  </div>

  <div class="panel output-panel">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v13"/><path d="M7 11l5 5 5-5"/><path d="M4 21h16"/></svg>
      Fetch from Live
    </h2>
    <div class="fetch-grid">
      <div class="fetch-box">
        <h3>Workflow</h3>
        <div class="tenant-row">
          <input type="text" id="fetchWorkflowId" placeholder="WORKFLOW-...">
          <button class="secondary" id="fetchWorkflowBtn">Fetch</button>
        </div>
        <div class="hint">By workflow id. Fills Source, ready to retarget and Create in Live.</div>
      </div>
      <div class="fetch-box">
        <h3>Custom Table</h3>
        <div class="tenant-row">
          <input type="text" id="fetchTableName" placeholder="table-name">
          <label title="Also pull the table&#39;s rows. Rows can't be imported - they're captured for reference.">
            <input type="checkbox" id="fetchTableRows"> with rows
          </label>
          <button class="secondary" id="fetchTableBtn">Fetch table</button>
        </div>
        <div class="hint">By table name.</div>
        <details class="help"><summary>How this works</summary><div>No endpoint for reading a table
          definition is confirmed: this probes, verifies what comes back, and says what it tried if
          none work. Rows can't be imported - they're captured for reference.</div></details>
      </div>
    </div>
    <div class="fetch-shared">
      <label title="Applies to both fetches above">
        <input type="checkbox" id="saveToFolder"> Also save into the app folder
      </label>
      <span id="saveFolderHint">no app folder set</span>
      <span class="status" id="fetchStatus"></span>
    </div>
  </div>

  <div class="panel output-panel">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>
      Runs in Live
    </h2>
    <div class="fetch-grid">
      <div class="fetch-box">
        <h3>One workflow</h3>
        <div class="tenant-row">
          <input type="text" id="runsWorkflowId" placeholder="WORKFLOW-...">
          <button class="secondary" id="runsListBtn">List runs</button>
        </div>
        <div class="hint">Its executions, newest first.</div>
        <details class="help"><summary>What a trace shows</summary><div>Open a run for a step-by-step
          trace: rows in and out of each step, and M3's message per row.</div></details>
      </div>
      <div class="fetch-box">
        <h3>Whole tenant</h3>
        <div class="tenant-row">
          <input type="text" id="runsMatch" placeholder="name contains... (optional)">
          <label title="Also ask workflows whose list entry says they have never run">
            <input type="checkbox" id="runsIncludeIdle"> idle too
          </label>
          <button class="secondary" id="runsSweepBtn">Sweep</button>
        </div>
        <div class="hint">Every workflow in the token's tenant, worst first.</div>
        <details class="help"><summary>How long it takes</summary><div>One request per workflow, so a
          big tenant takes minutes.</div></details>
      </div>
    </div>
    <div class="fetch-shared">
      <span class="muted" style="font-size:14px;font-weight:600;">Read-only</span>
      <details class="help"><summary>Details</summary><div>Uses the bearer token above and only ever
        GETs. Traces and sweeps are saved under RUN/runs/.</div></details>
      <span class="status" id="runsStatus"></span>
    </div>
    <div id="runsOut"></div>
  </div>

  <div class="actions">
    <button class="primary" id="generateBtn">Generate</button>
    <button class="secondary" id="createBtn" disabled>Create in Live</button>
    <span class="status" id="statusMsg"></span>
  </div>

  <div class="panel output-panel">
    <div class="output-header">
      <h2 style="margin:0">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H6a2 2 0 0 0-2 2v3a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3a2 2 0 0 0 2 2h2M16 3h2a2 2 0 0 1 2 2v3a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3a2 2 0 0 1-2 2h-2"/></svg>
        Generated JSON
      </h2>
      <button class="secondary" id="copyBtn">Copy</button>
    </div>
    <div id="blockPicker" style="display:none"></div>
    <textarea id="outputJson" readonly placeholder="Generated Live workflow JSON will appear here."></textarea>
    <div id="needsReview" style="display:none" class="needs-review"></div>
  </div>

  <div class="panel output-panel">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      Import an app folder
    </h2>
    <label>App folder <span class="muted" style="font-weight:500">(a path on this machine)</span></label>
    <div class="app-row">
      <input type="text" id="appFolder" placeholder="C:\path\to\MyApp">
      <button class="secondary" id="appScanBtn">Scan</button>
      <label title="Scan and build everything, but send nothing to Live">
        <input type="checkbox" id="appDryRun"> Dry run
      </label>
      <label title="Also write app-builder-prompt.generated.md next to the app. Skipped on a dry run - a dry-run prompt has placeholder ids.">
        <input type="checkbox" id="promptToFolder" checked> Prompt into folder
      </label>
      <button class="primary" id="appImportBtn">Import app</button>
      <span class="status" id="appStatus"></span>
    </div>
    <div class="hint">Scan and Dry run send nothing to Live.</div>
    <details class="help"><summary>What gets imported, and in what order</summary><div>
      The folder holds tables, workflows, Classic XML and App Builder source. Import uses the Target
      and bearer token above. Tables are created first, then workflows (a workflow references a table
      by name, so the table has to exist). Table rows are never imported - there's no confirmed API
      for writing them.
    </div></details>
    <div id="appProgress" hidden>
      <div class="bar"><span></span></div>
      <div class="text"></div>
    </div>
    <div id="appPlan"></div>
    <div id="appPromptWrap">
      <div class="output-header">
        <h2 style="margin:0">App Builder prompt</h2>
        <button class="secondary" id="copyPromptBtn">Copy</button>
      </div>
      <textarea id="appPrompt" readonly></textarea>
      <div id="appPromptPath" class="hint"></div>
    </div>
  </div>
</main>

<div class="modal-backdrop" id="historyModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 4v6c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>
        Run history
      </h2>
      <span class="spacer"></span>
      <button class="secondary" id="refreshHistoryBtn">Refresh</button>
      <button class="secondary" id="closeHistoryBtn">Close</button>
    </div>
    <div class="modal-body">
      <table id="historyTable">
        <thead>
          <tr>
            <th><input type="checkbox" id="historySelectAll" title="Select / clear every deletable row in the filtered list (all pages)"></th>
            <th>When</th>
            <th>Result</th>
            <th>Name</th>
            <th>Workflow ID / error</th>
            <th>Target</th>
          </tr>
        </thead>
        <tbody id="historyBody"></tbody>
      </table>
    </div>
    <div class="modal-foot history-tools">
      <input type="text" id="historyFilter" placeholder="Filter by name, app or id" style="width:260px">
      <label class="muted"><input type="checkbox" id="historyOnlyDeletable"> only deletable</label>
      <button class="secondary" id="historySelectShownBtn">Select all shown</button>
      <button class="secondary" id="historyClearSelBtn">Clear</button>
      <span class="spacer"></span>
      <button class="destructive" id="historyDeleteBtn" disabled>Delete selected from Live</button>
    </div>
    <div id="historyStatus" style="padding:0 24px 10px;font-size:14px;"></div>
    <div class="modal-foot">
      <button class="secondary" id="historyPrevBtn">Previous</button>
      <span id="historyPageInfo"></span>
      <button class="secondary" id="historyNextBtn">Next</button>
      <span class="spacer"></span>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="confirmModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2 id="confirmTitle">Confirm</h2>
      <span class="spacer"></span>
    </div>
    <div class="modal-body" id="confirmBody"></div>
    <div class="modal-foot">
      <span class="spacer"></span>
      <button class="secondary" id="confirmCancelBtn">Cancel</button>
      <button class="primary" id="confirmGoBtn">Send to Live</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="promptModal" hidden>
  <div class="modal" style="max-width: 480px;">
    <div class="modal-head">
      <h2 id="promptTitle">Name</h2>
      <span class="spacer"></span>
    </div>
    <div class="modal-body">
      <p id="promptMessage" style="margin: 0 0 8px;"></p>
      <input type="text" id="promptInput" style="width: 100%;">
    </div>
    <div class="modal-foot">
      <span class="spacer"></span>
      <button class="secondary" id="promptCancelBtn">Cancel</button>
      <button class="primary" id="promptOkBtn">OK</button>
    </div>
  </div>
</div>

<script>
let currentSourceType = "auto";
let lastResult = null;
// Kept so the confirmation box can repeat what Generate warned about. The
// warnings are already on the page, but the box is the last moment before
// the thing is created, and some of them ("this endpoint is still a
// placeholder - it will save happily and fail at execution") are exactly
// what someone about to click Create needs put back in front of them.
let lastWarnings = [];
// Every workflow the current source produced. One entry for all sources
// except a multi-block Classic file, which yields one per block - see the
// Generate handler.
let lastUnits = [];
let currentUnit = 0;

// Put one of the generated workflows on screen and make it the one Create
// in Live will post.
function showUnit(index) {
  const unit = lastUnits[index];
  if (!unit) return;
  currentUnit = index;
  lastResult = unit.result;
  lastWarnings = (unit.warnings || []).slice();
  document.getElementById("outputJson").value = JSON.stringify(unit.result, null, 2);

  const notes = [];
  if (JSON.stringify(unit.result).includes("_needs_review")) {
    notes.push("Some fields were flagged for manual review - search the JSON below for \"_needs_review\".");
  }
  // Warnings arrive out-of-band rather than inside the JSON: a
  // Transform/Code/REST stepConfig with an extra key in it can silently
  // reset on save, so nothing extra goes in there.
  lastWarnings.forEach((w) => notes.push(w));
  const box = document.getElementById("needsReview");
  if (notes.length) {
    box.style.display = "block";
    box.textContent = notes.join("\n\n");
    box.style.whiteSpace = "pre-wrap";
  } else {
    box.style.display = "none";
  }
}

function renderBlockPicker() {
  const picker = document.getElementById("blockPicker");
  if (lastUnits.length < 2) {
    picker.style.display = "none";
    picker.innerHTML = "";
    return;
  }
  picker.style.display = "block";
  picker.innerHTML =
    '<div class="muted" style="margin-bottom:6px">This source holds ' + lastUnits.length +
    " blocks, so it becomes " + lastUnits.length + " workflows. Create in Live posts the " +
    "selected one; import the folder to create them all.</div>" +
    lastUnits.map((u, i) =>
      '<button class="secondary" data-unit="' + i + '" style="margin:0 6px 6px 0">' +
      escapeHtml(u.name || "block " + (i + 1)) + "</button>").join("");
  picker.querySelectorAll("button[data-unit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      showUnit(parseInt(btn.dataset.unit, 10));
      markSelectedBlock();
    });
  });
  markSelectedBlock();
}

function markSelectedBlock() {
  document.querySelectorAll("#blockPicker button[data-unit]").forEach((btn) => {
    const on = parseInt(btn.dataset.unit, 10) === currentUnit;
    btn.style.outline = on ? "2px solid var(--vince-accent, #0a7)" : "none";
  });
}

// button[data-type], not every button in the row: Scaffold lives in the
// same row but is an action, not a source type.
document.querySelectorAll("#sourceTabs button[data-type]").forEach(btn => {
  btn.addEventListener("click", () => selectSourceTab(btn.dataset.type));
});

function selectSourceTab(type) {
  document.querySelectorAll("#sourceTabs button[data-type]").forEach(b => b.classList.toggle("active", b.dataset.type === type));
  currentSourceType = type;
}

// Same detection rule as detect_source_type() server-side, just enough to
// pick which naming prompt (if any) to show before Generate fires.
function guessSourceType(content) {
  const s = (content || "").trim();
  if (s.startsWith("<?xml") || s.includes("<VinceExcelConfig")) return "classic-xml";
  if (s.startsWith("{")) {
    try {
      const parsed = JSON.parse(s);
      if (parsed && String(parsed.vinceforge || "").startsWith("pipeline/")) return "pipeline-spec";
      return "live-json";
    } catch (e) { return "prompt"; }
  }
  return "prompt";
}

// No file upload here, only paste - so the best available suggestion is
// the source XML's own first block Label, not a filename.
function guessXmlLabel(content) {
  const m = (content || "").match(/<(?:Export|Import)\b[^>]*\bLabel="([^"]*)"/);
  return m ? m[1] : "";
}

function setStatus(msg, cls) {
  const el = document.getElementById("statusMsg");
  el.textContent = msg || "";
  el.className = "status " + (cls || "");
}

let tenantPresets = {};

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
}

// --- Pre-flight confirmation ---------------------------------------------
//
// Everything that writes to a customer tenant goes through here first. The
// browser's own confirm() can only show one line of text, which is not
// enough to answer the two questions that actually matter before an import:
// what exactly is about to be created, and whose tenant is it going into.
// The second one is the dangerous one - the TOKEN decides the tenant, not
// the tenantId in the body, and a mismatched pair fails silently rather
// than with a 401 (see CLAUDE.md, auth), so both are shown side by side.

let confirmResolve = null;

function openConfirm(opts) {
  document.getElementById("confirmTitle").textContent = opts.title;
  document.getElementById("confirmBody").innerHTML = opts.bodyHtml;
  const go = document.getElementById("confirmGoBtn");
  go.textContent = opts.actionLabel || "Send to Live";
  // Red only for removing things from Live; every other confirm is the primary action.
  go.classList.toggle("destructive", !!opts.destructive);
  go.classList.toggle("primary", !opts.destructive);
  document.getElementById("confirmModal").hidden = false;
  go.focus();
  return new Promise(resolve => { confirmResolve = resolve; });
}

function closeConfirm(answer) {
  document.getElementById("confirmModal").hidden = true;
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(answer);
}

document.getElementById("confirmCancelBtn").addEventListener("click", () => closeConfirm(false));
document.getElementById("confirmGoBtn").addEventListener("click", () => closeConfirm(true));
document.getElementById("confirmModal").addEventListener("click", (e) => {
  if (e.target.id === "confirmModal") closeConfirm(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("confirmModal").hidden) closeConfirm(false);
});

// --- A native prompt()/confirm() blocks the whole page (and anything
// automating it - a browser extension's remote control included) until a
// human dismisses it by hand. This modal answers the same two questions
// (get a short string, get a yes/no) without ever handing control to the
// browser chrome.

let promptResolve = null;

function openPrompt(title, defaultValue, message) {
  document.getElementById("promptTitle").textContent = title;
  const msgEl = document.getElementById("promptMessage");
  msgEl.textContent = message || "";
  msgEl.hidden = !message;
  const input = document.getElementById("promptInput");
  input.hidden = false;
  input.value = defaultValue || "";
  document.getElementById("promptOkBtn").textContent = "OK";
  document.getElementById("promptModal").hidden = false;
  input.focus();
  input.select();
  return new Promise(resolve => { promptResolve = resolve; }).then(ok => ok ? input.value : null);
}

function openConfirmSimple(title, message) {
  document.getElementById("promptTitle").textContent = title;
  const msgEl = document.getElementById("promptMessage");
  msgEl.textContent = message || "";
  msgEl.hidden = false;
  document.getElementById("promptInput").hidden = true;
  document.getElementById("promptOkBtn").textContent = "Yes";
  document.getElementById("promptModal").hidden = false;
  document.getElementById("promptOkBtn").focus();
  return new Promise(resolve => { promptResolve = resolve; });
}

function closePrompt(answer) {
  document.getElementById("promptModal").hidden = true;
  const resolve = promptResolve;
  promptResolve = null;
  if (resolve) resolve(answer);
}

document.getElementById("promptCancelBtn").addEventListener("click", () => closePrompt(false));
document.getElementById("promptOkBtn").addEventListener("click", () => closePrompt(true));
document.getElementById("promptModal").addEventListener("click", (e) => {
  if (e.target.id === "promptModal") closePrompt(false);
});
document.getElementById("promptInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") closePrompt(true);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("promptModal").hidden) closePrompt(false);
});

// The token for a call to Live: the pasted one if there is one, otherwise
// one the server mints from the selected preset's API Client. "" means
// neither is available, so each caller keeps its own "enter a token"
// message; a failed mint throws with the server's reason.
let apiClientState = {configured: false};
let lastMinted = null;

async function currentBearer() {
  const pasted = document.getElementById("bearerToken").value.trim();
  if (pasted) return pasted;
  const target = currentTarget();
  if (!target.preset || !apiClientState.configured) return "";
  const resp = await fetch("/api/apiclient/token", {method: "POST",
    body: JSON.stringify({preset: target.preset, tenantId: target.tenantId})});
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "Couldn't get a token from the API Client.");
  lastMinted = {token: data.token, preset: data.preset, tenantId: data.tenantId};
  return data.token;
}

async function tokenInfo(bearerToken) {
  let info;
  try {
    const resp = await fetch("/api/token/info", {method: "POST", body: JSON.stringify({bearerToken})});
    info = await resp.json();
  } catch (e) {
    info = {ok: false, tenant: null, hasToken: !!bearerToken};
  }
  // A minted token may not name its tenant. It is only handed out for the
  // preset whose ids are in the Target panel, so that preset's tenant is
  // the one it acts on - said as such in the box, not dressed up as a claim.
  if (lastMinted && bearerToken === lastMinted.token) {
    info.via = "API Client of \u201c" + lastMinted.preset + "\u201d";
    if (!info.tenant) info.presetTenant = lastMinted.tenantId;
  }
  return info;
}

function confirmRow(label, value) {
  return "<dt>" + escapeHtml(label) + "</dt><dd>" + escapeHtml(value || "\u2014") + "</dd>";
}

// The target block: the three ids being written into the workflows, plus
// the tenant the token itself resolves to. Those two can disagree, and when
// they do the token wins - so say so in red rather than in a footnote.
// `opts.tokenOptional` marks a run that sends nothing to Live - a dry run.
// A missing token is fatal to an import and merely degrading to a dry run
// (no metadata fetch, so converted fields get TODO headers), and saying
// "this will fail" about a run that will not fail teaches people to click
// past the box, which is the one thing a confirmation must not do.
function targetBlockHtml(target, info, opts) {
  const parts = [];
  const tokenOptional = !!(opts && opts.tokenOptional);
  const tokenTenant = info && info.tenant;

  if (info && info.hasToken === false) {
    parts.push(tokenOptional
      ? '<div class="confirm-note">No bearer token. The dry run itself is fine, but no M3 field ' +
        "metadata can be fetched, so converted Classic XML fields get a &quot;TODO: metadata not " +
        "found&quot; column header and type UNKNOWN.</div>"
      : '<div class="confirm-flag">No bearer token. This will fail.</div>');
  } else if (info && info.expiryWarning) {
    parts.push('<div class="confirm-flag">' + escapeHtml(info.expiryWarning) + "</div>");
  } else if (tokenTenant && target.tenantId && tokenTenant !== target.tenantId) {
    parts.push('<div class="confirm-flag"><strong>Tenant mismatch.</strong> The token belongs to ' +
      escapeHtml(tokenTenant) + ", but these workflows are being built for " +
      escapeHtml(target.tenantId) + ". The token decides where this lands \u2014 the id in the body does " +
      "not \u2014 and the mismatch does not raise a 401, it just makes the connection invisible.</div>");
  } else if (!tokenTenant && info && info.via) {
    parts.push('<div class="confirm-note">Using the ' + escapeHtml(info.via) + ". Its token doesn't name " +
      "a tenant; it is used only because the Target ids below match that preset (tenant " +
      escapeHtml(info.presetTenant || "") + ").</div>");
  } else if (!tokenTenant && info && info.hasToken) {
    parts.push('<div class="confirm-note">Could not read a tenant from the token, so the target below ' +
      "could not be cross-checked against it.</div>");
  }

  parts.push('<div class="confirm-target"><dl>' +
    confirmRow("Tenant", target.tenantId) +
    (info && info.via ? confirmRow("Token", info.via) : "") +
    (tokenTenant ? confirmRow("Token's tenant", tokenTenant + (tokenTenant === target.tenantId ? "  \u2713 same" : "")) : "") +
    confirmRow("Connection", target.connectionId) +
    confirmRow("Environment", target.environmentId) +
    (target.preset ? confirmRow("Preset", target.preset) : "") +
    "</dl></div>");
  return parts.join("");
}

function confirmGroupHtml(title, items, emptyText) {
  if (!items.length) {
    return emptyText ? '<div class="confirm-group"><h3>' + escapeHtml(title) + "</h3>" +
      '<div class="confirm-empty">' + escapeHtml(emptyText) + "</div></div>" : "";
  }
  return '<div class="confirm-group"><h3>' + escapeHtml(title) + " (" + items.length + ")</h3><ul>" +
    items.map(i => "<li>" + i + "</li>").join("") + "</ul></div>";
}

function currentTarget() {
  return {
    connectionId: document.getElementById("connectionId").value.trim(),
    tenantId: document.getElementById("tenantId").value.trim(),
    environmentId: document.getElementById("environmentId").value.trim(),
    preset: document.getElementById("tenantPreset").value || "",
  };
}

const TENANT_FIELD_IDS = ["connectionId", "tenantId", "environmentId"];
const TENANT_FIELD_PLACEHOLDERS = {
  connectionId: "CONNECTION-...",
  tenantId: "TENANT-...",
  environmentId: "ENVIRONMENT-...",
};

function shortId(value) {
  // "CONNECTION-4f2b91..." - enough to tell two targets apart at a glance
  // without the panel having to be open.
  if (!value) return "";
  return value.length > 20 ? value.slice(0, 17) + "\u2026" : value;
}

function updateTargetSummary() {
  const ids = TENANT_FIELD_IDS.map(id => document.getElementById(id).value.trim());
  const preset = document.getElementById("tenantPreset").value;
  const matchesPreset = preset && tenantPresets[preset] &&
    tenantPresets[preset].connectionId === ids[0] &&
    tenantPresets[preset].tenantId === ids[1] &&
    tenantPresets[preset].environmentId === ids[2];

  const name = document.getElementById("targetName");
  if (!ids.some(Boolean)) {
    name.textContent = "No target set";
    name.style.color = "var(--vince-error)";
  } else {
    // An edited preset is NOT that preset any more - say so, or you can
    // create into the wrong tenant believing the label.
    name.textContent = matchesPreset ? preset : (preset ? preset + " (edited)" : "Custom");
    name.style.color = "";
  }
  document.getElementById("targetIds").textContent = ids.filter(Boolean).map(shortId).join("  ");
}

function updateApiClientHint() {
  const el = document.getElementById("apiClientHint");
  const pasted = document.getElementById("bearerToken").value.trim();
  const preset = document.getElementById("tenantPreset").value || "";
  el.textContent = (!pasted && apiClientState.configured)
    ? "No token pasted - calls to Live will use the API Client saved for \u201c" + preset + "\u201d."
    : "";
}

function setApiClientStatus(msg, cls) {
  const el = document.getElementById("apiClientStatus");
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}

async function loadApiClient() {
  const preset = document.getElementById("tenantPreset").value || "";
  apiClientState = {configured: false};
  lastMinted = null;
  document.getElementById("apiClientSecret").value = "";
  document.getElementById("apiClientSecret").placeholder = "";
  document.getElementById("apiClientId").value = "";
  document.getElementById("apiTokenUri").value = "";
  setApiClientStatus("");
  if (preset) {
    try {
      const resp = await fetch("/api/apiclient/get", {method: "POST", body: JSON.stringify({preset})});
      const data = await resp.json();
      if (data.ok) {
        apiClientState = data;
        document.getElementById("apiClientId").value = data.clientId || "";
        document.getElementById("apiTokenUri").value = data.tokenUri || "";
        document.getElementById("apiClientSecret").placeholder = data.hasSecret ? "saved - leave blank to keep" : "";
        if (data.warning) setApiClientStatus(data.warning, "error");
      }
    } catch (e) { /* a convenience - a pasted token still works */ }
  }
  updateApiClientHint();
}

document.getElementById("apiClientSaveBtn").addEventListener("click", async () => {
  const preset = document.getElementById("tenantPreset").value || "";
  if (!preset) { setApiClientStatus("Save the Target Tenant with \u201c+ Add\u201d first.", "error"); return; }
  const payload = {
    preset,
    clientId: document.getElementById("apiClientId").value.trim(),
    clientSecret: document.getElementById("apiClientSecret").value.trim(),
    tokenUri: document.getElementById("apiTokenUri").value.trim(),
  };
  try {
    const resp = await fetch("/api/apiclient/save", {method: "POST", body: JSON.stringify(payload)});
    const data = await resp.json();
    if (!data.ok) { setApiClientStatus(data.error || "Couldn't save.", "error"); return; }
    await loadApiClient();
    setApiClientStatus(data.warning || "Saved for \u201c" + preset + "\u201d.", data.warning ? "error" : "ok");
  } catch (e) {
    setApiClientStatus("Request failed: " + e, "error");
  }
});

document.getElementById("apiClientTestBtn").addEventListener("click", async () => {
  const preset = document.getElementById("tenantPreset").value || "";
  if (!apiClientState.configured) { setApiClientStatus("Save an API Client first.", "error"); return; }
  setApiClientStatus("Testing...");
  try {
    const resp = await fetch("/api/apiclient/test", {method: "POST", body: JSON.stringify({preset})});
    const data = await resp.json();
    if (data.ok) {
      setApiClientStatus("Works - api.vince.live answered with " + data.workflowsOnFirstPage +
        " workflow(s) on the first page.", "ok");
    } else {
      setApiClientStatus((data.stage === "token" ? "Getting a token failed: " : "") + (data.error || "Failed."), "error");
    }
  } catch (e) {
    setApiClientStatus("Request failed: " + e, "error");
  }
});

document.getElementById("apiClientRemoveBtn").addEventListener("click", async () => {
  const preset = document.getElementById("tenantPreset").value || "";
  if (!preset || !apiClientState.configured) return;
  if (!(await openConfirmSimple("Remove API Client", 'Remove the API Client saved for "' + preset + '"?'))) return;
  try {
    await fetch("/api/apiclient/remove", {method: "POST", body: JSON.stringify({preset})});
    await loadApiClient();
    setApiClientStatus("Removed.", "ok");
  } catch (e) {
    setApiClientStatus("Request failed: " + e, "error");
  }
});

function applyTenantToFields(name) {
  const t = tenantPresets[name];
  if (!t) return;
  TENANT_FIELD_IDS.forEach(id => {
    document.getElementById(id).placeholder = TENANT_FIELD_PLACEHOLDERS[id];
  });
  document.getElementById("connectionId").value = t.connectionId || "";
  document.getElementById("tenantId").value = t.tenantId || "";
  document.getElementById("environmentId").value = t.environmentId || "";
  document.getElementById("ionApiBaseUrl").value = t.ionApiBaseUrl || "";
  updateTargetSummary();
  loadApiClient();
}

function clearTenantFields() {
  // No Target Tenant selected - blank the fields AND drop the example
  // placeholders, so an empty field reads as "nothing configured" rather
  // than looking like it's still hinting at a value to type.
  TENANT_FIELD_IDS.forEach(id => {
    const el = document.getElementById(id);
    el.value = "";
    el.placeholder = "";
  });
  // Not in TENANT_FIELD_IDS: it is optional and only the pipeline paths
  // read it, so it must not count towards "is a target configured".
  document.getElementById("ionApiBaseUrl").value = "";
  updateTargetSummary();
  loadApiClient();
}

async function loadTenants(selectName) {
  try {
    const resp = await fetch("/api/tenants");
    const data = await resp.json();
    tenantPresets = data.tenants || {};
    const select = document.getElementById("tenantPreset");
    const names = Object.keys(tenantPresets);
    select.innerHTML = names.map(n => '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + '</option>').join("");
    if (!names.length) {
      clearTenantFields();
      return;
    }
    // Preference order: an explicit caller-supplied name (just
    // created/edited this preset) - the one the server remembers was
    // selected last - whatever happens to be first. Without the middle
    // option, every page reload silently fell back to "first in the
    // file", which looked exactly like a saved preset reverting even
    // though it was still there the whole time.
    const toSelect = (selectName && names.includes(selectName)) ? selectName
      : (data.lastSelected && names.includes(data.lastSelected)) ? data.lastSelected
      : names[0];
    select.value = toSelect;
    applyTenantToFields(toSelect);
    updateTargetSummary();
  } catch (e) {
    // presets are a convenience - a failed fetch shouldn't block manual entry
  }
}

function rememberTenantSelection(name) {
  // Best-effort - if this fails, the worst case is the OLD behaviour
  // (falls back to names[0] next load), not a broken session now.
  fetch("/api/tenants/select", {method: "POST", body: JSON.stringify({name})}).catch(() => {});
}

document.getElementById("tenantPreset").addEventListener("change", (e) => {
  applyTenantToFields(e.target.value);
  rememberTenantSelection(e.target.value);
});

TENANT_FIELD_IDS.forEach(id => {
  document.getElementById(id).addEventListener("input", updateTargetSummary);
});

document.getElementById("toggleTargetBtn").addEventListener("click", () => {
  const fields = document.getElementById("targetFields");
  fields.hidden = !fields.hidden;
  document.getElementById("toggleTargetBtn").textContent = fields.hidden ? "Change" : "Done";
});

document.getElementById("addTenantBtn").addEventListener("click", async () => {
  const name = await openPrompt("Name for this Target Tenant", document.getElementById("tenantPreset").value || "");
  if (!name || !name.trim()) return;
  const payload = {
    name: name.trim(),
    connectionId: document.getElementById("connectionId").value.trim(),
    tenantId: document.getElementById("tenantId").value.trim(),
    environmentId: document.getElementById("environmentId").value.trim(),
    ionApiBaseUrl: document.getElementById("ionApiBaseUrl").value.trim(),
  };
  try {
    const resp = await fetch("/api/tenants/save", {method: "POST", body: JSON.stringify(payload)});
    const data = await resp.json();
    if (!data.ok) {
      setStatus(data.error || "Couldn't save Target Tenant.", "error");
      return;
    }
    await loadTenants(payload.name);
    setStatus('Saved Target Tenant "' + payload.name + '"', "ok");
  } catch (e) {
    setStatus("Request failed: " + e, "error");
  }
});

document.getElementById("removeTenantBtn").addEventListener("click", async () => {
  const select = document.getElementById("tenantPreset");
  const name = select.value;
  if (!name) return;
  if (!(await openConfirmSimple("Remove Target Tenant", 'Remove Target Tenant "' + name + '"?'))) return;
  try {
    const resp = await fetch("/api/tenants/delete", {method: "POST", body: JSON.stringify({name})});
    const data = await resp.json();
    if (!data.ok) {
      setStatus(data.error || "Couldn't remove Target Tenant.", "error");
      return;
    }
    await loadTenants();
    setStatus('Removed Target Tenant "' + name + '"', "ok");
  } catch (e) {
    setStatus("Request failed: " + e, "error");
  }
});

loadTenants();

// --- Bearer token persistence -------------------------------------------
//
// Off by default in spirit (CLAUDE.md: "not stored anywhere in this
// project") - this install asked for an exception, so it persists to a
// gitignored local file (token_store.py) with a pasted-at timestamp, shown
// here so a stale token is visible instead of silently retried into 401s.

function formatTokenAge(pastedAt) {
  if (!pastedAt) return "Short-lived - expect a 401 after an hour or so and paste a fresh one.";
  const ageMs = Date.now() - pastedAt * 1000;
  const mins = Math.floor(ageMs / 60000);
  const when = new Date(pastedAt * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  let ageText;
  if (mins < 1) ageText = "just now";
  else if (mins === 1) ageText = "1 minute ago";
  else if (mins < 60) ageText = mins + " minutes ago";
  else ageText = Math.floor(mins / 60) + "h " + (mins % 60) + "m ago";
  const stale = mins >= 50;
  return "Pasted at " + when + " (" + ageText + ")" +
    (stale ? " - likely expired, paste a fresh one." : " - expect a 401 after an hour or so.");
}

let lastTokenPastedAt = null;

function refreshTokenAgeDisplay() {
  document.getElementById("tokenPastedInfo").textContent = formatTokenAge(lastTokenPastedAt);
}

async function loadSavedToken() {
  try {
    const resp = await fetch("/api/token");
    const data = await resp.json();
    if (data.ok && data.token) {
      document.getElementById("bearerToken").value = data.token;
      lastTokenPastedAt = data.pastedAt;
    }
  } catch (e) {
    // a failed load just leaves the field empty - not fatal
  }
  refreshTokenAgeDisplay();
}

async function saveTokenNow() {
  const token = document.getElementById("bearerToken").value.trim();
  try {
    const resp = await fetch("/api/token/save", {method: "POST", body: JSON.stringify({token})});
    const data = await resp.json();
    if (data.ok) lastTokenPastedAt = data.pastedAt;
  } catch (e) {
    // saving is a convenience - a failed save shouldn't block using the token this session
  }
  refreshTokenAgeDisplay();
}

document.getElementById("bearerToken").addEventListener("change", saveTokenNow);
document.getElementById("bearerToken").addEventListener("input", updateApiClientHint);
setInterval(refreshTokenAgeDisplay, 60000);
loadSavedToken();

document.getElementById("fetchWorkflowBtn").addEventListener("click", async () => {
  let workflowId = document.getElementById("fetchWorkflowId").value.trim().replace(/^["']|["']$/g, "");
  // Live's API only accepts the literal uppercase "WORKFLOW-" prefix, but
  // typing/pasting is error-prone - normalize any case variant of the
  // prefix instead of rejecting it.
  workflowId = workflowId.replace(/^workflow-/i, "WORKFLOW-");
  if (!workflowId) {
    setFetchStatus("Enter a workflow id (WORKFLOW-...) to fetch.", "error");
    return;
  }
  if (!/^WORKFLOW-[a-f0-9]{32}$/i.test(workflowId)) {
    setFetchStatus('Doesn\'t look like a valid workflow id - expected "WORKFLOW-" + 32 hex chars.', "error");
    return;
  }
  workflowId = "WORKFLOW-" + workflowId.slice("WORKFLOW-".length).toLowerCase();
  let bearerToken;
  try { bearerToken = await currentBearer(); } catch (e) { setFetchStatus(e.message, "error"); return; }
  if (!bearerToken) {
    setFetchStatus("Paste a bearer token, or save an API Client for this Target Tenant (Target > Change).", "error");
    return;
  }
  const folder = saveFolderOrNull();
  if (folder === null) {
    setFetchStatus("Save-into-folder is on but no app folder is set - enter a path or untick it.", "error");
    return;
  }
  setFetchStatus("Fetching workflow...");
  try {
    const resp = await fetch("/api/fetch-workflow", {
      method: "POST",
      body: JSON.stringify({workflowId, bearerToken, folder}),
    });
    const data = await resp.json();
    if (!data.ok) {
      setFetchStatus(data.error || "Fetch failed.", "error");
      return;
    }
    document.getElementById("sourceContent").value = JSON.stringify(data.result, null, 2);
    selectSourceTab("live-json");
    document.getElementById("detectedBadge").textContent = "";

    const name = data.workflowName || data.result.workflowName || workflowId;
    if (folder) {
      reportFetchWrites(data);
      setFetchStatus("Fetched " + name + " into Source and " + folder, "ok");
      // Re-scan so the plan picks up the workflow just written in.
      document.getElementById("appScanBtn").click();
    } else {
      setFetchStatus("Fetched " + name + " into Source", "ok");
    }
  } catch (e) {
    setFetchStatus("Request failed: " + e, "error");
  }
});

// --- Runs in Live (read-only, runs.py) ------------------------------------

function setRunsStatus(msg, cls) {
  const el = document.getElementById("runsStatus");
  el.textContent = msg || "";
  el.className = "status" + (cls ? " " + cls : "");
}

async function runsCall(path, body) {
  const bearerToken = await currentBearer();
  if (!bearerToken) throw new Error("Paste a bearer token, or save an API Client for this Target Tenant (Target > Change).");
  const resp = await fetch(path, {method: "POST", body: JSON.stringify(Object.assign({bearerToken}, body))});
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "Request failed.");
  return data;
}

function runsWarnings(list) {
  return (list || []).length
    ? '<ul class="runs-notes">' + list.map(w => "<li>" + escapeHtml(w) + "</li>").join("") + "</ul>" : "";
}

async function listRuns(workflowId) {
  workflowId = workflowId.trim().replace(/^workflow-/i, "WORKFLOW-");
  if (!/^WORKFLOW-[a-f0-9]{32}$/i.test(workflowId)) {
    setRunsStatus('Expected "WORKFLOW-" + 32 hex chars.', "error");
    return;
  }
  document.getElementById("runsWorkflowId").value = workflowId;
  setRunsStatus("Listing runs...");
  try {
    const data = await runsCall("/api/runs/list", {workflowId});
    const rows = data.executions.map(e =>
      "<tr><td class=\"mono\">" + escapeHtml(e.startTime || "-") + "</td>" +
      "<td>" + (e.failed ? '<span class="runs-bad">FAILED</span>' : "ok") + "</td>" +
      "<td>" + escapeHtml(e.status || "") + "</td>" +
      "<td>" + escapeHtml(e.error || "") + "</td>" +
      "<td><button class=\"secondary\" data-exec=\"" + escapeHtml(e.executionId) + "\">Trace</button></td></tr>"
    ).join("");
    document.getElementById("runsOut").innerHTML =
      '<div class="runs-meta">' + escapeHtml(workflowId) + " - " + data.executions.length + " run(s) listed" +
      (data.total != null ? " of " + data.total : "") + "</div>" + runsWarnings(data.warnings) +
      (rows ? '<table class="runs-table"><thead><tr><th>Started</th><th>Result</th><th>Status</th>' +
              "<th>Error</th><th></th></tr></thead><tbody>" + rows + "</tbody></table>"
            : "<div class=\"runs-meta\">No executions.</div>");
    document.querySelectorAll("#runsOut button[data-exec]").forEach(b =>
      b.addEventListener("click", () => inspectRun(workflowId, b.dataset.exec)));
    setRunsStatus("", "");
  } catch (e) {
    setRunsStatus(String(e.message || e), "error");
  }
}

async function inspectRun(workflowId, executionId) {
  setRunsStatus("Reading " + executionId + "...");
  try {
    const data = await runsCall("/api/runs/inspect", {workflowId, executionId});
    const t = data.trace;
    const steps = t.steps.map(s => {
      const msgs = Object.entries(s.messages).map(([col, counts]) =>
        Object.entries(counts).map(([m, n]) =>
          n + " x " + escapeHtml(col) + ": " + (m ? escapeHtml(m) : "<i>(blank)</i>")).join("<br>")).join("<br>");
      return "<tr><td class=\"mono\">" + escapeHtml(s.stepId) + "</td>" +
        "<td>" + (s.success === false ? '<span class="runs-bad">FAILED</span>' : "ok") + "</td>" +
        "<td class=\"num\">" + s.rowsIn + "</td>" +
        "<td class=\"num\">" + (s.rowsOut == null ? "-" : s.rowsOut) + "</td>" +
        "<td class=\"num\">" + (s.recordsProcessed == null ? "-" : s.recordsProcessed) + "</td>" +
        "<td class=\"num\">" + (s.durationMs == null ? "-" : s.durationMs + " ms") + "</td>" +
        "<td>" + msgs + "</td></tr>";
    }).join("");
    document.getElementById("runsOut").innerHTML =
      '<div class="runs-meta"><button class="secondary" id="runsBackBtn">&larr; Runs</button> ' +
      escapeHtml(t.executionId) + " - ended " + escapeHtml(t.endTime || "-") +
      (t.failed ? ' - <span class="runs-bad">FAILED</span>' : " - ok") +
      " - env " + escapeHtml(t.m3Environment || "-") + " - " + t.inputRows + " input row(s)" +
      (t.finalOutputRows != null ? ", " + t.finalOutputRows + " output row(s)" : "") + "</div>" +
      '<div class="runs-meta">Trigger header: <span class="mono">' +
      escapeHtml(JSON.stringify(t.triggerHeader)) + "</span></div>" +
      runsWarnings(t.notes) +
      '<table class="runs-table"><thead><tr><th>Step</th><th>Result</th><th>Rows in</th><th>Rows out</th>' +
      "<th>Processed</th><th>Time</th><th>M3 messages</th></tr></thead><tbody>" + steps + "</tbody></table>" +
      '<div class="runs-meta">Raw record saved to ' + escapeHtml(data.savedTo) + "</div>";
    document.getElementById("runsBackBtn").addEventListener("click", () => listRuns(workflowId));
    setRunsStatus("", "");
  } catch (e) {
    setRunsStatus(String(e.message || e), "error");
  }
}

document.getElementById("runsListBtn").addEventListener("click", () =>
  listRuns(document.getElementById("runsWorkflowId").value));

document.getElementById("runsSweepBtn").addEventListener("click", async () => {
  const btn = document.getElementById("runsSweepBtn");
  btn.disabled = true;
  setRunsStatus("Sweeping the tenant - one request per workflow, this can take minutes...");
  try {
    const d = await runsCall("/api/runs/sweep", {
      match: document.getElementById("runsMatch").value.trim(),
      includeIdle: document.getElementById("runsIncludeIdle").checked,
    });
    const s = d.summary;
    const rows = d.workflows.map(r => {
      const flag = r.error ? '<span class="runs-bad">read error</span>'
        : r.lastFailed ? '<span class="runs-bad">last run failed</span>'
        : r.failedListed ? "had failures" : (r.listed ? "ok" : "never ran");
      return "<tr><td>" + flag + "</td>" +
        "<td><a href=\"#\" data-wf=\"" + escapeHtml(r.workflowId) + "\">" + escapeHtml(r.name) + "</a></td>" +
        "<td class=\"num\">" + r.failedListed + " / " + r.listed + (r.partial ? "+" : "") + "</td>" +
        "<td class=\"mono\">" + escapeHtml(r.lastStart || "-") + "</td>" +
        "<td>" + escapeHtml(String(r.error || r.lastError || "").slice(0, 200)) + "</td></tr>";
    }).join("");
    document.getElementById("runsOut").innerHTML =
      '<div class="runs-meta">' + escapeHtml(d.tenantId || "?") + " - " + d.workflowsInTenant +
      " workflow(s), " + d.workflowsMatched + " matched, " + d.workflowsAsked + " asked, " +
      d.workflowsSkippedIdle + " skipped (0 runs). Last run failed: " + s.lastRunFailed +
      ", any listed failure: " + s.anyListedFailure + ", never ran: " + s.neverRan +
      ", read errors: " + s.readErrors + (d.complete ? "" : ' - <span class="runs-bad">INCOMPLETE</span>') +
      "</div>" + runsWarnings(d.warnings) +
      '<table class="runs-table"><thead><tr><th>Health</th><th>Workflow</th><th>Failed / listed</th>' +
      "<th>Last run</th><th>Last error</th></tr></thead><tbody>" + rows + "</tbody></table>" +
      '<div class="runs-meta">Saved to ' + escapeHtml(d.savedTo) + "</div>";
    document.querySelectorAll("#runsOut a[data-wf]").forEach(a => a.addEventListener("click", (ev) => {
      ev.preventDefault();
      listRuns(a.dataset.wf);
    }));
    setRunsStatus("Swept " + d.workflowsAsked + " workflow(s).", d.complete ? "ok" : "error");
  } catch (e) {
    setRunsStatus(String(e.message || e), "error");
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("scaffoldBtn").addEventListener("click", async () => {
  try {
    const resp = await fetch("/api/pipeline/scaffold", {method: "POST", body: "{}"});
    const data = await resp.json();
    document.getElementById("sourceContent").value = JSON.stringify(data.spec, null, 2);
    document.querySelectorAll("#sourceTabs button[data-type]").forEach((b) => {
      b.classList.toggle("active", b.dataset.type === "pipeline-spec");
    });
    currentSourceType = "pipeline-spec";
    setStatus("Starter pipeline spec loaded - edit it, then Generate.", "ok");
  } catch (e) {
    setStatus("Could not load scaffold: " + e, "error");
  }
});

document.getElementById("generateBtn").addEventListener("click", async () => {
  const content = document.getElementById("sourceContent").value;
  const effectiveType = currentSourceType === "auto" ? guessSourceType(content) : currentSourceType;

  // Ask for a name before anything is built - suggested from the source,
  // theirs to override. Every workflow-producing source gets exactly one
  // prompt here, whatever it fans out into server-side.
  const payload = {
    sourceType: currentSourceType,
    content,
    connectionId: document.getElementById("connectionId").value.trim(),
    tenantId: document.getElementById("tenantId").value.trim(),
    environmentId: document.getElementById("environmentId").value.trim(),
    // Sent so a Classic XML conversion can fetch real M3 field metadata,
    // the way the CLI already does. Without it every field falls back to
    // the tiny static table and the Excel headers read
    // "TODO: metadata not found for <program>.<transaction>.<field>".
    bearerToken: await currentBearer().catch(() => ""),
    // Only the pipeline paths use this - it is what a generated M3
    // bulk-execute endpoint is built from, and nothing can derive it.
    ionApiBaseUrl: document.getElementById("ionApiBaseUrl").value.trim(),
    tableHeaderRow: document.getElementById("tableHeaderRow").value.trim(),
  };

  if (effectiveType === "classic-xml" || effectiveType === "classic-pipeline") {
    const baseName = await openPrompt("Name for this import", guessXmlLabel(content),
      "Every block becomes \"<this name> - <block's own function>\". Enter to leave block " +
      "names exactly as the XML has them.");
    if (baseName === null) return;
    payload.baseName = baseName;
  } else if (effectiveType === "live-json") {
    let suggested = "";
    try {
      const parsed = JSON.parse(content);
      suggested = (parsed.definition || {}).workflowConfig?.name || parsed.workflowName || "";
    } catch (e) { /* invalid JSON - let generate() report that */ }
    const overrideName = await openPrompt("Name for this clone", suggested,
      "The source workflow's own name is appended after this. Enter to keep it unchanged.");
    if (overrideName === null) return;
    payload.overrideName = overrideName;
  } else if (effectiveType === "pipeline-spec") {
    let suggested = "";
    try { suggested = JSON.parse(content).name || ""; } catch (e) { /* invalid JSON */ }
    const nameOverride = await openPrompt("Name for this workflow", suggested,
      "Used as typed, no suffix - Enter to keep the spec's own name.");
    if (nameOverride === null) return;
    payload.nameOverride = nameOverride;
  }

  setStatus("Generating...");
  document.getElementById("needsReview").style.display = "none";
  document.getElementById("createBtn").disabled = true;
  lastResult = null;
  lastUnits = [];
  currentUnit = 0;
  renderBlockPicker();

  try {
    const resp = await fetch("/api/generate", {method: "POST", body: JSON.stringify(payload)});
    const data = await resp.json();

    if (data.detectedType && currentSourceType === "auto") {
      document.getElementById("detectedBadge").textContent = "Detected: " + data.detectedType;
    }

    if (!data.ok) {
      setStatus(data.error || "Generation failed.", "error");
      document.getElementById("outputJson").value = "";
      return;
    }

    // A Classic file with several <Export>/<Import> blocks becomes several
    // workflows. Create in Live posts one at a time, so all of them are
    // built server-side and this picks which one is on screen - rather
    // than the panel quietly showing block 1 and dropping the rest, which
    // is what it did before.
    lastUnits = data.units || [{name: (data.result || {}).workflowName, result: data.result,
                                warnings: data.warnings || []}];
    showUnit(0);
    renderBlockPicker();
    setStatus(lastUnits.length > 1
      ? "Generated " + lastUnits.length + " workflows ✓" : "Generated ✓", "ok");
    document.getElementById("createBtn").disabled = false;
  } catch (e) {
    setStatus("Request failed: " + e, "error");
  }
});

document.getElementById("createBtn").addEventListener("click", async () => {
  if (!lastResult) return;
  let bearerToken;
  try { bearerToken = await currentBearer(); } catch (e) { setStatus(e.message, "error"); return; }
  if (!bearerToken) {
    setStatus("Paste a bearer token, or save an API Client for this Target Tenant (Target > Change).", "error");
    return;
  }

  // Read the target off the payload that is actually about to be POSTed,
  // not off the Target panel: those can differ if the fields were edited
  // after Generate, and the box must show what is really being sent.
  const tags = lastResult.tags || {};
  const target = {
    tenantId: lastResult.tenantId || "",
    connectionId: (tags.connectionId || [])[0] || "",
    environmentId: tags.apiEnvironment || "",
  };
  const info = await tokenInfo(bearerToken);
  const steps = Object.keys(((lastResult.definition || {}).stepConfig) || {});
  const txns = Object.keys((((lastResult.definition || {}).stepConfig) || {}).m3_api_1 || {}).length;
  const placeholders = (JSON.stringify(lastResult).match(/TODO: metadata not found for /g) || []).length;

  const body = [
    targetBlockHtml(target, info),
    confirmGroupHtml("Workflow to create", [
      "<strong>" + escapeHtml(lastResult.workflowName || "(unnamed)") + '</strong> <span class="muted">' +
      (txns ? txns + " M3 transaction(s) &middot; " : "") + steps.length + " step(s) &middot; " +
      escapeHtml(lastResult.workflowType || "STANDARD") + "</span>",
    ]),
    confirmGroupHtml("Warnings from Generate", lastWarnings.map(w => escapeHtml(w))),
    placeholders
      ? '<div class="confirm-note">' + placeholders + " field(s) still carry a " +
        "&quot;TODO: metadata not found&quot; column header and type UNKNOWN. Creating it is fine, but " +
        "those headers land in the workflow's Excel step.</div>"
      : "",
    '<div class="confirm-empty">Live assigns its own workflow id; a repeat create makes a second ' +
      "workflow rather than replacing this one.</div>",
  ].join("");

  const proceed = await openConfirm({
    title: "Create workflow in Live",
    actionLabel: "Create in Live",
    bodyHtml: body,
  });
  if (!proceed) { setStatus("Cancelled - nothing was sent.", ""); return; }

  setStatus("Creating in Live...");
  try {
    const resp = await fetch("/api/create", {
      method: "POST",
      body: JSON.stringify({result: lastResult, bearerToken}),
    });
    const data = await resp.json();
    if (data.ok) {
      const wfId = data.body && data.body.workflowId;
      setStatus("Created" + (wfId ? " - " + wfId : ""), "ok");
    } else {
      setStatus("Create failed (HTTP " + (data.status || "?") + "): " +
        (typeof data.body === "string" ? data.body : JSON.stringify(data.body || data.error)), "error");
    }
  } catch (e) {
    setStatus("Request failed: " + e, "error");
  } finally {
    loadHistory();
  }
});

function fmtTime(iso) {
  try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
}

const HISTORY_PAGE_SIZE = 25;
let historyRows = [];
let historyPage = 0;
// Workflow ids ticked for deletion - kept across pages and filter changes.
const historySelected = new Set();

// The filtered view the table and "Select all shown" work on.
function historyView() {
  const needle = document.getElementById("historyFilter").value.trim().toLowerCase();
  const onlyDeletable = document.getElementById("historyOnlyDeletable").checked;
  return historyRows.filter(r =>
    (!onlyDeletable || r.deletable) &&
    (!needle || [r.workflowName, r.app, r.workflowId].some(v => String(v || "").toLowerCase().includes(needle))));
}

function historyPageCount() {
  return Math.max(1, Math.ceil(historyView().length / HISTORY_PAGE_SIZE));
}

function updateHistorySelectAll() {
  const box = document.getElementById("historySelectAll");
  const deletable = historyView().filter(r => r.deletable);
  const picked = deletable.filter(r => historySelected.has(r.workflowId)).length;
  box.disabled = deletable.length === 0;
  box.checked = deletable.length > 0 && picked === deletable.length;
  box.indeterminate = picked > 0 && picked < deletable.length;
}

function updateHistoryDeleteBtn() {
  const btn = document.getElementById("historyDeleteBtn");
  btn.disabled = historySelected.size === 0;
  btn.textContent = "Delete selected from Live" + (historySelected.size ? " (" + historySelected.size + ")" : "");
  updateHistorySelectAll();
}

function renderHistoryPage() {
  const body = document.getElementById("historyBody");
  const info = document.getElementById("historyPageInfo");
  const pages = historyPageCount();
  historyPage = Math.min(Math.max(historyPage, 0), pages - 1);

  document.getElementById("historyPrevBtn").disabled = historyPage === 0;
  document.getElementById("historyNextBtn").disabled = historyPage >= pages - 1;

  const view = historyView();
  updateHistoryDeleteBtn();
  if (!view.length) {
    body.innerHTML = '<tr><td colspan="6" style="color:var(--vince-text-muted);">' +
      (historyRows.length ? "Nothing matches the filter." : "Nothing has been created in Live from here yet.") +
      "</td></tr>";
    info.textContent = "";
    return;
  }

  const from = historyPage * HISTORY_PAGE_SIZE;
  const rows = view.slice(from, from + HISTORY_PAGE_SIZE);
  info.textContent = "Page " + (historyPage + 1) + " of " + pages +
    "  \u00b7  " + (from + 1) + "-" + (from + rows.length) + " of " + view.length;

  body.innerHTML = rows.map(r => {
      const badge = r.deletedAt
        ? '<span style="color:var(--vince-text-muted);font-weight:600;" title="Deleted ' +
            escapeHtml(fmtTime(r.deletedAt)) + '">Deleted</span>'
        : r.ok
        ? '<span style="color:var(--vince-teal-dark);font-weight:600;">Success</span>'
        : '<span style="color:var(--vince-error);font-weight:600;">Failed</span>';
      // Only workflows this tool created and that still exist can be ticked;
      // groups and tables have no confirmed delete endpoint.
      const tick = r.deletable
        ? '<input type="checkbox" class="history-pick" data-id="' + escapeHtml(r.workflowId) + '"' +
            (historySelected.has(r.workflowId) ? " checked" : "") + ">"
        : "";
      const detail = r.ok ? (r.workflowId || "") : (r.error || "");
      const target = [r.connectionId, r.tenantId, r.environmentId].filter(Boolean).join(" / ");
      return '<tr style="border-top:1px solid var(--vince-border);">' +
        '<td>' + tick + '</td>' +
        '<td>' + fmtTime(r.timestamp) + '</td>' +
        '<td>' + badge + '</td>' +
        '<td>' + (r.workflowName || "") + '</td>' +
        '<td class="hist-detail" title="' + String(detail).replace(/"/g, "&quot;") + '">' + detail + '</td>' +
        '<td class="hist-target">' + target + '</td>' +
        '</tr>';
  }).join("");
}

async function loadHistory() {
  try {
    const resp = await fetch("/api/history");
    const data = await resp.json();
    historyRows = data.history || [];
    renderHistoryPage();
  } catch (e) {
    // history is a convenience view - a failed fetch here shouldn't block anything else
  }
}

function setHistoryOpen(open) {
  document.getElementById("historyModal").hidden = !open;
}

document.getElementById("openHistoryBtn").addEventListener("click", async () => {
  historyPage = 0;
  await loadHistory();       // always show the current log, not a stale one
  setHistoryOpen(true);
});
document.getElementById("closeHistoryBtn").addEventListener("click", () => setHistoryOpen(false));

document.getElementById("historyBody").addEventListener("change", (e) => {
  if (!e.target.classList.contains("history-pick")) return;
  if (e.target.checked) historySelected.add(e.target.dataset.id);
  else historySelected.delete(e.target.dataset.id);
  updateHistoryDeleteBtn();
});
["historyFilter", "historyOnlyDeletable"].forEach(id =>
  document.getElementById(id).addEventListener("input", () => { historyPage = 0; renderHistoryPage(); }));
document.getElementById("historySelectAll").addEventListener("change", (e) => {
  const deletable = historyView().filter(r => r.deletable);
  deletable.forEach(r => e.target.checked ? historySelected.add(r.workflowId) : historySelected.delete(r.workflowId));
  renderHistoryPage();
});
document.getElementById("historySelectShownBtn").addEventListener("click", () => {
  historyView().filter(r => r.deletable).forEach(r => historySelected.add(r.workflowId));
  renderHistoryPage();
});
document.getElementById("historyClearSelBtn").addEventListener("click", () => {
  historySelected.clear();
  renderHistoryPage();
});

document.getElementById("historyDeleteBtn").addEventListener("click", async () => {
  const status = document.getElementById("historyStatus");
  const picked = historyRows.filter(r => r.deletable && historySelected.has(r.workflowId));
  if (!picked.length) return;
  let bearerToken;
  try { bearerToken = await currentBearer(); }
  catch (e) { status.innerHTML = '<span class="err">' + escapeHtml(e.message) + '</span>'; return; }
  if (!bearerToken) { status.innerHTML = '<span class="err">Paste a bearer token, or save an API Client for this Target Tenant (Target > Change).</span>'; return; }

  const info = await tokenInfo(bearerToken);
  const tenants = Array.from(new Set(picked.map(r => r.tenantId)));
  const mismatch = info.tenant && tenants.some(t => t !== info.tenant);
  const body =
    (info.expiryWarning ? '<div class="confirm-flag">' + escapeHtml(info.expiryWarning) + "</div>" : "") +
    (mismatch ? '<div class="confirm-flag"><strong>Tenant mismatch.</strong> The token belongs to ' +
      escapeHtml(info.tenant) + "; workflows created in another tenant will be refused.</div>" : "") +
    '<div class="confirm-note">This permanently deletes these workflows in Live. Each one is checked ' +
      "first: it must be in this history, in the token's tenant, and still carry the name it was " +
      "created with. Deletion counts only when Live then answers 404 for it. Groups and tables are " +
      "not touched.</div>" +
    '<div class="confirm-target"><dl>' + confirmRow("Token's tenant", info.tenant || "unknown") + "</dl></div>" +
    confirmGroupHtml("Workflows to delete", picked.map(r =>
      "<strong>" + escapeHtml(r.workflowName || "") + '</strong> <span class="muted">' +
      escapeHtml(r.workflowId) + " &middot; created " + escapeHtml(fmtTime(r.timestamp)) + "</span>"));
  const proceed = await openConfirm({
    title: "Delete " + picked.length + " workflow(s) from Live",
    actionLabel: "Delete " + picked.length + " workflow(s)",
    destructive: true,
    bodyHtml: body,
  });
  if (!proceed) { status.textContent = "Cancelled - nothing was deleted."; return; }

  status.textContent = "Deleting " + picked.length + " workflow(s)...";
  try {
    const resp = await fetch("/api/workflows/delete", {method: "POST",
      body: JSON.stringify({workflowIds: picked.map(r => r.workflowId), bearerToken})});
    const data = await resp.json();
    if (data.error && !(data.results || []).length) {
      status.innerHTML = '<span class="err">' + escapeHtml(data.error) + "</span>";
      return;
    }
    const results = data.results || [];
    const good = results.filter(r => r.ok);
    results.filter(r => r.ok).forEach(r => historySelected.delete(r.workflowId));
    status.innerHTML = good.length + " of " + results.length + " deleted." +
      results.filter(r => !r.ok).map(r => '<div class="err">' + escapeHtml((r.name || r.workflowId) +
        " - " + r.status + ": " + (r.error || ""))).join("</div>") +
      (results.some(r => !r.ok) ? "</div>" : "");
    await loadHistory();
  } catch (e) {
    status.innerHTML = '<span class="err">Request failed: ' + escapeHtml(String(e)) + "</span>";
  }
});
document.getElementById("refreshHistoryBtn").addEventListener("click", loadHistory);
document.getElementById("historyPrevBtn").addEventListener("click", () => {
  historyPage -= 1; renderHistoryPage();
});
document.getElementById("historyNextBtn").addEventListener("click", () => {
  historyPage += 1; renderHistoryPage();
});
// Click the backdrop (but not the dialog itself) or press Escape to dismiss.
document.getElementById("historyModal").addEventListener("click", (e) => {
  if (e.target.id === "historyModal") setHistoryOpen(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") setHistoryOpen(false);
});

// The table header row belongs to the workbook, not the tenant preset, so it is
// only remembered in this browser as a convenience.
(function () {
  const box = document.getElementById("tableHeaderRow");
  try { box.value = localStorage.getItem("tableHeaderRow") || ""; } catch (e) {}
  box.addEventListener("input", () => { try { localStorage.setItem("tableHeaderRow", box.value.trim()); } catch (e) {} });
})();

loadHistory();

// --- App import ----------------------------------------------------------
// Scan and Dry run are local-only; only "Import app" with Dry run unchecked
// sends anything to Live, which is why the confirm below is on that path
// alone rather than on every button here.

function setAppStatus(msg, cls) {
  const el = document.getElementById("appStatus");
  el.textContent = msg || "";
  el.className = "status " + (cls || "");
}

// The fetches now sit in their own panel, well away from the import status
// line - they get their own so a fetch result isn't reported somewhere the
// eye isn't.
function setFetchStatus(msg, cls) {
  const el = document.getElementById("fetchStatus");
  el.textContent = msg || "";
  el.className = "status " + (cls || "");
}

function updateSaveFolderHint() {
  const folder = document.getElementById("appFolder").value.trim();
  const hint = document.getElementById("saveFolderHint");
  hint.textContent = folder || "no app folder set";
  hint.style.color = (!folder && document.getElementById("saveToFolder").checked)
    ? "var(--vince-error)" : "";
}

document.getElementById("appFolder").addEventListener("input", updateSaveFolderHint);
document.getElementById("saveToFolder").addEventListener("change", updateSaveFolderHint);
updateSaveFolderHint();

// The suggested base name for a Classic XML row - its own filename, minus
// the extension. Mirrors app_classic.xml_stem() server-side.
function xmlStem(path) {
  const base = (path || "").split("/").pop() || path || "";
  return base.toLowerCase().endsWith(".xml") ? base.slice(0, -4) : base;
}

// Reads every currently-rendered Classic XML name field back out of the
// DOM, keyed by source path - the single source of truth for what gets
// sent as baseNames. Called both to preserve edits across a re-scan (the
// import button always re-scans before confirming) and to build the final
// payload right before the request fires.
function currentBaseNames() {
  const out = {};
  document.querySelectorAll(".xml-basename-input").forEach(el => {
    out[el.dataset.path] = el.value;
  });
  return out;
}

// Same idea for the design-file dropdowns: path -> chosen target, "" for
// "do not upload". "?" (not chosen yet) is left out, so the server applies
// its own rules to that file and still flags it as needing a choice.
function currentDesignMatches() {
  const out = {};
  document.querySelectorAll(".design-match-select").forEach(el => {
    if (el.value !== "?") out[el.dataset.path] = el.value;
  });
  return out;
}

function unresolvedDesignFiles() {
  return Array.from(document.querySelectorAll(".design-match-select"))
    .filter(el => el.value === "?").map(el => el.dataset.path);
}

function designMatchSelect(d, targets, designMatches) {
  const chosen = Object.prototype.hasOwnProperty.call(designMatches, d.path)
    ? designMatches[d.path]
    : (d.target || (d.needsChoice ? "?" : ""));
  const option = (value, label) =>
    '<option value="' + escapeHtml(value) + '"' + (value === chosen ? " selected" : "") + ">" +
    escapeHtml(label) + "</option>";
  return '<select class="design-match-select" data-path="' + escapeHtml(d.path) + '">' +
    (chosen === "?" ? option("?", "-- choose --") : "") +
    option("", "-- do not upload --") +
    targets.map(t => option(t.key, t.key + (t.kind === "classicXml" ? " (Classic XML)" : ""))).join("") +
    "</select>";
}

function itemClass(ok) {
  if (ok === true) return "ok";
  if (ok === false) return "bad";
  return "planned";  // null = planned but not attempted (dry run)
}

function renderGroup(title, items) {
  if (!items.length) return "";
  return '<div class="app-group"><h3>' + escapeHtml(title) + "</h3>" + items.join("") + "</div>";
}

// Plain list for a couple of warnings; a native <details> disclosure once
// there are enough to be noise on their own resource card (nothing is lost
// either way - collapsed just means one click instead of always-visible).
function warnLines(warnings) {
  warnings = warnings || [];
  if (!warnings.length) return "";
  const items = warnings.map(w => '<div class="warn">! ' + escapeHtml(w) + "</div>").join("");
  if (warnings.length <= 2) return items;
  return '<details class="warn-details"><summary>' + warnings.length +
    " warning(s) - click to expand</summary>" + items + "</details>";
}

function renderPlan(plan, baseNames, designMatches) {
  baseNames = baseNames || {};
  designMatches = designMatches || {};
  const parts = [];

  parts.push(renderGroup("Custom Tables", plan.tables.map(t =>
    '<div class="app-item planned">' +
      '<div class="name">' + escapeHtml(t.tableName) + "</div>" +
      '<div class="meta">' + escapeHtml(t.path) + " &middot; keys: " +
        escapeHtml((t.primaryKeys || []).join(", ") || "none") +
        " &middot; " + escapeHtml(t.updateType || "no updateType") +
        " &middot; " + (t.columns || []).length + " column(s)" +
        (t.rowCount ? " &middot; " + t.rowCount + " row(s) found" : "") + "</div>" +
      warnLines(t.warnings) +
    "</div>")));

  parts.push(renderGroup("Workflows", plan.workflows.map(w =>
    '<div class="app-item planned">' +
      '<div class="name">' + escapeHtml(w.name) + "</div>" +
      '<div class="meta">' + escapeHtml(w.path) +
        (w.sourceWorkflowId ? " &middot; from " + escapeHtml(w.sourceWorkflowId) : "") +
        (w.designFile ? " &middot; design file: " + escapeHtml(w.designFile) : "") + "</div>" +
      warnLines(w.warnings) +
    "</div>")));

  // Every design file, each with its match. Paired by the Classic XML it
  // shares a folder with, else by name; an ambiguous one says "choose" and
  // blocks a real import until someone does.
  parts.push(renderGroup("Design files", (plan.designFiles || []).map(d => {
    const open = d.needsChoice && !Object.prototype.hasOwnProperty.call(designMatches, d.path);
    return '<div class="app-item ' + (open ? "bad" : "planned") + '">' +
      '<div class="name">' + escapeHtml(d.filename) + "</div>" +
      '<div class="meta">' + escapeHtml(d.path) + " &middot; " + d.size + " bytes" +
        (d.matchedBy ? " &middot; matched by " + escapeHtml(d.matchedBy) : "") + "</div>" +
      '<div class="meta">Upload to: ' + designMatchSelect(d, plan.designTargets || [], designMatches) +
        "</div>" +
      warnLines(d.warnings) +
    "</div>";
  })));

  parts.push(renderGroup("Classic XML (converted on import)", (plan.classicXml || []).map(x =>
    '<div class="app-item planned">' +
      '<div class="name">' + escapeHtml(x.label) + "</div>" +
      '<div class="meta">' + escapeHtml(x.path) + " &rarr; " + escapeHtml(x.generatedPath) +
        ((x.blockCount || 1) > 1 ? " (+ " + ((x.blockCount || 1) - 1) + " more)" : "") +
        " &middot; " + x.transactionCount + " M3 call(s)" +
        (x.totalTransactionCount > x.transactionCount
          ? " of " + x.totalTransactionCount + " in the file" : "") +
        ((x.blockCount || 1) > 1 ? " &middot; " + x.blockCount + " workflows" : "") +
        (x.designFile ? " &middot; design file: " + escapeHtml(x.designFile) : "") + "</div>" +
      '<div class="meta">Name: <input type="text" class="xml-basename-input" data-path="' +
        escapeHtml(x.path) + '" value="' + escapeHtml(baseNames[x.path] || xmlStem(x.path)) +
        '" style="width:260px"> <span class="muted">' +
        ((x.blockCount || 1) > 1
          ? "each block appends its own function after this"
          : "appended with this block's own function") + "</span></div>" +
      warnLines(x.warnings) +
    "</div>")));

  parts.push(renderGroup("Pipeline specs (built on import)", (plan.pipelineSpecs || []).map(p =>
    '<div class="app-item planned">' +
      '<div class="name">' + escapeHtml(p.name) + "</div>" +
      '<div class="meta">' + escapeHtml(p.path) + " &rarr; " + escapeHtml(p.generatedPath) +
        (p.groupWith ? " &middot; grouped with " + escapeHtml(p.groupWith) : "") +
        " &middot; uses the ION API root above</div>" +
      warnLines(p.warnings) +
    "</div>")));

  if (plan.assets.length) {
    parts.push('<div class="app-group"><h3>App Builder source (' + plan.assets.length + " file(s))</h3>" +
      '<div class="app-files">' + plan.assets.map(a =>
        escapeHtml(a.path) + " &middot; " + a.size + " bytes" +
        (a.inlined ? "" : " &middot; not inlined: " + escapeHtml(a.skippedReason || ""))
      ).join("<br>") + "</div></div>");
  }

  if (plan.links.length) {
    parts.push('<div class="app-group"><h3>References between these files</h3><div class="app-files">' +
      plan.links.map(l => escapeHtml(l.detail)).join("<br>") + "</div></div>");
  }

  if (plan.warnings.length) {
    parts.push(renderGroup("Folder warnings", plan.warnings.map(w =>
      '<div class="app-item bad"><div class="warn">! ' + escapeHtml(w) + "</div></div>")));
  }

  if (plan.promptTemplate) {
    parts.push('<div class="app-files">Using the prompt template in this folder: <code>' +
      escapeHtml(plan.promptTemplate) + "</code></div>");
  }
  return parts.join("");
}

function renderResults(data) {
  const s = data.summary;
  const parts = [];
  parts.push('<div class="app-summary">' + (data.dryRun ? "DRY RUN - nothing was sent to Live. " : "") +
    (s.xmlConverted || s.xmlFailed
      ? s.xmlConverted + " XML converted" + (s.xmlFailed ? ", " + s.xmlFailed + " failed" : "") + " &middot; "
      : "") +
    (s.designFilesUploaded || s.designFilesFailed
      ? s.designFilesUploaded + " design file(s) uploaded" +
        (s.designFilesFailed ? ", " + s.designFilesFailed + " failed" : "") + " &middot; "
      : "") +
    s.tablesCreated + " table(s) created, " + s.tablesSkipped + " already existed, " +
    s.tablesFailed + " failed &middot; " + s.workflowsCreated + " workflow(s) created, " +
    (s.workflowsSkipped || 0) + " already existed, " +
    s.workflowsFailed + " failed" +
    (s.groupsCreated || s.groupsFailed
      ? " &middot; " + s.groupsCreated + " group(s) created" +
        (s.groupsFailed ? ", " + s.groupsFailed + " failed" : "")
      : "") +
    (s.rowsNotImported ? " &middot; " + s.rowsNotImported + " row(s) not imported" : "") + "</div>");

  parts.push(renderGroup("Converted (Classic XML and pipeline specs)", (data.classicXml || []).map(x =>
    '<div class="app-item ' + itemClass(x.ok) + '">' +
      '<div class="name">' + escapeHtml(x.label || x.path) +
        (x.buildPath === "pipeline" ? ' <span class="tag">pipeline path</span>' : "") +
        (x.skipped ? ' <span class="tag">skipped</span>' : "") + "</div>" +
      '<div class="meta">' + escapeHtml(x.path) + " &middot; " + escapeHtml(x.status) +
        (x.generatedPath ? " &rarr; " + escapeHtml(x.generatedPath) : "") + "</div>" +
      warnLines(x.warnings) +
      (x.error ? '<div class="err">' + escapeHtml(x.error) + "</div>" : "") +
    "</div>")));

  parts.push(renderGroup("Custom Tables", data.tables.map(t =>
    '<div class="app-item ' + itemClass(t.ok) + '">' +
      '<div class="name">' + escapeHtml(t.tableName) + "</div>" +
      '<div class="meta">' + escapeHtml(t.status) + "</div>" +
      warnLines(t.warnings) +
      (t.error ? '<div class="err">' + escapeHtml(t.error) + "</div>" : "") +
    "</div>")));

  parts.push(renderGroup("Workflows", data.workflows.map(w =>
    '<div class="app-item ' + itemClass(w.ok) + '">' +
      '<div class="name">' + escapeHtml(w.name) + "</div>" +
      '<div class="meta">' + escapeHtml(w.status) +
        (w.workflowId ? " &middot; " + escapeHtml(w.workflowId) : "") + "</div>" +
      warnLines(w.warnings) +
      (w.error ? '<div class="err">' + escapeHtml(w.error) + "</div>" : "") +
      (w.designFile
        ? '<div class="meta">design file ' + escapeHtml(w.designFile.filename) + ": " +
            escapeHtml(w.designFile.status) +
            (w.designFile.key ? " &middot; " + escapeHtml(w.designFile.key) : "") + "</div>" +
          (w.designFile.error
            ? '<div class="' + (w.designFile.ok === false ? "err" : "warn") + '">' +
              escapeHtml(w.designFile.error) + "</div>"
            : "")
        : "") +
    "</div>")));

  parts.push(renderGroup("Groups (workflows that open together in VXL Live)", (data.groups || []).map(g =>
    '<div class="app-item ' + itemClass(g.ok) + '">' +
      '<div class="name">' + escapeHtml(g.groupName) + "</div>" +
      '<div class="meta">' + g.memberCount + " workflow(s): " + escapeHtml((g.members || []).join(", ")) +
        " &middot; " + escapeHtml(g.status) +
        (g.groupId ? " &middot; " + escapeHtml(g.groupId) : "") + "</div>" +
      (g.error ? '<div class="err">' + escapeHtml(g.error) + "</div>" : "") +
    "</div>")));

  // Grouped by resource, DEDUPED within each group, and collapsed by
  // default - a folder-scale run (e.g. a real CustomerA customer folder, 21
  // workflows) both repeats a multi-block file's own file-level notes once
  // per block AND, even deduped, still has enough distinct messages to be
  // a wall of text if shown flat. Nothing is lost: `warnings` (the raw,
  // undeduped list) is still in results.json in full, and the deduped,
  // human-readable version is in warningsPath (warnings.txt) too.
  const rawWarningCount = (data.warnings || []).length;
  if (rawWarningCount) {
    const groups = data.warningGroups && data.warningGroups.length
      ? data.warningGroups : [{resource: "General", messages: data.warnings}];
    const dedupedCount = groups.reduce((n, g) => n + g.messages.length, 0);
    const groupHtml = groups.map(g =>
      '<details class="warn-group"><summary>' + escapeHtml(g.resource) + " (" + g.messages.length +
      ")</summary>" + g.messages.map(m => '<div class="warn">! ' + escapeHtml(m) + "</div>").join("") +
      "</details>"
    ).join("");
    parts.push('<div class="app-group"><h3>Warnings (' + dedupedCount +
      (dedupedCount !== rawWarningCount ? ", " + rawWarningCount + " raw" : "") +
      ')</h3>' + groupHtml + "</div>");
  }
  parts.push('<div class="app-files">Artifacts: <code>' + escapeHtml(data.artifactsDir) + "</code>" +
    (data.warningsPath ? "<br>Full warnings: <code>" + escapeHtml(data.warningsPath) + "</code>" : "") +
    "</div>");
  return parts.join("");
}

// One toggle governs where BOTH fetches land. Returns the folder to write
// into, "" for source-box-only, or null when the toggle is on but no folder
// is set - which is a mistake worth stopping on rather than silently
// downgrading to source-only.
function saveFolderOrNull() {
  if (!document.getElementById("saveToFolder").checked) return "";
  const folder = document.getElementById("appFolder").value.trim();
  if (!folder) return null;
  return folder;
}

function reportFetchWrites(data) {
  const files = data.files || [];
  const warnings = data.warnings || [];
  if (!files.length && !warnings.length) return;
  document.getElementById("appPlan").innerHTML =
    '<div class="app-files">' + files.map(f => "Wrote " + escapeHtml(f)).join("<br>") + "</div>" +
    renderGroup("Warnings", warnings.map(w =>
      '<div class="app-item bad"><div class="warn">! ' + escapeHtml(w) + "</div></div>"));
}

document.getElementById("fetchTableBtn").addEventListener("click", async () => {
  const folder = saveFolderOrNull();
  if (folder === null) {
    setFetchStatus("Save-into-folder is on but no app folder is set - enter a path or untick it.", "error");
    return;
  }
  const payload = {
    tableName: document.getElementById("fetchTableName").value.trim(),
    bearerToken: "",
    withRows: document.getElementById("fetchTableRows").checked,
    folder: folder,
  };
  if (!payload.tableName) { setFetchStatus("Enter a table name to fetch.", "error"); return; }
  try { payload.bearerToken = await currentBearer(); } catch (e) { setFetchStatus(e.message, "error"); return; }
  if (!payload.bearerToken) { setFetchStatus("Paste a bearer token, or save an API Client for this Target Tenant (Target > Change).", "error"); return; }
  setFetchStatus("Fetching table...");
  try {
    const resp = await fetch("/api/table/fetch", {method: "POST", body: JSON.stringify(payload)});
    const data = await resp.json();

    if (!data.ok) {
      setFetchStatus(data.error || "Table fetch failed.", "error");
      // The attempt log is the useful part of a failure here - it's the
      // evidence for which endpoint (if any) actually reads a table meta.
      const attempts = (data.probeAttempts || []).map(a =>
        '<div class="app-item bad"><div class="name">' + escapeHtml(a.endpoint) + "</div>" +
        '<div class="meta">HTTP ' + escapeHtml(String(a.status)) + "</div>" +
        '<div class="err">' + escapeHtml(a.verdict) + "</div></div>");
      document.getElementById("appPlan").innerHTML =
        '<div class="app-summary">' + escapeHtml(data.error || "") + "</div>" +
        renderGroup("Endpoints tried", attempts);
      return;
    }

    document.getElementById("sourceContent").value = JSON.stringify(data.meta, null, 2);
    document.getElementById("detectedBadge").textContent = "Fetched a Custom Table definition";

    const bits = ["Read from " + data.metaEndpoint];
    if (data.rowCount !== undefined && data.rowCount !== null) bits.push(data.rowCount + " row(s) in Live");
    if (data.rows) bits.push(data.rows.length + " row(s) captured" + (data.rowsTruncated ? " (truncated)" : ""));
    (data.files || []).forEach(f => bits.push("Wrote " + f));
    document.getElementById("appPlan").innerHTML =
      '<div class="app-summary">Fetched ' + escapeHtml(data.tableName) + "</div>" +
      '<div class="app-files">' + bits.map(escapeHtml).join("<br>") + "</div>" +
      renderGroup("Warnings", (data.warnings || []).map(w =>
        '<div class="app-item bad"><div class="warn">! ' + escapeHtml(w) + "</div></div>"));

    setFetchStatus("Fetched " + data.tableName, "ok");
    // Re-scan so the plan reflects the table that was just written in.
    if (payload.folder) document.getElementById("appScanBtn").click();
  } catch (e) {
    setFetchStatus("Request failed: " + e, "error");
  }
});

document.getElementById("appScanBtn").addEventListener("click", async () => {
  const folder = document.getElementById("appFolder").value.trim();
  if (!folder) { setAppStatus("Enter the path to an app folder first.", "error"); return; }
  setAppStatus("Scanning...");
  document.getElementById("appPromptWrap").style.display = "none";
  const existingBaseNames = currentBaseNames();
  const existingDesignMatches = currentDesignMatches();
  try {
    const resp = await fetch("/api/app/scan", {method: "POST",
      body: JSON.stringify({folder, designMatches: existingDesignMatches})});
    const data = await resp.json();
    if (!data.ok) {
      setAppStatus(data.error || "Scan failed.", "error");
      document.getElementById("appPlan").innerHTML = "";
      return;
    }
    document.getElementById("appPlan").innerHTML =
      renderPlan(data.plan, existingBaseNames, existingDesignMatches);
    setAppStatus("Scanned " + data.plan.name + " - nothing sent to Live", "ok");
  } catch (e) {
    setAppStatus("Request failed: " + e, "error");
  }
});

function appConfirmBodyHtml(plan, target, info, dryRun, baseNames) {
  baseNames = baseNames || {};
  const parts = [];

  if (dryRun) {
    parts.push('<div class="confirm-note"><strong>Dry run.</strong> Nothing is sent to Live. Any Classic ' +
      "XML below is still converted and written into the app folder \u2014 that is what makes a dry run " +
      "worth doing \u2014 so the target ids are baked into those files and still matter.</div>");
  }
  parts.push(targetBlockHtml(target, info, {tokenOptional: dryRun}));

  parts.push(confirmGroupHtml("Classic XML to convert", (plan.classicXml || []).map(x =>
    "<strong>" + escapeHtml(baseNames[x.path] || xmlStem(x.path)) +
    ((x.blockCount || 1) > 1 ? " (" + x.blockCount + " workflows, grouped)" : "") +
    '</strong> <span class="muted">from ' + escapeHtml(x.path) +
    " &middot; " + x.transactionCount + " M3 call(s) &rarr; " +
    ((x.blockCount || 1) > 1 ? x.blockCount + " workflows, first at " : "") +
    escapeHtml(x.generatedPath) +
    (x.designFile ? " &middot; + design file " + escapeHtml(x.designFile) : "") + "</span>" +
    (x.warnings || []).map(w => '<br><span class="muted">! ' + escapeHtml(w) + "</span>").join("")
  )));

  parts.push(confirmGroupHtml("Pipeline specs to build", (plan.pipelineSpecs || []).map(p =>
    "<strong>" + escapeHtml(p.name) + '</strong> <span class="muted">from ' + escapeHtml(p.path) +
    (p.groupWith ? " &middot; grouped with " + escapeHtml(p.groupWith) : "") + "</span>" +
    (p.warnings || []).map(w => '<br><span class="muted">! ' + escapeHtml(w) + "</span>").join("")
  )));

  parts.push(confirmGroupHtml("Custom Tables to create", plan.tables.map(t =>
    "<strong>" + escapeHtml(t.tableName) + '</strong> <span class="muted">keys: ' +
    escapeHtml((t.primaryKeys || []).join(", ") || "none") + " &middot; " +
    escapeHtml(t.updateType || "no updateType") + " &middot; " + (t.columns || []).length +
    " column(s)" + (t.rowCount ? " &middot; " + t.rowCount + " row(s) will NOT be imported" : "") +
    "</span>"
  )));

  parts.push(confirmGroupHtml("Workflows to create", plan.workflows.map(w =>
    "<strong>" + escapeHtml(w.name) + '</strong> <span class="muted">from ' + escapeHtml(w.path) +
    (w.designFile ? " &middot; + design file " + escapeHtml(w.designFile) : "") + "</span>"
  )));

  const orphanDesign = (plan.designFiles || []).filter(d => !d.target);
  if (orphanDesign.length) {
    parts.push('<div class="confirm-note">' + orphanDesign.length + " Excel file(s) are matched to no " +
      "workflow and will NOT be uploaded: " +
      orphanDesign.map(d => escapeHtml(d.path)).join(", ") + "</div>");
  }

  // A Classic XML contributes one workflow PER BLOCK, not one per file.
  const fromXml = (plan.classicXml || []).reduce((n, x) => n + (x.blockCount || 1), 0);
  const created = plan.tables.length + plan.workflows.length + fromXml + (plan.pipelineSpecs || []).length;
  if (!created) {
    parts.push('<div class="confirm-empty">Nothing in this folder would be created in Live.</div>');
  }

  if ((plan.warnings || []).length) {
    parts.push(confirmGroupHtml("Folder warnings", plan.warnings.map(w => escapeHtml(w))));
  }
  if (plan.assets.length) {
    parts.push('<div class="confirm-empty">' + plan.assets.length + " App Builder source file(s) go into " +
      "the generated prompt, not into Live.</div>");
  }
  return parts.join("");
}

document.getElementById("appImportBtn").addEventListener("click", async () => {
  const payload = {
    folder: document.getElementById("appFolder").value.trim(),
    connectionId: document.getElementById("connectionId").value.trim(),
    tenantId: document.getElementById("tenantId").value.trim(),
    environmentId: document.getElementById("environmentId").value.trim(),
    bearerToken: "",
    dryRun: document.getElementById("appDryRun").checked,
    promptToFolder: document.getElementById("promptToFolder").checked,
    // Same field the pipeline tabs use, from the current Target Tenant
    // preset. Only used by a Classic XML block with a RowFilter native
    // can't express (a GE/GT/LT/LE on a non-numeric value) - everything else in an
    // app import ignores it, so leaving it blank is fine unless the folder
    // holds a file like that.
    ionApiBaseUrl: document.getElementById("ionApiBaseUrl").value.trim(),
    tableHeaderRow: document.getElementById("tableHeaderRow").value.trim(),
  };
  if (!payload.folder) { setAppStatus("Enter the path to an app folder first.", "error"); return; }
  try {
    payload.bearerToken = await currentBearer();
  } catch (e) {
    // A dry run can go ahead without one (it only loses M3 metadata).
    if (!payload.dryRun) { setAppStatus(e.message, "error"); return; }
    payload.bearerToken = "";
  }
  if (!payload.dryRun && !payload.bearerToken) {
    setAppStatus("Paste a bearer token, or save an API Client for this Target Tenant (Target > Change).", "error");
    return;
  }

  // Always re-scan before confirming. The box has to describe the folder as
  // it is right now, not as it was when someone last pressed Scan - files
  // change, and a confirmation that shows a stale list is worse than none.
  // Names the user already typed into the Classic XML rows must survive
  // that re-render, though - re-scanning isn't a reason to forget them.
  const existingBaseNames = currentBaseNames();
  const existingDesignMatches = currentDesignMatches();
  setAppStatus("Checking the folder...");
  let plan;
  try {
    const resp = await fetch("/api/app/scan", {method: "POST",
      body: JSON.stringify({folder: payload.folder, designMatches: existingDesignMatches})});
    const data = await resp.json();
    if (!data.ok) { setAppStatus(data.error || "Scan failed.", "error"); return; }
    plan = data.plan;
  } catch (e) {
    setAppStatus("Request failed: " + e, "error");
    return;
  }
  document.getElementById("appPlan").innerHTML = renderPlan(plan, existingBaseNames, existingDesignMatches);
  payload.baseNames = currentBaseNames();
  payload.designMatches = currentDesignMatches();

  // An ambiguous design file is a question for a person, not something to
  // guess or quietly skip on a real import. A dry run may go ahead.
  const unresolved = unresolvedDesignFiles();
  if (unresolved.length && !payload.dryRun) {
    setAppStatus("Choose where " + unresolved.length + " design file(s) go in the plan below " +
      "(or pick \"do not upload\"): " + unresolved.join(", "), "error");
    return;
  }

  const target = currentTarget();
  const info = await tokenInfo(payload.bearerToken);
  const proceed = await openConfirm({
    title: payload.dryRun ? "Dry run: " + plan.name : "Import " + plan.name + " into Live",
    actionLabel: payload.dryRun ? "Run dry run" : "Create in Live",
    bodyHtml: appConfirmBodyHtml(plan, target, info, payload.dryRun, payload.baseNames),
  });
  if (!proceed) { setAppStatus("Cancelled - nothing was sent.", ""); return; }

  setAppStatus(payload.dryRun ? "Building (dry run)..." : "Importing...");
  payload.importId = "imp-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
  const importBtn = document.getElementById("appImportBtn");
  importBtn.disabled = true;
  const stopProgress = startImportProgress(payload.importId);
  try {
    const resp = await fetch("/api/app/import", {method: "POST", body: JSON.stringify(payload)});
    const data = await resp.json();
    if (data.error && !data.summary) {
      setAppStatus(data.error, "error");
      document.getElementById("appPlan").innerHTML = data.plan ? renderPlan(data.plan) : "";
      return;
    }
    document.getElementById("appPlan").innerHTML = renderResults(data);
    // A dry run generates no prompt at all now (nothing real to point App
    // Builder at yet) - hide the box rather than show an empty one.
    if (data.promptPath) {
      document.getElementById("appPrompt").value = data.prompt || "";
      // Name both homes: the run artifact always, and the copy next to the
      // app when one was written.
      document.getElementById("appPromptPath").textContent =
        (data.folderPromptPath ? "Saved to " + data.folderPromptPath + "  and  " : "Saved to ") +
        data.promptPath;
      document.getElementById("appPromptWrap").style.display = "block";
    } else {
      document.getElementById("appPromptWrap").style.display = "none";
    }
    setAppStatus(data.ok ? (data.dryRun ? "Dry run complete" : "Imported \u2713") : "Finished with failures",
                 data.ok ? "ok" : "error");
  } catch (e) {
    setAppStatus("Request failed: " + e, "error");
  } finally {
    stopProgress();
    importBtn.disabled = false;
    loadHistory();
  }
});

// Polls the server's progress for one import while its request is still
// open. The import itself is one long request; this is only so a person
// can see it moving ("Creating workflows 2/6").
function renderImportProgress(p) {
  const box = document.getElementById("appProgress");
  const text = box.querySelector(".text");
  const bar = box.querySelector(".bar span");
  if (!p) return;
  const counted = p.total > 0;
  box.classList.toggle("indeterminate", !counted);
  bar.style.width = counted ? Math.round(100 * p.done / p.total) + "%" : "";
  const shown = counted ? Math.min(p.done + 1, p.total) : 0;
  const count = counted ? (p.done >= p.total ? " \u2013 " + p.total + "/" + p.total + " done"
                                              : " \u2013 " + shown + " of " + p.total) : "";
  text.innerHTML = escapeHtml(p.phase || "") + escapeHtml(count) +
    (p.item && p.done < p.total ? ' <span class="item">' + escapeHtml(p.item) + "</span>" : "");
}

function startImportProgress(importId) {
  const box = document.getElementById("appProgress");
  box.hidden = false;
  renderImportProgress({phase: "Starting", done: 0, total: 0});
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try {
      const resp = await fetch("/api/app/import/progress", {method: "POST", body: JSON.stringify({importId})});
      const data = await resp.json();
      if (!stopped && data.progress) renderImportProgress(data.progress);
    } catch (e) { /* the import's own result is what matters */ }
    if (!stopped) timer = setTimeout(tick, 700);
  };
  let timer = setTimeout(tick, 300);
  return () => { stopped = true; clearTimeout(timer); box.hidden = true; };
}

document.getElementById("copyPromptBtn").addEventListener("click", () => {
  const el = document.getElementById("appPrompt");
  el.select();
  document.execCommand("copy");
  setAppStatus("App Builder prompt copied to clipboard", "ok");
});

document.getElementById("copyBtn").addEventListener("click", () => {
  const el = document.getElementById("outputJson");
  el.select();
  document.execCommand("copy");
  setStatus("Copied to clipboard", "ok");
});
</script>
</body>
</html>
"""

class _Server(ThreadingHTTPServer):
    # ThreadingHTTPServer defaults allow_reuse_address = 1. On Windows that
    # maps to SO_REUSEADDR, which lets a SECOND process bind a port another
    # process is already serving: the new server prints its banner and looks
    # healthy, while the browser keeps being answered by the old one. That
    # cost a real debugging session - a stale server silently served
    # pre-change code through several restarts and a "why is my new source
    # tab missing" hunt. Refusing to start is the far kinder failure.
    allow_reuse_address = False


def main():
    try:
        server = _Server(("127.0.0.1", PORT), Handler)
    except OSError as error:
        print(f"Could not bind 127.0.0.1:{PORT} - something is already serving it.")
        print(f"  ({type(error).__name__}: {error})")
        print("")
        print("That is almost always an older webui.py still running. Find and stop it:")
        print(f"  PowerShell:  Get-NetTCPConnection -LocalPort {PORT} -State Listen |"
              " Select-Object OwningProcess")
        print("               taskkill /F /PID <pid>")
        print("")
        print("Then start this again. Nothing is lost - run history and the name counter")
        print("live in RUN/*.json, not in the server's memory.")
        raise SystemExit(1)

    print(f"VinceForge UI running at http://127.0.0.1:{PORT}")
    print("(Code is read once, at startup - restart after editing any converter/*.py.)")
    server.serve_forever()


if __name__ == "__main__":
    main()
