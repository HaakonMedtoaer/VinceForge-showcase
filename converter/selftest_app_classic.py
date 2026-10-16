"""
Self-test for app-import's per-BLOCK native-vs-generic-pipeline routing in
app_classic.py. No test framework - same stdlib-only, run-it-directly style
as the other selftest_*.py files:

    python selftest_app_classic.py

Added 2026-09-16, alongside the routing itself. Until now app_classic.py and
appimport.py had NO self-test coverage at all - exactly the kind of gap this
project's own testing philosophy warns about (a routing mistake here is
silent: the converter still emits valid JSON, the POST still succeeds, and
a workflow just quietly runs on the wrong path - either losing a filter it
didn't have to, or landing on the Code-step escape hatch for a block that
never needed it).

What's checked, all against real parse+build calls, not mocks:

  - `unrepresentable_filter_operators` correctly finds nothing for an
    EQ/NE-only block, and correctly names GE/GT/LT/LE.
  - A block that doesn't need it is built on the NATIVE path, unchanged -
    `buildPath: "native"`, a real `m3_api_1` step, and no pipeline-only
    warnings.
  - A block that needs it, given an ION API root, is built on the
    PIPELINE path - `buildPath: "pipeline"`, a real Code step (no
    `m3_api_1`), `debug: false` baked in (not `classic-pipeline`'s
    standalone default of `true` - see CLAUDE.md's debug self-poisoning
    hazard), and a "Routed to the generic-pipeline path" warning naming
    the operator(s).
  - The SAME block, with NO ION API root given, still gets built - on the
    native path, with a loud "PIPELINE BUILD FAILED" warning explaining
    why, and no exception raised. Individual blocks failing to get the
    better path must never abort the run.
  - A file with two blocks, one of each kind, splits correctly: one native,
    one pipeline, independently - this is the whole reason routing is
    per-block and not per-file (a real CustomerA customer file was found to
    need exactly this: 3 of 8 blocks native-safe, 5 needing GE/GT/LT).

Added 2026-09-16, alongside naming/tagging/grouping: `base_names` composes
into every block's name, falling back to the file's own stem when a file
has no entry, and every block from the same file gets the same
`group_key` - what `appimport._create_groups` buckets on to decide which
workflows become a Live Group after creation.

Also added 2026-09-16, same day: `scan_app_folder`'s "a .generated.json
whose source XML is still present is not imported a second time" rule only
ever matched the FIRST block's plain `<stem>.generated.json` path - blocks
2+ of a multi-block file use a numbered `<stem>.N-slug.generated.json`
name (see app_classic._file_suffix) that exact check never recognised. So
re-running app-import against a folder holding a multi-block file's stale
output from an EARLIER run silently imported those stale files a SECOND
time, as unrelated "plain" workflows under whatever name that earlier run
gave them - a real, confirmed duplicate-workflow-creation bug, not a
hypothetical. `test_stale_multi_block_output_is_not_reimported` below
exercises `scan_app_folder` for real (not the `_bundle_for` helper other
tests here use, which builds an `AppBundle` by hand and never touches this
code path at all).
"""

import json
import os
import sys
import tempfile

from app_classic import convert_classic_xml, convert_pipeline_specs
from source_app_folder import AppBundle, AppClassicXml, match_design_files, scan_app_folder
from source_classic_xml import (parse_classic_xml_units, peek_classic_xml,
                                unrepresentable_filter_operators)

CONNECTION = "CONNECTION-" + "a" * 32
TENANT = "TENANT-" + "b" * 32
ENVIRONMENT = "ENVIRONMENT-" + "c" * 32
ION_BASE_URL = "https://mingle-ionapi.eu1.inforcloudsuite.com/TEST_TENANT"

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def _xml(body):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<VinceExcelConfig>\n'
            + body + "\n</VinceExcelConfig>\n")


# EQ only - native-safe, untouched by this routing.
NATIVE_BLOCK = """
<Import Label="Native Block" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="OIS017MI" Transaction="AddPriceList">
      <InputField Name="PRRF" Value="*B" Label="Pricelist"/>
    </M3Api>
    <RowFilterEQ>
      <RowFilter Column="C" Value="1" IsHidden="true"/>
    </RowFilterEQ>
  </ImportDetail>
</Import>"""

# A GE filter on a non-numeric value - native only emits a range as a
# NUMBER comparison (build.native_filter_problem), so this should route onto
# the generic-pipeline path. '!' is the real CustomerA shape (hundreds of them);
# a numeric GE, e.g. 100, would now stay native.
PIPELINE_BLOCK = """
<Import Label="Pipeline Block" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="OIS017MI" Transaction="AddPriceList">
      <InputField Name="PRRF" Value="*B" Label="Pricelist"/>
    </M3Api>
    <RowFilterGE>
      <RowFilter Column="C" Value="!" IsHidden="true"/>
    </RowFilterGE>
  </ImportDetail>
</Import>"""

MIXED_FILE = _xml(NATIVE_BLOCK + PIPELINE_BLOCK)


def _write(xml_text, filename="test.xml"):
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml_text)
    return tmpdir, path


def _bundle_for(xml_text, filename="test.xml"):
    tmpdir, path = _write(xml_text, filename)
    peek = peek_classic_xml(xml_text)
    entry = AppClassicXml(
        path=filename,
        label=peek["label"],
        generated_path="workflows/" + filename[:-4] + ".generated.json",
        transaction_count=peek["transactionCount"],
        total_transaction_count=peek["totalTransactionCount"],
        block_count=peek.get("blockCount", 1),
        warnings=list(peek["warnings"]),
    )
    bundle = AppBundle(root=tmpdir, name="test", classic_xml=[entry])
    return bundle


def _has_m3_api_step(body):
    return "m3_api_1" in body.get("definition", {}).get("stepConfig", {})


def _code_content(body):
    for value in body.get("definition", {}).get("stepConfig", {}).values():
        if isinstance(value, dict) and "content" in value and isinstance(value["content"], str):
            if "gatePasses" in value["content"]:
                return value["content"]
    return None


def _debug_is_false(body):
    """
    Written to a real file and read back as text, on purpose - `body`'s
    Code step content is a Python string holding a JSON literal
    (`"debug": false`), and json.dumping `body` a second time in memory
    re-escapes those inner quotes differently than round-tripping through
    an actual file does. Matching what a real app-import run would
    actually write to disk is the point, not a shortcut around it.
    """
    handle, path = tempfile.mkstemp(suffix=".json")
    os.close(handle)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        return '\\"debug\\": false' in text
    finally:
        os.unlink(path)


def test_operator_detection_finds_only_the_unrepresentable_ones():
    native_unit = parse_classic_xml_units(_write(_xml(NATIVE_BLOCK))[1])[0]
    pipeline_unit = parse_classic_xml_units(_write(_xml(PIPELINE_BLOCK))[1])[0]
    check(unrepresentable_filter_operators(native_unit) == [],
          f"EQ-only block should need nothing beyond native, got "
          f"{unrepresentable_filter_operators(native_unit)!r}")
    check(unrepresentable_filter_operators(pipeline_unit) == ["GE"],
          f"GE block should be flagged as needing the pipeline path, got "
          f"{unrepresentable_filter_operators(pipeline_unit)!r}")


def test_native_safe_block_is_built_on_the_native_path():
    bundle = _bundle_for(_xml(NATIVE_BLOCK))
    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT,
                                  write_to_folder=False, ion_api_base_url=ION_BASE_URL)
    check(len(results) == 1, f"expected 1 workflow, got {len(results)}")
    result = results[0]
    check(result.get("ok"), f"conversion failed: {result.get('error')}")
    check(result.get("buildPath") == "native",
          f"EQ-only block should stay on the native path, got {result.get('buildPath')!r}")
    check(_has_m3_api_step(result["body"]), "native-path body should have an m3_api_1 step")
    check(not any("pipeline" in w.lower() for w in result["warnings"]),
          f"an unrouted block should carry no pipeline-path warnings, got {result['warnings']}")


def test_an_oversized_native_block_is_rebuilt_on_the_pipeline_path():
    # Live refuses a too-large workflow; a native block over the limit is
    # rebuilt as a Code step. The limit is lowered here so a small fixture
    # trips it - the rule is under test, not the real threshold.
    import app_classic
    saved = app_classic.NATIVE_BODY_SIZE_LIMIT
    app_classic.NATIVE_BODY_SIZE_LIMIT = 100
    try:
        with_root = convert_classic_xml(_bundle_for(_xml(NATIVE_BLOCK)), CONNECTION, TENANT,
                                        ENVIRONMENT, write_to_folder=False,
                                        ion_api_base_url=ION_BASE_URL)[0]
        without_root = convert_classic_xml(_bundle_for(_xml(NATIVE_BLOCK)), CONNECTION, TENANT,
                                           ENVIRONMENT, write_to_folder=False)[0]
    finally:
        app_classic.NATIVE_BODY_SIZE_LIMIT = saved
    check(with_root.get("buildPath") == "pipeline" and _code_content(with_root["body"]),
          f"an oversized native block was not rebuilt as a Code step: {with_root.get('buildPath')!r}")
    check(any("too large for Live" in w for w in with_root["warnings"]),
          "the reroute does not say why it happened")
    check(without_root.get("buildPath") == "native",
          "without an ION API root the block must stay native, not fail")
    check(any("PIPELINE BUILD FAILED" in w for w in without_root["warnings"]),
          "staying native because no ION API root was given must be warned about loudly")


def test_ge_block_routes_to_pipeline_when_ion_api_root_is_given():
    bundle = _bundle_for(_xml(PIPELINE_BLOCK))
    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT,
                                  write_to_folder=False, ion_api_base_url=ION_BASE_URL)
    check(len(results) == 1, f"expected 1 workflow, got {len(results)}")
    result = results[0]
    check(result.get("ok"), f"conversion failed: {result.get('error')}")
    check(result.get("buildPath") == "pipeline",
          f"GE block with an ION API root should route to pipeline, got {result.get('buildPath')!r}")
    check(not _has_m3_api_step(result["body"]), "pipeline-path body should NOT have an m3_api_1 step")
    code = _code_content(result["body"])
    check(code is not None, "pipeline-path body should have a Code step with the row-pipeline JS")
    check(_debug_is_false(result["body"]),
          "a pipeline-routed block built by app-import must default debug to false, not true")
    check(any("routed to the generic-pipeline path" in w.lower() for w in result["warnings"]),
          f"expected a 'routed to the generic-pipeline path' warning, got {result['warnings']}")


def test_ge_block_falls_back_to_native_without_an_ion_api_root():
    bundle = _bundle_for(_xml(PIPELINE_BLOCK))
    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT,
                                  write_to_folder=False, ion_api_base_url="")
    check(len(results) == 1, f"expected 1 workflow, got {len(results)}")
    result = results[0]
    check(result.get("ok"), f"conversion should still succeed (degraded, not failed): {result.get('error')}")
    check(result.get("buildPath") == "native",
          f"without an ION API root the block must fall back to native, got {result.get('buildPath')!r}")
    check(_has_m3_api_step(result["body"]), "fallback body should still be a native-path workflow")
    check(any("pipeline build failed" in w.lower() for w in result["warnings"]),
          f"expected a loud 'PIPELINE BUILD FAILED' warning explaining the fallback, got {result['warnings']}")


def test_mixed_file_routes_each_block_independently():
    bundle = _bundle_for(MIXED_FILE)
    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT,
                                  write_to_folder=False, ion_api_base_url=ION_BASE_URL)
    check(len(results) == 2, f"expected 2 workflows (one per block), got {len(results)}")
    by_label = {r["label"]: r for r in results}
    check(by_label.get("Native Block", {}).get("buildPath") == "native",
          "the EQ-only block in a mixed file should still be native")
    check(by_label.get("Pipeline Block", {}).get("buildPath") == "pipeline",
          "the GE block in a mixed file should still be routed to pipeline, independently of its sibling")


def test_base_names_compose_and_group_key_ties_blocks_together():
    """
    Naming + grouping, added 2026-09-16 alongside Live's real Groups
    feature (liveapi.create_group). A `base_names` entry composes into
    every block's name as "<base name> - <block's own function>", and
    every AppWorkflow produced from the SAME source file gets the SAME
    `group_key` - the correlation appimport._create_groups buckets on to
    decide what gets grouped together after creation.
    """
    bundle = _bundle_for(MIXED_FILE, filename="two-blocks.xml")
    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT,
                                  write_to_folder=False, ion_api_base_url=ION_BASE_URL,
                                  base_names={"two-blocks.xml": "Customer Function"})
    check(len(results) == 2, f"expected 2 workflows, got {len(results)}")
    by_label = {r["label"]: r for r in results}
    check(by_label.get("Native Block", {}).get("name") == "Customer Function - Native Block",
          f"expected the composed name, got {by_label.get('Native Block', {}).get('name')!r}")
    check(by_label.get("Pipeline Block", {}).get("name") == "Customer Function - Pipeline Block",
          f"expected the composed name, got {by_label.get('Pipeline Block', {}).get('name')!r}")

    # The composed name has to reach the actual POST body, not just this
    # function's own result dict / AppWorkflow.name bookkeeping - a real
    # regression here once had `result["name"]` correct while the body
    # still carried the bare block Label, because build_live_workflow and
    # translate_classic_to_pipeline both read the name from `workflow.label`
    # (the IR object), which nothing was reassigning. Live then created
    # workflows under their bare, non-unique block names, colliding by name
    # across re-imports and with any unrelated workflow sharing that label.
    native_body = by_label["Native Block"]["body"]
    check(native_body["workflowName"] == "Customer Function - Native Block",
          f"the native-path POST body still carries the bare block label: {native_body['workflowName']!r}")
    check(native_body["workflowAlias"] == "Customer Function - Native Block",
          f"workflowAlias should match workflowName: {native_body['workflowAlias']!r}")

    pipeline_body = by_label["Pipeline Block"]["body"]
    check(pipeline_body["workflowName"] == "Customer Function - Pipeline Block",
          f"the pipeline-path POST body still carries the bare block label: {pipeline_body['workflowName']!r}")

    check(len(bundle.workflows) == 2, f"expected 2 AppWorkflow entries, got {len(bundle.workflows)}")
    group_keys = {w.group_key for w in bundle.workflows}
    check(group_keys == {"two-blocks.xml"},
          f"every block from one file should share the same group_key, got {group_keys!r}")


def test_no_base_names_entry_falls_back_to_the_file_stem():
    bundle = _bundle_for(_xml(NATIVE_BLOCK), filename="unmapped.xml")
    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT,
                                  write_to_folder=False, ion_api_base_url=ION_BASE_URL,
                                  base_names={})
    check(len(results) == 1, f"expected 1 workflow, got {len(results)}")
    check(results[0]["name"] == "unmapped - Native Block",
          f"a file with no base_names entry should fall back to its own stem, got {results[0]['name']!r}")


def test_stale_multi_block_output_is_not_reimported():
    tmpdir = tempfile.mkdtemp()
    workflows_dir = os.path.join(tmpdir, "workflows")
    os.makedirs(workflows_dir, exist_ok=True)
    xml_path = os.path.join(workflows_dir, "two-blocks.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(MIXED_FILE)

    # Run 1: convert and write both blocks' generated JSON to disk, exactly
    # as a real app-import does.
    bundle1 = scan_app_folder(tmpdir)
    convert_classic_xml(bundle1, CONNECTION, TENANT, ENVIRONMENT,
                        write_to_folder=True, ion_api_base_url=ION_BASE_URL,
                        base_names={"workflows/two-blocks.xml": "Run One"})
    written = sorted(os.listdir(workflows_dir))
    check(len(written) == 3,  # the .xml itself + 2 generated files
          f"expected the xml plus 2 generated files on disk, got {written}")

    # Run 2: a FRESH scan of the same folder, source XML still present.
    # Before the fix, block 2's stale "*.2-....generated.json" leaked
    # through as an unrelated plain AppWorkflow here - block 1's plain
    # "<stem>.generated.json" was already excluded correctly, which is
    # exactly why this needs a genuine 2+ block file to catch.
    bundle2 = scan_app_folder(tmpdir)
    check(len(bundle2.classic_xml) == 1,
          f"expected the source XML to still be found once, got {len(bundle2.classic_xml)}")
    check(bundle2.workflows == [],
          f"a multi-block file's stale generated output leaked back in as plain workflow(s): "
          f"{[(w.path, w.name) for w in bundle2.workflows]}")


def test_design_file_pairs_by_folder_fallback_on_stem_mismatch():
    # The real CustomerA case: "Classic_MMS001_MassUpload.xml" next
    # to "MMS001_MassUpload_1.0.xlsx" - same folder, same
    # function, but a "Classic_" prefix and a "_1.0" version suffix mean the
    # stems never match textually. Only one XML and one design file live in
    # that folder, so there is no ambiguity about which workflow the sheet
    # belongs to.
    tmpdir = tempfile.mkdtemp()
    func_dir = os.path.join(tmpdir, "func")
    os.makedirs(func_dir, exist_ok=True)
    with open(os.path.join(func_dir, "Classic_Something.xml"), "w", encoding="utf-8") as f:
        f.write(_xml(NATIVE_BLOCK))
    with open(os.path.join(func_dir, "Something_1.0.xlsx"), "wb") as f:
        f.write(b"not a real workbook, just a stand-in for size/extension matching")

    bundle = scan_app_folder(tmpdir)
    convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT, write_to_folder=True)

    design = bundle.design_files[0]
    check(design.workflow_path == "workflows/Classic_Something.generated.json",
          f"expected the mismatched-stem design file to pair by folder fallback, got "
          f"workflow_path={design.workflow_path!r}, warnings={design.warnings!r}")
    check(design.warnings == [],
          f"a successful folder-fallback pairing should carry no warning, got {design.warnings!r}")


def test_design_file_fallback_stays_unmatched_when_ambiguous():
    # Two XML files sharing a folder with one mismatched-stem design file -
    # the fallback must NOT guess which one the sheet belongs to.
    tmpdir = tempfile.mkdtemp()
    func_dir = os.path.join(tmpdir, "func")
    os.makedirs(func_dir, exist_ok=True)
    with open(os.path.join(func_dir, "Classic_One.xml"), "w", encoding="utf-8") as f:
        f.write(_xml(NATIVE_BLOCK))
    with open(os.path.join(func_dir, "Classic_Two.xml"), "w", encoding="utf-8") as f:
        f.write(_xml(NATIVE_BLOCK))
    with open(os.path.join(func_dir, "Something_1.0.xlsx"), "wb") as f:
        f.write(b"not a real workbook, just a stand-in for size/extension matching")

    bundle = scan_app_folder(tmpdir)
    convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT, write_to_folder=True)

    design = bundle.design_files[0]
    check(design.workflow_path is None,
          f"an ambiguous folder (2 XML files) must not guess a pairing, got "
          f"workflow_path={design.workflow_path!r}")
    check(design.needs_choice,
          "an ambiguous design file must be flagged for a person to choose")
    check(design.warnings != [],
          "an unmatched design file should still report why")


def _design_folder(files):
    """A temp app folder: {relative path: None for a native-block XML, else bytes}."""
    tmpdir = tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(tmpdir, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(_xml(NATIVE_BLOCK).encode("utf-8") if content is None else content)
    return tmpdir


def test_design_file_pairs_with_the_xml_in_its_own_folder_not_by_name():
    # Two functions, two folders. Each design file's name matches the OTHER
    # folder's XML - the folder must win, not the name.
    tmpdir = _design_folder({
        "a/Alpha.xml": None, "a/Beta.xlsx": b"x",
        "b/Beta.xml": None, "b/Alpha.xlsx": b"x",
    })
    bundle = scan_app_folder(tmpdir)
    targets = {d.path: (d.target, d.matched_by) for d in bundle.design_files}
    check(targets == {"a/Beta.xlsx": ("a/Alpha.xml", "folder"),
                      "b/Alpha.xlsx": ("b/Beta.xml", "folder")},
          f"design files should pair with the XML in their own folder, got {targets}")
    summary = bundle.summary()
    check({x["path"]: x["designFile"] for x in summary["classicXml"]}
          == {"a/Alpha.xml": "a/Beta.xlsx", "b/Beta.xml": "b/Alpha.xlsx"},
          f"the plan does not show each XML's design file: {summary['classicXml']}")
    convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT, write_to_folder=True)
    attached = {w.path: w.design_file.path for w in bundle.workflows if w.design_file}
    check(attached == {"workflows/Alpha.generated.json": "a/Beta.xlsx",
                       "workflows/Beta.generated.json": "b/Alpha.xlsx"},
          f"after conversion the design files are not on the XMLs' workflows: {attached}")


def test_two_xmls_in_a_folder_fall_back_to_an_exact_name():
    tmpdir = _design_folder({"f/One.xml": None, "f/Two.xml": None, "f/Two.xlsx": b"x"})
    design = scan_app_folder(tmpdir).design_files[0]
    check((design.target, design.matched_by, design.needs_choice) == ("f/Two.xml", "name", False),
          f"an exact name among the folder's XMLs should decide it, got "
          f"{design.target!r} {design.matched_by!r} needs_choice={design.needs_choice}")


def test_two_design_files_for_one_xml_ask_instead_of_guessing():
    tmpdir = _design_folder({"f/Func.xml": None, "f/Sheet_v1.xlsx": b"x", "f/Sheet_v2.xlsx": b"x"})
    bundle = scan_app_folder(tmpdir)
    check(all(d.needs_choice and d.target is None for d in bundle.design_files),
          "two design files next to one XML must both ask, not pick one: " +
          str([(d.path, d.target, d.needs_choice) for d in bundle.design_files]))


def test_a_manual_choice_beats_the_rules_and_can_skip_a_file():
    tmpdir = _design_folder({"f/Func.xml": None, "f/Sheet_v1.xlsx": b"x", "f/Sheet_v2.xlsx": b"x"})
    bundle = scan_app_folder(tmpdir)
    bundle.design_matches = {"f/Sheet_v2.xlsx": "f/Func.xml", "f/Sheet_v1.xlsx": ""}
    match_design_files(bundle)
    by_path = {d.path: d for d in bundle.design_files}
    check((by_path["f/Sheet_v2.xlsx"].target, by_path["f/Sheet_v2.xlsx"].matched_by)
          == ("f/Func.xml", "manual"),
          f"the hand-picked match was not applied: {by_path['f/Sheet_v2.xlsx']}")
    check(by_path["f/Sheet_v1.xlsx"].target is None and not by_path["f/Sheet_v1.xlsx"].needs_choice,
          f"'do not upload' should leave the file unmatched and no longer asking: "
          f"{by_path['f/Sheet_v1.xlsx']}")
    convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT, write_to_folder=True)
    attached = {w.path: w.design_file.path for w in bundle.workflows if w.design_file}
    check(attached == {"workflows/Func.generated.json": "f/Sheet_v2.xlsx"},
          f"the manual match did not survive conversion's re-match: {attached}")


_ROUTED_XML = _xml("""
<Export Label="Export Lines" TargetSheetName="Header">
  <LstApi MaxRows="100" OutputStartRow="6">
    <M3Api Program="OIS017MI" Transaction="LstBasePrice"><InputField Name="PRRF" Value="*Field1"/></M3Api>
    <Cell Column="C" Value="*ITNO"/>
  </LstApi>
</Export>
<Import Label="Import Lines" InputStartRow="6" SourceSheetName="Lines">
  <ImportDetail>
    <M3Api Program="OIS017MI" Transaction="UpdBasePrice" MessageColumn="N"><InputField Name="PRRF" Value="*A"/></M3Api>
  </ImportDetail>
</Import>""")

_SPEC = {
    "vinceforge": "pipeline/1", "name": "Lines via spec", "vxl": True, "groupWith": "prices.xml",
    "baseUrl": "https://mingle-ionapi.eu1.inforcloudsuite.com/<TENANT>",
    "trigger": {"hasSpreadSheetInput": True, "fields": [{"name": "F_PRRF", "mandatory": True}]},
    "steps": [{"kind": "m3Expand",
               "params": [{"name": "F_PRRF", "required": True}],
               "levels": [{"ref": "line", "program": "OIS017MI", "transaction": "LstBasePrice", "each": True,
                           "record": {"PRRF": {"param": "F_PRRF"}}}],
               "columns": [{"header": "Pricelist", "from": "line.PRRF"}, {"header": "Overall", "message": True}]}],
}


def test_routing_file_and_pipeline_spec_build_with_the_target():
    root = tempfile.mkdtemp(prefix="vinceforge-routing-")
    io_write = lambda rel, text: open(os.path.join(root, rel.replace("/", os.sep)), "w", encoding="utf-8").write(text)
    os.makedirs(os.path.join(root, "workflows"))
    io_write("prices.xml", _ROUTED_XML)
    io_write("prices.routing.json", json.dumps({"vinceforge": "classic-routing/1",
        "blocks": {"Export Lines": "skip", "Import Lines": "pipeline", "No Such Block": "skip"}}))
    io_write("workflows/lines.spec.json", json.dumps(_SPEC))
    # A stale build of the spec, from a run with other ids, must not be imported as well.
    io_write("workflows/lines.generated.json", json.dumps({"definition": {"workflowConfig": {"name": "stale"}}}))

    bundle = scan_app_folder(root)
    check(not any("not a recognised workflow" in w for w in bundle.warnings),
          f"a pipeline spec or routing file was treated as App Builder source: {bundle.warnings}")
    check(any("No Such Block" in w for w in bundle.warnings), "a routing entry naming no block was not reported")
    check(not bundle.workflows, f"the spec's stale generated output was picked up: {[w.path for w in bundle.workflows]}")
    check(bundle.classic_xml[0].block_count == 1, "a skipped block still counts as a workflow to create")

    results = convert_classic_xml(bundle, CONNECTION, TENANT, ENVIRONMENT, ion_api_base_url=ION_BASE_URL)
    results += convert_pipeline_specs(bundle, CONNECTION, TENANT, ENVIRONMENT, ion_api_base_url=ION_BASE_URL)
    by_label = {r["label"]: r for r in results}
    check(by_label["Export Lines"].get("skipped") is True, "a block marked 'skip' was built anyway")
    check(by_label["Import Lines"].get("buildPath") == "pipeline",
          "a block marked 'pipeline' was not built as a Code step")
    check([w.name for w in bundle.workflows] == ["prices - Import Lines", "Lines via spec"],
          f"expected exactly the routed import and the spec: {[w.name for w in bundle.workflows]}")
    spec_body = bundle.workflows[1].data
    code = spec_body["definition"]["stepConfig"]["code_1"]["content"]
    check("TEST_TENANT/M3/m3api-rest/v2/execute" in code and "<TENANT>" not in code,
          "the spec's Code step was not built against the import's ION API root")
    check(CONNECTION in code, "the spec's Code step does not carry the target connection id")
    check(bundle.workflows[1].group_key == "prices.xml", "groupWith did not put the spec in the XML's Group")


def main():
    tests = (
        test_operator_detection_finds_only_the_unrepresentable_ones,
        test_native_safe_block_is_built_on_the_native_path,
        test_ge_block_routes_to_pipeline_when_ion_api_root_is_given,
        test_an_oversized_native_block_is_rebuilt_on_the_pipeline_path,
        test_ge_block_falls_back_to_native_without_an_ion_api_root,
        test_mixed_file_routes_each_block_independently,
        test_base_names_compose_and_group_key_ties_blocks_together,
        test_no_base_names_entry_falls_back_to_the_file_stem,
        test_stale_multi_block_output_is_not_reimported,
        test_design_file_pairs_by_folder_fallback_on_stem_mismatch,
        test_design_file_fallback_stays_unmatched_when_ambiguous,
        test_design_file_pairs_with_the_xml_in_its_own_folder_not_by_name,
        test_two_xmls_in_a_folder_fall_back_to_an_exact_name,
        test_two_design_files_for_one_xml_ask_instead_of_guessing,
        test_a_manual_choice_beats_the_rules_and_can_skip_a_file,
        test_routing_file_and_pipeline_spec_build_with_the_target,
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
    print(f"All {len(tests)} app-classic routing self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
