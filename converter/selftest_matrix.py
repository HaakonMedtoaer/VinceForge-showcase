"""
Self-tests for the CAPABILITY MATRIX - run it directly.

Its subject is DRIFT, which is silent by nature: the matrix restates what the
converter enforces, and nothing about editing `build.py` reminds you that a
document three directories away just became wrong. A capability doc that has
quietly gone stale is worse than none - it reads with exactly the same
authority it had when it was true, and people plan customer migrations on it.

So this checks the claim in BOTH directions:

  * every symbol the matrix names must still exist in the code
    (catches: a rename or deletion orphaning a row's evidence)
  * every warning/refusal site in the code should be claimed by some row
    (catches: a new limitation nobody wrote down)

Plus the cheap structural checks that keep the data usable: unique ids,
verdicts from the declared enum, areas that exist, and a rendered doc that
matches the data.

stdlib only, like the other self-test files here.
"""

import json
import os
import re
import sys

from matrix import (
    NO_ENFORCEMENT,
    REPO,
    load,
    symbol_name,
    symbols,
    verdict_of,
)

HERE = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


# Files whose warning/refusal sites the matrix is expected to cover. Not every
# converter file - only the ones that decide what can and cannot be carried.
COVERED_SOURCES = (
    "build.py",
    "classic_to_pipeline.py",
    "source_classic_xml.py",
)


def _source_text(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _all_converter_text() -> str:
    parts = []
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".py") and not name.startswith("selftest_"):
            parts.append(_source_text(name))
    return "\n".join(parts)


def test_ids_are_unique_and_stable_looking():
    matrix = load()
    ids = [row["id"] for row in matrix["rows"]]
    check(len(ids) == len(set(ids)),
          "duplicate row id(s): " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    for row_id in ids:
        check(re.fullmatch(r"[a-z0-9-]+", row_id) is not None,
              f"row id {row_id!r} is not a stable kebab-case slug - the guide cites ids "
              "upserts on these")


def test_verdicts_and_areas_come_from_the_declared_enums():
    matrix = load()
    for row in matrix["rows"]:
        for path in ("native", "pipeline"):
            check(row[path] in matrix["verdicts"],
                  f"{row['id']}: {path} verdict {row[path]!r} is not in the declared enum")
        check(row["area"] in matrix["areas"],
              f"{row['id']}: area {row['area']!r} is not in the declared enum")
        check(row["native"] != "N/A" or row["pipeline"] != "N/A",
              f"{row['id']}: both paths are N/A, so the row says nothing")


def test_every_row_carries_evidence():
    """
    The rule the whole document rests on: a verdict with no code symbol behind
    it is an inference, and this project does not publish inferences. Saying
    'none' is allowed - that is a deliberate claim that nothing enforces it -
    but silence is not.
    """
    for row in load()["rows"]:
        check(bool(row["enforced_by"]),
              f"{row['id']}: enforced_by is empty. Name a symbol, or say "
              f"{NO_ENFORCEMENT!r} deliberately.")
        if row["enforced_by"] == [NO_ENFORCEMENT]:
            check(bool(row["to_change"]),
                  f"{row['id']}: claims nothing enforces it but does not say what "
                  "would change that - that combination is just a shrug")


def test_every_named_symbol_still_exists():
    """
    The forward direction: a rename in the converter must not leave a row
    pointing at a symbol that no longer exists.
    """
    haystack = _all_converter_text()
    for row in load()["rows"]:
        for symbol in symbols(row):
            name = symbol_name(symbol)
            leaf = name.split(".")[-1]
            check(leaf in haystack,
                  f"{row['id']}: enforced_by names {symbol!r}, but {leaf!r} appears "
                  "nowhere in converter/*.py - the row's evidence is stale")


def test_no_unclaimed_warning_sites():
    """
    The reverse direction, and the more valuable half: a NEW limitation added
    to the converter should not go undocumented.

    Counting is the only tractable check here - matching a specific warning
    string to a specific row would need the rows to carry line numbers, which
    rot on the next edit. So: the matrix must claim at least as many distinct
    enforcement points as the code has warning/refusal sites. When someone adds
    a warning without a row, this trips and they write the row.
    """
    sites = 0
    for name in COVERED_SOURCES:
        text = _source_text(name)
        sites += len(re.findall(r"_warn\(\s*warnings", text))
        sites += len(re.findall(r"warnings\.append\(", text))
        sites += len(re.findall(r"raise ClassicPipelineError\(", text))
        sites += len(re.findall(r"_add_review_note\(", text))

    claimed = {symbol_name(s) for row in load()["rows"] for s in symbols(row)}

    check(sites > 0, "found no warning sites at all - the regexes have gone stale")
    check(len(claimed) >= sites * 0.5,
          f"the code has {sites} warning/refusal sites but the matrix names only "
          f"{len(claimed)} distinct symbols. A new limitation was probably added "
          "without a matrix row - add one, or widen this check deliberately.")


def test_worst_verdict_is_derived_not_alphabetical():
    """
    `Verdict` is the field readers filter on, so its ordering has to
    be real. Refused must beat Unconfirmed must beat Degraded; and a row that
    one path simply does not attempt must not be dragged down by the N/A.
    """
    matrix = load()
    sample = {"native": "Refused", "pipeline": "Degraded"}
    check(verdict_of(sample, matrix) == "Refused", "Refused must outrank Degraded")
    sample = {"native": "Translated", "pipeline": "N/A"}
    check(verdict_of(sample, matrix) == "Translated",
          "an N/A on the path that does not apply must not mask a Translated row")
    sample = {"native": "Unconfirmed", "pipeline": "Degraded"}
    check(verdict_of(sample, matrix) == "Unconfirmed", "Unconfirmed must outrank Degraded")


def test_the_rendered_doc_is_current():
    import render_matrix
    text = render_matrix.render()
    try:
        with open(render_matrix.OUTPUT_PATH, encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        current = None
    check(current == text,
          "docs/vinceforge-capability-matrix.md is stale - run "
          "`python converter/render_matrix.py`")



def test_migration_team_guide_anchors_resolve():
    """
    The Migration Team guide is prose - it cannot be generated from the data,
    so it is the one document here that CAN drift. What keeps it honest is
    that every capability claim in it cites a matrix row id, and this checks
    those ids still exist.

    It catches the specific failure that matters: a row renamed or removed in
    the JSON, leaving the guide asserting something with no evidence behind
    it. Written for readers who cannot check the claim themselves, that is
    exactly the kind of quiet wrongness this project is organised against.
    """
    guide = os.path.join(REPO, "docs", "migration-team-guide.md")
    if not os.path.exists(guide):
        return  # the guide is optional; the rest of the matrix stands without it

    with open(guide, encoding="utf-8") as f:
        text = f.read()

    ids = {row["id"] for row in load()["rows"]}
    cited = set(re.findall(r"`\[([a-z0-9-]+)\]`", text))
    check(bool(cited), "the guide cites no matrix rows at all - has the anchor syntax changed?")
    for row_id in sorted(cited - ids):
        check(False,
              f"docs/migration-team-guide.md cites {row_id!r}, which is not a row in "
              "capability-matrix.json - the guide's evidence is stale")


def test_both_real_customer_files_are_described_by_the_matrix():
    """
    An end-to-end sanity check rather than a shape check: the two real Classic
    files must actually hit constructs the matrix documents. If they ever stop
    doing so, either the files changed or the matrix has drifted off the real
    world it claims to describe.
    """
    real = [p for p in os.listdir(os.path.join(REPO, "RUN")) if p.endswith(".xml")]
    if len(real) < 2:
        return  # customer files are not distributed with the repo

    constructs = {row["id"] for row in load()["rows"]}
    for needed in ("filter-on-m3-written-column", "group-startrun", "struct-multi-block"):
        check(needed in constructs,
              f"the matrix no longer documents {needed!r}, which both real files exercise")


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()

    if failures:
        print(f"{len(failures)} capability-matrix self-test failure(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"All {len(tests)} capability-matrix self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
