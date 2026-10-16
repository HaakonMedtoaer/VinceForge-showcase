"""
A Vince Live API Client as a fallback for the pasted bearer token.

The pasted token (token_store.py) is a Cognito ID token copied out of
Live's own web UI, and it dies after about an hour. An API Client
(Live UI -> Administration -> Access Management -> API Clients) has a
Client ID and Secret that don't expire that way, and trades them for a
token over OAuth2 `client_credentials`. This module stores one API Client
per Target Tenant preset and mints (and caches) a token from it when
the bearer-token field is empty.

What is CONFIRMED and what is NOT - read before trusting a result:

- CONFIRMED (docs/vince-live-api-reference-prompt.md, auth): API Clients
  exist, they use `client_credentials`, and a real "self" connection
  showed a Token URI shaped
  `https://tenant-<hex>.auth.<aws-region>.amazoncognito.com/oauth2/token`.
  It is per-tenant and cannot be derived, so it is typed in, never built.
- The token request itself is standard OAuth2 (RFC 6749 section 4.4) as
  Cognito implements it: form-encoded `grant_type=client_credentials`,
  the id/secret as HTTP Basic auth. Standard, not a Vince guess.
- CONFIRMED 2026-10-01: `api.vince.live` accepts the resulting ACCESS
  token (`test_connection` answered 200 against a real tenant). The client
  needs a Role with workflow permissions: without one the same call gave
  `HTTP 400: Empty or invalid ROLE permissions`. Not yet checked: that the
  role is enough for every write an app import makes.
- NOT CONFIRMED: which tenant a minted token acts on. The web UI's token
  names its tenant in a claim (metadata.tenant_from_token); an access
  token may not. So `mint_token` only hands out a token for the preset
  that owns the API Client, and the caller must check the tenant it is
  about to send matches that preset's (webui does).

Stored in RUN/api_clients.json (gitignored), same plain-JSON pattern as
token_store.py. The secret is a long-lived credential in plain text on
this machine - acceptable for this single-user install for the same
reason token_store.py is, and never sent back to the browser: the UI
only learns whether one is set.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

_CLIENTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "api_clients.json"
)
TIMEOUT = 30
# Minted tokens are reused until this many seconds before they expire, so a
# long app import doesn't start with a token that dies halfway through it.
EXPIRY_MARGIN = 120

# preset name -> {"token": str, "expiresAt": float}. In memory only: a
# restart just mints a fresh one.
_cache = {}


def _load(clients_file: str = _CLIENTS_FILE) -> dict:
    if not os.path.exists(clients_file):
        return {}
    try:
        with open(clients_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict, clients_file: str = _CLIENTS_FILE) -> None:
    with open(clients_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def token_uri_problem(token_uri: str):
    """Why this Token URI won't be used, or None. Only https is required;
    the confirmed Cognito shape is checked separately, as a warning, since
    one captured example is not proof every tenant looks the same."""
    parsed = urllib.parse.urlparse((token_uri or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return "The Token URI must be a full https:// address."
    return None


def token_uri_warning(token_uri: str):
    parsed = urllib.parse.urlparse((token_uri or "").strip())
    if parsed.netloc.endswith(".amazoncognito.com") and parsed.path.rstrip("/") == "/oauth2/token":
        return None
    return ("This Token URI doesn't have the shape seen on a real API Client "
            "(https://tenant-<hex>.auth.<region>.amazoncognito.com/oauth2/token). "
            "Double-check it against the API Client's connection screen in Live.")


def get_client_summary(preset: str, clients_file: str = _CLIENTS_FILE) -> dict:
    """What the UI may know: never the secret itself."""
    client = _load(clients_file).get(preset or "") or {}
    return {
        "configured": bool(client.get("clientId") and client.get("clientSecret") and client.get("tokenUri")),
        "clientId": client.get("clientId", ""),
        "tokenUri": client.get("tokenUri", ""),
        "hasSecret": bool(client.get("clientSecret")),
        "savedAt": client.get("savedAt"),
    }


def save_client(preset: str, client_id: str, client_secret: str, token_uri: str,
                clients_file: str = _CLIENTS_FILE) -> dict:
    """Store the API Client for a preset. A blank secret keeps the stored
    one, so editing the id or URI doesn't mean re-typing the secret."""
    preset = (preset or "").strip()
    if not preset:
        raise ValueError("Save the Target Tenant preset first - an API Client belongs to one.")
    client_id = (client_id or "").strip()
    token_uri = (token_uri or "").strip()
    if not client_id:
        raise ValueError("Client ID is empty.")
    problem = token_uri_problem(token_uri)
    if problem:
        raise ValueError(problem)
    data = _load(clients_file)
    existing = data.get(preset) or {}
    secret = (client_secret or "").strip() or existing.get("clientSecret", "")
    if not secret:
        raise ValueError("Client Secret is empty.")
    data[preset] = {"clientId": client_id, "clientSecret": secret, "tokenUri": token_uri,
                    "savedAt": time.time()}
    _save(data, clients_file)
    _cache.pop(preset, None)
    return get_client_summary(preset, clients_file)


def remove_client(preset: str, clients_file: str = _CLIENTS_FILE) -> None:
    data = _load(clients_file)
    if data.pop(preset or "", None) is not None:
        _save(data, clients_file)
    _cache.pop(preset or "", None)


def _request_token(client: dict) -> dict:
    """One client_credentials exchange. Returns {"ok", "token", "expiresIn",
    "error"}; the server's own error body is kept, as elsewhere here."""
    basic = base64.b64encode(f"{client['clientId']}:{client['clientSecret']}".encode("utf-8")).decode("ascii")
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
    req = urllib.request.Request(client["tokenUri"], data=data, method="POST", headers={
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": e.code, "error": f"Token URI answered HTTP {e.code}: {raw[:300]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "status": None, "error": f"Couldn't reach the Token URI: {e.reason}"}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "status": 200, "error": "Token URI answered, but not with JSON."}
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        return {"ok": False, "status": 200, "error": "Token URI answered without an access_token."}
    return {"ok": True, "token": token, "expiresIn": int(body.get("expires_in") or 3600), "error": None}


def mint_token(preset: str, clients_file: str = _CLIENTS_FILE) -> dict:
    """A token from the preset's API Client, reused while it is still good.
    Returns {"ok", "token", "expiresAt", "cached", "error"}."""
    client = _load(clients_file).get(preset or "") or {}
    if not (client.get("clientId") and client.get("clientSecret") and client.get("tokenUri")):
        return {"ok": False, "error": "No API Client is saved for the Target Tenant '" + (preset or "") +
                "'. Paste a bearer token, or add an API Client under Target > Change."}
    cached = _cache.get(preset)
    if cached and cached["expiresAt"] - EXPIRY_MARGIN > time.time():
        return {"ok": True, "token": cached["token"], "expiresAt": cached["expiresAt"], "cached": True, "error": None}
    result = _request_token(client)
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}
    expires_at = time.time() + result["expiresIn"]
    _cache[preset] = {"token": result["token"], "expiresAt": expires_at}
    return {"ok": True, "token": result["token"], "expiresAt": expires_at, "cached": False, "error": None}


def preset_for_token(token: str):
    """The preset whose API Client minted exactly this token (still cached),
    else None. Lets the server vouch for a token that names no tenant, without
    trusting the browser to say which tenant it is for."""
    if not token:
        return None
    for preset, cached in _cache.items():
        if cached.get("token") == token:
            return preset
    return None


def test_connection(preset: str, clients_file: str = _CLIENTS_FILE) -> dict:
    """Mint a token, then make ONE read-only call to api.vince.live with it
    (the first page of the workflow list). This is how the open question in
    the module docstring gets answered - report it as it comes back."""
    from liveapi import list_workflows

    _cache.pop(preset or "", None)
    minted = mint_token(preset, clients_file)
    if not minted["ok"]:
        return {"ok": False, "stage": "token", "error": minted["error"]}
    listed = list_workflows(minted["token"], max_pages=1)
    if not listed["ok"]:
        hint = ""
        if listed.get("status") in (401, 403):
            hint = (" The API Client's token was issued, but api.vince.live refused it. That would mean "
                    "this API accepts only the web UI's token, or the client lacks a role - worth "
                    "noting in docs/vince-live-api-reference-prompt.md either way.")
        return {"ok": False, "stage": "api", "status": listed.get("status"),
                "error": (listed.get("error") or "Request failed.") + hint}
    return {"ok": True, "stage": "api", "status": listed.get("status"),
            "workflowsOnFirstPage": len(listed.get("body") or []),
            "expiresAt": minted["expiresAt"]}
