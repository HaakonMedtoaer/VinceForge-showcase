"""
Self-test for the m3Expand Code step (expand_code.py) - run it directly:

    python selftest_expand.py

Its subject is what an Expand does wrong SILENTLY: a fan-out that loses or
duplicates rows, a range filter that keeps the wrong price lists, a lookup
failure that drops a line instead of noting it, output keyed by anything
other than the table's header names (VXL would append new columns), and a
run that reaches M3 when it should have stopped. The generated step is
EXECUTED under node against a fake M3 - shape checks alone cannot see any
of these. Skipped with a message when node is absent.
"""

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

from build_pipeline import build_pipeline_spec
from expand_code import ExpandSpecError, render_expand_js, validate_expand_spec
from pipeline_ir import PipelineSpecError

CONNECTION = "CONNECTION-" + "a" * 32
BASE = "https://mingle-ionapi.eu1.inforcloudsuite.com/TEST_TST"
NODE = shutil.which("node")
HERE = os.path.dirname(os.path.abspath(__file__))
CUSTOMER_B = os.path.join(os.path.dirname(HERE), "RUN", "runs", "CustomerB", "workflows")

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


# The fake answers each call with the FIRST entry whose `match` is contained
# in the call's record - enough to give every price list its own lines.
_HARNESS_JS = r"""
let input = '';
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', async () => {
  const spec = JSON.parse(input);
  const calls = [];
  const stubs = {
    authorizedAxios: async () => ({
      post: async (url, payload) => {
        const t = payload.transactions[0];
        const key = payload.program + '.' + t.transaction;
        calls.push({ key: key, record: t.record, max: payload.maxReturnedRecords });
        const entry = (spec.m3[key] || []).find((e) =>
          Object.keys(e.match || {}).every((k) => String(t.record[k]) === String(e.match[k])));
        const ok = entry ? entry.ok !== false : true;
        return { status: 200, data: { statusCode: 200, body: {
          nrOfSuccessfullTransactions: ok ? 1 : 0, nrOfFailedTransactions: ok ? 0 : 1, wasTerminated: false,
          results: [Object.assign({ transaction: t.transaction, records: (entry && entry.records) || [] },
            entry && entry.errorMessage ? { errorMessage: entry.errorMessage, errorCode: entry.errorCode } : {})] } } };
      }
    }),
    getContext: async () => ({})
  };
  const handler = (function (require) { const module = { exports: null }; eval(spec.source); return module.exports; })((n) => stubs[n]);
  try {
    const result = await handler(null, {}, { header: spec.header, body: spec.rows });
    process.stdout.write(JSON.stringify({ result: result, calls: calls }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ error: String(error && error.message), calls: calls }));
  }
});
"""


def run(spec_step, header, rows, m3):
    source = render_expand_js(spec_step, CONNECTION, BASE)
    directory = tempfile.mkdtemp(prefix="vinceforge-expand-")
    try:
        harness = os.path.join(directory, "harness.js")
        io.open(harness, "w", encoding="utf-8").write(_HARNESS_JS)
        done = subprocess.run([NODE, harness], capture_output=True, text=True,
                              input=json.dumps({"source": source, "header": header, "rows": rows, "m3": m3}))
        if done.returncode != 0:
            failures.append("the generated m3Expand step did not run under node: " + done.stderr[:800])
            return {"calls": []}
        return json.loads(done.stdout)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def load_step(name):
    with io.open(os.path.join(CUSTOMER_B, name), encoding="utf-8") as f:
        return json.load(f)["steps"][0]


# A sheet as VXL sends it: title, field names, the table header (labels), one old body row.
LINES_SHEET = [
    {"A": "Sales Price List"},
    {"A": "PRRF", "B": "CUNO", "C": "ITNO"},
    {"A": "Pricelist", "B": "Customer", "C": "Item nr", "D": "Item name", "E": "Sales price",
     "F": "Sales price QTY", "G": "Sales price U/M", "H": "Currency", "I": "Valid from", "J": "Valid to",
     "K": "EPD", "L": "FPC/DPC", "M": "Overall", "N": "Update", "O": "Add"},
    {"A": "OLD"},
]

LISTS = [
    {"PRRF": "P01", "CUCD": "NOK", "CUNO": "", "FVDT": "20260101"},
    {"PRRF": "P02", "CUCD": "NOK", "CUNO": "", "FVDT": "20260101"},
    {"PRRF": "P03", "CUCD": "SEK", "CUNO": "", "FVDT": "20260101"},
    {"PRRF": "P09", "CUCD": "NOK", "CUNO": "", "FVDT": "20260101"},
]


def price_m3():
    return {
        "OIS017MI.LstPriceList": [{"records": LISTS}],
        "OIS017MI.LstBasePrice": [
            {"match": {"PRRF": "P01"}, "records": [
                {"PRRF": "P01", "CUNO": "", "ITNO": "A1 ", "SAPR": "10.5", "CUCD": "NOK", "FVDT": "20260101"},
                {"PRRF": "P01", "CUNO": "", "ITNO": "B2", "SAPR": "7", "CUCD": "NOK", "FVDT": "20260101"}]},
            {"match": {"PRRF": "P02"}, "records": [
                {"PRRF": "P02", "CUNO": "", "ITNO": "A1", "SAPR": "11", "CUCD": "NOK", "FVDT": "20260101"}]},
            {"match": {"PRRF": "P03"}, "ok": False, "errorMessage": "Price list does not exist", "errorCode": "WPR0103"},
        ],
        "MMS200MI.GetItmBasic": [
            {"match": {"ITNO": "A1"}, "records": [{"ITDS": "Apple"}]},
            {"match": {"ITNO": "B2"}, "records": [{"ITDS": "Banana"}]}],
        "MMS025MI.GetAlias": [
            {"match": {"ITNO": "A1"}, "records": [{"POPN": "7012345"}]},
            {"match": {"ITNO": "B2"}, "ok": False, "errorMessage": "Alias does not exist"}],
        "MMS015MI.Get": [{"records": [{"COFA": "12"}]}],
    }


def test_lines_fan_out_filter_and_lookups():
    if not NODE:
        print("  (node not found - skipping the executed m3Expand tests)")
        return
    step = load_step("P2-lines.spec.json")
    answer = run(step, {"F_PRRF": "P01", "T_PRRF": "P03", "CUCD": "NOK"}, LINES_SHEET, price_m3())
    rows = answer.get("result") or []
    check(not answer.get("error"), f"the Lines step threw: {answer.get('error')}")
    check([r["Pricelist"] + "/" + r["Item nr"] for r in rows] == ["P01/A1", "P01/B2", "P02/A1"],
          "the range P01..P03 with currency NOK should give P01's two lines then P02's one, in M3's "
          f"order (P03 is SEK, P09 out of range): {[(r.get('Pricelist'), r.get('Item nr')) for r in rows]}")
    check(all(set(r) == {c["header"] for c in step["columns"]} for r in rows),
          f"rows are not keyed by exactly the table header names: {rows[:1]}")
    check(rows and rows[0]["Item name"] == "Apple" and rows[0]["EPD"] == "7012345" and rows[0]["FPC/DPC"] == "12",
          f"a lookup's field did not land in its column: {rows[:1]}")
    check(rows and rows[0]["Item nr"] == "A1", "values are not trimmed (M3 pads fields)")
    banana = [r for r in rows if r["Item nr"] == "B2"]
    check(banana and banana[0]["EPD"] == "" and "alias: failed: Alias does not exist" in banana[0]["Overall"],
          f"a failed lookup must keep its row and say why in the message column: {banana}")
    item_calls = [c for c in answer["calls"] if c["key"] == "MMS200MI.GetItmBasic"]
    check(len(item_calls) == 2, f"GetItmBasic for item A1 was not cached across price lists: {len(item_calls)} calls")
    called_lists = [c["record"]["PRRF"] for c in answer["calls"] if c["key"] == "OIS017MI.LstBasePrice"]
    check(called_lists == ["P01", "P02"], f"lines were fetched for filtered-out price lists: {called_lists}")
    list_call = next((c for c in answer["calls"] if c["key"] == "OIS017MI.LstPriceList"), {})
    check(list_call.get("max") == 10000, f"maxReturnedRecords not sent on a list call: {list_call}")

    # One list only (To left blank): defaults to From.
    single = run(step, {"F_PRRF": "P02"}, LINES_SHEET, price_m3()).get("result") or []
    check([r["Pricelist"] for r in single] == ["P02"], f"a blank 'to' did not mean 'just this list': {single}")

    # A list that fails is shown, not silently empty.
    failed = run(step, {"F_PRRF": "P03"}, LINES_SHEET, price_m3()).get("result") or []
    check(len(failed) == 1 and "WPR0103" in failed[0]["Overall"],
          f"a failed LstBasePrice did not produce a row carrying M3's message: {failed}")

    # Nothing matches: one explanatory row, not an empty table.
    none = run(step, {"F_PRRF": "Q01"}, LINES_SHEET, price_m3()).get("result") or []
    check(len(none) == 1 and "No records found" in none[0]["Overall"], f"an empty selection returned {none}")


def test_range_follows_m3_text_order():
    """
    Price lists in the order DEMO_TENANT's OIS017MI/LstPriceList really returned
    them (2026-10-16). CustomerB's Classic range came from M3 itself, so From/To
    must compare as TEXT: 001..001 is not "01", and 1..2 includes "1001".
    """
    if not NODE:
        return
    step = load_step("P2-lines.spec.json")
    order = ["001", "01", "056A", "1-NLDISPLY", "1001", "73", "77", "AMPL_001"]
    m3 = price_m3()
    m3["OIS017MI.LstPriceList"] = [{"records": [
        {"PRRF": p, "CUCD": "SEK", "CUNO": "", "FVDT": "20240101"} for p in order]}]
    m3["OIS017MI.LstBasePrice"] = [{"records": [{"ITNO": "A1"}]}]

    def lists(header):
        answer = run(step, header, LINES_SHEET, m3)
        return [c["record"]["PRRF"] for c in answer["calls"] if c["key"] == "OIS017MI.LstBasePrice"]

    got = lists({"F_PRRF": "001"})
    check(got == ["001"], f"From 001 (To blank) must be price list 001 only, not also '01': {got}")
    got = lists({"F_PRRF": "1", "T_PRRF": "2"})
    check(got == ["1-NLDISPLY", "1001"], f"1..2 must follow M3's text order: {got}")

    numeric = copy.deepcopy(step)
    for cond in numeric["levels"][0]["where"][:2]:
        cond["as"] = "number"
    answer = run(numeric, {"F_PRRF": "1", "T_PRRF": "100"}, LINES_SHEET, m3)
    got = [c["record"]["PRRF"] for c in answer["calls"] if c["key"] == "OIS017MI.LstBasePrice"]
    check(got == ["001", "01", "73", "77"], f"\"as\": \"number\" must compare numerically: {got}")

    bad = copy.deepcopy(step)
    bad["levels"][0]["where"][0]["as"] = "numeric"
    try:
        validate_expand_spec(bad)
        failures.append("a where condition with \"as\": \"numeric\" was accepted")
    except ExpandSpecError:
        pass


def test_it_stops_before_m3_when_it_should():
    if not NODE:
        return
    step = load_step("P2-lines.spec.json")
    answer = run(step, {}, LINES_SHEET, price_m3())
    check("Fill in Price list from" in (answer.get("error") or ""),
          f"a missing required form value did not stop the run: {answer.get('error') or answer.get('result')}")
    check(not answer["calls"], "M3 was called although a required form value was missing")

    wrong_sheet = [{"A": "Title"}, {"A": "Pricelist", "B": "Customer"}]
    answer = run(step, {"F_PRRF": "P01"}, wrong_sheet, price_m3())
    check("Sheet check failed" in (answer.get("error") or "") and "Item nr" in answer.get("error", ""),
          f"a table missing headers this step writes did not stop the run: {answer.get('error')}")
    check(not answer["calls"], "M3 was called although the sheet's table lacks headers")

    # No rows inline (a big sheet goes as a file): the check is skipped, the run goes on.
    answer = run(step, {"F_PRRF": "P01"}, None, price_m3())
    check(not answer.get("error") and answer.get("result"), f"a run with no inline rows failed: {answer.get('error')}")


def test_scales_nest_three_lists_and_item_filter():
    if not NODE:
        return
    step = load_step("P5-scales.spec.json")
    m3 = price_m3()
    m3["OIS017MI.LstGradSlsPrc"] = [
        {"match": {"PRRF": "P01", "ITNO": "A1"}, "records": [
            {"PRRF": "P01", "ITNO": "A1", "QTYL": "10", "SAPR": "9"}, {"PRRF": "P01", "ITNO": "A1", "QTYL": "50", "SAPR": "8"}]},
        {"match": {"PRRF": "P01", "ITNO": "B2"}, "records": []},
    ]
    sheet = [{"A": "t"}, {"A": "Pricelist", "B": "Customer", "C": "Item nr", "D": "Item name", "E": "Quantity",
                         "F": "Price adjustment", "G": "Sales price", "H": "Currency", "I": "Valid From", "J": "Overall"}]
    rows = run(step, {"F_PRRF": "P01"}, sheet, m3).get("result") or []
    check([r["Quantity"] for r in rows] == ["10", "50"],
          f"a line with two scales gave {[r.get('Quantity') for r in rows]}; a line with none must add no row")
    check(all(r["Item name"] == "Apple" for r in rows), "the item lookup under the scales level is missing")
    only_b2 = run(step, {"F_PRRF": "P01", "ITNO": "B2"}, sheet, m3)
    scale_calls = [c["record"]["ITNO"] for c in only_b2["calls"] if c["key"] == "OIS017MI.LstGradSlsPrc"]
    check(scale_calls == ["B2"], f"the optional item filter did not narrow the scale calls: {scale_calls}")


def test_spec_mistakes_fail_at_build_time():
    good = load_step("P2-lines.spec.json")
    cases = {
        "a ref that names a LATER level": ("levels", 1, "record", "PRRF", {"from": "item.PRRF"}),
        "an undeclared param": ("levels", 0, "record", "PRRF", {"param": "NOPE"}),
    }
    for what, (k, i, sub, field, value) in cases.items():
        bad = copy.deepcopy(good)
        bad[k][i][sub][field] = value
        try:
            validate_expand_spec(bad)
            failures.append(f"{what} was accepted - it would only fail inside the customer's Excel")
        except ExpandSpecError:
            pass
    bad = copy.deepcopy(good)
    bad["columns"].append({"header": "Pricelist", "from": "line.PRRF"})
    try:
        validate_expand_spec(bad)
        failures.append("a duplicate header was accepted")
    except ExpandSpecError:
        pass
    spec = json.load(io.open(os.path.join(CUSTOMER_B, "P2-lines.spec.json"), encoding="utf-8"))
    spec["steps"][0]["levels"][0]["typo"] = 1
    spec["steps"][0]["bogus"] = 1
    try:
        build_pipeline_spec(spec, CONNECTION, "TENANT-x", "ENVIRONMENT-x")
        failures.append("an unknown m3Expand key was accepted")
    except PipelineSpecError:
        pass


def test_customer_b_specs_build():
    for name in ("P2-lines.spec.json", "P5-scales.spec.json", "D3-discount-scales.spec.json"):
        spec = json.load(io.open(os.path.join(CUSTOMER_B, name), encoding="utf-8"))
        spec["baseUrl"] = BASE
        live, warnings = build_pipeline_spec(spec, CONNECTION, "TENANT-x", "ENVIRONMENT-x")
        config = live["definition"]["stepConfig"]
        check("code_1" in config and "module.exports" in config["code_1"]["content"],
              f"{name} did not build to a Code step")
        check(len(json.dumps(live)) < 80000, f"{name} is over the size Live is known to accept")
        check(any("REPLACES the body" in w for w in warnings), f"{name} built without the table warning")


def main():
    if not os.path.isdir(CUSTOMER_B):
        print("RUN/runs/CustomerB/workflows is missing - the CustomerB specs these tests run are not on this machine.")
        return 0
    tests = (test_lines_fan_out_filter_and_lookups, test_range_follows_m3_text_order,
             test_it_stops_before_m3_when_it_should,
             test_scales_nest_three_lists_and_item_filter, test_spec_mistakes_fail_at_build_time,
             test_customer_b_specs_build)
    for test in tests:
        try:
            test()
        except Exception as error:
            failures.append(f"{test.__name__} raised: {type(error).__name__}: {error}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"All {len(tests)} m3Expand self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
