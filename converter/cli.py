"""
CLI entry point - dispatches to whichever source is being used. Replaces
the bottom of the original single-file script.

Usage:
  python cli.py classic-xml <classic.xml> <connection_id> <tenant_id> <environment_id> <output.json> [bearer_token]
  python cli.py classic-pipeline <classic.xml> <connection_id> <tenant_id> <environment_id> <output.json>
                              --base-url <ion_api_root> [--column-headers <map.json>] [--header-row <n>]
  python cli.py live-clone  <source.json>  <connection_id> <tenant_id> <environment_id> <output.json>
  python cli.py pipeline    <spec.json>    <connection_id> <tenant_id> <environment_id> <output.json>
  python cli.py pipeline-scaffold <output.json>
  python cli.py app-scan    <app_folder>
  python cli.py probe-metadata <connection_id> <program> <transaction> <bearer_token> [--explore]
  python cli.py probe-metadata-program <connection_id> <program> <bearer_token> [--out <file.json>]
  python cli.py check-token <bearer_token>          (local only - no network call)
  python cli.py fetch-table    <table_name>  <bearer_token> [--rows] [--into <app_folder>] [--out <file.json>]
  python cli.py fetch-workflow <workflow_id> <bearer_token> [--into <app_folder>] [--out <file.json>]
  python cli.py app-import     <app_folder> <connection_id> <tenant_id> <environment_id> [bearer_token]
                              [--dry-run] [--no-folder-prompt] [--ion-api-base-url <ion_api_root>]
                              [--header-row <n>]
  python cli.py runs        <workflow_id> <bearer_token> [--exec <WFEXEC-id> | --latest]
  python cli.py runs-sweep  <bearer_token> [--match <name text>] [--include-idle]

runs / runs-sweep are READ-ONLY (GETs on the execution-results API). `runs`
lists a workflow's executions; with --exec or --latest it prints one run's
step trace - rows in/out per step and M3's per-row messages - and saves the
raw record to RUN/runs/. `runs-sweep` does the same listing for every
workflow in the token's tenant (skipping those that report zero runs unless
--include-idle), worst first, and saves the whole result to RUN/runs/.

app-scan makes no HTTP calls and touches nothing - run it first to see
exactly what an app folder holds.

Every command that builds a workflow (classic-xml, classic-pipeline,
live-clone, pipeline, and app-import once per source Classic XML file)
prompts for a name, suggested from the source, Enter to accept it as-is.
Classic paths compose it as "<your name> - <block's own function>";
live-clone appends the source workflow's original name the same way;
pipeline uses it as typed, with no suffix. Every workflow created also gets
the "Imported" label. app-import additionally groups a source Classic XML
file's workflows together in Live (a real "Groups" feature - see
docs/vince-live-api-reference-prompt.md) whenever it produced 2+ of them.

app-import --dry-run sends nothing to Live, but it is NOT read-only: it
still converts any Classic XML in the folder and writes the result to
workflows/<stem>.generated.json, because inspecting those files before
committing is the point of a dry run. The .xml files are never modified.

app-import auto-routes an individual Classic XML BLOCK onto the generic
pipeline path, instead of native, when it has a RowFilter native cannot
express (a GE/GT/LT/LE on a non-numeric value) - most files need neither this nor
--ion-api-base-url, only ones like it. Omit the flag and an affected block
still gets built, on the native path, with the unsupported filter gate left
off and a loud warning naming it; pass it and that block is instead built
correctly on the pipeline path. See app_classic.convert_classic_xml's
docstring for the full routing rules.

`pipeline` is the non-M3-API-step path: a Trigger -> Transform/REST API/Code
chain that talks to M3 over HTTP instead, for workflows Live's native shape
can't express (see docs/vince-generic-m3-pipeline.md). Start from
`pipeline-scaffold`, or from an example in RUN/pipeline-examples/.

`classic-pipeline` converts the SAME Classic file as `classic-xml`, but onto
that generic path instead of the native one - which is the only way to keep
a Classic function's per-call conditions (`<RowFilter>` on a column an
earlier M3 call wrote, and `<BasedOnTransaction>`). It needs the target
tenant's ION API root, because nothing can derive it. Read the warnings it
prints: two of them name assumptions that only a real run can settle.
Prefer `classic-xml` whenever the native path can express the file.
"""

import json
import os
import sys
from typing import Optional

from appimport import plan_app_import, run_app_import
from build import build_live_workflow
from build_pipeline import build_pipeline_spec, build_pipeline_workflow, pipeline_warnings
from classic_to_pipeline import ClassicPipelineError, translate_classic_to_pipeline
from metadata import fetch_metadata_for_workflow, fill_from_catalog, unmapped_mandatory_warning
from source_app_folder import _design_targets, scan_app_folder
from source_classic_xml import compose_workflow_name, parse_classic_xml_units, peek_classic_xml
from source_live_json import clone_and_retarget
from state import next_labeled_name
from live_fetch import fetch_table, fetch_workflow
import runs as runs_api


def _prompt_name(suggested: str) -> str:
    """
    Ask for a base name, showing the suggestion; Enter (or no TTY) accepts
    it as-is. Shared by every interactive naming prompt below.
    """
    suggested = (suggested or "").strip() or "Untitled"
    try:
        typed = input(f"Name for this import [{suggested}]: ").strip()
    except EOFError:
        typed = ""
    return typed or suggested


def _prompt_design_match(design, targets: list) -> str:
    """
    Ask which workflow an ambiguous design file belongs to. Returns a target
    key, or "" for "do not upload" (also what Enter / no TTY gives - never a
    guess).
    """
    print("\nDesign file " + design.path + " could not be matched automatically:")
    for warning in design.warnings:
        print("  " + warning)
    for number, (key, kind, _folder, _stem, _wf) in enumerate(targets, 1):
        print(f"  {number}. {key}" + ("  (Classic XML)" if kind == "classicXml" else ""))
    while True:
        try:
            typed = input("Number of its workflow [Enter = do not upload]: ").strip()
        except EOFError:
            typed = ""
        if not typed:
            return ""
        if typed.isdigit() and 1 <= int(typed) <= len(targets):
            return targets[int(typed) - 1][0]
        print("  not one of the numbers above")


def _unit_output_path(output_path: str, index: int, label: str) -> str:
    """
    Where the index'th workflow of a multi-block file goes.

    The first keeps the path the caller asked for; later ones become
    siblings of it, numbered and labelled. A Classic file can hold several
    blocks and each becomes its own workflow (see
    source_classic_xml._parse_raw_units), and Live's POST takes one
    workflow per call, so one output file per workflow is the only shape
    that stays usable.
    """
    if index == 0:
        return output_path
    stem, ext = os.path.splitext(output_path)
    slug = "".join(c if c.isalnum() else "-" for c in (label or "block").lower())
    slug = "-".join(part for part in slug.split("-") if part)[:40]
    return f"{stem}.{index + 1}-{slug or 'block'}{ext or '.json'}"


def convert_classic_xml(classic_xml_path: str, connection_id: str, tenant_id: str, environment_id: str,
                          output_path: str, bearer_token: Optional[str] = None):
    units = parse_classic_xml_units(classic_xml_path)

    # Findings from the read-only scan - the block fan-out above all -
    # reported against every workflow the file produces.
    block_warnings = []
    try:
        with open(classic_xml_path, encoding="utf-8") as f:
            peeked = peek_classic_xml(f.read())
        block_warnings = (peeked or {}).get("warnings", [])
    except OSError:
        pass

    if len(units) > 1:
        print(f"This file holds {len(units)} block(s), so it converts to {len(units)} workflows.")

    base_name = _prompt_name(os.path.splitext(os.path.basename(classic_xml_path))[0])

    for index, wf in enumerate(units):
        wf.label = next_labeled_name(compose_workflow_name(base_name, wf.label))
        path = _unit_output_path(output_path, index, wf.label)

        metadata_overrides = None
        if bearer_token:
            print(f"Fetching live field metadata for {wf.label!r}...")
            metadata_overrides = fetch_metadata_for_workflow(wf, connection_id, bearer_token)
            print(f"  Got metadata for {len(metadata_overrides)} fields.")
        elif index == 0:
            print("No bearer token provided - no live field metadata; falling back to the offline "
                  "M3 field catalog if present, then the static table (fields in neither show as UNKNOWN).")

        warnings = list(block_warnings)
        metadata_overrides = fill_from_catalog(wf, metadata_overrides, warnings)
        warnings.extend(unmapped_mandatory_warning(wf, metadata_overrides))
        live_json = build_live_workflow(wf, connection_id, tenant_id, environment_id,
                                        metadata_overrides, warnings)
        _write(live_json, path)
        if warnings:
            print("")
            print(f"Things this conversion could not represent ({wf.label}):")
            for warning in warnings:
                print(f"  - {warning}")


def convert_classic_xml_to_pipeline(classic_xml_path: str, connection_id: str, tenant_id: str,
                                    environment_id: str, output_path: str,
                                    base_url: str = "", column_headers_path: str = "",
                                    header_row: int = None):
    """
    Convert a Classic file onto the GENERIC pipeline path, one workflow per
    block - the same fan-out as `classic-xml`.

    An <Export>-only block is skipped rather than bent into a pipeline: it
    has no input row to iterate and this path has no Excel step, so the
    native path is strictly better for it. The message says so per block.
    """
    column_headers = {}
    if column_headers_path:
        with open(column_headers_path, "r", encoding="utf-8") as f:
            column_headers = json.load(f)
        print(f"Using a column -> header map with {len(column_headers)} entry/entries.")

    units = parse_classic_xml_units(classic_xml_path)
    if len(units) > 1:
        print(f"This file holds {len(units)} block(s), so it converts to {len(units)} workflows.")

    base_name = _prompt_name(os.path.splitext(os.path.basename(classic_xml_path))[0])

    built = 0
    for index, wf in enumerate(units):
        wf.label = next_labeled_name(compose_workflow_name(base_name, wf.label))
        try:
            pipeline, warnings = translate_classic_to_pipeline(
                wf, base_url=base_url or None, connection_id=connection_id,
                column_headers=column_headers, header_row=header_row,
            )
        except ClassicPipelineError as error:
            print("")
            print(f"Skipped block {index + 1} ({wf.label}): {error}")
            continue

        live_json = build_pipeline_workflow(pipeline, connection_id, tenant_id, environment_id)
        path = _unit_output_path(output_path, index, wf.label)
        _write(live_json, path)
        built += 1

        warnings = warnings + pipeline_warnings(pipeline)
        if warnings:
            print("")
            print(f"Worth knowing about this conversion ({wf.label}):")
            for warning in warnings:
                print(f"  - {warning}")

    if not built:
        print("")
        print("Nothing was written - every block in this file belongs on the native path "
              "(`cli.py classic-xml`).")


def convert_live_json_clone(source_json_path: str, connection_id: str, tenant_id: str, environment_id: str,
                              output_path: str):
    with open(source_json_path, "r", encoding="utf-8") as f:
        source = json.load(f)
    suggested = source.get("definition", {}).get("workflowConfig", {}).get("name") or source.get("workflowName")
    override_name = _prompt_name(suggested)
    clone = clone_and_retarget(source, connection_id, tenant_id, environment_id, override_name=override_name)
    _write(clone, output_path)


def convert_pipeline_spec(spec_path: str, connection_id: str, tenant_id: str, environment_id: str,
                          output_path: str):
    """
    Build a generic (no M3 API step, no Excel step) workflow from a
    pipeline spec. The name is whatever is typed at the prompt (suggested
    from the spec's own "name", Enter to keep it) - used as-is, no "function"
    suffix (there's no Classic block concept here) and, unlike the Classic
    path, still NOT auto-incremented: a spec is a file you edit and rebuild,
    so auto-incrementing it would leave a trail of near-identical workflows
    in the tenant every time you iterate.
    """
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    typed_name = _prompt_name(spec.get("name"))
    if typed_name != (spec.get("name") or "Untitled"):
        spec["name"] = typed_name
    live_json, warnings = build_pipeline_spec(spec, connection_id, tenant_id, environment_id)
    _write(live_json, output_path)
    if warnings:
        print("")
        print("Worth knowing about this pipeline:")
        for warning in warnings:
            print(f"  - {warning}")


def write_pipeline_scaffold(output_path: str):
    from pipeline_ir import scaffold_spec

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(scaffold_spec(), f, indent=2)
    print(f"Wrote a starter pipeline spec to {output_path}")
    print("Edit it, then: python cli.py pipeline <spec.json> <connection_id> <tenant_id> "
          "<environment_id> <output.json>")


def check_token(bearer_token: str):
    """
    Inspect a bearer token string locally - no network call, nothing printed
    that could leak it.

    Exists because a FRESH token was rejected 401 by this tool while the
    identical call succeeded in Postman. When that happens the token is
    almost never the problem; what reaches the Authorization header is.
    The usual suspects, all invisible when you just look at the paste:
    a "Bearer " prefix pasted along with the token (giving
    "Bearer Bearer ey..."), a trailing newline or space from the clipboard,
    smart quotes, or a truncated copy.
    """
    import base64 as _b64
    import json as _json
    import time

    raw = bearer_token
    print(f"Length: {len(raw)} characters")

    stripped = raw.strip()
    if stripped != raw:
        print("PROBLEM: the token has leading/trailing whitespace - that alone causes a 401.")
    for label, prefix in (("Bearer ", "Bearer "), ("bearer ", "bearer ")):
        if stripped.startswith(prefix):
            print(f"PROBLEM: the token starts with \"{label}\". Paste ONLY the token - the tool adds")
            print("         the Bearer prefix itself, so this sends \"Bearer Bearer ey...\".")
            stripped = stripped[len(prefix):].strip()

    inner = "".join(stripped.split())
    if inner != stripped:
        print("PROBLEM: there is whitespace INSIDE the token - it was copied across a line break.")
    if stripped.startswith(('"', "'", "“")) or stripped.endswith(('"', "'", "”")):
        print("PROBLEM: the token is wrapped in quote characters - strip them.")

    parts = inner.split(".")
    print(f"Dot-separated parts: {len(parts)} (a JWT has exactly 3)")
    if len(parts) != 3:
        print("PROBLEM: that is not a well-formed JWT. A truncated copy is the usual cause.")
        return

    for index, name in enumerate(("header", "payload")):
        chunk = parts[index] + "=" * (-len(parts[index]) % 4)
        try:
            claims = _json.loads(_b64.urlsafe_b64decode(chunk).decode("utf-8"))
        except Exception as error:
            print(f"PROBLEM: the {name} does not base64-decode ({error}) - the copy is damaged.")
            return
        if name == "payload":
            exp = claims.get("exp")
            if isinstance(exp, (int, float)):
                remaining = int(exp - time.time())
                state = f"{remaining // 60} min left" if remaining > 0 else f"EXPIRED {-remaining // 60} min ago"
                print(f"exp: {state}")
            print(f"token_use: {claims.get('token_use')}   (Live's API expects an ID token)")
            print(f"claims present: {sorted(claims.keys())}")

    print("")
    print("If everything above looks clean, compare against Postman directly: in Postman use")
    print("Code -> cURL on the request that WORKS, and diff its headers against ours. The tool")
    print("sends exactly two: Authorization and Accept.")


def _print_token_tenant(bearer_token: str):
    """
    Say which tenant the token belongs to, so a cross-tenant pairing is
    visible before you interpret the results. A token from another tenant
    makes the connection invisible and every lookup answers empty - with
    a 200, not a 401.
    """
    from metadata import tenant_from_token, token_expiry_warning

    tenant = tenant_from_token(bearer_token)
    if tenant:
        print(f"Token belongs to: {tenant}  (must be the same tenant as the connection above)")
    else:
        print("Token tenant: could not read one from the token - check the pairing yourself")

    # Stop rather than warn. An expired token turns every probe into a 401,
    # and a page of 401s reads like a finding when it is just noise - which
    # has already burned three runs.
    expiry = token_expiry_warning(bearer_token)
    if expiry:
        print("")
        print(f"STOPPING: {expiry}")
        raise SystemExit(2)


def _metadata_get(url: str, bearer_token: str):
    """GET a metadata URL, returning (status, parsed-or-raw-body)."""
    import json as _json
    import urllib.error
    import urllib.request

    from metadata import clean_token

    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {clean_token(bearer_token)}",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace") or "")[:400]
    except urllib.error.URLError as e:
        return None, str(e.reason)
    try:
        return status, _json.loads(body)
    except ValueError:
        return status, body[:400]


def explore_metadata(connection_id: str, program: str, transaction: str, bearer_token: str):
    """
    Try several shapes of the `details` query against one connection.

    Reason this exists: the confirmed shape
    (`FIELDS#<program>#<transaction>#<I|O>`) started answering
    `{"count": 0, "items": []}` for every pair on a connection where it
    previously returned real fields. A 200-with-nothing is what BOTH an
    empty metadata store and a changed query format look like, and those
    call for completely different responses - one is a Vince-side
    provisioning question, the other is a bug in this tool.

    Every shape below except the first is an EXPLICIT GUESS. That is the
    point of a probe; do not copy any of them into metadata.py unless one
    actually returns data.
    """
    import json as _json
    import urllib.parse

    base = f"https://api.vince.live/metadata/{connection_id}"
    attempts = [
        ("confirmed: fields, one direction", f"FIELDS#{program}#{transaction}#I"),
        ("guess: fields, no direction", f"FIELDS#{program}#{transaction}"),
        ("guess: fields, program only", f"FIELDS#{program}"),
        ("guess: transactions of a program", f"TRANSACTIONS#{program}"),
        ("guess: programs", "PROGRAMS"),
    ]

    print(f"Connection: {connection_id}")
    _print_token_tenant(bearer_token)
    statuses = []
    with_data = []
    for label, details in attempts + [("no details param at all", None)]:
        url = base if details is None else f"{base}?details={urllib.parse.quote(details, safe='')}"
        status, body = _metadata_get(url, bearer_token)
        statuses.append(status)
        summary = body
        if isinstance(body, dict):
            count = body.get("count")
            items = body.get("items")
            summary = f"count={count} items={len(items) if isinstance(items, list) else items}"
            if isinstance(items, list) and items:
                with_data.append(details)
                summary += " -> " + _json.dumps(items[0])[:200]
        print(f"\n  [{label}]" + ("" if details is None else f"  details={details}"))
        print(f"    HTTP {status}: {summary}")

    # Say what the run actually showed. Getting this wrong sends you off
    # chasing a provisioning question when the real answer was "your token
    # expired ten minutes ago" - which is easy, because a short-lived
    # Cognito token dies mid-investigation more often than not.
    print("\n--- verdict ---")
    if all(s == 401 for s in statuses):
        print("Every call was REJECTED (401). This says nothing about the metadata store - the")
        print("token is expired or not valid for this tenant. Grab a fresh one from Live's own")
        print("DevTools Network tab and run this again.")
    elif with_data:
        confirmed_shape = attempts[0][1]
        print("Query shapes that returned data:")
        for details in with_data:
            print(f"  {details}")
        if confirmed_shape in with_data:
            print("")
            print("The confirmed shape works, so the query format has NOT moved and")
            print("metadata.fetch_live_metadata is correct as written. If the same pair answered")
            print("count=0 earlier, the difference is on the server side - a store that populates")
            print("on demand is the obvious suspect, so re-run the pair that failed before")
            print("concluding anything about it.")
        else:
            print("")
            print("The confirmed shape returned NOTHING while another shape returned data - the")
            print("query format has moved, and metadata.fetch_live_metadata needs updating.")
    elif any(s == 200 for s in statuses):
        print("Answered (200) but described nothing, on every shape tried. The metadata store")
        print("for this connection appears genuinely empty - a Vince-side question about the")
        print("connection, not something this tool can fix. Cross-check in Live's own builder:")
        print("if picking a transaction there DOES populate its fields, the builder is reading")
        print("metadata some other way and that call is worth capturing in DevTools.")
    else:
        print("No usable response at all - see the statuses above.")


def probe_metadata_program(connection_id: str, program: str, bearer_token: str, out_path: str = ""):
    """
    Fetch a whole program's field metadata in ONE call (details=FIELDS#<program>).

    Open question this answers: that shape returns many items (55 for
    MRS001MI), but it is unknown whether an item says WHICH transaction it
    belongs to. If it does, the converter can make one call per program
    instead of two per transaction. If it does not, the shape is unusable
    for our per-(program, transaction, field) keying, however fast it is.
    """
    import json as _json
    import urllib.parse

    _print_token_tenant(bearer_token)
    details = f"FIELDS#{program}"
    url = f"https://api.vince.live/metadata/{connection_id}?details={urllib.parse.quote(details, safe='')}"
    print(f"GET {url}")
    status, body = _metadata_get(url, bearer_token)
    print(f"HTTP {status}")
    if not isinstance(body, dict):
        print(body)
        return

    items = body.get("items") or []
    print(f"count={body.get('count')} items={len(items)}")
    if items:
        print("")
        print(f"Keys present on each item: {sorted(items[0].keys())}")
        print("First 3 items, with their 'data' replaced by a field count:")
        for item in items[:3]:
            trimmed = {k: (f"<{len(v)} fields>" if k == "data" and isinstance(v, list) else v)
                       for k, v in item.items()}
            print(f"  {_json.dumps(trimmed)[:300]}")
        print("")
        print("Does any item name a transaction? Look for it in the keys above -")
        print("without one, the whole-program shape cannot be attributed per transaction.")

    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            _json.dump(body, handle, indent=2)
        print("")
        print(f"Full raw response written to {out_path}")


def probe_metadata(connection_id: str, program: str, transaction: str, bearer_token: str):
    """
    Ask Live's metadata endpoint about one program/transaction and print
    exactly what comes back, raw.

    Exists because "the Excel headers say TODO: metadata not found" has
    now happened for more than one transaction (AUTCHKMI, BUS100MI), and
    the three explanations - a rejected token, a wrong transaction name,
    and a program the connection genuinely cannot describe - are
    indistinguishable from the converter's output alone.
    """
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    _print_token_tenant(bearer_token)
    for direction, label in (("I", "input"), ("O", "output")):
        details = f"FIELDS#{program}#{transaction}#{direction}"
        url = f"https://api.vince.live/metadata/{connection_id}?details={urllib.parse.quote(details, safe='')}"
        print(f"\n=== {program}.{transaction} {label} fields ({direction}) ===")
        print(f"GET {url}")
        from metadata import clean_token

        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {clean_token(bearer_token)}",
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code} {e.reason}")
            print((e.read().decode('utf-8', 'replace') or "")[:600])
            continue
        except urllib.error.URLError as e:
            print(f"could not reach it: {e.reason}")
            continue

        print(f"HTTP {status}")
        try:
            data = _json.loads(body)
        except ValueError:
            print("(response was not JSON)")
            print(body[:600])
            continue

        fields = [f for item in data.get("items", []) for f in item.get("data", [])]
        if not fields:
            print("NO FIELDS in the response - the endpoint answered but describes nothing here.")
            print("Raw body (first 800 chars):")
            print(_json.dumps(data)[:800])
            continue
        print(f"{len(fields)} field(s):")
        for f in fields:
            print(f"  {f.get('name'):<10} type={f.get('type')} length={f.get('length')} "
                  f"mandatory={f.get('mandatory')} descr={f.get('descr')!r}")


def scan_app(app_folder: str):
    plan = plan_app_import(app_folder)["plan"]
    _print_plan(plan)


def import_app(app_folder: str, connection_id: str, tenant_id: str, environment_id: str,
               bearer_token: str = "", dry_run: bool = False, prompt_to_folder: bool = True,
               ion_api_base_url: str = "", header_row: int = None):
    # One prompt per source Classic XML file (not per block/workflow it
    # produces) - the CLI's equivalent of the web UI's per-row name field.
    # A folder with none of these (e.g. a plain workflows-only app) prompts
    # for nothing.
    bundle = scan_app_folder(app_folder)
    base_names = {}
    for entry in bundle.classic_xml:
        base_names[entry.path] = _prompt_name(os.path.splitext(os.path.basename(entry.path))[0])

    # A design file the folder/name rules could not pair is asked about,
    # never guessed - see source_app_folder.match_design_files.
    design_matches = {}
    targets = _design_targets(bundle)
    for design in bundle.design_files:
        if design.needs_choice:
            design_matches[design.path] = _prompt_design_match(design, targets)

    result = run_app_import(app_folder, connection_id, tenant_id, environment_id, bearer_token, dry_run,
                            prompt_to_folder, ion_api_base_url, base_names, design_matches,
                            header_row=header_row)
    if not result.get("ok") and result.get("error") and "plan" in result and "tables" not in result:
        print("ERROR: " + result["error"])
        sys.exit(1)

    _print_plan(result["plan"])

    print("\nResults" + (" (DRY RUN - nothing was sent to Live)" if result["dryRun"] else "") + ":")
    for xml in result.get("classicXml", []):
        print("  xml      " + xml["path"] + ": " + xml["status"] +
              (" -> " + xml["generatedPath"] if xml.get("generatedPath") else "") +
              (" [pipeline path]" if xml.get("buildPath") == "pipeline" else ""))
        for warning in xml.get("warnings", []):
            print("             ! " + warning)
        if xml.get("error"):
            print("             " + xml["error"])
    for table in result["tables"]:
        print("  table    " + table["tableName"] + ": " + table["status"])
        for warning in table.get("warnings", []):
            print("             ! " + warning)
        if table.get("error"):
            print("             " + table["error"])
    for workflow in result["workflows"]:
        detail = workflow.get("workflowId") or workflow.get("error") or ""
        print("  workflow " + workflow["name"] + ": " + workflow["status"] + (" " + detail if detail else ""))
        for warning in workflow.get("warnings", []):
            print("             ! " + warning)
        design = workflow.get("designFile")
        if design:
            print("      design " + design["filename"] + ": " + design["status"] +
                  (" -> " + design["key"] if design.get("key") else ""))
            if design.get("error"):
                print("             " + design["error"])
    for group in result.get("groups", []):
        detail = group.get("groupId") or group.get("error") or ""
        print("  group    " + group["groupName"] + " (" + str(group["memberCount"]) + " workflows): " +
              group["status"] + (" " + detail if detail else ""))

    for warning in result.get("warnings", []):
        print("  ! " + warning)

    for design in result["plan"].get("designFiles", []):
        if not design.get("workflowPath"):
            print("  design file NOT uploaded: " + design["path"])
            for warning in design["warnings"]:
                print("             ! " + warning)

    summary = result["summary"]
    if summary.get("xmlConverted") or summary.get("xmlFailed"):
        print("\n" + str(summary["xmlConverted"]) + " Classic XML file(s) converted, " +
              str(summary["xmlFailed"]) + " failed" +
              (" - " + str(summary["xmlWithMissingMetadata"]) + " with missing field metadata"
               if summary.get("xmlWithMissingMetadata") else "") +
              (" - " + str(summary["xmlOnPipelinePath"]) + " on the generic-pipeline path"
               if summary.get("xmlOnPipelinePath") else "") + ".")
    print("\n" + str(summary["tablesCreated"]) + " table(s) created, " +
          str(summary["tablesSkipped"]) + " already existed, " +
          str(summary["tablesFailed"]) + " failed; " +
          str(summary["workflowsCreated"]) + " workflow(s) created, " +
          str(summary.get("workflowsSkipped", 0)) + " already existed, " +
          str(summary["workflowsFailed"]) + " failed.")
    if summary.get("groupsCreated") or summary.get("groupsFailed"):
        print(str(summary["groupsCreated"]) + " group(s) created, " +
              str(summary["groupsFailed"]) + " failed.")
    if summary.get("designFilesUploaded") or summary.get("designFilesFailed"):
        print(str(summary["designFilesUploaded"]) + " design file(s) uploaded, " +
              str(summary["designFilesFailed"]) + " failed.")
    if summary["rowsNotImported"]:
        print(str(summary["rowsNotImported"]) + " table row(s) were NOT imported - no confirmed row-write API. "
              "Saved to the artifacts directory.")
    if result.get("folderPromptPath"):
        print("App Builder prompt: " + result["folderPromptPath"] + "  (in the app folder)")
    if result.get("promptPath"):
        print("App Builder prompt: " + result["promptPath"])
    if result.get("warningsPath"):
        deduped = sum(len(g["messages"]) for g in result.get("warningGroups") or [])
        raw = len(result.get("warnings") or [])
        print("Full warnings:      " + result["warningsPath"] +
              "  (" + str(deduped) + (", " + str(raw) + " raw" if deduped != raw else "") + ")")
    print("Artifacts:          " + result["artifactsDir"])
    if not result.get("ok"):
        sys.exit(1)


def fetch_table_cmd(table_name: str, bearer_token: str, with_rows: bool = False,
                    app_folder: str = "", out_path: str = ""):
    result = fetch_table(table_name, bearer_token, with_rows=with_rows, app_folder=app_folder)

    if result.get("rowCount") is not None:
        print("Table '" + result["tableName"] + "' exists - " + str(result["rowCount"]) + " row(s).")

    if not result["ok"]:
        print("ERROR: " + str(result.get("error")))
        for warning in result.get("warnings", []):
            print("  ! " + warning)
        for attempt in result.get("probeAttempts", []):
            # A failed attempt's verdict is the server's own error, which
            # already carries the status; don't print the status twice.
            verdict = attempt["verdict"]
            prefix = "" if verdict.startswith("HTTP") else "HTTP " + str(attempt["status"]) + ": "
            print("  tried " + attempt["endpoint"] + " -> " + prefix + verdict)
        print("\nNo endpoint for reading a table definition is confirmed in this project. If one of the "
              "above actually worked, write it down in docs/vince-custom-tables-guide.md and collapse "
              "liveapi.META_READ_CANDIDATES to it.")
        sys.exit(1)

    print("Definition read from: " + result["metaEndpoint"])
    print("  primaryKeys: " + ", ".join(result["meta"].get("primaryKeys") or ["-"]))
    print("  updateType:  " + str(result["meta"].get("updateType")))
    print("  columns:     " + ", ".join(sorted((result["meta"].get("schema") or {}).get("properties", {}))) or "-")
    if with_rows:
        print("  rows pulled: " + str(len(result.get("rows") or [])) +
              (" (truncated)" if result.get("rowsTruncated") else ""))

    for warning in result.get("warnings", []):
        print("  ! " + warning)
    for path in result.get("files", []):
        print("Wrote " + path)

    if out_path:
        _write(result["meta"], out_path)


def fetch_workflow_cmd(workflow_id: str, bearer_token: str, app_folder: str = "", out_path: str = ""):
    result = fetch_workflow(workflow_id, bearer_token, app_folder)
    if not result["ok"]:
        print("ERROR: " + str(result.get("error")))
        sys.exit(1)

    print("Fetched '" + result["workflowName"] + "'")
    for warning in result.get("warnings", []):
        print("  ! " + warning)
    for path in result.get("files", []):
        print("Wrote " + path)
    if out_path:
        _write(result["result"], out_path)


def runs_cmd(workflow_id: str, bearer_token: str, execution_id: str = "", latest: bool = False):
    try:
        listing = runs_api.list_runs(workflow_id, bearer_token)
        for warning in listing["warnings"]:
            print("  ! " + warning)
        if latest:
            if not listing["executions"]:
                print("No executions listed for " + workflow_id)
                return
            execution_id = listing["executions"][0]["executionId"]
        if not execution_id:
            print(f"{workflow_id}: {len(listing['executions'])} execution(s) listed"
                  + (f" of {listing['total']}" if listing["total"] is not None else ""))
            for e in listing["executions"]:
                print(f"  {e['startTime'] or '-':<26} {'FAILED' if e['failed'] else 'ok':<7} "
                      f"{str(e['status'] or ''):<12} {e['executionId']}"
                      + (f"  {e['error']}" if e["error"] else ""))
            return
        result = runs_api.inspect_run(workflow_id, execution_id, bearer_token)
    except runs_api.RunsError as error:
        print("ERROR: " + str(error))
        sys.exit(1)

    trace = result["trace"]
    print(f"{trace['executionId']}  ended {trace['endTime'] or '-'}  "
          f"{'FAILED' if trace['failed'] else 'ok'}  env={trace['m3Environment'] or '-'}")
    print(f"  input: {trace['inputRows']} row(s); trigger header {json.dumps(trace['triggerHeader'])}")
    for step in trace["steps"]:
        print(f"  {str(step['stepId']):<22} {'ok' if step['success'] else 'FAILED':<7} "
              f"in {step['rowsIn']!s:>5}  out {step['rowsOut']!s:>5}  "
              f"processed {step['recordsProcessed']!s:>5}  {step['durationMs']} ms")
        for column, counts in step["messages"].items():
            for message, count in counts.items():
                print(f"      {count:>5} x {column}: {message or '(blank)'}")
    if "finalOutputRows" in trace:
        print(f"  final output: {trace['finalOutputRows']} row(s)")
    for note in trace["notes"]:
        print("  - " + note)
    print("Saved " + runs_api.write_artifact("run-" + execution_id, result))


def runs_sweep_cmd(bearer_token: str, match: str = "", include_idle: bool = False):
    def progress(done, total):
        if done == total or done % 25 == 0:
            print(f"  ...{done}/{total}", file=sys.stderr)
    try:
        result = runs_api.sweep_tenant(bearer_token, match=match, include_idle=include_idle,
                                       progress=progress)
    except runs_api.RunsError as error:
        print("ERROR: " + str(error))
        sys.exit(1)

    print(f"Tenant {result['tenantId']}: {result['workflowsInTenant']} workflow(s), "
          f"{result['workflowsMatched']} matched, {result['workflowsAsked']} asked, "
          f"{result['workflowsSkippedIdle']} skipped (0 runs)")
    s = result["summary"]
    print(f"  last run failed: {s['lastRunFailed']}   any listed failure: {s['anyListedFailure']}   "
          f"never ran: {s['neverRan']}   read errors: {s['readErrors']}")
    for row in result["workflows"]:
        if not (row["error"] or row["failedListed"]):
            continue
        flag = "READ ERR" if row["error"] else "LAST FAIL" if row["lastFailed"] else "had fails"
        print(f"  {flag:<9} {row['failedListed']:>3}/{row['listed']:<3} {row['lastStart'] or '-':<26} "
              f"{row['name']}  ({row['workflowId']})")
        if row["error"] or row["lastError"]:
            print("            " + str(row["error"] or row["lastError"])[:200])
    for warning in result["warnings"]:
        print("  ! " + warning)
    print("Saved " + runs_api.write_artifact("sweep", result))


def _print_plan(plan: dict):
    print("App: " + plan["name"] + "  (" + plan["root"] + ")")
    classic = plan.get("classicXml", [])
    print("  " + str(len(plan["tables"])) + " table(s), " + str(len(plan["workflows"])) + " workflow(s), " +
          (str(len(classic)) + " Classic XML file(s), " if classic else "") +
          str(len(plan["assets"])) + " App Builder source file(s)")
    for xml in classic:
        blocks = xml.get("blockCount", 1)
        # The path shown is the FIRST workflow's; a multi-block file writes
        # numbered siblings next to it, so say how many rather than let the
        # single filename imply one.
        target = xml["generatedPath"] + (" (+ " + str(blocks - 1) + " more)" if blocks > 1 else "")
        print("    xml      " + xml["path"] + "  -> " + target +
              "  (" + xml["label"] + ", " + str(xml["transactionCount"]) + " M3 call(s)" +
              (", " + str(blocks) + " workflows" if blocks > 1 else "") + ")")
        for warning in xml["warnings"]:
            print("             ! " + warning)
    for spec in plan.get("pipelineSpecs", []):
        print("    spec     " + spec["path"] + "  -> " + spec["generatedPath"] + "  (" + spec["name"] +
              (", grouped with " + spec["groupWith"] if spec.get("groupWith") else "") + ")")
        for warning in spec["warnings"]:
            print("             ! " + warning)
    for table in plan["tables"]:
        print("    table    " + table["tableName"] + "  keys=" + ",".join(table["primaryKeys"] or ["-"]) +
              "  rows=" + str(table["rowCount"]))
        for warning in table["warnings"]:
            print("             ! " + warning)
    for workflow in plan["workflows"]:
        print("    workflow " + workflow["name"] + "  (" + workflow["path"] + ")" +
              ("  + design file " + workflow["designFile"] if workflow.get("designFile") else ""))
        for warning in workflow["warnings"]:
            print("             ! " + warning)
    for asset in plan["assets"]:
        print("    source   " + asset["path"] + "  " + str(asset["size"]) + " bytes" +
              ("" if asset["inlined"] else "  [not inlined: " + str(asset["skippedReason"]) + "]"))
    for link in plan["links"]:
        print("    link     " + link["detail"])
    for warning in plan["warnings"]:
        print("    ! " + warning)


def _write(live_json: dict, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(live_json, f, indent=2)
    print(f"Wrote {output_path}")

    flagged = json.dumps(live_json)
    if "_needs_review" in flagged:
        print("\nWARNING: Some fields were flagged for manual review - search the output for \"_needs_review\".")


def main():
    if len(sys.argv) < 2:
        _usage()

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "classic-xml":
        if len(args) not in (5, 6):
            _usage()
        token = args[5] if len(args) == 6 else None
        convert_classic_xml(args[0], args[1], args[2], args[3], args[4], token)

    elif command == "classic-pipeline":
        base_url = _take_option(args, "--base-url")
        column_headers = _take_option(args, "--column-headers")
        header_row = _take_option(args, "--header-row")
        if len(args) != 5:
            _usage()
        convert_classic_xml_to_pipeline(args[0], args[1], args[2], args[3], args[4],
                                        base_url, column_headers,
                                        int(header_row) if header_row else None)

    elif command == "live-clone":
        if len(args) != 5:
            _usage()
        convert_live_json_clone(args[0], args[1], args[2], args[3], args[4])

    elif command == "pipeline":
        if len(args) != 5:
            _usage()
        convert_pipeline_spec(args[0], args[1], args[2], args[3], args[4])

    elif command == "pipeline-scaffold":
        if len(args) != 1:
            _usage()
        write_pipeline_scaffold(args[0])

    elif command == "check-token":
        if len(args) != 1:
            _usage()
        check_token(args[0])

    elif command == "probe-metadata-program":
        out_path = _take_option(args, "--out")
        if len(args) != 3:
            _usage()
        probe_metadata_program(args[0], args[1], args[2], out_path)

    elif command == "probe-metadata":
        explore = "--explore" in args
        args = [a for a in args if a != "--explore"]
        if len(args) != 4:
            _usage()
        if explore:
            explore_metadata(args[0], args[1], args[2], args[3])
        else:
            probe_metadata(args[0], args[1], args[2], args[3])

    elif command == "fetch-table":
        with_rows = "--rows" in args
        app_folder = _take_option(args, "--into")
        out_path = _take_option(args, "--out")
        args = [a for a in args if a != "--rows"]
        if len(args) != 2:
            _usage()
        fetch_table_cmd(args[0], args[1], with_rows, app_folder, out_path)

    elif command == "fetch-workflow":
        app_folder = _take_option(args, "--into")
        out_path = _take_option(args, "--out")
        if len(args) != 2:
            _usage()
        fetch_workflow_cmd(args[0], args[1], app_folder, out_path)

    elif command == "app-scan":
        if len(args) != 1:
            _usage()
        scan_app(args[0])

    elif command == "app-import":
        dry_run = "--dry-run" in args
        prompt_to_folder = "--no-folder-prompt" not in args
        ion_api_base_url = _take_option(args, "--ion-api-base-url")
        header_row = _take_option(args, "--header-row")
        args = [a for a in args if a not in ("--dry-run", "--no-folder-prompt")]
        if len(args) not in (4, 5):
            _usage()
        token = args[4] if len(args) == 5 else ""
        import_app(args[0], args[1], args[2], args[3], token, dry_run, prompt_to_folder,
                  ion_api_base_url, int(header_row) if header_row else None)

    elif command == "runs":
        latest = "--latest" in args
        execution_id = _take_option(args, "--exec")
        args = [a for a in args if a != "--latest"]
        if len(args) != 2:
            _usage()
        runs_cmd(args[0], args[1], execution_id, latest)

    elif command == "runs-sweep":
        include_idle = "--include-idle" in args
        match = _take_option(args, "--match")
        args = [a for a in args if a != "--include-idle"]
        if len(args) != 1:
            _usage()
        runs_sweep_cmd(args[0], match, include_idle)

    else:
        _usage()


def _take_option(args: list, flag: str) -> str:
    """Pull `--flag value` out of args in place, returning the value (or "")."""
    if flag not in args:
        return ""
    index = args.index(flag)
    value = args[index + 1] if index + 1 < len(args) else ""
    del args[index:index + 2]
    return value


def _usage():
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
