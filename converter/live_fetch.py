"""
Fetch workflows and Custom Tables out of Live as app-folder-ready JSON -
the read side of app import, so an app folder can be built by pulling from
a source tenant instead of hand-writing it.

Produces exactly what source_app_folder expects to find:

    <app folder>/workflows/<name>.json       a GET /workflows/{id} response, verbatim
    <app folder>/tables/<name>.json          the definition (re-postable as-is)
    <app folder>/tables/<name>.rows.json     the rows, if asked for

Writing to a folder is always the caller's choice: pass `app_folder` and it
gets written, omit it and you just get the JSON back. The web UI wires that
to a single "also save into this folder" toggle shared by both fetches, so
the default stays look-don't-touch.

## The definition is probed for, not read from a known endpoint

No endpoint for reading a table's meta is confirmed - see
liveapi.get_table_meta, which probes and validates rather than trusting a
status code. A fetch therefore either comes back with a definition it has
actually verified names this table, or comes back honest about failing and
hands over the full attempt log. It never synthesises a definition from
whatever it could reach.

Row count comes from the confirmed `aggregated` endpoint independently of
all that, so even a failed meta probe still tells you the table is real and
how big it is.

## Rows

Rows are opt-in. The scroll endpoint is documented but explicitly marked
untested in docs/vince-custom-tables-guide.md, so its response shape is
treated as unknown here: the paging loop stops and says so rather than
guessing if it can't find rows where it expected them. Rows are captured
verbatim, `__id`/`__created`/`__modified` included - they're a faithful
capture, and app import never sends them anywhere anyway.
"""

import json
import os
import re

from liveapi import get_table_meta, get_workflow, search_table, table_row_count
from source_app_folder import clean_table_meta

# A page size that isn't a guess about limits, just a sane round trip. The
# guide's own example uses size=10; nothing documents a maximum, so if a
# large page is refused the caller sees the real error rather than a silent
# fallback.
DEFAULT_PAGE_SIZE = 1000

# Stops a runaway paging loop against a table far bigger than expected (the
# catalog table in this project's own history has 22,535 rows). Hitting it
# is reported as truncation, never silently.
DEFAULT_MAX_ROWS = 50_000
MAX_PAGES = 200


def _safe_filename(name: str) -> str:
    """Live slugifies table names to hyphens already; this only guards the filesystem."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "table")).strip("-") or "table"


def _extract_rows(body):
    """
    Rows out of a search response whose shape is not confirmed.

    Accepts the `items` key (the shape the confirmed data endpoint uses) or
    a bare list. Returns None - meaning "shape not recognised", which the
    caller reports rather than papers over - for anything else.
    """
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return body["items"]
    if isinstance(body, list):
        return body
    return None


def _extract_scroll_id(body):
    if isinstance(body, dict):
        for key in ("scrollId", "scroll_id"):
            if isinstance(body.get(key), str) and body[key]:
                return body[key]
    return None


def fetch_rows(table_name: str, bearer_token: str, max_rows: int = DEFAULT_MAX_ROWS,
               page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """Page the whole table via the scroll endpoint. Returns {"rows", "truncated", "warnings", "error"}."""
    rows, warnings, scroll_id = [], [], None

    for page in range(MAX_PAGES):
        response = search_table(table_name, bearer_token, size=page_size, scroll_id=scroll_id)
        if not response["ok"]:
            return {"rows": rows, "truncated": True, "warnings": warnings,
                    "error": "Row fetch stopped on page " + str(page + 1) + ": " + str(response.get("error"))}

        page_rows = _extract_rows(response.get("body"))
        if page_rows is None:
            return {"rows": rows, "truncated": True, "warnings": warnings,
                    "error": "The search endpoint returned a shape this tool doesn't recognise (no `items` list "
                             "and not a bare array). Its response shape is documented but untested - capture the "
                             "real body and write it down rather than guessing at it."}

        rows.extend(page_rows)
        scroll_id = _extract_scroll_id(response.get("body"))

        if not page_rows or not scroll_id:
            break
        if len(rows) >= max_rows:
            warnings.append("Stopped at " + str(len(rows)) + " rows (limit " + str(max_rows) +
                            "). The table has more - raise the limit if you need all of them.")
            return {"rows": rows[:max_rows], "truncated": True, "warnings": warnings, "error": None}
    else:
        warnings.append("Stopped after " + str(MAX_PAGES) + " pages - the table may have more rows.")
        return {"rows": rows, "truncated": True, "warnings": warnings, "error": None}

    return {"rows": rows, "truncated": False, "warnings": warnings, "error": None}


def fetch_table(table_name: str, bearer_token: str, with_rows: bool = False,
                app_folder: str = "", max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """
    Fetch one table's definition (and optionally its rows), and - when
    `app_folder` is given - write them into that folder's tables/ directory
    in the layout source_app_folder reads back.
    """
    table_name = (table_name or "").strip()
    if not table_name:
        return {"ok": False, "error": "Enter a table name first."}
    if not bearer_token:
        return {"ok": False, "error": "No bearer token provided."}

    warnings = []
    result = {"ok": False, "tableName": table_name, "warnings": warnings, "files": []}

    # Confirmed endpoint, and independent of the meta probe below: it says
    # whether the table is real and how big it is even if the probe fails.
    count_response = table_row_count(table_name, bearer_token)
    if count_response["ok"] and isinstance(count_response.get("body"), dict):
        result["rowCount"] = count_response["body"].get("count")
    elif count_response.get("status") == 404:
        return {**result, "error": "No table named '" + table_name + "' in this tenant (the row-count "
                                    "endpoint returned 404)."}
    else:
        warnings.append("Couldn't read the row count: " + str(count_response.get("error")))

    probe = get_table_meta(table_name, bearer_token)
    result["probeAttempts"] = probe["attempts"]
    if not probe["ok"]:
        return {**result, "error": probe["error"]}

    result["metaEndpoint"] = probe["endpoint"]
    meta, meta_warnings = clean_table_meta(probe["body"])
    warnings.extend(meta_warnings)
    result["meta"] = meta

    if with_rows:
        row_result = fetch_rows(table_name, bearer_token, max_rows=max_rows)
        warnings.extend(row_result["warnings"])
        result["rows"] = row_result["rows"]
        result["rowsTruncated"] = row_result["truncated"]
        if row_result["error"]:
            warnings.append(row_result["error"])

    if app_folder:
        stem = _safe_filename(table_name)
        targets = [(stem + ".json", meta)]
        if with_rows:
            targets.append((stem + ".rows.json", result.get("rows") or []))
        try:
            result["files"] = _write_into_app_folder(app_folder, "tables", targets, warnings)
        except OSError as e:
            return {**result, "error": "Couldn't write into " + app_folder + ": " + str(e)}

    result["ok"] = True
    return result


def _write_into_app_folder(app_folder: str, subdir: str, targets: list, warnings: list) -> list:
    """
    Write `targets` - a list of (filename, json-able content) - into
    <app folder>/<subdir>/, creating the folder if it doesn't exist yet
    (fetching is a normal way to START an app folder, not just add to one).
    """
    directory = os.path.join(os.path.abspath(app_folder), subdir)
    os.makedirs(directory, exist_ok=True)
    written = []

    for filename, content in targets:
        path = os.path.join(directory, filename)
        # A re-fetch overwriting its own earlier capture is the normal case,
        # but say so - silently replacing a file someone hand-edited would
        # be a bad way for them to find out.
        if os.path.exists(path):
            warnings.append("Replaced the existing " + subdir + "/" + filename + " in this folder.")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content, f, indent=2)
        written.append(path)
    return written


def fetch_workflow(workflow_id: str, bearer_token: str, app_folder: str = "") -> dict:
    """
    GET one workflow and, when `app_folder` is given, drop it into that
    folder's workflows/ directory.

    The GET response is written **verbatim**. That's deliberate: it is
    exactly what source_app_folder classifies as a workflow and what
    clone_and_retarget expects as input, and it is the raw capture - the
    stripping of GET-only fields belongs at import time, in
    clone_and_retarget, not here. An app folder should hold what Live
    actually returned.
    """
    workflow_id = (workflow_id or "").strip()
    if not workflow_id:
        return {"ok": False, "error": "Enter a workflow id (WORKFLOW-...) first."}
    if not bearer_token:
        return {"ok": False, "error": "No bearer token provided."}

    response = get_workflow(workflow_id, bearer_token)
    if not response["ok"]:
        return {"ok": False, "status": response.get("status"), "error": response.get("error")}

    body = response["body"]
    if not isinstance(body, dict):
        return {"ok": False, "error": "Live returned something that isn't a workflow object."}

    warnings = []
    name = body.get("workflowName") or body.get("definition", {}).get("workflowConfig", {}).get("name") \
        or workflow_id
    files = []
    if app_folder:
        try:
            files = _write_into_app_folder(app_folder, "workflows",
                                           [(_safe_filename(name) + ".json", body)], warnings)
        except OSError as e:
            return {"ok": False, "error": "Couldn't write into " + app_folder + ": " + str(e),
                    "result": body}

    return {"ok": True, "result": body, "workflowName": name, "files": files, "warnings": warnings}
