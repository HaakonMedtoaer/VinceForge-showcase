"""
Run history - the log of resources this tool has actually created in Live
(workflow "Create in Live" attempts, and now app-import table/workflow
creations too). Lifted out of webui.py unchanged in behaviour so that
appimport.py can append to the same log without importing webui (which
imports appimport - a circular import).

Same pattern as converter_state.json / tenants.json: a plain JSON file in
RUN/, newest entry first, capped at _HISTORY_LIMIT.
"""

import json
import os

_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "webui_history.json")
_HISTORY_LIMIT = 200


def load_history(history_file: str = _HISTORY_FILE) -> list:
    if not os.path.exists(history_file):
        return []
    try:
        with open(history_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(history: list, history_file: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(history_file)), exist_ok=True)
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def append_history(entry: dict, history_file: str = _HISTORY_FILE) -> None:
    history = load_history(history_file)
    history.insert(0, entry)  # newest first
    _save(history[:_HISTORY_LIMIT], history_file)


def mark_deleted(workflow_ids, when: str, history_file: str = _HISTORY_FILE) -> int:
    """
    Stamp `deletedAt` on every row for these ids - the row stays (it is still
    a true record of what was created), it just stops being offered for
    deletion. Returns how many rows were stamped.
    """
    ids = set(workflow_ids)
    history = load_history(history_file)
    stamped = 0
    for entry in history:
        if entry.get("workflowId") in ids and not entry.get("deletedAt"):
            entry["deletedAt"] = when
            stamped += 1
    if stamped:
        _save(history, history_file)
    return stamped


def resource_name(entry: dict) -> str:
    """
    The workflow's own name as created. App-import rows store it in
    `resourceName`; older ones only have the display form
    "<app> / <name> (<kind>)", which is unwrapped here.
    """
    if entry.get("resourceName"):
        return entry["resourceName"]
    name = entry.get("workflowName") or ""
    if entry.get("source") == "app-import":
        prefix = (entry.get("app") or "") + " / "
        suffix = " (" + (entry.get("kind") or "") + ")"
        if name.startswith(prefix):
            name = name[len(prefix):]
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name
