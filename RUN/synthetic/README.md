# Synthetic fixtures — not captured data, not evidence

Everything in this folder was **hand-authored to exercise the converter**.
None of it came out of a real Classic installation, a real tenant, or a
real API response. That makes it the opposite of the rest of `RUN/`, which
holds real captures and real customer exports.

The distinction matters because of this project's one rule (see
`CLAUDE.md`): every confirmed structural fact came from building a real
example or reading a real response, never from inference. A fixture in
here can look exactly like a real capture and carry none of that
authority. So:

- **Do not cite a file in here as a source** for how Classic or Live
  behaves, in a doc, a comment, or a commit message.
- **Do not infer behaviour from one.** If a fixture shows two constructs
  co-occurring, that proves the converter handles the combination — not
  that Classic ever produces it.
- **Do not import one into a real Classic/VXL installation** expecting it
  to run. Conformance to `VinceExcelConfig.xsd` has never been checked for
  anything in here.

What they *are* good for: regression coverage. The two real Classic files
in `RUN/` each exercise only part of the parser, and neither covers
everything at once, so a change to `source_classic_xml.py` or `build.py`
can be checked against one file instead of hoping the real pair happens to
cover the affected path.

Each file states its own provenance in a header comment — which real file
every element and field name was copied from, and which parts are
recombination rather than observation. Keep that up to date if you add
one, and keep the borrowed-vocabulary discipline: build a fixture out of
constructs the real files actually contain, rather than inventing markup,
or it stops testing anything real.

## Contents

- **`classic-coverage.xml`** — one Classic export exercising every
  construct the converter knows about: two `<Import>` blocks with
  conflicting `InputStartRow`/`MessageColumn` (so it must split into two
  workflows), `<ImportHeading>`/`<StartRun>` grouping both with and
  without a `<Cell>`, `<Cell>`s at ImportDetail level, all four IR source
  kinds, `Source="UserID"`, a block-level `RowFilterEQ`, a per-call
  `RowFilterEQ` gating on an earlier call's output, a `RowFilterGT` with
  `IncludeBlank`, `TransactionFailed="true"` (get-or-create), and the same
  `(program, transaction)` twice in one block to pin down the
  nearest-preceding-occurrence reference rule.
