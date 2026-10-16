"""
Render docs/vinceforge-capability-matrix.md from the data + the template.

Deterministic and LLM-free by design: this is the step that turns rows into
prose, and it is exactly the step where an LLM could quietly reword a verdict.
Run it, diff it, commit it.

    python converter/render_matrix.py            # write the doc
    python converter/render_matrix.py --check    # fail if the doc is stale

--check is what the pre-push hook uses: it never rewrites anything, it just
reports that the committed doc no longer matches the data.
"""

import sys

from matrix import (
    NO_ENFORCEMENT,
    OUTPUT_PATH,
    TEMPLATE_PATH,
    load,
    rows_by_area,
    symbols,
    verdict_of,
)


def _cell(verdict: str, note: str, icons: dict) -> str:
    """One path's column: the icon, the word, and any qualifying note."""
    icon = icons[verdict]
    if verdict == "N/A":
        return f"{icon} {note}" if note else icon
    text = f"{icon} **{verdict}**"
    return f"{text} — {note}" if note else text


def _symbols_cell(row) -> str:
    named = symbols(row)
    if not named:
        return f"*{NO_ENFORCEMENT}* — nothing in the code enforces this"
    return ", ".join(f"`{s}`" for s in named)


def _table(rows, icons) -> str:
    lines = [
        "| Construct | Native | Pipeline | Enforced by | To change it |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| {} | {} | {} | {} | {} |".format(
            row["construct"].replace("|", "\\|"),
            _cell(row["native"], row["native_note"], icons),
            _cell(row["pipeline"], row["pipeline_note"], icons),
            _symbols_cell(row),
            row["to_change"] or "—",
        ))
    return "\n".join(lines)


def _legend(matrix) -> str:
    icons = matrix["verdict_icons"]
    meanings = {
        "Translated": "Expressed faithfully. No action needed.",
        "Degraded": ("Something is emitted, but it differs from the source. The converter "
                     "always says so. Read the warning."),
        "Reported only": ("Parsed and flagged, never translated. The behaviour is simply "
                          "absent from the result."),
        "Refused": "Conversion stops with an error naming the reason.",
        "Unconfirmed": ("Nobody knows what the source construct means. Deliberately not "
                        "guessed at."),
        "N/A": "Does not apply to this path.",
    }
    lines = ["| | Meaning |", "|---|---|"]
    for verdict in matrix["verdicts"]:
        lines.append(f"| {icons[verdict]} **{verdict}** | {meanings[verdict]} |")
    return "\n".join(lines)


def _blockers(matrix) -> str:
    """
    The hard-blocker list, derived rather than hand-kept.

    A blocker is a row no path carries AND whose "to change it" names real
    outside work. Deriving it means the list cannot fall out of step with the
    table above it - the failure mode a hand-written summary always eventually
    has.
    """
    blockers = [
        row for row in matrix["rows"]
        if "Translated" not in (row["native"], row["pipeline"])
        and row["to_change"]
        and not row["to_change"].startswith(("nothing", "by design"))
    ]
    lines = []
    for index, row in enumerate(blockers, start=1):
        lines.append(f"{index}. **{row['construct']}** — {row['to_change']}")
    return "\n".join(lines)


def render() -> str:
    matrix = load()
    icons = matrix["verdict_icons"]
    grouped = rows_by_area(matrix)

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        text = f.read()

    # The template opens with an HTML comment telling whoever edits it what
    # the placeholders are. That guidance is for the template, not for the
    # reader of the generated doc - and it necessarily contains a literal
    # placeholder, which would otherwise trip the leftover check below.
    if text.startswith("<!--"):
        text = text[text.index("-->") + len("-->"):].lstrip("\r\n")

    for area, rows in grouped.items():
        placeholder = "{{TABLE:" + area + "}}"
        if placeholder not in text:
            raise SystemExit(f"Template is missing {placeholder} - every area needs a table.")
        text = text.replace(placeholder, _table(rows, icons))

    text = text.replace("{{LEGEND}}", _legend(matrix))
    text = text.replace("{{BLOCKERS}}", _blockers(matrix))

    if "{{" in text:
        leftover = text[text.index("{{"):][:40]
        raise SystemExit(f"Unreplaced placeholder in the template: {leftover!r}")

    # Every row must reach the reader. A row in the JSON whose area has no
    # placeholder would vanish silently, which is precisely the kind of quiet
    # loss this whole document exists to prevent.
    rendered_rows = sum(text.count("| " + row["construct"].replace("|", "\\|") + " |")
                        for row in matrix["rows"])
    if rendered_rows < len(matrix["rows"]):
        raise SystemExit(f"Only {rendered_rows} of {len(matrix['rows'])} rows reached the output.")

    return text


def main() -> int:
    text = render()
    check_only = "--check" in sys.argv

    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        current = None

    if current == text:
        print(f"Capability matrix is up to date ({len(load()['rows'])} rows).")
        return 0

    if check_only:
        print("STALE: docs/vinceforge-capability-matrix.md no longer matches "
              "docs/capability-matrix.json or the template.")
        print("Run: python converter/render_matrix.py")
        return 1

    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"Wrote {OUTPUT_PATH} ({len(load()['rows'])} rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
