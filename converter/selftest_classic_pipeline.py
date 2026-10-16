"""
Self-test for the CLASSIC -> GENERIC PIPELINE translator. No test
framework - same stdlib-only, run-it-directly style as its two siblings:

    python selftest_classic_pipeline.py

Its subject is CONTROL FLOW, because that is the only reason this path
exists and every way it can be wrong is silent. A mistranslated condition
produces valid JSON, saves cleanly, and then calls M3 for rows Classic
would have skipped - or, in the case that has teeth, runs a create that
Classic ran only when a Get had FAILED. Nothing throws.

So the checks are about MEANING:

  - `<BasedOnTransaction TransactionFailed="false">` becomes a gate on the
    producer having succeeded, and `="true"` one on it having FAILED. The
    second is the get-or-create idiom ("Add Price List" adds only when
    GetPriceList failed); the two swapped would double-create in a
    customer's tenant.
  - A `<RowFilter>` on a column an earlier M3 call WRITES resolves to that
    call's own result - the whole point of this path, since Live's Generic
    Filter can never see it - and to the NEAREST EARLIER writer, by its
    occurrence-suffixed ref. `AUTCHKMI.ChkAuthority` appears twice in one
    real block, writing two different columns.
  - A `<RowFilter>` on a column nothing writes stays a plain row value.
  - An `<Import>`-level filter becomes a ROW gate (it skips the row), not a
    gate repeated onto every call.
  - A `<StartRun>` reaches the step as a once-per-group call, with the
    group columns beside it.
  - A filter this path will not represent - an unknown operator, or the
    real file's `GT` against an empty value - is NEVER emitted and IS
    reported.
  - An `<Export>`-only block is refused, pointing at build.py, rather than
    bent into a row pipeline; in a mixed block the Export call is left out
    and said out loud.
  - The built Live JSON is a Code step under the trigger, with no excel_1
    and the VXL label.

The last section EXECUTES the generated Code step against a fake M3 under
node, which is the only way to check the semantics rather than the shape:
that a group's later rows still run, that a chain stops at its own break
and not further, and that get-or-create fires exactly once. It is skipped
with a message when node is not installed, so this file stays runnable
with the stdlib alone.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

from build_pipeline import build_pipeline_workflow, pipeline_warnings
from classic_to_pipeline import (
    ClassicPipelineError,
    GROUP_RESULT_SCOPE,
    translate_classic_to_pipeline,
)
from m3_rest import render_row_pipeline_js
from pipeline_ir import PipelineSpecError
from source_classic_xml import parse_classic_xml_units

CONNECTION = "CONNECTION-" + "a" * 32
TENANT = "TENANT-" + "b" * 32
ENVIRONMENT = "ENVIRONMENT-" + "c" * 32
BASE_URL = "https://mingle-ionapi.eu1.inforcloudsuite.com/SELFTEST"

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(os.path.dirname(HERE), "RUN")

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def parse(xml_text):
    handle, path = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    try:
        io.open(path, "w", encoding="utf-8").write(xml_text)
        return parse_classic_xml_units(path)
    finally:
        os.unlink(path)


def translate(xml_text, unit=0, **kwargs):
    """Parse and translate one unit: returns (pipeline, step, warnings)."""
    workflow = parse(xml_text)[unit]
    pipeline, warnings = translate_classic_to_pipeline(
        workflow, base_url=BASE_URL, connection_id=CONNECTION, **kwargs)
    return pipeline, pipeline.steps[0], warnings


def call_named(step, ref):
    for call in step.calls:
        if call.ref == ref:
            return call
    return None


def gate_tuples(call_or_step):
    gates = getattr(call_or_step, "gates", None)
    if gates is None:
        gates = call_or_step.row_gates
    return [(gate.left, gate.op, gate.right) for gate in gates]


def mentions(warnings, *needles):
    return any(all(needle in warning for needle in needles) for warning in warnings)


def _xml(body):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<VinceExcelConfig>\n'
            + body + "\n</VinceExcelConfig>\n")


# The get-or-create chain, trimmed out of RUN/Existing Price Lists.xml:
# a group-scoped call, an authority check writing a column, a Get gated on
# that column, and an Add that runs only when the Get FAILED. Two
# occurrences of ChkAuthority, writing different columns, so a resolved
# filter has to name the right one.
CHAIN = _xml("""
<Import Label="Add Price List" InputStartRow="8" MessageColumn="BU" SourceSheetName="PriceList">
  <ImportHeading GroupColumns="B C" IsCritical="true">
    <StartRun>
      <M3Api Program="MNS150MI" Transaction="SelUsers">
        <InputField Name="FRES" Value="*USERID" Source="UserID" Label="From User"/>
      </M3Api>
    </StartRun>
  </ImportHeading>
  <ImportDetail IsCritical="true">
    <M3Api Program="AUTCHKMI" Transaction="ChkAuthority">
      <BasedOnTransaction TransactionName="MNS150MI.SelUsers" TransactionFailed="false"/>
      <InputField Name="APGM" Value="OIS017" Source="Constant" Label="Check program"/>
    </M3Api>
    <Cell Value="*ALO2" Column="BK" Type="decimal"/>
  </ImportDetail>
  <ImportDetail IsCritical="false">
    <M3Api Program="OIS017MI" Transaction="GetPriceList">
      <BasedOnTransaction TransactionName="AUTCHKMI.ChkAuthority" TransactionFailed="false"/>
      <InputField Name="PRRF" Value="*B" Label="Pricelist"/>
    </M3Api>
    <RowFilterEQ>
      <RowFilter Column="BK" Value="1" Label="API 2" IsHidden="true"/>
    </RowFilterEQ>
  </ImportDetail>
  <ImportDetail IsCritical="true">
    <M3Api Program="OIS017MI" Transaction="AddPriceList">
      <BasedOnTransaction TransactionName="OIS017MI.GetPriceList" TransactionFailed="true"/>
      <InputField Name="PRRF" Value="*B" Label="Pricelist"/>
      <InputField Name="CUCD" Value="*C" Label="Currency"/>
    </M3Api>
  </ImportDetail>
  <ImportDetail IsCritical="true">
    <M3Api Program="AUTCHKMI" Transaction="ChkAuthority">
      <BasedOnTransaction TransactionName="MNS150MI.SelUsers" TransactionFailed="false"/>
      <InputField Name="APGM" Value="OIS021" Source="Constant" Label="Check program"/>
    </M3Api>
    <Cell Value="*ALO2" Column="BO" Type="decimal"/>
  </ImportDetail>
  <ImportDetail IsCritical="true">
    <M3Api Program="OIS017MI" Transaction="AddBasePrice">
      <InputField Name="ITNO" Value="*AM" Label="Itemnumber"/>
    </M3Api>
    <RowFilterEQ>
      <RowFilter Column="BO" Value="1" Label="API 4" IsHidden="true"/>
    </RowFilterEQ>
    <RowFilterGT>
      <RowFilter Column="AM" Value="" Label="Item number" IncludeBlank="true" IsHidden="true"/>
    </RowFilterGT>
  </ImportDetail>
  <RowFilterEQ>
    <RowFilter Column="BH" Value="OK" Label="input valid" IsHidden="true"/>
  </RowFilterEQ>
</Import>""")

# A grouped block whose line call is gated on the GROUP-SCOPED call's own
# output column - C05_MMS100's shape, and the case GROUP_RESULT_SCOPE
# decides.
GROUPED_LINES = _xml("""
<Import Label="Create RO" InputStartRow="4" MessageColumn="AF" SourceSheetName="Excel-template">
  <ImportHeading GroupColumns="B C" IsCritical="true">
    <StartRun>
      <M3Api Program="AUTCHKMI" Transaction="ChkAuthority">
        <InputField Name="APGM" Value="MMS101" Source="Constant" Label="Check program"/>
      </M3Api>
      <Cell Value="*ALO1" Column="P" Type="decimal"/>
    </StartRun>
  </ImportHeading>
  <ImportDetail IsCritical="true">
    <M3Api Program="MMZ101MI" Transaction="AddLine">
      <InputField Name="WHLO" Value="*C" Label="Warehouse"/>
      <InputField Name="ITNO" Value="*F" Label="Item number"/>
    </M3Api>
    <RowFilterEQ>
      <RowFilter Column="P" Value="1" IsHidden="true"/>
    </RowFilterEQ>
  </ImportDetail>
</Import>""")

EXPORT_ONLY = _xml("""
<Export Label="Extract Price Lists" TargetSheetName="Existing Price Lists">
  <LstApi MaxRows="10000" OutputStartRow="3">
    <M3Api Program="CMS100MI" Transaction="LstC05_OPRICH">
      <InputField Name="F_CUNO" Value="*Field1" Label="From Customer"/>
    </M3Api>
    <Cell Column="A" Value="*OJPRRF"/>
  </LstApi>
</Export>""")

# The first <Export> and the first <Import> share unit 1 (see
# source_classic_xml._parse_raw_units), so this is the real mixed shape.
MIXED = _xml("""
<Export Label="Extract" TargetSheetName="Sheet1">
  <LstApi MaxRows="100" OutputStartRow="3">
    <M3Api Program="CMS100MI" Transaction="LstC05_OPRICH">
      <InputField Name="F_CUNO" Value="*Field1" Label="From Customer"/>
    </M3Api>
    <Cell Column="A" Value="*OJPRRF"/>
  </LstApi>
</Export>
<Import Label="Use it" InputStartRow="8" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="OIS017MI" Transaction="AddPriceList">
      <InputField Name="OJPRRF" Value="*A" Label="Pricelist"/>
      <InputField Name="CUCD" Value="*C" Label="Currency"/>
    </M3Api>
  </ImportDetail>
</Import>""")

UNKNOWN_OPERATOR = _xml("""
<Import Label="Odd filter" InputStartRow="4" SourceSheetName="Sheet1">
  <ImportDetail>
    <M3Api Program="MMS200MI" Transaction="Get">
      <InputField Name="ITNO" Value="*F" Label="Item number"/>
    </M3Api>
    <RowFilterBETWEEN>
      <RowFilter Column="K" Value="5" IsHidden="true"/>
    </RowFilterBETWEEN>
  </ImportDetail>
</Import>""")


# --- the checks -------------------------------------------------------

def test_based_on_becomes_a_call_status_gate():
    _, step, _ = translate(CHAIN)
    ok_gate = ({"callStatus": "AUTCHKMI_ChkAuthority"}, "eq", {"const": "ok"})
    check(ok_gate in gate_tuples(call_named(step, "OIS017MI_GetPriceList")),
          'BasedOnTransaction TransactionFailed="false" did not become a callStatus == "ok" gate: '
          f'{gate_tuples(call_named(step, "OIS017MI_GetPriceList"))}')

    # The one with teeth. Classic runs AddPriceList only when GetPriceList
    # FAILED; inverted, this creates a price list that already exists.
    failed_gate = ({"callStatus": "OIS017MI_GetPriceList"}, "eq", {"const": "failed"})
    add_gates = gate_tuples(call_named(step, "OIS017MI_AddPriceList"))
    check(failed_gate in add_gates,
          'the get-or-create idiom (TransactionFailed="true") did not become a '
          f'callStatus == "failed" gate: {add_gates}')
    check(({"callStatus": "OIS017MI_GetPriceList"}, "eq", {"const": "ok"}) not in add_gates,
          "the get-or-create gate is INVERTED - it would create a record Classic only created "
          "when the Get failed")


def test_filter_on_a_written_column_reads_that_call():
    _, step, _ = translate(CHAIN)
    # BK is written by the FIRST ChkAuthority, BO by the second. Resolving
    # either to the wrong occurrence silently gates on another program's
    # authority answer.
    get_gates = gate_tuples(call_named(step, "OIS017MI_GetPriceList"))
    check(({"call": "AUTCHKMI_ChkAuthority.ALO2"}, "eq", {"const": "1"}) in get_gates,
          f"a filter on column BK did not resolve to the call that writes it: {get_gates}")

    base_gates = gate_tuples(call_named(step, "OIS017MI_AddBasePrice"))
    check(({"call": "AUTCHKMI_ChkAuthority_2.ALO2"}, "eq", {"const": "1"}) in base_gates,
          "a filter on column BO resolved to the wrong occurrence of ChkAuthority - it must be "
          f"the nearest EARLIER writer: {base_gates}")

    # And the producing call must be asked for the field the gate reads.
    check("ALO2" in (call_named(step, "AUTCHKMI_ChkAuthority").selected_columns or []),
          "the producing call does not request the field a later gate reads, so M3 may not "
          "return it")


def test_block_level_filter_gates_the_row_not_a_call():
    _, step, _ = translate(CHAIN)
    check(gate_tuples(step) == [({"row": "BH"}, "eq", {"const": "OK"})],
          f"an <Import>-level filter did not become a single row gate: {gate_tuples(step)}")
    for call in step.calls:
        check(({"row": "BH"}, "eq", {"const": "OK"}) not in gate_tuples(call),
              f"the block-level filter was also copied onto call {call.ref}, which would report "
              "it as a per-call skip instead of a skipped row")


def test_grouping_reaches_the_step():
    _, step, warnings = translate(GROUPED_LINES)
    check(step.group_columns == ["B", "C"],
          f"group columns did not reach the step: {step.group_columns}")
    group_calls = [c.ref for c in step.calls if c.once_per_group]
    check(group_calls == ["AUTCHKMI_ChkAuthority"],
          f"expected exactly the StartRun call to be once-per-group, got {group_calls}")
    check(step.group_result_scope == GROUP_RESULT_SCOPE,
          "the step's group scope does not match the module's single decision point")
    check(mentions(warnings, "GROUPING", "GROUP_RESULT_SCOPE"),
          "grouping was translated without pointing at the constant that governs it")
    check(mentions(warnings, "ROW ADDRESSING"),
          "row values were emitted without saying how the column letter is looked up")


def test_untranslatable_filters_are_reported_not_emitted():
    _, step, warnings = translate(CHAIN)
    base_gates = gate_tuples(call_named(step, "OIS017MI_AddBasePrice"))
    check(not any(op == "gt" for _, op, _ in base_gates),
          f"a GT against an empty value was emitted as a gate: {base_gates}")
    check(mentions(warnings, "FILTER NOT TRANSLATED", "column AM GT"),
          "the dropped GT filter was not reported")

    _, odd_step, odd_warnings = translate(UNKNOWN_OPERATOR)
    check(gate_tuples(odd_step.calls[0]) == [],
          "an unknown Classic filter operator was approximated instead of reported")
    check(mentions(odd_warnings, "FILTER OPERATOR NOT TRANSLATED", "BETWEEN"),
          f"an unknown filter operator vanished silently: {odd_warnings}")

    # A range on a non-numeric value: the gate compares numerically, so
    # rendered literally it would fail on every row and the call would
    # never run. The real CustomerA set has hundreds of GE/GT '!'.
    bang = UNKNOWN_OPERATOR.replace("RowFilterBETWEEN", "RowFilterGE").replace('Value="5"', 'Value="!"')
    _, bang_step, bang_warnings = translate(bang)
    check(gate_tuples(bang_step.calls[0]) == [],
          "a GE '!' filter was emitted as a numeric gate - it would block every row")
    check(mentions(bang_warnings, "FILTER NOT TRANSLATED", "not a number", "not blank"),
          f"a dropped GE '!' filter was not reported with the likely 'not blank' reading: {bang_warnings}")


def test_export_only_block_is_refused():
    workflow = parse(EXPORT_ONLY)[0]
    try:
        translate_classic_to_pipeline(workflow, base_url=BASE_URL)
    except ClassicPipelineError as error:
        check("build.py" in str(error) or "classic-xml" in str(error),
              f"an Export-only block was refused without pointing at the native path: {error}")
    else:
        failures.append("an <Export>-only block was bent into a row pipeline instead of being "
                        "refused - it has no input row to iterate and this path has no Excel step")


def test_mixed_block_leaves_the_export_call_out():
    _, step, warnings = translate(MIXED)
    check([c.ref for c in step.calls] == ["OIS017MI_AddPriceList"],
          f"an <Export> call was made part of the row pipeline: {[c.ref for c in step.calls]}")
    check(mentions(warnings, "EXPORT CALL(S) NOT TRANSLATED", "CMS100MI.LstC05_OPRICH"),
          "the excluded Export call was not reported")
    # *A was classified API_CHAIN (the Export writes OJPRRF to column A for
    # the same field name), and its producer is not in the pipeline - so it
    # has to fall back to the column, not to an empty value.
    record = call_named(step, "OIS017MI_AddPriceList").record
    check(record.get("OJPRRF") == {"row": "A"},
          f"a chained value from an Export call did not fall back to its column: {record}")
    check(mentions(warnings, "CHAINED VALUE READ FROM THE SHEET"),
          "the fallback to a spreadsheet column was not reported")


def test_trigger_fields_and_constants():
    _, step, _ = translate(CHAIN)
    users = call_named(step, "MNS150MI_SelUsers")
    check(users.record.get("FRES") == {"trigger": "FRES"},
          f'Source="UserID" did not become a trigger value: {users.record}')
    check(call_named(step, "AUTCHKMI_ChkAuthority").record.get("APGM") == {"const": "OIS017"},
          "a Classic Constant did not become a const value")
    check(call_named(step, "OIS017MI_AddPriceList").record.get("CUCD") == {"row": "C"},
          "a Classic column value did not become a row value")


def test_a_forward_reference_is_rejected():
    """
    The IR refuses a gate that reads a LATER call. Nothing in the
    translator can produce one (Classic resolves by document order), but
    the guard is what makes that safe to rely on - without it a
    mis-ordered chain reads undefined at run time instead of failing here.
    """
    from pipeline_ir import CodeCall, Gate, M3RowPipelineStep

    try:
        M3RowPipelineStep(calls=[
            CodeCall(ref="first", program="P", transaction="T",
                     gates=[Gate(left={"callStatus": "second"}, op="eq", right={"const": "ok"})]),
            CodeCall(ref="second", program="P", transaction="T"),
        ])
    except PipelineSpecError:
        pass
    else:
        failures.append("a gate reading a LATER call was accepted - it would read undefined at "
                        "run time instead of failing at build time")


def test_repeated_gates_are_stored_once_in_the_plan():
    # Live refuses an oversized workflow, and a real CustomerA block repeats
    # the same gates on 168 calls - so PLAN shares them instead of copying.
    import m3_rest
    _, step, _ = translate(CHAIN)
    pool, sets, calls = m3_rest._plan_calls(step.calls)
    gated = [c for c in step.calls if c.gates]
    check(sum(len(c.gates) for c in gated) >= len(pool),
          "the gate pool is larger than the gates it replaces")
    check(len(pool) == len({json.dumps(m3_rest._plan_gate(g), sort_keys=True)
                            for c in gated for g in c.gates}),
          f"the gate pool holds duplicates: {pool}")
    check(all(isinstance(c.get("gates", 0), int) for c in calls),
          "a call still carries its gates inline instead of a set index")
    rebuilt = [[pool[i] for i in sets[c["gates"]]] for c in calls if "gates" in c]
    check(rebuilt == [[m3_rest._plan_gate(g) for g in c.gates] for c in gated],
          "resolving set -> pool does not give back each call's own gates, in order")


def test_built_live_json_is_a_code_step_under_the_trigger():
    pipeline, _, _ = translate(CHAIN)
    live = build_pipeline_workflow(pipeline, CONNECTION, TENANT, ENVIRONMENT)
    definition = live["definition"]
    steps = definition["workflowConfig"]["steps"]
    check(len(steps) == 1 and steps[0]["stepId"] == "trigger_1",
          "the chain does not start from a single trigger_1")
    children = steps[0]["childSteps"]
    check(len(children) == 1 and children[0]["type"] == "GENERIC_CODE",
          f"the trigger's child is not a Code step: {[c.get('type') for c in children]}")
    check(children[0]["childSteps"] == [],
          "the Code step is not a terminal step")
    check("excel_1" not in definition["stepConfig"],
          "excel_1 reached a generic pipeline - it can only bind to m3_api_1 transaction ids and "
          "broke saving in the builder for real")
    check(live.get("labels") == ["VXL", "Imported"],
          f"expected the VXL and Imported labels, got: {live.get('labels')}")
    content = definition["stepConfig"]["code_1"]["content"]
    check("nrOfFailedTransactions" in content,
          "the generated Code step does not read the confirmed per-transaction failure counter, "
          "so BasedOnTransaction cannot work")


def test_the_real_files_translate_and_build():
    for name in ("C05_MMS100_Destruction_Order_ADD_1.4.xml", "Existing Price Lists.xml"):
        path = os.path.join(RUN_DIR, name)
        if not os.path.exists(path):
            print(f"  (skipped {name} - not in RUN/)")
            continue
        for index, workflow in enumerate(parse_classic_xml_units(path)):
            try:
                pipeline, warnings = translate_classic_to_pipeline(
                    workflow, base_url=BASE_URL, connection_id=CONNECTION)
            except ClassicPipelineError:
                continue   # an Export-only block, refused on purpose
            live = build_pipeline_workflow(pipeline, CONNECTION, TENANT, ENVIRONMENT)
            check("code_1" in live["definition"]["stepConfig"],
                  f"{name} unit {index} built without a Code step")
            check(bool(warnings + pipeline_warnings(pipeline)),
                  f"{name} unit {index} translated with NO warnings - every real Classic block "
                  "rests on at least the row-addressing assumption")


# --- executing the generated step -------------------------------------

_HARNESS_JS = r"""
// Runs one generated Code step against a fake M3. Reads {source, rows, m3}
// as JSON on stdin, writes {result, calls} on stdout. The fake answers with
// the confirmed bulk response shape, so the step's own outcome detection is
// what is under test.
let input = '';
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  const spec = JSON.parse(input);
  const calls = [];
  const stubs = {
    authorizedAxios: async () => ({
      defaults: { baseURL: 'https://fake.example/TENANT' },
      post: async (url, payload) => {
        const transaction = payload.transactions[0];
        const key = payload.program + '.' + transaction.transaction;
        calls.push({ key: key, record: transaction.record });
        const plan = spec.m3[key] || { ok: true, records: [] };
        return {
          status: 200,
          data: {
            statusCode: 200,
            body: {
              nrOfSuccessfullTransactions: plan.ok ? 1 : 0,
              nrOfFailedTransactions: plan.ok ? 0 : 1,
              wasTerminated: false,
              results: [Object.assign({ transaction: transaction.transaction, records: plan.records || [] },
                plan.errorMessage ? { errorMessage: plan.errorMessage, errorCode: plan.errorCode } : {})]
            }
          }
        };
      }
    }),
    getContext: async () => ({})
  };
  const handler = (function (require) {
    const module = { exports: null };
    eval(spec.source);
    return module.exports;
  })((name) => stubs[name]);
  const result = await handler(null, {}, { body: { data: spec.rows } });
  process.stdout.write(JSON.stringify({ result: result, calls: calls }));
});
"""


def _run_generated_step(step, rows, m3):
    """(list of program.transaction actually called, _diagnostics)."""
    step.debug = True   # _diagnostics is only returned in debug mode
    answer = _execute_generated_step(step, rows, m3)
    if answer is None:
        return [], {}
    return ([entry["key"] for entry in answer["calls"]],
            answer["result"].get("_diagnostics", {}))


def _execute_generated_step(step, rows, m3, expect_error=False):
    """The harness's raw {result, calls}, or None when node failed (recorded).
    With expect_error, the step is SUPPOSED to throw: returns its stderr, or
    None (recorded) when it ran to the end instead."""
    source = render_row_pipeline_js(step, CONNECTION)
    directory = tempfile.mkdtemp(prefix="vinceforge-selftest-")
    try:
        harness = os.path.join(directory, "harness.js")
        io.open(harness, "w", encoding="utf-8").write(_HARNESS_JS)
        payload = json.dumps({"source": source, "rows": rows, "m3": m3})
        finished = subprocess.run([NODE, harness], input=payload,
                                  capture_output=True, text=True)
        if expect_error:
            if finished.returncode == 0:
                failures.append("the generated Code step was expected to throw, but it ran: "
                                + (finished.stdout or "")[:400])
                return None
            return finished.stderr or ""
        if finished.returncode != 0:
            failures.append("the generated Code step did not run under node: "
                            + (finished.stderr or "")[:800])
            return None
        return json.loads(finished.stdout)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# A real Excel table has a header over every column, so the fixture's header
# row always covers the columns CHAIN writes (its two <Cell>s and its
# MessageColumn) - the generated step refuses to run when one is missing.
CHAIN_WRITTEN_HEADERS = {"BK": "Auth 1", "BO": "Auth 2", "BU": "Message"}


def _on_sheet(rows, start, headers=None):
    """What VXL really sends: the sheet's used range from ROW 1. Rows above
    `start` are a title row and (at start-1) the table header row."""
    header = dict(CHAIN_WRITTEN_HEADERS)
    header.update(headers or {"A": "Title"})
    return [{"A": "A title row"}] * (start - 2) + [header] + rows


def test_generated_step_runs_a_group_once_and_its_lines_always():
    _, step, _ = translate(GROUPED_LINES)
    rows = [
        {"B": "F1", "C": "W1", "F": "ITEM-1"},
        {"B": "F1", "C": "W1", "F": "ITEM-2"},
        {"B": "F1", "C": "W1", "F": "ITEM-3"},
        {"B": "F2", "C": "W2", "F": "ITEM-4"},
    ]
    rows = _on_sheet(rows, 4, headers={"A": "Title", "P": "Authority", "AF": "Message"})
    m3 = {
        "AUTCHKMI.ChkAuthority": {"ok": True, "records": [{"ALO1": "1"}]},
        "MMZ101MI.AddLine": {"ok": True, "records": []},
    }
    keys, diagnostics = _run_generated_step(step, rows, m3)
    check(keys.count("AUTCHKMI.ChkAuthority") == 2,
          f"the once-per-group call fired {keys.count('AUTCHKMI.ChkAuthority')} time(s) for 2 "
          "groups of rows - it must fire on each group's FIRST row only")
    check(keys.count("MMZ101MI.AddLine") == 4,
          f"the line call fired {keys.count('MMZ101MI.AddLine')} time(s) for 4 rows. It is gated "
          "on the column the once-per-group call writes, so this is GROUP_RESULT_SCOPE at work: "
          "under 'group' every row of a group sees that answer")
    check(diagnostics.get("groupsSeen") == 2,
          f"group breaks were not detected against the previous row: {diagnostics}")
    check(diagnostics.get("dataFrom") == 3,
          f"rows above InputStartRow 4 were not skipped: {diagnostics}")


def test_generated_step_reproduces_get_or_create():
    _, step, _ = translate(CHAIN)
    row = _on_sheet([{"BH": "OK", "B": "PL1", "C": "NOK", "AM": "IT1"}], 8)
    base = {
        "MNS150MI.SelUsers": {"ok": True, "records": [{"USID": "u"}]},
        "AUTCHKMI.ChkAuthority": {"ok": True, "records": [{"ALO2": "1"}]},
        "OIS017MI.AddPriceList": {"ok": True, "records": []},
        "OIS017MI.AddBasePrice": {"ok": True, "records": []},
    }

    missing = dict(base, **{"OIS017MI.GetPriceList": {"ok": False, "records": []}})
    keys, _ = _run_generated_step(step, row, missing)
    check("OIS017MI.AddPriceList" in keys,
          f"the price list was NOT created although the Get failed: {keys}")

    existing = dict(base, **{"OIS017MI.GetPriceList": {"ok": True, "records": [{"PRRF": "PL1"}]}})
    keys, _ = _run_generated_step(step, row, existing)
    check("OIS017MI.AddPriceList" not in keys,
          f"the price list was created although the Get SUCCEEDED - a duplicate create: {keys}")
    # The independent branch must still run: a chain break stops what
    # depends on it, not everything after it.
    check("OIS017MI.AddBasePrice" in keys,
          f"an unrelated later call was dropped by an earlier call's chain break: {keys}")


def test_generated_step_skips_a_row_the_block_gate_rejects():
    _, step, _ = translate(CHAIN)
    rows = _on_sheet([{"BH": "NO", "B": "PL1", "C": "NOK", "AM": "IT1"}], 8)
    keys, diagnostics = _run_generated_step(step, rows, {})
    check(keys == [],
          f"a row the <Import>-level filter rejects still called M3: {keys}")
    check(diagnostics.get("rowsSkipped") == 1,
          f"the skipped row was not reported as a row skip: {diagnostics}")


def test_generated_step_returns_the_table_body_keyed_by_header_name():
    # OBSERVED 2026-10-01: VXL writes the returned rows into the table by
    # HEADER NAME from its first body row, and appends a new column for any
    # key it does not know. So the output must be only the rows below the
    # header, keyed by the header's names - never letters, never the rows
    # above the data start (they would also reach M3 as input).
    _, step, _ = translate(CHAIN)
    step.debug = False
    names = {"A": "Dummy", "B": "Price list", "C": "Type", "AM": "Item", "BH": "Gate",
             "BU": "Message", "BK": "Auth 1", "BO": "Auth 2"}
    data = [
        {"B": "PL1", "C": "NOK", "AM": "IT1", "BH": "OK", "BU": "old"},
        {"B": "PL2", "C": "NOK", "AM": "IT2", "BH": "NO", "BU": "old"},
    ]
    # the CHAIN's data starts on row 8; the table header is on row 7
    rows = _on_sheet(data, 8, headers=names)
    m3 = {
        "MNS150MI.SelUsers": {"ok": True, "records": [{"USID": "u"}]},
        "AUTCHKMI.ChkAuthority": {"ok": True, "records": [{"ALO2": "1"}]},
        "OIS017MI.GetPriceList": {"ok": False, "records": []},
    }
    answer = _execute_generated_step(step, rows, m3)
    if answer is None:
        return
    check(not any("title" in json.dumps(entry["record"]).lower() or "Price list" in json.dumps(entry["record"])
                  for entry in answer["calls"]),
          f"a row above InputStartRow reached M3: {answer['calls']}")
    out = answer["result"]
    check(isinstance(out, list) and len(out) == 2,
          f"expected only the 2 table-body rows back, got: {json.dumps(out)[:300]}")
    if not isinstance(out, list) or len(out) != 2:
        return
    check(set(out[0]) == set(out[1]),
          f"the returned rows do not all carry the same keys: {[sorted(r) for r in out]}")
    check(not any(key.startswith(("_", "I_")) for key in out[0]),
          f"call-log keys leaked into the sheet rows: {sorted(out[0])}")
    letters = [k for k in out[0] if k in names]
    check(not letters,
          f"output is still keyed by column letter {letters} - VXL would append them as new columns")
    check(set(out[0]) <= set(names.values()) | {"A title row"},
          f"output carries keys that are not table headers: {sorted(out[0])}")
    check([r["Price list"] for r in out] == ["PL1", "PL2"], "the rows came back out of order")
    check(out[0]["Auth 1"] == "1",
          f"ChkAuthority's ALO2 was not written to its <Cell> column (header 'Auth 1'): {out[0]}")
    check(out[1]["Auth 1"] == "",
          f"a row the block gate skipped got a value written: {out[1]}")
    check("GetPriceList" in str(out[0]["Message"]),
          f"the failed Get was not reported in MessageColumn BU ('Message'): {out[0]['Message']!r}")
    check(out[1]["Message"] == "",
          f"a skipped row's MessageColumn should be blank, got: {out[1]['Message']!r}")


def test_rows_between_the_header_and_the_data_start_pass_through_uncalled():
    # Haakon's layout: header on row 6, a sample-record row 7, data from row 8.
    _, step, _ = translate(CHAIN)
    step.debug = False
    step.header_row = 6
    names = dict(CHAIN_WRITTEN_HEADERS, B="Price list")
    sample = {"B": "Sample record", "BU": "sample"}
    rows = [{"A": "title"}] * 5 + [names, sample, {"B": "PL1", "BH": "OK", "BU": "old"}]
    answer = _execute_generated_step(step, rows, {"OIS017MI.GetPriceList": {"ok": True}})
    if answer is None:
        return
    out = answer["result"]
    check(isinstance(out, list) and len(out) == 2,
          f"expected the sample row + 1 data row (everything below header row 6), got: {json.dumps(out)[:300]}")
    if not isinstance(out, list) or len(out) != 2:
        return
    check(out[0]["Price list"] == "Sample record" and out[0]["Message"] == "sample",
          f"the sample-record row was altered or lost: {out[0]}")
    check("Sample record" not in json.dumps(answer["calls"]),
          "the sample-record row (above InputStartRow) was sent to M3")


def test_blank_rows_are_not_called_but_stay_in_place():
    # 203 blank table rows once produced 203 failing M3 lookups on empty input.
    _, step, _ = translate(CHAIN)
    names = {"B": "Price list", "BU": "Message"}
    rows = _on_sheet([{"B": "PL1", "BH": "OK"}, {}, {"B": " ", "BH": ""}, {"B": "PL2", "BH": "OK"}],
                     8, headers=names)
    m3 = {"OIS017MI.GetPriceList": {"ok": True}}
    keys, diagnostics = _run_generated_step(step, rows, m3)
    step.debug = False
    answer = _execute_generated_step(step, rows, m3)
    if answer is None:
        return
    # each real row makes 2 ChkAuthority calls; a blank row must add none
    check(keys.count("AUTCHKMI.ChkAuthority") == 4,
          f"expected M3 to be called for the 2 non-blank rows only (4 ChkAuthority calls), got: {keys}")
    check(diagnostics.get("blankSkipped") == 2, f"blank rows were not counted: {diagnostics}")
    out = answer["result"]
    check(isinstance(out, list) and len(out) == 4,
          f"the 2 blank rows must still come back so the table stays aligned: {json.dumps(out)[:300]}")
    if isinstance(out, list) and len(out) == 4:
        check(out[1].get("Message", "") == "" and out[2].get("Message", "") == "",
              f"a blank row was given a message: {out[1:3]}")
        check([r["Price list"] for r in out] == ["PL1", "", " ", "PL2"], "rows moved or changed")


def test_a_failed_call_is_reported_as_the_plain_m3_error():
    # The sheet cell should read like a person wrote it, not carry ~250 chars of raw JSON.
    _, step, _ = translate(CHAIN)
    step.debug = False
    rows = _on_sheet([{"B": "PL1", "C": "NOK", "AM": "IT1", "BH": "OK"}], 8,
                     headers={"B": "Price list", "BU": "Message"})
    m3 = {
        "MNS150MI.SelUsers": {"ok": True, "records": [{"USID": "u"}]},
        "AUTCHKMI.ChkAuthority": {"ok": True, "records": [{"ALO2": "1"}]},
        "OIS017MI.GetPriceList": {"ok": False, "records": [], "errorMessage": "Price list  does not exist",
                                  "errorCode": "WPR0103"},
    }
    answer = _execute_generated_step(step, rows, m3)
    if answer is None:
        return
    message = answer["result"][0]["Message"]
    check("Price list does not exist (WPR0103)" in message and "{" not in message,
          f"expected the plain M3 error text, got: {message!r}")


def test_trailing_blank_rows_are_not_returned():
    _, step, _ = translate(CHAIN)
    step.debug = False
    names = {"B": "Price list", "BU": "Message"}
    data = [{"B": "PL1", "BH": "OK"}, {}, {"B": "PL2", "BH": "OK"}] + [{}] * 5
    rows = _on_sheet(data, 8, headers=names)
    answer = _execute_generated_step(step, rows, {"OIS017MI.GetPriceList": {"ok": True}})
    if answer is None:
        return
    out = answer["result"]
    check(isinstance(out, list) and len(out) == 3,
          f"expected rows up to the last data row only (PL1, the blank between, PL2), got {len(out)}: "
          f"{json.dumps(out)[:200]}")
    # a table with nothing but blank rows below the start returns only what is above it
    only_blank = _on_sheet([{}] * 4, 8, headers=names)
    answer = _execute_generated_step(step, only_blank, {})
    if answer is not None:
        check(answer["result"] == [], f"all-blank input should return nothing: {answer['result']}")


def test_each_call_writes_its_failure_to_its_own_message_column():
    # Classic's <M3Api MessageColumn>: ChkAuthority's failure goes to its own column.
    xml = CHAIN.replace('<M3Api Program="OIS017MI" Transaction="GetPriceList"',
                        '<M3Api Program="OIS017MI" Transaction="GetPriceList" MessageColumn="BW"', 1)
    check('MessageColumn="BW"' in xml, "the fixture has no GetPriceList call to put a column on")
    _, step, _ = translate(xml)
    step.debug = False
    check(any(c.message_column == "BW" for c in step.calls), "the call's MessageColumn was not translated")
    names = {"B": "Price list", "BU": "Message", "BW": "Get message"}
    rows = _on_sheet([{"B": "PL1", "C": "NOK", "AM": "IT1", "BH": "OK", "BW": "stale"},
                      {"B": "PL2", "C": "NOK", "AM": "IT2", "BH": "OK"}], 8, headers=names)
    m3 = {
        "MNS150MI.SelUsers": {"ok": True, "records": [{"USID": "u"}]},
        "AUTCHKMI.ChkAuthority": {"ok": True, "records": [{"ALO2": "1"}]},
        "OIS017MI.GetPriceList": {"ok": False, "records": [], "errorMessage": "Gone", "errorCode": "E1"},
    }
    answer = _execute_generated_step(step, rows, m3)
    if answer is None:
        return
    out = answer["result"]
    check(all(r["Get message"] == "failed: Gone (E1)" for r in out),
          f"the call's own column should hold its failure text (stale value cleared): {[r.get('Get message') for r in out]}")
    check("GetPriceList" in out[0]["Message"], f"the block column lost the failure: {out[0]['Message']!r}")
    ok = {**m3, "OIS017MI.GetPriceList": {"ok": True, "records": [{"PRRF": "PL1"}]}}
    out = _execute_generated_step(step, rows, ok)["result"]
    check(all(r["Get message"] == "" for r in out),
          f"a successful call must leave its column blank, clearing last run's text: {[r.get('Get message') for r in out]}")


def test_a_sheet_shifted_by_an_empty_row_1_is_refused_before_m3():
    # Read from the add-in's code 2026-10-13: a workflow without an Excel step
    # gets the sheet's USED RANGE, so an empty row 1 drops out and every row
    # moves up one. The "header" row is then the first DATA row, and writing
    # back would replace the table body with rows shifted up. Must stop before
    # any M3 call - and by throwing, so nothing is written back.
    _, step, _ = translate(CHAIN)
    step.debug = False
    data = [{"B": "PL1", "C": "NOK", "AM": "IT1", "BH": "OK"},
            {"B": "PL2", "C": "NOK", "AM": "IT2", "BH": "OK"}]
    rows = _on_sheet(data, 8)[1:]   # row 1 was empty: VXL never sends it
    error = _execute_generated_step(step, rows, {}, expect_error=True)
    if error is None:
        return
    check("Sheet layout check failed" in error and "row 1" in error,
          f"the refusal does not say why: {error[:400]}")
    check("PL1" not in error or "Nothing was sent to M3" in error,
          f"the refusal does not say M3 was untouched: {error[:400]}")


def test_a_correctly_placed_header_still_runs():
    # The other side of the layout check: a full header on the expected row
    # runs normally, even when the sheet has columns outside what it writes.
    _, step, _ = translate(CHAIN)
    step.debug = False
    rows = _on_sheet([{"B": "PL1", "C": "NOK", "AM": "IT1", "BH": "OK"}], 8,
                     headers={"A": "Title", "B": "Price list", "ZZ": "Extra"})
    answer = _execute_generated_step(step, rows, {"OIS017MI.GetPriceList": {"ok": True}})
    if answer is None:
        return
    check(answer["calls"], "a correctly laid-out sheet made no M3 calls")


def test_without_a_start_row_every_row_comes_back_by_letter():
    # The old behaviour, kept when there is no header to name columns by.
    _, step, _ = translate(CHAIN)
    step.debug = False
    step.input_start_row = None
    out = _execute_generated_step(step, [{"B": "PL1", "BH": "OK"}], {})
    if out is None:
        return
    check(isinstance(out["result"], list) and len(out["result"]) == 1 and "B" in out["result"][0],
          f"no-start-row output changed shape: {json.dumps(out['result'])[:200]}")


NODE = shutil.which("node")

STATIC_TESTS = [
    test_based_on_becomes_a_call_status_gate,
    test_filter_on_a_written_column_reads_that_call,
    test_block_level_filter_gates_the_row_not_a_call,
    test_grouping_reaches_the_step,
    test_untranslatable_filters_are_reported_not_emitted,
    test_export_only_block_is_refused,
    test_mixed_block_leaves_the_export_call_out,
    test_trigger_fields_and_constants,
    test_a_forward_reference_is_rejected,
    test_built_live_json_is_a_code_step_under_the_trigger,
    test_the_real_files_translate_and_build,
    test_repeated_gates_are_stored_once_in_the_plan,
]

RUNTIME_TESTS = [
    test_generated_step_runs_a_group_once_and_its_lines_always,
    test_generated_step_reproduces_get_or_create,
    test_generated_step_skips_a_row_the_block_gate_rejects,
    test_generated_step_returns_the_table_body_keyed_by_header_name,
    test_rows_between_the_header_and_the_data_start_pass_through_uncalled,
    test_blank_rows_are_not_called_but_stay_in_place,
    test_a_failed_call_is_reported_as_the_plain_m3_error,
    test_trailing_blank_rows_are_not_returned,
    test_each_call_writes_its_failure_to_its_own_message_column,
    test_without_a_start_row_every_row_comes_back_by_letter,
    test_a_sheet_shifted_by_an_empty_row_1_is_refused_before_m3,
    test_a_correctly_placed_header_still_runs,
]


def main():
    tests = list(STATIC_TESTS)
    if NODE:
        tests += RUNTIME_TESTS
    for test in tests:
        test()

    if not NODE:
        print("node was not found, so the generated Code step was checked for SHAPE only. "
              "Install node to also run it against a fake M3 - that is the half that checks "
              "the control flow actually behaves like Classic.")

    if failures:
        print(f"{len(failures)} Classic-pipeline self-test failure(s):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print(f"All {len(tests)} Classic-pipeline self-tests passed"
          + ("." if NODE else ", minus the node-only ones."))


if __name__ == "__main__":
    main()
