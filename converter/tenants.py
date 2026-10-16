"""
Named Target Tenant presets (Connection-ID / Tenant-ID / Environment-ID,
plus the ION API root) for the web UI, so switching targets is picking a
name instead of retyping or copy-pasting four raw values. Bearer token
deliberately isn't part of a preset - it's short-lived and pasted fresh per
session regardless of target.

The ION API root belongs here for the same reason the ids do: it is
per-tenant twice over (the region segment AND the tenant segment), so
nothing can derive it, and the generic pipeline path cannot build an
endpoint without it. Optional - only that path needs it.

Persisted next to the other run artifacts (RUN/tenants.json), same pattern
as converter_state.json / webui_history.json - plain JSON, no database.

The seed default (see _seed_defaults) intentionally does NOT hardcode a
real tenant/connection/environment - those are real infrastructure ids and
don't belong in tracked source. It reads them from the environment instead
(CONNECTION_ID / TENANT_ID / ENVIRONMENT_ID), loaded from a gitignored
.env at the project root if present - see .env.example for the shape. A
fresh checkout with no .env still runs fine, it just starts with zero
presets instead of one.
"""

import json
import os

from envfile import load_dotenv

_TENANTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "tenants.json"
)
# Which preset the Target panel had selected, kept in its own tiny file
# rather than as a key inside tenants.json - that dict's keys are read
# directly as the list of preset names (Object.keys in the web UI), so a
# sentinel key living alongside them would show up as a fake tenant in the
# dropdown. Without this, loadTenants() had nothing to prefer over
# names[0] on every page load, so the panel silently snapped back to
# whichever preset happened to be first in the file - not whatever was
# last selected - even though the preset itself was saved correctly.
_LAST_SELECTED_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "tenants_last_selected.json"
)
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")

load_dotenv(_ENV_FILE)


def _seed_defaults() -> dict:
    connection_id = os.environ.get("CONNECTION_ID", "").strip()
    tenant_id = os.environ.get("TENANT_ID", "").strip()
    environment_id = os.environ.get("ENVIRONMENT_ID", "").strip()
    if not (connection_id and tenant_id and environment_id):
        return {}
    return {
        "Default": {
            "connectionId": connection_id,
            "tenantId": tenant_id,
            "environmentId": environment_id,
            # Optional: only the generic pipeline path needs it.
            "ionApiBaseUrl": os.environ.get("ION_API_BASE_URL", "").strip(),
        }
    }


def load_tenants(tenants_file: str = _TENANTS_FILE) -> dict:
    if not os.path.exists(tenants_file):
        tenants = _seed_defaults()
        save_tenants(tenants, tenants_file)
        return tenants
    try:
        with open(tenants_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _seed_defaults()


def save_tenants(tenants: dict, tenants_file: str = _TENANTS_FILE) -> None:
    with open(tenants_file, "w", encoding="utf-8") as f:
        json.dump(tenants, f, indent=2)


def upsert_tenant(name: str, connection_id: str, tenant_id: str, environment_id: str,
                   ion_api_base_url: str = "", tenants_file: str = _TENANTS_FILE) -> dict:
    """Add a new Target Tenant, or overwrite an existing one with the same name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Tenant name can't be empty.")
    tenants = load_tenants(tenants_file)
    tenants[name] = {
        "connectionId": (connection_id or "").strip(),
        "tenantId": (tenant_id or "").strip(),
        "environmentId": (environment_id or "").strip(),
        "ionApiBaseUrl": (ion_api_base_url or "").strip(),
    }
    save_tenants(tenants, tenants_file)
    return tenants


def remove_tenant(name: str, tenants_file: str = _TENANTS_FILE) -> dict:
    tenants = load_tenants(tenants_file)
    tenants.pop((name or "").strip(), None)
    save_tenants(tenants, tenants_file)
    return tenants


def get_last_selected(state_file: str = _LAST_SELECTED_FILE) -> str:
    """The Target Tenant preset name that was selected last, or "" if none was ever recorded."""
    if not os.path.exists(state_file):
        return ""
    try:
        with open(state_file, encoding="utf-8") as f:
            return (json.load(f).get("name") or "").strip()
    except (json.JSONDecodeError, OSError):
        return ""


def set_last_selected(name: str, state_file: str = _LAST_SELECTED_FILE) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"name": (name or "").strip()}, f, indent=2)
