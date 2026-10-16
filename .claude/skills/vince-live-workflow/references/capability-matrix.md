# Capability matrix — verdicts for common conditional-logic / chaining constructs

Condensed from VinceForge's own capability matrix (a tool that migrates VXL
Classic exports to Vince Live workflows). Useful for the **verify** side of
this skill: if a proposed design (or a description of an existing Classic /
legacy function) needs one of the constructs below, this table says whether
Live's **native M3 API step** or the **generic pipeline path**
(`m3-generic-pipeline.md`) can express it, and why.

This describes what is **possible on the real platform**, distilled from
confirmed facts — it is not describing "a tool's coverage" the way the
source matrix originally was; read every verdict here as "can Live express
this at all, on this path", using the same confirmed mechanics documented
in `m3-native-api-step.md` and `m3-generic-pipeline.md`.

### Verdict vocabulary

| | Meaning |
|---|---|
| ✅ **Possible** | Expressible on this path with confirmed mechanics. |
| ⚠️ **Possible, with a caveat** | Expressible, but something about it differs from a naive reading — read the note. |
| 🚫 **Not possible** | This path genuinely cannot express it — structural, not a missing feature. |
| ❓ **Unconfirmed** | Nobody has confirmed what the underlying construct even means; don't guess an implementation for it. |

## Conditional logic (row-level filters / gates)

| Need | Native M3 API step (`GENERIC_FILTER`) | Generic pipeline (JSONata predicate) |
|---|---|---|
| Gate the whole call set on `column = value` / `column != value` | ✅ **Possible** — `GENERIC_FILTER`'s confirmed operator set is exactly `equal`/`notEqual` | ✅ **Possible** |
| Gate on `>`, `<`, `>=`, `<=` | 🚫 **Not possible** — `GENERIC_FILTER` only confirms `equal`/`notEqual`; not a config option, a real platform limit | ✅ **Possible** — any JSONata comparison, with `$number()` wrapping to avoid lexical comparison of string values |
| Gate one M3 call while another runs unconditionally | 🚫 **Not possible** — `GENERIC_FILTER` runs once, before the whole M3 API step, never per-transaction | ✅ **Possible** — a JSONata predicate in each call's own Transform |
| Gate on a value an *earlier* M3 call in this workflow produced | 🚫 **Not possible, structurally** — no placement of `GENERIC_FILTER` can see M3 output; it only ever sees raw spreadsheet rows, at any operator, not just comparisons | ✅ **Possible** — index-correlated `$filter` against a prior step's `results.records[$i]` (fragile: needs unbroken 1:1 row alignment — see `m3-generic-pipeline.md` §6) |
| A hidden/fixed (non-user-editable) filter value | 🚫 **Not possible** — every confirmed `GENERIC_FILTER` example creates an *editable* trigger field; no confirmed fixed/hidden variant exists | ⚠️ **Possible, with a caveat** — bake the value into the Code/Transform logic itself rather than exposing it as a trigger field; this is a design choice, not a platform-confirmed "hidden filter" feature |
| Case-insensitive comparison | 🚫 **Not possible** — no confirmed case-insensitive operator or flag on `GENERIC_FILTER` | ✅ **Possible** — wrap both sides in `$lowercase()` |
| Two different filters on the same raw column | ⚠️ **Possible, with a caveat** — Live keys Generic Filter fields by column letter, so two conditions on the same column collide; only one can be expressed per column on this step | ✅ **Possible** — no such collision; JSONata predicates compose freely |

## Chaining and dependencies

| Need | Native M3 API step | Generic pipeline |
|---|---|---|
| Run call B only after call A succeeds | ✅ **Possible** — `basedOnTransaction` pointing at A's instance id | ✅ **Possible** — check A's per-call `nrOfFailedTransactions`/status before running B (needs the one-call-per-row Code-step shape for a true per-call verdict — see `m3-generic-pipeline.md` §8) |
| Get-or-create (create only if a prior Get failed) | ✅ **Possible** — `basedOnTransaction` + `runOnFailure: true`, a real confirmed idiom | ✅ **Possible** — same idea, gated on the Get's failure status |
| One call's output feeding another call's input | ✅ **Possible** — `source: "API"` input field with a `ref` to the producer, and the producer must declare that field in its own `output` even without a column mapping | ✅ **Possible** — reference the earlier step's output directly in a later Transform/predicate |
| A call whose (program, transaction) repeats more than once in the same step | ✅ **Possible** — each occurrence gets its own instance id; a later reference should resolve to the nearest preceding occurrence | ✅ **Possible** — no id collision risk on this path |

## Grouping (header-row-fires-once, detail-rows-fire-per-row patterns)

| Need | Native M3 API step | Generic pipeline |
|---|---|---|
| A call that fires once per group of rows (not once per row) | 🚫 **Not possible** — Live's M3 API step has no group concept at all; every transaction runs once per row it's bound to | ✅ **Possible** — a Code step can implement group-break detection explicitly (compare only to the previous row, so the input must be sorted by the group columns) |
| A grouped call's result reaching every row in its group, not just the triggering row | — (N/A on this path) | ✅ **Possible** — confirmed against a real run: a once-per-group call's result correctly carried to every row sharing that group key |

## Excel / sheet-layout dependent behaviour

| Need | Native M3 API step | Generic pipeline |
|---|---|---|
| Per-row status/message written back to a spreadsheet column | ✅ **Possible** — an `excel_1` output column, or the auto-injected `<id>_MSG_COL` | 🚫 **Not possible directly** — no Excel step on this path; the equivalent is a richer per-row status field in the returned record array, not a spreadsheet column |
| A row limit on a list-style call | ✅ **Possible** — `maxRows` on the transaction (`0` = unlimited) | ⚠️ **Possible, with a caveat** — no native equivalent; the row count is simply whatever the triggering input sends |

## Flags whose real-platform meaning is unconfirmed — do not implement behaviour for these

- **"Critical" / abort-on-failure semantics** on a call or a group of calls:
  whether such a flag aborts just that row, the whole batch, or leaves a
  half-written record behind is **unconfirmed**. If a design mentions
  "critical" or "must not fail silently" semantics, say plainly that Live's
  actual behaviour here needs a real test (a deliberately failing call,
  observed directly) before it can be relied on — do not assume any
  particular abort scope.
- **Include-blank / include-empty variants of a range filter** (e.g. "greater
  than, but also include blanks"): the combination reads self-contradictory
  and its real meaning is **unconfirmed**. Flag rather than guess.

## How to use this file

1. Identify which construct(s) the proposed design needs from the tables
   above.
2. If every one is ✅ **Possible** on the native path, that's the
   recommendation — it has the strongest save-time validation.
3. If any is 🚫 **Not possible** on native but ✅ on the pipeline path,
   recommend the pipeline path for that part of the design (or the whole
   workflow, if the native and pipeline halves can't be usefully split —
   they generally can't, since a workflow is one linear chain).
4. If something needed isn't covered by any row here, say explicitly that
   it's outside what this skill's bundled facts cover, and describe the
   verification path (see `SKILL.md`) rather than guessing a verdict.
