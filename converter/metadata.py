"""
M3 field metadata lookup - type/length/mandatory/descr per field. Already
source-agnostic in the original script (keyed by program/transaction/field
name, nothing Classic-specific about it), so this moves over unchanged.

THIS IS THE KNOWN GAP referenced in the project summary. Live's JSON wants
type/length/mandatory/descr per field; the static table below is a stand-in
for programs/transactions not covered by a live metadata fetch. Fill in
real entries as you discover them, or rely on fetch_live_metadata() when a
bearer token is available.
"""

import base64
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from ir import Workflow

# Key: (program, transaction, field_name) -> metadata dict
M3_FIELD_METADATA = {
    ("MMS301MI", "LstStockTakeDet", "WHLO"): dict(type="STRING", length="3", mandatory=True, descr="Warehouse"),
    ("MMS301MI", "LstStockTakeDet", "CONO"): dict(type="NUMERIC", length="3", mandatory=True, descr="Company"),
    # ... add more as you confirm them
}

# M3 field type codes -> Live's own type vocabulary. Confirmed against
# real captured data: A (alphanumeric), N (numeric), D (date) are the
# only codes observed so far. D is mapped to NUMERIC as a reasonable
# guess (M3 typically stores dates as numeric YYYYMMDD), NOT confirmed
# against a real Live-created field of type D.
_M3_TYPE_CODE_MAP = {"A": "STRING", "N": "NUMERIC", "D": "NUMERIC"}


_TENANT_PATTERN = re.compile(r"^TENANT-[0-9a-fA-F]{32}$")


def tenant_from_token(bearer_token: str) -> Optional[str]:
    """
    Read the tenant id out of a Cognito ID token's payload, or None if it
    can't be found.

    WHY THIS EXISTS: a token belonging to tenant A, used against tenant
    B's connection id, does NOT come back 401. The call succeeds and the
    connection is simply invisible, so the metadata endpoint answers
    `{"count": 0, "items": []}` - indistinguishable, from the outside,
    from a transaction M3 genuinely can't describe. That cost a long
    debugging session: every field silently fell back to
    "TODO: metadata not found ..." and the tooling blamed the transaction.

    Deliberately conservative: the JWT is only base64-decoded and read,
    never verified (we are not authenticating anything, just comparing
    ids), and if no claim looks like a tenant id this returns None and the
    caller stays quiet rather than inventing a warning.
    """
    parts = clean_token(bearer_token).split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)          # restore base64 padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None

    # Claim name is not confirmed and may vary, so match on the VALUE's
    # shape instead of guessing a key.
    for value in claims.values():
        if isinstance(value, str) and _TENANT_PATTERN.match(value):
            return value
    return None


def clean_token(bearer_token: str) -> str:
    """
    Normalise a pasted token: strip surrounding whitespace and quotes, drop a
    "Bearer " prefix if it came along with the paste, and remove any internal
    whitespace from a copy that wrapped across lines.

    All three produce an identical, unexplained 401 - the same call succeeds
    from Postman, because Postman's Authorization field holds a clean value.
    Being forgiving here costs nothing and removes a whole class of
    dead-end debugging.
    """
    token = (bearer_token or "").strip().strip('"').strip("'").strip()
    for prefix in ("Bearer ", "bearer ", "BEARER "):
        if token.startswith(prefix):
            token = token[len(prefix):].strip()
    return "".join(token.split())


def _token_claims(bearer_token: str) -> Optional[dict]:
    """Decode a JWT payload without verifying it. None if unreadable."""
    parts = clean_token(bearer_token).split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def token_expiry_warning(bearer_token: str) -> Optional[str]:
    """
    Say whether the token is already dead, reading its own `exp` claim.

    WHY: an expired token is indistinguishable from a wrong one at the
    call site - everything just returns 401 - and it expires mid-session
    constantly (~1 hour). Three separate diagnostic runs in one session
    were wasted before anyone checked. This costs one local base64 decode
    and no network call, so there is no reason not to look first.

    Returns None when the token is fine, or when there is no readable
    `exp` (in which case say nothing rather than guess).
    """
    import time

    claims = _token_claims(bearer_token)
    if not claims:
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None

    remaining = exp - time.time()
    if remaining < -30:                      # a little slack for clock skew
        minutes = int(-remaining // 60)
        return (f"This bearer token EXPIRED about {minutes} minute(s) ago. Every call will come "
                "back 401 and tell you nothing. Grab a fresh one from Live's DevTools Network tab.")
    if remaining < 120:
        return f"This bearer token expires in under {int(max(remaining, 0))}s - grab a fresh one first."
    return None


def tenant_mismatch_warning(bearer_token: str, tenant_id: str) -> Optional[str]:
    """The warning to show when a token and a target tenant disagree."""
    token_tenant = tenant_from_token(bearer_token)
    if not token_tenant or not tenant_id:
        return None
    if token_tenant.lower() == tenant_id.lower():
        return None
    return (
        f"The bearer token belongs to {token_tenant}, but the target is {tenant_id}. "
        "These must match. A cross-tenant token does NOT fail loudly here - metadata lookups "
        "return an empty result rather than an error, so every field silently falls back to "
        "\"TODO: metadata not found ...\", and anything created lands in the token's tenant, "
        "not the one shown in Target."
    )


def field_metadata(program: str, transaction: str, field_name: str, overrides: Optional[dict] = None,
                   direction: Optional[str] = None) -> dict:
    """
    `direction` is "I" or "O" - which side of the transaction the field is
    used on. It matters because M3 describes a field once PER direction and
    the two can disagree: BUS100MI's DIVI is mandatory "1" as an input and
    "0" as an output (RUN/meta-BUS100MI.json). Overrides carry a
    direction-qualified key for each; the plain 3-tuple key is the fallback.
    """
    key = (program, transaction, field_name)
    if overrides and direction and key + (direction,) in overrides:
        return overrides[key + (direction,)]
    if overrides and key in overrides:
        return overrides[key]
    if key in M3_FIELD_METADATA:
        return M3_FIELD_METADATA[key]
    # Explicit placeholder rather than a silent guess - easy to grep for.
    return dict(type="UNKNOWN", length="0", mandatory=False,
                descr=f"TODO: metadata not found for {program}.{transaction}.{field_name}")


def fetch_live_metadata(connection_id: str, program: str, transaction: str, bearer_token: str,
                        problems: Optional[list] = None) -> dict:
    """
    Query Live's own M3 metadata endpoint for a program/transaction's
    input and output fields, and return them in the same shape
    field_metadata() returns - i.e. ready to use as an "overrides" dict
    keyed by (program, transaction, field_name).

    This endpoint was discovered by capturing a full HAR of Live's own
    workflow builder in action - it's exactly what powers the "pick a
    transaction, see its fields populate" behaviour in the UI.

    Requires a valid bearer token (the same short-lived kind captured
    from the browser elsewhere in this workflow) - this makes a real
    network call, so it will fail once the token expires.
    """
    overrides = {}

    # ONE call, not one per direction. `FIELDS#<program>#<transaction>`
    # returns an item per direction that exists - confirmed by real runs:
    # count=2 for MRS001MI/LstAllTrans (input and output), count=1 for
    # BUS100MI/DelBudgetLines (input only, being a delete). Halving the
    # calls matters more than it sounds: a token lasts about an hour and a
    # big workflow used to burn two round trips per transaction.
    details = f"FIELDS#{program}#{transaction}"
    url = f"https://api.vince.live/metadata/{connection_id}?details={urllib.parse.quote(details, safe='')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {clean_token(bearer_token)}",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _note(problems, f"{program}.{transaction}: HTTP {e.code} {e.reason}")
        return overrides
    except urllib.error.URLError as e:
        _note(problems, f"{program}.{transaction}: {e.reason}")
        return overrides
    except json.JSONDecodeError as e:
        _note(problems, f"{program}.{transaction}: response was not JSON ({e})")
        return overrides

    # Each item is one direction, named "I" or "O" (its `descr` says "Input"
    # on both in RUN/meta-BUS100MI.json, so `name` is the one to read). This
    # used to key on the field name alone, so whichever direction came LAST
    # won - and since the output item follows the input one, every mandatory
    # INPUT field that is also an output (DIVI: "1" in, "0" out) silently
    # came out mandatory=False. Now each direction gets its own key, and the
    # plain key keeps the INPUT one when a field has both.
    for item in data.get("items", []):
        direction = item.get("name")
        for f in item.get("data", []):
            name = f.get("name")
            if not name:
                continue
            entry = {
                "type": _M3_TYPE_CODE_MAP.get(f.get("type"), "UNKNOWN"),
                "length": f.get("length", "0"),
                "mandatory": f.get("mandatory") == "1",
                "descr": f.get("descr", name),
            }
            _store(overrides, (program, transaction, name), direction, entry)

    if not overrides:
        # Only complain when the transaction has NO fields at all. A
        # single empty direction is normal and must not be reported:
        # BUS100MI/DelBudgetLines legitimately has 14 inputs and zero
        # outputs, because deletes return nothing. The old per-direction
        # check called that a failure and sent us hunting for tenant
        # mismatches that were not there.
        _note(problems,
              f"{program}.{transaction}: endpoint answered, but described no fields at all "
              "- suspect a token from a different tenant than the connection first, then the "
              "transaction name")
    return overrides


def _store(overrides: dict, key: tuple, direction: Optional[str], entry: dict) -> None:
    """Record one direction's metadata; the plain key prefers the input side."""
    if direction in ("I", "O"):
        overrides[key + (direction,)] = entry
    if direction == "I" or key not in overrides:
        overrides[key] = entry


def _note(problems: Optional[list], message: str) -> None:
    """Report a lookup problem to the caller as well as the console."""
    print(f"  Warning: metadata lookup: {message}")
    if problems is not None:
        problems.append(message)


def fetch_metadata_for_workflow(wf: Workflow, connection_id: str, bearer_token: str,
                                problems: Optional[list] = None) -> dict:
    """
    Fetch live field metadata for every distinct (program, transaction)
    pair used anywhere in a parsed workflow. Returns a single merged
    overrides dict, ready to pass into build_live_workflow().

    Pass a list as `problems` to get the reasons any lookup came back
    empty - otherwise they only reach the console, which is invisible to
    anyone driving this from the web UI.
    """
    overrides = {}
    seen_pairs = set()
    for txn in wf.transactions:
        pair = (txn.program, txn.transaction)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        print(f"  Fetching metadata for {txn.program}.{txn.transaction}...")
        overrides.update(
            fetch_live_metadata(connection_id, txn.program, txn.transaction, bearer_token, problems)
        )
    return overrides


# --- Offline fallback: the M3 field catalog ---------------------------------
# A SQLite file built by the separate "M3 APIs" project
# (docs/vince-m3-field-catalog-handoff.md): every program/transaction/field
# M3 exposes, from MRS001MI.LstFields via ION API. It sits BETWEEN the live
# fetch and the two-entry static table - only a (program, transaction) the
# live fetch returned nothing for is filled from it, because the live answer
# describes the TARGET tenant's M3 and the catalog describes one reference
# tenant's (which one is not recorded in the file). Absent file = silently
# skipped: it is a local convenience, not a dependency.
CATALOG_ENV = "M3_FIELD_CATALOG"
_DEFAULT_CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "M3 APIs", "m3-field-catalog.sqlite")


def catalog_path() -> Optional[str]:
    """The catalog file to use: $M3_FIELD_CATALOG, else the sibling project's copy, else None."""
    path = os.environ.get(CATALOG_ENV) or _DEFAULT_CATALOG
    return os.path.normpath(path) if os.path.isfile(path) else None


def catalog_metadata(program: str, transaction: str, path: str) -> tuple:
    """
    One transaction's fields from the catalog, as (overrides, fetched_at).
    Same shape fetch_live_metadata() returns, direction keys included.
    `Mandatory` is "1"/"0" (and "" on some rows, read as not mandatory) -
    the "1" encoding the handoff doc left open is observed in the catalog
    itself, on 26,767 rows.
    """
    overrides = {}
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT Direction, FieldName, FieldLabel, Type, Length, Mandatory FROM fields "
            "WHERE Program = ? AND TransactionName = ?", (program, transaction)).fetchall()
        fetched = conn.execute(
            "SELECT FieldsFetchedAt FROM transactions WHERE Program = ? AND TransactionName = ?",
            (program, transaction)).fetchone()
    finally:
        conn.close()
    for direction, name, label, type_code, length, mandatory in rows:
        _store(overrides, (program, transaction, name), direction, {
            "type": _M3_TYPE_CODE_MAP.get(type_code, "UNKNOWN"),
            "length": str(length if length is not None else 0),
            "mandatory": mandatory == "1",
            "descr": label or name,
        })
    return overrides, (fetched[0] if fetched else None)


def fill_from_catalog(wf: Workflow, overrides: Optional[dict], notes: Optional[list] = None) -> Optional[dict]:
    """
    Fill every (program, transaction) in `wf` that `overrides` has nothing
    for from the offline catalog, and say so in `notes` - a field that came
    from another tenant's M3 must never look like it came from the target.
    Returns the merged overrides (None stays None when nothing was filled).
    """
    path = catalog_path()
    if not path:
        return overrides
    covered = {key[:2] for key in (overrides or {})}
    merged = dict(overrides or {})
    used, dates = [], set()
    for txn in wf.transactions:
        pair = (txn.program, txn.transaction)
        if pair in covered:
            continue
        covered.add(pair)
        try:
            found, fetched_at = catalog_metadata(txn.program, txn.transaction, path)
        except sqlite3.Error as e:
            _note(notes, f"offline field catalog unreadable ({e}) - {path}")
            return overrides
        if found:
            merged.update(found)
            used.append(".".join(pair))
            if fetched_at:
                dates.add(fetched_at[:10])
    if not used:
        return overrides
    if notes is not None:
        notes.append(
            f"Field metadata for {len(used)} transaction(s) ({', '.join(used)}) came from the OFFLINE "
            f"M3 field catalog ({os.path.basename(path)}, fetched {', '.join(sorted(dates)) or 'date unknown'}) "
            "- one reference tenant's M3, NOT the target connection. Field lengths and mandatory flags can "
            "differ between tenants and M3 versions; a bearer token for the target tenant gives the live answer."
        )
    return merged


def unmapped_mandatory_inputs(wf: Workflow, overrides: Optional[dict]) -> list:
    """
    `PROGRAM.Transaction.FIELD` for every INPUT field M3's metadata marks
    mandatory that the source never maps. A Classic migration has to flag
    exactly these API mandatory-field discrepancies. Only direction-qualified
    metadata counts - the static table has no direction, and guessing one
    would invent a warning.
    """
    missing = set()
    for txn in wf.transactions:
        mapped = {f.name for f in txn.inputs}
        for key, meta in (overrides or {}).items():
            if (len(key) == 4 and key[3] == "I" and key[:2] == (txn.program, txn.transaction)
                    and meta.get("mandatory") and key[2] not in mapped):
                missing.add(f"{txn.program}.{txn.transaction}.{key[2]}")
    return sorted(missing)


def unmapped_mandatory_warning(wf: Workflow, overrides: Optional[dict]) -> list:
    """The warning for unmapped_mandatory_inputs(), as a 0- or 1-item list."""
    missing = unmapped_mandatory_inputs(wf, overrides)
    if not missing:
        return []
    shown = ", ".join(missing[:8]) + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else "")
    return [
        f"{len(missing)} input field(s) that M3's metadata marks MANDATORY are not mapped by the source: "
        f"{shown}. Expect those calls to fail in M3 unless the field is supplied some other way. "
        "Check each against the Classic file before running."
    ]


# The placeholder build.py writes into a field's Excel column header when no
# metadata - static or live - was found for it. Lives here rather than in the
# web UI because two callers now need to detect it: the UI (warn before
# "Create in Live") and app_classic.py (warn on a converted Classic XML).
METADATA_TODO = "TODO: metadata not found for "


def missing_metadata_fields(built_workflow: dict) -> list:
    """
    The `PROGRAM.Transaction.FIELD` names in a built workflow that still have
    no real metadata. Scans the serialised JSON rather than walking the
    structure: the placeholder can land in an Excel column header, a field
    descr or a header cell, and a text scan can't miss a new one.
    """
    fields = set()
    for chunk in json.dumps(built_workflow).split(METADATA_TODO)[1:]:
        fields.add(chunk.split('"')[0])
    return sorted(fields)
