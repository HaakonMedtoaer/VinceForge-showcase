"""
Self-test for the generic-pipeline path. No test framework - same
stdlib-only, run-it-directly style as the rest of this project:

    python selftest_pipeline.py

What it checks is specifically the stuff that would produce a broken
workflow SILENTLY rather than an error: the confirmed step type/target
values, the Transform+REST pairing an m3Bulk step expands into, linear
childSteps nesting, that no excel_1 or m3_api_1 ever appears, that a
ref-based reference resolves to the right stepId, and that a gate reading
an earlier call's output correlates per row instead of collapsing into
that field's whole flattened sequence.

It does NOT check that the generated JSONata evaluates correctly - that
needs a real JSONata engine, not available in the stdlib. That was done
during development against the reference implementation:

    npm install jsonata
    # bind {context: {...}} shaped like a real bulk response and evaluate
    # each stepConfig template

Every template parsed, and the gated step emitted only the rows whose
prior call had passed. Worth repeating that way after any change to
m3_rest.render_bulk_transform.
"""

import json
import os
import sys

from build_pipeline import build_pipeline_spec
from pipeline_ir import scaffold_spec

CONNECTION = "CONNECTION-" + "a" * 32
TENANT = "TENANT-" + "b" * 32
ENVIRONMENT = "ENVIRONMENT-" + "c" * 32

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def build(spec):
    return build_pipeline_spec(spec, CONNECTION, TENANT, ENVIRONMENT)


def flatten(node, out=None):
    out = [] if out is None else out
    out.append(node)
    for child in node.get("childSteps", []):
        flatten(child, out)
    return out


def test_scaffold_shape():
    workflow, _ = build(scaffold_spec())
    definition = workflow["definition"]
    chain = flatten(definition["workflowConfig"]["steps"][0])

    check([n["stepId"] for n in chain] ==
          ["trigger_1", "transform_1", "rest_api_1", "transform_2", "rest_api_2"],
          f"unexpected chain: {[n['stepId'] for n in chain]}")

    for node in chain:
        check(len(node.get("childSteps", [])) <= 1,
              f"{node['stepId']} has {len(node['childSteps'])} children - nesting must stay linear")
        if node["stepId"] == "trigger_1":
            check("retryAttempts" not in node, "trigger_1 must not carry retryAttempts")
        else:
            check(node.get("retryAttempts") == 0, f"{node['stepId']} is missing retryAttempts: 0")

    shapes = {n["stepId"]: (n["type"], n["target"]) for n in chain}
    check(shapes["transform_1"] == ("TRANSFORMER_MORPH", "workflow-transform"),
          "wrong Transform type/target")
    check(shapes["rest_api_1"] == ("GENERIC_API", "workflow-rest-api"),
          "wrong REST API type/target")
    check(shapes["trigger_1"] == ("TRIGGER", "workflow-trigger"), "wrong Trigger type/target")

    # excel_1 binds only to m3_api_1 instance ids and broke saving on a
    # non-M3 chain for real; m3_api_1 has no business here either.
    ids = set(shapes)
    check("excel_1" not in ids and "m3_api_1" not in ids,
          "a generic pipeline must contain neither excel_1 nor m3_api_1")
    check(set(definition["stepConfig"]) == ids,
          f"stepConfig keys {sorted(definition['stepConfig'])} do not match the chain {sorted(ids)}")

    check(workflow["workflowType"] == "STANDARD", "workflowType must be STANDARD at creation")
    check(workflow["hasError"] is False, "hasError must be set false by us")


def test_vxl_label():
    # Every workflow also gets "Imported" (envelope.IMPORTED_LABEL),
    # appended last by workflow_envelope() regardless of path - see
    # envelope.py and CLAUDE.md's naming/tagging section.
    workflow, _ = build(scaffold_spec())
    check(workflow["labels"] == ["VXL", "Imported"], f"expected VXL + Imported, got {workflow['labels']!r}")

    spec = dict(scaffold_spec())
    spec.pop("vxl")
    plain, warnings = build(spec)
    check(plain["labels"] == ["Imported"], "no vxl in the spec should still mean the Imported label")
    check(any("VXL label" in w for w in warnings),
          "a spreadsheet-input pipeline without the VXL label should warn")

    # Other labels survive, and the vxl sugar appends rather than replaces.
    spec = dict(scaffold_spec())
    spec["labels"] = ["Ops"]
    both, _ = build(spec)
    check(both["labels"] == ["Ops", "VXL", "Imported"], f"labels should compose, got {both['labels']!r}")


def test_ref_resolution_and_correlation():
    workflow, _ = build(scaffold_spec())
    gated = workflow["definition"]["stepConfig"]["transform_2"]["template"]
    # "authority" is the spec's own ref for the first m3Bulk step; it must
    # resolve to that step's REST stepId and not appear literally.
    check("rest_api_1.body.results.records" in gated,
          f"ref did not resolve to the producing REST stepId:\n{gated}")
    check("authority." not in gated, "the spec-level ref leaked into the generated JSONata")
    # Per-row correlation, not the whole flattened field sequence.
    check("$filter(" in gated and "[$i]" in gated,
          f"a gate on an earlier call must correlate per row:\n{gated}")


def test_rest_config_is_the_confirmed_shape():
    workflow, _ = build(scaffold_spec())
    stored = workflow["definition"]["stepConfig"]["rest_api_1"]
    # CONFIRMED against a GET of a hand-built workflow: a GENERIC_API
    # step's config is a JSON *string* under "content", and the compiled
    # state machine parses it back out for the proxy lambda. Flat keys in
    # stepConfig leave the proxy with nothing.
    check(list(stored) == ["content"],
          f"a REST API stepConfig must be exactly {{content}}, got {sorted(stored)}")
    check(isinstance(stored["content"], str), "content must be a JSON string, not an object")
    rest = json.loads(stored["content"])
    check(rest["version"] == 2, "M3 bulk REST steps carry version 2")
    check(rest["method"] == "post", "M3 bulk execute is a POST")
    check(rest["bodyPath"] == "$.payload", "confirmed bodyPath is $.payload")
    check(rest["headers"] == {"content-type": "application/json"}, "wrong headers")
    check(rest["endpoint"].endswith("/M3/m3api-rest/v2/execute"),
          f"wrong bulk endpoint: {rest['endpoint']}")
    check(rest["connectionId"] == CONNECTION,
          "the REST step must carry the connection - it is what attaches M3 auth")

    # ...while a Transform's config really is flat. Same GET, same workflow.
    transform = workflow["definition"]["stepConfig"]["transform_1"]
    check(sorted(transform) == ["template", "trimAll"],
          f"a Transform stepConfig stays flat, got {sorted(transform)}")


def test_unfiltered_call_keeps_the_production_idiom():
    """A call with no gates should read exactly like the real examples."""
    workflow, _ = build(scaffold_spec())
    plain = workflow["definition"]["stepConfig"]["transform_1"]["template"]
    check("$data := $context.data.trigger.body;" in plain,
          f"unfiltered rows should bind straight to the trigger body:\n{plain}")
    check("$filter(" not in plain, "an ungated call must not pay for a $filter callback")
    check('"payload"' in plain and '"transactions"' in plain,
          "the payload/transactions envelope is what bodyPath $.payload selects")
    check(plain.rstrip().endswith(")"), "the JSONata block must be closed")


def test_errors_are_actionable():
    cases = [
        ({"vinceforge": "pipeline/1", "name": "x", "steps": []}, "at least one"),
        ({"vinceforge": "pipeline/9", "name": "x", "steps": []}, "Unsupported spec version"),
        ({"vinceforge": "pipeline/1", "steps": []}, 'needs a "name"'),
        ({"vinceforge": "pipeline/1", "name": "x", "steps": [{"kind": "nope"}]}, "unknown kind"),
        ({"vinceforge": "pipeline/1", "name": "x", "baseUrl": "https://host/T",
          "steps": [{"kind": "m3Bulk", "program": "P",
                     "calls": [{"transaction": "T", "record": {"A": {"bogus": 1}}}]}]},
         "expected exactly one of"),
        ({"vinceforge": "pipeline/1", "name": "x",
          "steps": [{"kind": "m3Bulk", "program": "P",
                     "calls": [{"transaction": "T", "record": {}}]}]},
         "needs a baseUrl"),
        ({"vinceforge": "pipeline/1", "name": "x", "baseUrl": "https://host/T",
          "steps": [{"kind": "m3Bulk", "ref": "a", "program": "P",
                     "calls": [{"transaction": "T", "record": {}}]},
                    {"kind": "m3Bulk", "ref": "a", "program": "P",
                     "calls": [{"transaction": "T", "record": {}}]}]},
         "Duplicate m3Bulk ref"),
        ({"vinceforge": "pipeline/1", "name": "x",
          "steps": [{"kind": "restApi", "endpoint": "https://x", "bodyPath": "$",
                     "body": {"a": 1}}]},
         "not both"),
        ({"vinceforge": "pipeline/1", "name": "x", "labels": [7],
          "steps": [{"kind": "transform", "template": "$"}]},
         '"labels" must be strings'),
    ]
    for spec, expected in cases:
        try:
            build(spec)
        except Exception as error:      # PipelineSpecError or M3RestError
            check(expected in str(error),
                  f"error should mention {expected!r}, said: {error}")
        else:
            failures.append(f"spec should have been rejected: {json.dumps(spec)[:90]}")


def test_repo_examples_build():
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "RUN", "pipeline-examples")
    names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
    check(bool(names), "no example specs found")
    for name in names:
        with open(os.path.join(folder, name), "r", encoding="utf-8") as handle:
            spec = json.load(handle)
        try:
            workflow, _ = build(spec)
        except Exception as error:
            failures.append(f"example {name} failed to build: {error}")
            continue
        blob = json.dumps(workflow)
        check("<TENANT>" not in blob, f"example {name} still carries a placeholder endpoint")
        check('"excel_1"' not in blob, f"example {name} somehow produced an excel_1 step")


def test_webui_page_js_parses():
    """
    Slightly outside this file's remit (the web UI, not the pipeline), but
    it lives here because this is the file anyone runs.

    A single syntax error anywhere in HTML_PAGE's <script> block stops the
    whole script executing, so NO event listener attaches and every button
    in the UI goes dead - with nothing in the terminal to hint at it. That
    shipped twice in one session, both times from an escaped "\\n" in a
    patch turning into a real newline and cutting a string literal in half.
    Cheap to check, invisible to miss.

    Needs node on PATH; skipped (loudly) if absent.
    """
    import re
    import shutil
    import subprocess
    import tempfile

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "webui.py"), encoding="utf-8") as handle:
        source = handle.read()

    marker = 'HTML_PAGE = r"""'
    check(marker in source, "webui.py no longer defines HTML_PAGE the expected way")
    if marker not in source:
        return
    page = source.split(marker, 1)[1].rsplit('"""', 1)[0]

    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    check(bool(scripts), "found no <script> block in the page")

    # An odd number of unescaped double quotes on a JS line is the exact
    # signature of the bug, but string concatenation across lines makes it
    # noisy, so leave that to node rather than guessing here.
    if not shutil.which("node"):
        print("  (skipped the page-JS syntax check: node not on PATH)")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write("\n".join(scripts))
        path = handle.name
    try:
        done = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        check(done.returncode == 0,
              f"the web UI's page JavaScript does not parse - every button would be dead:\n"
              f"{done.stderr.strip()}")
    finally:
        os.unlink(path)


def main():
    tests = (
        test_scaffold_shape,
        test_vxl_label,
        test_ref_resolution_and_correlation,
        test_rest_config_is_the_confirmed_shape,
        test_unfiltered_call_keeps_the_production_idiom,
        test_errors_are_actionable,
        test_repo_examples_build,
        test_webui_page_js_parses,
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
    print(f"All {len(tests)} generic-pipeline self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
