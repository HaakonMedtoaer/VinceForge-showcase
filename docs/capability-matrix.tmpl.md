<!--
  TEMPLATE for docs/vinceforge-capability-matrix.md.

  Edit the PROSE here. Do NOT edit the generated .md - it is overwritten.
  The rows come from docs/capability-matrix.json; {{TABLE:<Area>}} is
  replaced by that area's rendered table. Run: python converter/render_matrix.py
-->
# VinceForge capability matrix — what converts, what degrades, what doesn't

<!-- GENERATED FILE - edit docs/capability-matrix.json (rows) or
     docs/capability-matrix.tmpl.md (prose), then run render_matrix.py. -->

**Audience:** anyone deciding whether a Classic function or a proposed
workflow architecture can be carried onto Vince Live *with this tool*, before
committing to it in front of a customer. Written to be readable without
access to the code.

**Status of this document:** it describes **VinceForge's coverage**, not the
Live platform. For what the platform itself can do, see
`vince-classic-to-live-migration-summary-MERGED-v2.md` and
`docs/vince-live-workflows-and-tables-guide.md`. A construct can be perfectly
expressible in Live and still be listed here as not translated — that gap is
this tool's, and the "To change it" column says what closing it would take.

---

## 0. How to read this — and the one rule

**This document is not the verdict. The converter is.**

Every row below is anchored to the code symbol that actually enforces it, so
any claim here can be checked in seconds. If this document and the code ever
disagree, **the code is right and this document is a bug**. That is deliberate:
this project's founding rule is that no structural claim about Live is ever
made by inference, and a capability doc that drifts from the converter is
exactly that kind of inference wearing a confident voice. (See
`docs-unverified/README.md` for what that failure mode cost us once already.)

`converter/selftest_matrix.py` enforces the anchoring mechanically: every
symbol named below must still exist in the code, and every warning or refusal
site in the code must be claimed by some row.

So use this document to **predict and to explain**, and the CLI to **decide**:

```bash
# 1. Read-only triage of a folder of candidate files. No HTTP, writes nothing.
python converter/cli.py app-scan <folder>

# 2. The actual verdict for one file, on the native path.
#    Dummy ids are fine - nothing is sent anywhere.
python converter/cli.py classic-xml <file.xml> DUMMY DUMMY DUMMY out.json

# 3. If the native path degrades it, try the generic pipeline path.
python converter/cli.py classic-pipeline <file.xml> DUMMY DUMMY DUMMY out.json \
    --base-url <ion_api_root>
```

The warnings those print, plus the `_needs_review` keys inside the generated
JSON, **are** the per-file answer. This matrix tells you what they mean and
what to do about them.

Two things that are *not* failures, and are easy to misread as such:

- **`TODO: metadata not found for <program>.<transaction>.<field>`** — you
  converted without a bearer token, so field metadata fell back to the
  two-entry static table. It says nothing about representability. Re-run with
  a token.
- **A conversion with warnings still produced a workflow.** Warnings mean
  "this differs from the source in a way you must look at", not "this failed".

And one thing that *is* worth stating plainly: **a clean conversion is not a
passing test.** It means the shape is expressible. Whether it behaves
correctly against real M3 data is only ever settled by running it in a tenant
— and when you do, mind the Excel add-in's refresh/propagation delay
(`CLAUDE.md`, Auth / targeting), which has already produced one false
"the fix didn't work" verdict.

**The unit of migration is the BLOCK, not the file.** A multi-block Classic
file fans out into one workflow per block, and they do not share a verdict —
`C05_MMS100` converts its first block completely clean and needs the pipeline
path for its second. Triage per block.

### Verdict vocabulary

{{LEGEND}}

### The two paths

|  | **Native** (`classic-xml` → `build.py`) | **Generic pipeline** (`classic-pipeline` → `classic_to_pipeline.py`) |
|---|---|---|
| Shape | trigger → [generic filter] → m3_api → excel | trigger → one Code step |
| Prefer it when | it fits — always the default | the native path degrades the control flow |
| Gives you | field-metadata validation, pagination, `excel_1`, builder-UI visibility, strong save-time validation | per-call gates, gates that read an earlier call's output, `<BasedOnTransaction>`, grouping |
| Costs you | per-call conditional logic | Excel step, metadata validation, pagination, builder-UI visibility; sequential HTTP under a 29s synchronous ceiling |
| Extra input needed | — | the tenant's ION API root (nothing can derive it) |

---

## 1. Conditional logic (row filters)

The single most common reason a real Classic file can't go on the native path.

**Why:** Live's `GENERIC_FILTER` sits between the trigger and the M3 API step,
runs **once**, and sees **only raw spreadsheet rows**. It cannot gate one call
while the next runs unconditionally, and it can never see a value an M3 call
produced.

{{TABLE:Filters}}

> **Reading the shortlist:** both real customer files gate on M3-written
> columns — `C05` on `P (written by AUTCHKMI.ChkAuthority)` and `Q`,
> `Existing Price Lists` on `BK`/`BM`/`BO`. That shape is not rare. If a
> shortlisted function has conditions that depend on what an earlier M3 call
> returned, expect the pipeline path.

---

## 2. Call chaining and dependencies

{{TABLE:Chaining}}

---

## 3. Grouping

Confirmed Classic semantics (2026-09-10, from a real run): the `<StartRun>`
call fires **once per group**, on the group's **first row**; group breaks are
detected by comparing only to the **previous** row, so the sheet must be
sorted by the group columns; its `<Cell>` result lands on that first row alone
with no fill-down.

{{TABLE:Grouping}}

---

## 4. File and block structure

{{TABLE:Structure}}

---

## 5. Excel / sheet layout

The pipeline path has **no Excel step at all** — VXL Live sends the rows it
has selected, so sheet and start row are decided there.

{{TABLE:Excel}}

---

## 6. Per-call flags

{{TABLE:Flags}}

---

## 7. Beyond Classic XML

{{TABLE:Beyond Classic}}

---

## 8. Hard blockers — no path does these

Listed separately because no amount of converter work fixes them from here;
each needs a platform capability or a confirmed fact that doesn't exist yet.

{{BLOCKERS}}

---

## 9. Keeping this honest

Because this document restates what the code enforces, it can drift. Three
guards, and none is optional:

- **Every row names a code symbol**, or says `none` deliberately. A row with
  an unsourced verdict is exactly the inference this project forbids.
- **`converter/selftest_matrix.py`** checks that mechanically, both ways:
  named symbols must still exist, and warning/refusal sites in the code must
  be claimed by a row. Drift becomes a test failure rather than something
  nobody notices for six months.
- **The converter self-tests are the baseline** — `selftest_classic.py`,
  `selftest_pipeline.py`, `selftest_classic_pipeline.py`. The last one
  actually *executes* the generated Code step under node against a fake M3.

When a real file reveals something this matrix gets wrong, the durable fix is
to distil it into a synthetic fixture under `RUN/synthetic/` plus an assertion
in the relevant self-test — then correct the row. Not the other way round.
