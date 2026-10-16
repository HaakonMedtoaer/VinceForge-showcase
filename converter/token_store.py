"""
Optional local persistence for the bearer token.

CLAUDE.md's documented default is "not stored anywhere in this project" -
deliberate, because a bearer token is a credential and the tool is meant to
be safe to hand to anyone without leaving one lying around. This module
exists because the person running THIS install asked for an exception: a
single-user local dev tool, not something shared, where re-pasting a token
every restart is pure friction. So it persists to a gitignored
RUN/webui_token.json (same plain-JSON pattern as tenants.py /
converter_state.json), alongside the timestamp it was pasted at - so a
stale token is visible in the UI instead of silently retried until a 401.
"""

import json
import os
import time

_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "webui_token.json"
)


def load_token(token_file: str = _TOKEN_FILE) -> dict:
    if not os.path.exists(token_file):
        return {"token": "", "pastedAt": None}
    try:
        with open(token_file, encoding="utf-8") as f:
            data = json.load(f)
        return {"token": data.get("token", ""), "pastedAt": data.get("pastedAt")}
    except (json.JSONDecodeError, OSError):
        return {"token": "", "pastedAt": None}


def save_token(token: str, token_file: str = _TOKEN_FILE) -> dict:
    token = (token or "").strip()
    # Clearing the field clears the timestamp too - an empty token has no
    # "pasted at" to report.
    data = {"token": token, "pastedAt": time.time() if token else None}
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data
