"""
Self-test for the Classic-XML path. No test framework - same stdlib-only,
run-it-directly style as selftest_pipeline.py:

    python selftest_classic.py

Its subject is GROUPING, the multi-block unit split, and the <Cell>
parsing both depend on, because that is where this path's failures are
silent rather than loud. Nothing here throws when one of them is wrong:
the converter emits valid JSON, the POST succeeds, and the workflow does
the wrong thing in a customer's tenant - runs a once-per-group call once
per row, silently drops half a file's blocks, or keeps a gate that can
never match.

So the checks are about MEANING, not shape:

  - Every <Export>/<Import> block becomes its own unit, and a grouped
    block's <ImportHeading GroupColumns> and <StartRun> reach the IR with
    the StartRun call ordered FIRST, so the details' BasedOnTransaction
    references resolve to it.
  - <Cell> is read at ImportDetail and StartRun level, not just in the
    Export block.
  - A group-scoped call is emitted per row (Live documents a "Group by" on
    the M3 API step, but it is unconfirmed, so per-row is all this path
    emits) but always carries a review note, and the
    note distinguishes a read - same answer, more calls - from a possible
    WRITE, where per-row means one record per row instead of one per
    group.
  - A gate on a column an M3 call WRITES never reaches generic_filter_1,
    at either filter level. Live's Generic Filter runs before the M3 API
    step and sees only raw rows, so such a gate cannot work - and until
    ImportDetail cells were parsed this was undetectable.
  - The plain ungrouped single-block case is completely unchanged.

Fixtures are synthetic and inline. RUN/synthetic/classic-coverage.xml and
the two real customer files are built at the end as a smoke test; note
both real files happen to have READ-ONLY StartRuns, which is exactly why
the write case has to be written by hand here.
"""

import io
import json
import os
import sys
import tempfile

import build
from source_classic_xml import compose_workflow_name, parse_classic_xml_units, peek_classic_xml

CONNECTION = "CONNECTION-" + "a" * 32
TENANT = "TENANT-" + "b" * 32
ENVIRONMENT = "ENVIRONMENT-" + "c" * 32

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(os.path.dirname(HERE), "RUN")

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def parse(xml_text):
    """Every unit an XML text parses into."""
    handle, path = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    try:
        io.open(path, "w", encoding="utf-8").write(xml_text)
        return parse_classic_xml_units(path)
    finally:
        os.unlink(path)


def convert(xml_text, unit=0):
    """Parse and build one unit: returns (ir_workflow, live_json, warnings)."""
    workflow = parse(xml_text)[unit]
    warnings = []
    live = build.build_live_workflow(workflow, CONNECTION, TENANT, ENVIRONMENT, warnings=warnings)
    return workflow, live, warnings


def transactions_of(live):
    return live["definition"]["stepConfig"]["m3_api_1"]


def find_transaction(live, transaction_name):
    for entry in transactions_of(live).values():
        if entry["transactionName"] == transaction_name:
            return entry
    return None


def filter_columns(live):
    """The columns generic_filter_1 actually gates on, or [] if there is no such step."""
    step = live["definition"]["stepConfig"].get("generic_filter_1")
    if not step:
        return []
    from source_live_json import _filter_conditions
    return [condition["fact"] for condition in _filter_conditions(step)]


def _xml(body):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<VinceExcelConfig>\n'
            + body + "\n</VinceExcelConfig>\n")


# A grouped block whose StartRun only READS (an authority check - the
# shape both real customer files happen to have).
GROUPED_READ_ONLY = _xml("""
<Import Label="Grouped read" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportHeading GroupColumns="B C" IsCritical="true">
    <StartRun>
      <M3Api Program="AUTCHKMI" Transaction="ChkAuthority">
        <InputField Name="APGM" Value="MMS101" Source="Constant" Label="Check program"/>
      </M3Api>
      <Cell Value="*ALO1" Column="P" Type="decimal"/>
    </StartRun>
  </ImportHeading>
  <ImportDetail IsCritical="true">
    <M3Api Program="AUTCHKMI" Transaction="ChkAutTblRcd">
      <BasedOnTransaction TransactionName="AUTCHKMI.ChkAuthority" TransactionFailed="false"/>
      <InputField Name="FACI" Value="*B" Label="Facility"/>
    </M3Api>
    <Cell Value="*TX05" Column="Q" Type="string"/>
  </ImportDetail>
  <ImportDetail IsCritical="true">
    <M3Api Program="MMZ101MI" Transaction="AddLine">
      <InputField Name="ALO1" Value="*AUTCHKMI.ChkAuthority.ALO1" Label="Authority"/>
    </M3Api>
    <RowFilterEQ>
      <RowFilter Column="P" Value="1" IsHidden="true"/>
      <RowFilter Column="Q" Value="YES" IsHidden="true"/>
    </RowFilterEQ>
  </ImportDetail>
</Import>""")

# The same block with a WRITING StartRun. No real customer file has this
# yet, and it is the case with teeth: run per row, an Add* creates one
# record per row where Classic created one per group.
GROUPED_WRITING = _xml("""
<Import Label="Grouped write" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportHeading GroupColumns="B C" IsCritical="true">
    <StartRun>
      <M3Api Program="MMZ101MI" Transaction="AddHead">
        <InputField Name="FACI" Value="*B" Label="Facility"/>
      </M3Api>
      <Cell Value="*TRNR" Column="P" Type="string"/>
    </StartRun>
  </ImportHeading>
  <ImportDetail IsCritical="true">
    <M3Api Program="MMZ101MI" Transaction="AddLine">
      <InputField Name="TRNR" Value="*MMZ101MI.AddHead.TRNR" Label="Order number"/>
    </M3Api>
  </ImportDetail>
</Import>""")

UNGROUPED = _xml("""
<Import Label="Plain" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="MMS200MI" Transaction="Get">
      <InputField Name="ITNO" Value="*F" Label="Item number"/>
    </M3Api>
    <Cell Value="*ITDS" Column="H" Type="string"/>
  </ImportDetail>
</Import>""")

TWO_BLOCKS = _xml("""
<Import Label="Check Data" InputStartRow="3" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="MMS200MI" Transaction="Get">
      <InputField Name="ITNO" Value="*F" Label="Item number"/>
    </M3Api>
  </ImportDetail>
</Import>
<Import Label="Create RO" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="MMZ101MI" Transaction="AddHead">
      <InputField Name="FACI" Value="*B" Label="Facility"/>
    </M3Api>
  </ImportDetail>
</Import>""")

# One block EQ-only (native-safe), one with a GE on a non-numeric value
# (native can't do it) - for peek_classic_xml's per-block pipeline-routing
# PREDICTION.
TWO_BLOCKS_MIXED_FILTERS = _xml("""
<Import Label="Native Safe" InputStartRow="3" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="MMS200MI" Transaction="Get">
      <InputField Name="ITNO" Value="*F" Label="Item number"/>
    </M3Api>
    <RowFilterEQ>
      <RowFilter Column="C" Value="1" IsHidden="true"/>
    </RowFilterEQ>
  </ImportDetail>
</Import>
<Import Label="Needs Pipeline" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="MMZ101MI" Transaction="AddHead">
      <InputField Name="FACI" Value="*B" Label="Facility"/>
    </M3Api>
    <RowFilterGE>
      <RowFilter Column="D" Value="!" IsHidden="true"/>
    </RowFilterGE>
  </ImportDetail>
</Import>""")


# Shaped on CustomerB's "Sales Price List" (2026-10-16): the first Export and
# first Import are on DIFFERENT sheets, an Export carries three enrichment
# calls, a constant Cell, and an <Expand> reads one sheet to write another.
CUSTOMER_B_SHAPED = _xml("""
<Export Label="Export Header" TargetSheetName="Header">
  <LstApi MaxRows="100" OutputStartRow="6">
    <M3Api Program="OIS017MI" Transaction="LstPriceList"/>
    <Cell Column="A" Value="1"/>
    <Cell Column="B" Value="*PRRF"/>
  </LstApi>
</Export>
<Export Label="Export Lines" TargetSheetName="Lines">
  <LstApi MaxRows="100" OutputStartRow="6">
    <M3Api Program="OIS017MI" Transaction="LstBasePrice">
      <InputField Name="PRRF" Value="*Field1"/>
    </M3Api>
    <Cell Column="C" Value="*ITNO"/>
  </LstApi>
  <GetApi>
    <M3Api Program="MMS200MI" Transaction="GetItmBasic">
      <InputField Name="ITNO" Value="*OIS017MI.LstBasePrice.ITNO"/>
    </M3Api>
    <Cell Column="D" Value="*ITDS"/>
  </GetApi>
  <GetApi>
    <M3Api Program="MMS025MI" Transaction="GetAlias">
      <InputField Name="ITNO" Value="*OIS017MI.LstBasePrice.ITNO"/>
    </M3Api>
    <Cell Column="K" Value="*POPN"/>
  </GetApi>
  <GetApi>
    <M3Api Program="MMS015MI" Transaction="Get">
      <InputField Name="ITNO" Value="*OIS017MI.LstBasePrice.ITNO"/>
    </M3Api>
    <Cell Column="L" Value="*COFA"/>
  </GetApi>
</Export>
<Import Label="Import Lines" InputStartRow="6" SourceSheetName="Lines">
  <ImportDetail>
    <M3Api Program="OIS017MI" Transaction="UpdBasePrice">
      <InputField Name="PRRF" Value="*A"/>
    </M3Api>
  </ImportDetail>
</Import>
<Expand Label="Expand lines" TargetSheetName="Lines" SourceSheetName="Header" InputStartRow="6">
  <LstApi OutputStartRow="6" MaxRows="100">
    <M3Api Program="OIS017MI" Transaction="LstBasePrice">
      <InputField Name="PRRF" Value="*B"/>
    </M3Api>
    <Cell Column="C" Value="*ITNO"/>
  </LstApi>
  <LookupApi>
    <M3Api Program="MMS200MI" Transaction="GetItmBasic">
      <InputField Name="ITNO" Value="*OIS017MI.LstBasePrice.ITNO"/>
    </M3Api>
    <Cell Column="D" Value="*ITDS"/>
  </LookupApi>
</Expand>""")


def test_customer_b_shaped_file_loses_nothing_silently():
    units = parse(CUSTOMER_B_SHAPED)
    check([u.label for u in units] == ["Export Header", "Export Lines", "Import Lines"],
          "an Export and an Import on DIFFERENT sheets were paired into one workflow, or a "
          f"block went missing: {[u.label for u in units]}")
    lines = units[1]
    check([t.transaction for t in lines.transactions] == ["LstBasePrice", "GetItmBasic", "GetAlias", "Get"],
          "an Export's second and later <GetApi> were dropped: "
          f"{[t.transaction for t in lines.transactions]}")
    header = units[0].transactions[0]
    check([c.column for c in header.cells] == ["B"],
          f"a constant Cell (Value=\"1\") was read as an M3 field: {[c.field_name for c in header.cells]}")

    summary = peek_classic_xml(CUSTOMER_B_SHAPED)
    check(summary["blockCount"] == 3, f"peek reported {summary['blockCount']} workflows, expected 3")
    check(summary["transactionCount"] == 6 and summary["totalTransactionCount"] == 8,
          "peek counted the <Expand> block's calls as read: "
          f"{summary['transactionCount']} of {summary['totalTransactionCount']}")
    text = " ".join(summary["warnings"])
    check("<Expand> block(s) NOT CONVERTED" in text and "Expand lines" in text,
          "an <Expand> block is not reported by the scan, so it would vanish without a word")
    check("Constant cell(s) NOT WRITTEN" in text, "a constant Cell is not reported by the scan")
    check("different sheets" in text, "the unpaired first Export/Import is not reported")

    # The paired case still pairs: same sheet, one workflow.
    same_sheet = parse(CUSTOMER_B_SHAPED.replace('TargetSheetName="Header"', 'TargetSheetName="Lines"'))
    check(same_sheet[0].label == "Export Header" and len(same_sheet) == 2,
          "a first Export and first Import on the SAME sheet are no longer paired")


def test_every_block_becomes_its_own_unit():
    units = parse(TWO_BLOCKS)
    check(len(units) == 2, f"a two-block file parsed into {len(units)} unit(s), expected 2")
    check([u.label for u in units] == ["Check Data", "Create RO"],
          f"units are not named after their own block Labels: {[u.label for u in units]}")
    # Each block states its own data row; merging them would silently pick one.
    check([u.input_start_row for u in units] == [3, 4],
          "a unit did not keep its own block's InputStartRow")


def test_grouping_reaches_the_ir():
    workflow, _, _ = convert(GROUPED_READ_ONLY)
    check(workflow.group_columns == ["B", "C"],
          f"GroupColumns parsed as {workflow.group_columns}, expected ['B', 'C']")

    start_runs = [t for t in workflow.transactions if t.runs_once_per_group]
    check(len(start_runs) == 1,
          f"expected exactly 1 group-scoped transaction, got {len(start_runs)}")
    check(bool(start_runs) and start_runs[0].transaction == "ChkAuthority",
          "the StartRun's M3Api was not the transaction flagged runs_once_per_group")
    check(sum(1 for t in workflow.transactions if not t.runs_once_per_group) == 2,
          "an ImportDetail transaction was flagged as running once per group")


def test_start_run_comes_first_so_references_resolve():
    workflow, live, _ = convert(GROUPED_READ_ONLY)
    check(workflow.transactions[0].transaction == "ChkAuthority",
          "the StartRun call must be ordered before the ImportDetails, or Classic's "
          "document-order reference resolution points at nothing")

    chk_table = next(t for t in workflow.transactions if t.transaction == "ChkAutTblRcd")
    check(chk_table.based_on == workflow.transactions[0].ir_id,
          "BasedOnTransaction naming the StartRun call did not resolve to it")
    check(chk_table.needs_review is None,
          f"unexpected review note on a resolvable dependency: {chk_table.needs_review}")

    # ... and an input reading the StartRun's output becomes a real API reference.
    add_line = find_transaction(live, "AddLine")
    check(add_line["input"]["ALO1"].get("ref", {}).get("type") == "OUTPUT",
          "an input reading the StartRun call's output did not become an API reference")


def test_cells_are_read_at_every_level():
    workflow, _, _ = convert(GROUPED_READ_ONLY)
    by_name = {t.transaction: t for t in workflow.transactions}
    check([(c.column, c.field_name) for c in by_name["ChkAuthority"].cells] == [("P", "ALO1")],
          "the StartRun's own <Cell> was not parsed")
    check([(c.column, c.field_name) for c in by_name["ChkAutTblRcd"].cells] == [("Q", "TX05")],
          "an ImportDetail-level <Cell> was not parsed")


def test_a_read_only_start_run_says_it_now_runs_per_row():
    _, live, warnings = convert(GROUPED_READ_ONLY)
    check(find_transaction(live, "ChkAuthority") is not None,
          "the StartRun call was not emitted at all - dropping it is what left "
          "BasedOnTransaction references dangling")
    check(any("GROUPING NOT REPRESENTED" in w for w in warnings),
          "the workflow-level grouping divergence was not warned about")

    note = find_transaction(live, "ChkAuthority").get("_needs_review", "")
    check("RUNS ONCE PER GROUP" in note,
          "the group-scoped transaction carries no review note")
    check("read-only" in note,
          "a read-only StartRun is not identified as such, so the reader cannot tell this one "
          "is merely wasteful rather than dangerous")


def test_a_writing_start_run_is_called_out_as_dangerous():
    _, live, _ = convert(GROUPED_WRITING)
    add_head = find_transaction(live, "AddHead")
    check(add_head is not None, "the group-scoped transaction was not emitted")

    note = add_head.get("_needs_review", "")
    check("NOT a recognised read-only transaction" in note,
          "a possibly-WRITING StartRun is not distinguished from a read-only one - per row it "
          "creates one record per row where Classic created one per group")
    check("PER ROW" in note,
          "the note does not say what the per-row consequence actually is")


def test_gate_on_an_m3_written_column_never_reaches_the_filter_step():
    _, live, _ = convert(GROUPED_READ_ONLY)
    check(filter_columns(live) == [],
          "a gate on columns P/Q reached generic_filter_1, but both hold an M3 call's output "
          "and that step only ever sees raw spreadsheet rows")
    note = find_transaction(live, "AddLine").get("_needs_review", "")
    check("M3 call's output" in note,
          "the affected transaction does not say WHY its gate could not be translated")


def test_uniform_gate_is_still_blocked_when_it_reads_an_m3_column():
    # Every transaction shares the gate, so the uniform fast path would
    # normally emit it - but the column is written by one of them.
    xml = _xml("""
<Import Label="Uniform on an API column" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="AUTCHKMI" Transaction="ChkAuthority"/>
    <Cell Value="*ALO1" Column="Z" Type="decimal"/>
    <RowFilterEQ><RowFilter Column="Z" Value="1"/></RowFilterEQ>
  </ImportDetail>
  <ImportDetail>
    <M3Api Program="MMZ101MI" Transaction="AddHead"/>
    <RowFilterEQ><RowFilter Column="Z" Value="1"/></RowFilterEQ>
  </ImportDetail>
</Import>""")
    _, live, _ = convert(xml)
    check(filter_columns(live) == [],
          "a uniform gate was emitted even though its column holds an M3 call's output")


def test_block_level_gate_on_a_real_column_still_works():
    xml = _xml("""
<Import Label="Block gate" InputStartRow="4" SourceSheetName="Sheet1">
  <RowFilterEQ><RowFilter Column="BH" Value="OK"/></RowFilterEQ>
  <ImportDetail>
    <M3Api Program="MMS200MI" Transaction="Get"/>
    <Cell Value="*ITDS" Column="Z" Type="string"/>
  </ImportDetail>
</Import>""")
    _, live, _ = convert(xml)
    check(filter_columns(live) == ["BH"],
          "a block gate on a genuine spreadsheet column stopped reaching generic_filter_1 - "
          "the M3-written-column guard is too broad")

    # ... and the same gate moved onto the M3-written column does not.
    _, live, warnings = convert(xml.replace('Column="BH" Value="OK"', 'Column="Z" Value="OK"'))
    check(filter_columns(live) == [],
          "a block gate on an M3-written column reached generic_filter_1 - it would filter on a "
          "cell that is always empty at the moment the step runs")
    check(any("M3-WRITTEN COLUMN" in w for w in warnings),
          "a block gate dropped for reading an M3-written column was not warned about")


def test_ungrouped_workflows_are_untouched():
    workflow, live, warnings = convert(UNGROUPED)
    check(workflow.group_columns == [], "a file with no ImportHeading reported group columns")
    check(not any(t.runs_once_per_group for t in workflow.transactions),
          "a transaction in an ungrouped file was flagged as running once per group")
    check(not any("GROUP" in w for w in warnings),
          f"an ungrouped file produced a grouping warning: {warnings}")
    check(find_transaction(live, "Get") is not None, "the ordinary transaction went missing")
    check(live["definition"]["workflowConfig"]["hasSpreadSheetInput"] is True,
          "an ImportDetail <Cell> no longer counts as spreadsheet output")


def test_peek_reports_grouping_and_fan_out_before_anything_is_created():
    summary = peek_classic_xml(GROUPED_READ_ONLY)
    check(summary is not None, "a grouped Classic file was not recognised as Classic")
    check(summary["transactionCount"] == 3,
          f"peek counted {summary['transactionCount']} readable calls, expected 3 - the "
          "StartRun call is parsed now and must be counted")
    check(any("once per group" in w for w in summary["warnings"]),
          "peek does not mention grouping, so it would not reach the app-import plan or the "
          "confirmation box")

    two = peek_classic_xml(TWO_BLOCKS)
    check(two["blockCount"] == 2,
          f"peek reported blockCount {two['blockCount']} for a two-block file - this is the "
          "fan-out an app import is about to create")


def test_peek_predicts_per_block_pipeline_routing_from_a_real_path():
    """
    peek_classic_xml(text) alone (no path) cannot say WHICH block of a
    multi-block file will auto-route to the generic-pipeline path - that
    needs a real parse. Given a path it can, and this is what lets the
    app-import SCAN (and therefore the confirmation box shown before a
    real, non-dry import) show routing before anything is even converted,
    let alone created - see app_classic.py / CLAUDE.md's App import
    section for why that gap mattered.
    """
    handle, path = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    try:
        io.open(path, "w", encoding="utf-8").write(TWO_BLOCKS_MIXED_FILTERS)

        text_only = peek_classic_xml(TWO_BLOCKS_MIXED_FILTERS)
        check(text_only["pipelineRoutedBlocks"] == [],
              "without a path, peek must not GUESS at per-block routing - it has no real parse "
              f"to base it on, got {text_only['pipelineRoutedBlocks']!r}")

        with_path = peek_classic_xml(TWO_BLOCKS_MIXED_FILTERS, path=path)
        routed = with_path["pipelineRoutedBlocks"]
        check(len(routed) == 1,
              f"expected exactly 1 of 2 blocks predicted to route, got {routed!r}")
        check(bool(routed) and routed[0]["label"] == "Needs Pipeline",
              f"the EQ-only block should not be predicted to route, got {routed!r}")
        check(bool(routed) and routed[0]["operators"] == ["GE"],
              f"expected the GE operator named, got {routed!r}")
        check(any("auto-route onto the generic-pipeline path" in w for w in with_path["warnings"])
              and any("Needs Pipeline" in w for w in with_path["warnings"]),
              "the prediction must land in peek's own warnings, or it never reaches the "
              f"app-import plan or confirmation box - got {with_path['warnings']}")
    finally:
        os.unlink(path)


def test_compose_workflow_name():
    check(compose_workflow_name("Creation_CentralRef", "Create RO") == "Creation_CentralRef - Create RO",
          "base name + function should compose with ' - '")
    check(compose_workflow_name("", "Create RO") == "Create RO",
          "an empty base name should leave the function label untouched")
    check(compose_workflow_name("Same Name", "Same Name") == "Same Name",
          "an identical base name and function label should not repeat themselves")
    check(compose_workflow_name("Only Base", "") == "Only Base",
          "an empty function label should leave the base name untouched")
    check(compose_workflow_name(None, None) == "Untitled",
          "both halves missing should fall back to a placeholder, not crash")
    check(compose_workflow_name("  Padded  ", "  Func  ") == "Padded - Func",
          "both halves should be stripped before composing")


def test_the_repo_fixtures_and_real_files_still_build():
    candidates = [
        os.path.join(RUN_DIR, "synthetic", "classic-coverage.xml"),
        os.path.join(RUN_DIR, "C05_MMS100_Destruction_Order_ADD_1.4.xml"),
        os.path.join(RUN_DIR, "Existing Price Lists.xml"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue      # RUN/ holds run artifacts - absence is not a failure
        name = os.path.basename(path)
        units = parse_classic_xml_units(path)
        check(bool(units), f"{name} parsed into no units at all")
        for unit in units:
            warnings = []
            live = build.build_live_workflow(unit, CONNECTION, TENANT, ENVIRONMENT,
                                              warnings=warnings)
            check(bool(transactions_of(live)) or not unit.transactions,
                  f"{name} unit {unit.label!r} built with no transactions")
            check(not any(t.needs_review and "Could not resolve dependency" in t.needs_review
                          for t in unit.transactions),
                  f"{name} unit {unit.label!r} has an unresolvable dependency - a StartRun call "
                  "it references should now be parsed")


def test_filter_operator_values_match_the_real_capture():
    # The operator strings are only trustworthy because they were read off a
    # real UI-built workflow - so check them against that file, not memory.
    path = os.path.join(RUN_DIR, "captures", "generic-filter-all-operators.json")
    with open(path, encoding="utf-8") as f:
        captured = set(json.load(f)["operator_by_column"].values())
    for classic, live in build.CAPTURED_FILTER_OPERATORS.items():
        check(live in captured, f"{classic} -> {live!r} is not an operator value in the real capture")
    check(set(build.REPRESENTABLE_FILTER_OPERATORS.items()) <= set(build.CAPTURED_FILTER_OPERATORS.items()),
          "an emitted filter operator has no captured JSON value behind it")


def test_filter_nesting_matches_the_ui_and_parses_back():
    # build.py must lay conditions out exactly as Live's UI does, and a real
    # UI-built filter must parse back without a nested block posing as a
    # condition - both checked against the real captures, not a hand copy.
    from source_live_json import _filter_conditions

    def shape(node):
        key = "all" if "all" in node else "any"
        return {key: [shape(i) if ("all" in i or "any" in i) else i["fact"] for i in node[key]]}

    with open(os.path.join(RUN_DIR, "captures", "generic-filter-all-operators.json"), encoding="utf-8") as f:
        real = json.load(f)["generic_filter_1"]
    facts = [c["fact"] for c in _filter_conditions_unchecked(real)]
    ours = build._nest_filter_conditions([{"fact": fact} for fact in facts])
    check(shape(ours) == shape(real), f"nesting differs from the UI's: {shape(ours)}")
    check(build._nest_filter_conditions([{"fact": "A"}, {"fact": "B"}]) == {"all": [{"fact": "A"}, {"fact": "B"}]},
          "two conditions must stay one flat 'all' (the earlier confirmed shape)")

    with open(os.path.join(RUN_DIR, "captures", "generic-filter-datatypes-and-or.json"), encoding="utf-8") as f:
        with_or = json.load(f)["generic_filter_1"]
    try:
        _filter_conditions(with_or)
        check(False, "an OR filter parsed silently - the IR cannot carry OR")
    except ValueError:
        pass
    try:
        _filter_conditions(real)       # has isBlank / stringContains
        check(False, "isBlank/stringContains parsed silently - no Classic equivalent")
    except ValueError:
        pass
    ranged = {"all": [{"all": [_cond("A", "equal"), _cond("B", "greaterThanInclusive")]}, _cond("C", "lessThan")]}
    parsed = [(c["fact"], c["operator"]) for c in _filter_conditions(ranged)]
    check(parsed == [("A", "equal"), ("B", "greaterThanInclusive"), ("C", "lessThan")],
          f"nested range filter parsed wrong: {parsed}")


def _cond(fact, operator):
    return {"fact": fact, "operator": operator, "logicalType": "AND", "defaultValue": "1"}


def _filter_conditions_unchecked(node):
    out = []
    for item in node.get("all", node.get("any", [])):
        out.extend(_filter_conditions_unchecked(item) if ("all" in item or "any" in item) else [item])
    return out


def test_numeric_range_and_hidden_filters_go_native_in_the_captured_shape():
    # A numeric range is emitted as a NUMBER comparison, and a filter Classic
    # hides is a CONSTANT condition with no trigger field - both shapes
    # checked against the real run-confirmed capture, not a hand copy.
    with open(os.path.join(RUN_DIR, "captures", "generic-filter-constant-and-run.json"),
              encoding="utf-8") as f:
        real = json.load(f)["generic_filter_1"]
    conditions_of = _filter_conditions_unchecked
    real_by_fact = {c["fact"]: c for c in conditions_of(real)}

    xml = _xml("""
<Import Label="Range" InputStartRow="4" SourceSheetName="Sheet1">
  <RowFilterGE><RowFilter Column="C" Value="20" IsHidden="true"/></RowFilterGE>
  <RowFilterLE><RowFilter Column="E" Value="80"/></RowFilterLE>
  <RowFilterEQ><RowFilter Column="D" Value="OK"/></RowFilterEQ>
  <ImportDetail>
    <M3Api Program="MMS200MI" Transaction="Get"><InputField Name="ITNO" Value="*B"/></M3Api>
  </ImportDetail>
</Import>""")
    _, live, warnings = convert(xml)
    definition = live["definition"]
    by_fact = {c["fact"]: c for c in conditions_of(definition["stepConfig"].get("generic_filter_1", {}))}
    check(sorted(by_fact) == ["C", "D", "E"], f"expected C, D and E gated natively, got {sorted(by_fact)}")
    if sorted(by_fact) != ["C", "D", "E"]:
        return
    check(by_fact["C"]["operator"] == "greaterThanInclusive" and by_fact["E"]["operator"] == "lessThanInclusive",
          "GE/LE did not map to their captured operator values")
    check(by_fact["C"]["dataType"] == by_fact["E"]["dataType"] == real_by_fact["C"]["dataType"] == "NUMBER",
          "a numeric range must be a NUMBER comparison - STRING may compare lexically")
    check(by_fact["D"]["dataType"] == "STRING", "EQ/NE must stay STRING, unchanged from before")

    # The hidden one: the captured CONSTANT shape, key for key.
    hidden, real_constant = by_fact["C"], real_by_fact["D"]
    check(set(hidden) == set(real_constant), f"CONSTANT condition keys {sorted(hidden)} differ from "
          f"the capture's {sorted(real_constant)}")
    check(hidden["source"] == "CONSTANT" and hidden["value"] == hidden["defaultValue"] == "20",
          f"a hidden filter must be CONSTANT with its literal in value and defaultValue, got {hidden}")
    # The visible ones: editable CLIENT fields, as before.
    check(by_fact["E"]["source"] == "CLIENT" and by_fact["E"]["value"] == "{{ header.Filter_E }}",
          "a visible filter must stay an editable CLIENT field")

    field_ids = [f["fieldId"] for f in definition["stepConfig"]["trigger_1"]["fields"]]
    inputs = definition["workflowConfig"]["inputs"]
    check("Filter_C" not in field_ids and "Filter_C" not in inputs,
          "a CONSTANT filter got a trigger field / workflow input - the capture has neither")
    check("Filter_E" in field_ids and inputs.get("Filter_E", {}).get("dataType") == "NUMBER",
          "a visible NUMBER filter's trigger input must carry dataType NUMBER, as the capture does")
    check(not any("UNREPRESENTABLE" in w for w in warnings),
          f"a numeric range filter was still reported as unrepresentable: {warnings}")


def test_a_non_numeric_range_stays_off_the_native_filter():
    # '!' is the real CustomerA shape (hundreds of GE/GT '!'). No run confirms
    # what a range does on text, so it must stay reported, not emitted.
    check(build.native_filter_problem("GE", "20") is None, "a numeric GE should be native")
    check(build.native_filter_problem("LT", "05") is None, "a zero-padded numeric LT should be native")
    for value in ("!", "", None, "1e5", "A"):
        check(build.native_filter_problem("GE", value) is not None,
              f"GE {value!r} was accepted as native - only a plain number is confirmed")
    check(build.native_filter_problem("EQ", "!") is None, "EQ takes any value, as before")

    xml = _xml("""
<Import Label="Bang" InputStartRow="4" SourceSheetName="Sheet1">
  <RowFilterGE><RowFilter Column="C" Value="!" IsHidden="true"/></RowFilterGE>
  <ImportDetail><M3Api Program="MMS200MI" Transaction="Get"/></ImportDetail>
</Import>""")
    _, live, warnings = convert(xml)
    check(filter_columns(live) == [], "a GE '!' filter reached generic_filter_1")
    check(any("UNREPRESENTABLE BLOCK-LEVEL FILTER" in w and "non-numeric" in w for w in warnings),
          f"a dropped GE '!' filter was not warned about with its reason: {warnings}")


def main():
    tests = (
        test_every_block_becomes_its_own_unit,
        test_customer_b_shaped_file_loses_nothing_silently,
        test_grouping_reaches_the_ir,
        test_start_run_comes_first_so_references_resolve,
        test_cells_are_read_at_every_level,
        test_a_read_only_start_run_says_it_now_runs_per_row,
        test_a_writing_start_run_is_called_out_as_dangerous,
        test_gate_on_an_m3_written_column_never_reaches_the_filter_step,
        test_uniform_gate_is_still_blocked_when_it_reads_an_m3_column,
        test_block_level_gate_on_a_real_column_still_works,
        test_ungrouped_workflows_are_untouched,
        test_peek_reports_grouping_and_fan_out_before_anything_is_created,
        test_peek_predicts_per_block_pipeline_routing_from_a_real_path,
        test_compose_workflow_name,
        test_filter_operator_values_match_the_real_capture,
        test_filter_nesting_matches_the_ui_and_parses_back,
        test_numeric_range_and_hidden_filters_go_native_in_the_captured_shape,
        test_a_non_numeric_range_stays_off_the_native_filter,
        test_the_repo_fixtures_and_real_files_still_build,
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
    print(f"All {len(tests)} Classic-XML self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
