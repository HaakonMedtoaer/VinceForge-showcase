"""
Shared loading/derivation for the capability matrix.

The matrix is DATA (docs/capability-matrix.json) with prose around it
(docs/capability-matrix.tmpl.md). Two consumers share this module:

  render_matrix.py    -> the human-readable markdown doc
  selftest_matrix.py  -> the drift check against the code

Kept deliberately dumb: no LLM, no network, no inference. A verdict is
whatever the JSON says, and the JSON is only ever edited by a human (or by
an agent proposing a change a human accepts) after checking the code. See
the _comment block at the top of the JSON for the editing rules.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA_PATH = os.path.join(REPO, "docs", "capability-matrix.json")
TEMPLATE_PATH = os.path.join(REPO, "docs", "capability-matrix.tmpl.md")
OUTPUT_PATH = os.path.join(REPO, "docs", "vinceforge-capability-matrix.md")

# A row whose "enforced_by" is exactly this is declaring "nothing in the code
# enforces this, deliberately" - the not-built cases. It is a real answer, not
# a missing one, so the drift check accepts it and the renderer prints it.
NO_ENFORCEMENT = "none"


def load():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def verdict_of(row, matrix):
    """
    The single field people filter on: the WORSE of the two paths.

    "Worse" is ordered by verdict_severity in the JSON, not alphabetically
    and not by enum position - Refused must outrank Unconfirmed must outrank
    Degraded, or sorting by verdict gives nonsense. N/A ranks just above
    Translated so that a capability only one path even attempts is judged on
    the path that attempts it.
    """
    severity = matrix["verdict_severity"]
    worst = max((row["native"], row["pipeline"]), key=lambda v: severity[v])
    # A row where one path is fine and the other simply does not apply is as
    # good as its working path - don't let "N/A" drag a Translated row down.
    if worst == "N/A":
        return "Translated" if "Translated" in (row["native"], row["pipeline"]) else "N/A"
    return worst


def symbols(row):
    """The code symbols a row claims, minus the deliberate 'none'."""
    return [s for s in row["enforced_by"] if s != NO_ENFORCEMENT]


def symbol_name(symbol: str) -> str:
    """
    The greppable part of an "enforced_by" entry.

    Entries are written for a human to read, so they may carry a
    parenthetical locator: "build.build_live_workflow (the is_uniform test)".
    Only the dotted path is a real identifier; the parenthetical is a hint
    about WHERE inside it to look, and is not checked.
    """
    return symbol.split("(")[0].strip()


def rows_by_area(matrix):
    """Rows grouped in the JSON's declared area order, preserving row order."""
    grouped = {area: [] for area in matrix["areas"]}
    for row in matrix["rows"]:
        grouped[row["area"]].append(row)
    return grouped
