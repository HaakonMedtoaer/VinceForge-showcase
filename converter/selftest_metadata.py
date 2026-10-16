"""
Self-test for field METADATA - the live fetch's merge, the offline catalog
fallback, and the unmapped-mandatory check. Stdlib only, run it directly:

    python selftest_metadata.py

Everything here fails silently when wrong. A mandatory flag lost in the
merge still builds valid JSON; a catalog answer passed off as the target
tenant's looks exactly like a live one; a missing mandatory field is only
discovered when M3 rejects the call in the customer's tenant. So:

  - An INPUT field that M3 describes as mandatory in its I direction and
    optional in its O direction (BUS100MI's DIVI, RUN/meta-BUS100MI.json)
    stays mandatory as an input. It used to come out False, because the O
    item followed the I item and overwrote it.
  - The catalog fills only what the live fetch did not answer, never
    overrides it, and always says in the warnings that it was used.
  - No catalog file means nothing changes.
  - An unmapped mandatory input is reported; a mapped one and a mandatory
    OUTPUT are not.

Network and the real catalog are never touched: urlopen is stubbed and the
catalog is a temporary SQLite file named via $M3_FIELD_CATALOG.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request

import metadata
from ir import InputField, Transaction, Workflow

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _live_response():
    """Shaped exactly like RUN/meta-BUS100MI.json: one item per direction, I first."""
    return {"count": 2, "items": [
        {"name": "I", "descr": "Input", "data": [
            {"name": "CONO", "descr": "Company", "length": "3", "type": "N", "mandatory": "0"},
            {"name": "DIVI", "descr": "Division", "length": "3", "type": "A", "mandatory": "1"}]},
        {"name": "O", "descr": "Input", "data": [
            {"name": "CONO", "descr": "Company", "length": "3", "type": "N", "mandatory": "0"},
            {"name": "DIVI", "descr": "Division", "length": "3", "type": "A", "mandatory": "0"}]},
    ]}


def _make_catalog(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (Program TEXT, TransactionName TEXT, Description TEXT, "
                 "Simu TEXT, Status TEXT, FieldsFetchedAt TEXT)")
    conn.execute("CREATE TABLE fields (Program TEXT, TransactionName TEXT, Direction TEXT, FieldName TEXT, "
                 "FieldLabel TEXT, Type TEXT, Length INTEGER, Mandatory TEXT, FromPos INTEGER, ToPos INTEGER)")
    conn.execute("INSERT INTO transactions VALUES ('CRS610MI','AddText','','','','2026-09-17T13:00:00Z')")
    conn.executemany("INSERT INTO fields VALUES (?,?,?,?,?,?,?,?,0,0)", [
        ("CRS610MI", "AddText", "I", "CUNO", "Customer", "A", 10, "1"),
        ("CRS610MI", "AddText", "I", "TXVR", "Text id", "A", 10, "1"),
        ("CRS610MI", "AddText", "I", "LNCD", "Language", "A", 2, "0"),
        ("CRS610MI", "AddText", "O", "TXID", "Text id", "N", 13, "1"),
        # Also in the catalog, but the live fetch answers it - must be ignored.
        ("BUS100MI", "DelBudgetLines", "I", "DIVI", "WRONG", "A", 99, "0"),
    ])
    conn.commit()
    conn.close()


def _workflow():
    return Workflow(label="t", transactions=[
        Transaction(ir_id="a", program="BUS100MI", transaction="DelBudgetLines",
                    inputs=[InputField(name="CONO", source_kind="CONSTANT", value="1")]),
        Transaction(ir_id="b", program="CRS610MI", transaction="AddText",
                    inputs=[InputField(name="CUNO", source_kind="EXCEL_COLUMN", value="A")]),
    ])


def _live_overrides():
    original = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Response(json.dumps(_live_response()).encode())
    try:
        return metadata.fetch_live_metadata("CONNECTION-x", "BUS100MI", "DelBudgetLines", "token")
    finally:
        urllib.request.urlopen = original


def test_mandatory_input_survives_an_optional_output_of_the_same_name():
    live = _live_overrides()
    divi_in = metadata.field_metadata("BUS100MI", "DelBudgetLines", "DIVI", live, "I")
    divi_out = metadata.field_metadata("BUS100MI", "DelBudgetLines", "DIVI", live, "O")
    check(divi_in["mandatory"] is True, "DIVI as an INPUT lost its mandatory flag to the O item")
    check(divi_out["mandatory"] is False, "DIVI as an OUTPUT should keep the O item's flag")
    check(metadata.field_metadata("BUS100MI", "DelBudgetLines", "DIVI", live)["mandatory"] is True,
          "the undirected key should prefer the input side")


def test_catalog_fills_only_what_live_did_not_answer():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "catalog.sqlite")
        _make_catalog(path)
        os.environ[metadata.CATALOG_ENV] = path
        try:
            notes = []
            merged = metadata.fill_from_catalog(_workflow(), _live_overrides(), notes)
        finally:
            del os.environ[metadata.CATALOG_ENV]
    check(metadata.field_metadata("CRS610MI", "AddText", "CUNO", merged, "I")["descr"] == "Customer",
          "the catalog did not fill a transaction the live fetch had no answer for")
    check(metadata.field_metadata("BUS100MI", "DelBudgetLines", "DIVI", merged, "I")["descr"] == "Division",
          "the catalog overrode a live answer")
    check(len(notes) == 1 and "OFFLINE" in notes[0] and "CRS610MI.AddText" in notes[0]
          and "BUS100MI" not in notes[0],
          f"the catalog note should name exactly the filled transaction: {notes}")


def test_no_catalog_changes_nothing():
    os.environ[metadata.CATALOG_ENV] = os.path.join(tempfile.gettempdir(), "no-such-catalog.sqlite")
    try:
        notes = []
        result = metadata.fill_from_catalog(_workflow(), None, notes)
    finally:
        del os.environ[metadata.CATALOG_ENV]
    check(result is None and notes == [], "a missing catalog must leave overrides and notes untouched")


def test_unmapped_mandatory_inputs_are_reported():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "catalog.sqlite")
        _make_catalog(path)
        os.environ[metadata.CATALOG_ENV] = path
        try:
            wf = _workflow()
            merged = metadata.fill_from_catalog(wf, _live_overrides(), [])
        finally:
            del os.environ[metadata.CATALOG_ENV]
    missing = metadata.unmapped_mandatory_inputs(wf, merged)
    check(missing == ["BUS100MI.DelBudgetLines.DIVI", "CRS610MI.AddText.TXVR"],
          f"expected DIVI and TXVR (mapped CUNO, optional LNCD and output TXID excluded), got {missing}")
    check(len(metadata.unmapped_mandatory_warning(wf, merged)) == 1, "expected one warning")
    check(metadata.unmapped_mandatory_warning(wf, None) == [], "no metadata must mean no warning")


def main():
    tests = (
        test_mandatory_input_survives_an_optional_output_of_the_same_name,
        test_catalog_fills_only_what_live_did_not_answer,
        test_no_catalog_changes_nothing,
        test_unmapped_mandatory_inputs_are_reported,
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
    print(f"All {len(tests)} metadata self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
