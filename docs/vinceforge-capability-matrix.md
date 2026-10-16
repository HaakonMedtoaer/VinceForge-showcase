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

| | Meaning |
|---|---|
| ✅ **Translated** | Expressed faithfully. No action needed. |
| ⚠️ **Degraded** | Something is emitted, but it differs from the source. The converter always says so. Read the warning. |
| 🚫 **Reported only** | Parsed and flagged, never translated. The behaviour is simply absent from the result. |
| ⛔ **Refused** | Conversion stops with an error naming the reason. |
| ❓ **Unconfirmed** | Nobody knows what the source construct means. Deliberately not guessed at. |
| — **N/A** | Does not apply to this path. |

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

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| RowFilterEQ / RowFilterNE, block level (sibling of the details) | ✅ **Translated** | ✅ **Translated** | `build.REPRESENTABLE_FILTER_OPERATORS`, `ir.Workflow.row_filters` | — |
| RowFilterEQ / RowFilterNE, per-call, identical on every call | ✅ **Translated** | ✅ **Translated** | `build.build_live_workflow (the is_uniform test)` | — |
| RowFilterEQ / RowFilterNE, per-call, NOT uniform | 🚫 **Reported only** — call runs unconditionally, flagged per transaction | ✅ **Translated** — each call gated independently | `build.build_live_workflow (the is_uniform test)` | a converter change that splits transactions across several M3 API steps so each gets its own Generic Filter |
| RowFilterGT | ✅ **Translated** — numeric value only: emitted as a NUMBER comparison (run-confirmed numeric, not lexical, 2026-09-28). A non-numeric value is [filter-range-non-numeric] | ✅ **Translated** | `build.native_filter_problem`, `build.RANGE_FILTER_OPERATORS`, `pipeline_ir.GATE_OPERATORS` | Untested: what a NUMBER range does with a blank or non-numeric CELL (the value side is always numeric here) - one run with such a cell settles it. |
| RowFilterGE | ✅ **Translated** — numeric value only: emitted as a NUMBER comparison (run-confirmed numeric, not lexical, 2026-09-28). A non-numeric value is [filter-range-non-numeric] | ✅ **Translated** | `build.native_filter_problem`, `build.RANGE_FILTER_OPERATORS`, `classic_to_pipeline.FILTER_OPERATORS`, `pipeline_ir.GATE_OPERATORS` | Untested: what a NUMBER range does with a blank or non-numeric CELL (the value side is always numeric here) - one run with such a cell settles it. |
| RowFilterLT | ✅ **Translated** — numeric value only: emitted as a NUMBER comparison (run-confirmed numeric, not lexical, 2026-09-28). A non-numeric value is [filter-range-non-numeric] | ✅ **Translated** | `build.native_filter_problem`, `build.RANGE_FILTER_OPERATORS`, `classic_to_pipeline.FILTER_OPERATORS`, `pipeline_ir.GATE_OPERATORS` | Untested: what a NUMBER range does with a blank or non-numeric CELL (the value side is always numeric here) - one run with such a cell settles it. |
| RowFilterLE | ✅ **Translated** — numeric value only: emitted as a NUMBER comparison (run-confirmed numeric, not lexical, 2026-09-28). A non-numeric value is [filter-range-non-numeric] | ✅ **Translated** | `build.native_filter_problem`, `build.RANGE_FILTER_OPERATORS`, `classic_to_pipeline.FILTER_OPERATORS`, `pipeline_ir.GATE_OPERATORS` | Untested: what a NUMBER range does with a blank or non-numeric CELL (the value side is always numeric here) - one run with such a cell settles it. |
| GT/GE/LT/LE against a non-numeric value (e.g. '!', hundreds in a real CustomerA file set) | 🚫 **Reported only** — gate left off; app import routes the block to the pipeline path | 🚫 **Reported only** — not applied, warned - the pipeline compares ranges numerically, so rendered literally the call would never run. '!' very likely means 'not blank' under a text comparison; named in the warning, not mapped | `build.native_filter_problem`, `classic_to_pipeline.translate_classic_to_pipeline (the non-numeric range branch)` | confirm what Classic compares a range as (text vs number). If '!' is confirmed as 'not blank', map it to Live's isNotBlank (captured) natively and the pipeline's notEmpty gate; a STRING range in Live is still untested |
| Any other RowFilter* wrapper | 🚫 **Reported only** — reported as an unknown operator, never guessed | 🚫 **Reported only** — same | `source_classic_xml._is_filter_wrapper`, `classic_to_pipeline.FILTER_OPERATORS` | meet one in a real file, then confirm its semantics before mapping it |
| Filter on a column an M3 call WRITES (<Cell>) | 🚫 **Reported only** — never emitted, at either level | ✅ **Translated** — gates can read an earlier call's result | `build._api_written`, `build.build_live_workflow (column_written_by)` | nothing - no placement of a Generic Filter can see M3 output. Structural. |
| Two different filters on the same column | ⚠️ **Degraded** — only the first applies, warned | ✅ **Translated** | `build.build_live_workflow (the clash check)` | Live keys Generic Filter fields by column; needs a confirmed alternative |
| Filter value hidden in Classic (not user-editable) | ✅ **Translated** — a CONSTANT condition (literal in value and defaultValue, no trigger field) - the fixed filter confirmed by a real run. On a range it is CONSTANT + NUMBER, each confirmed but never captured together | ⚠️ **Degraded** — baked into the Code step, so fixed rather than editable | `ir.RowFilter.is_hidden`, `build.build_live_workflow (_filter_condition)` | capture one CONSTANT + NUMBER condition to close the last pairing gap |
| IsLower (case-insensitive compare) | 🚫 **Reported only** — not applied | 🚫 **Reported only** — not applied | `ir.RowFilter.is_lower`, `build._describe_filter` | confirm the flag's meaning, then find a confirmed case-insensitive Live operator |
| IncludeBlank="true" | ❓ **Unconfirmed** — carried, not applied | ❓ **Unconfirmed** — carried, not applied, warned | `ir.RowFilter.include_blank` | confirm what it means in Classic - on a GT with an empty Value it currently reads self-contradictory |
| GT/GE/LT/LE against an empty value | 🚫 **Reported only** | 🚫 **Reported only** — not applied, warned | `classic_to_pipeline.translate_classic_to_pipeline (the empty-value branch)` | settle the meaning on the Classic side first |
| A block-level filter that resolves to a call result | — | ⛔ **Refused** | `classic_to_pipeline.ClassicPipelineError` | nothing - it cannot be evaluated before the first call has run |

> **Reading the shortlist:** both real customer files gate on M3-written
> columns — `C05` on `P (written by AUTCHKMI.ChkAuthority)` and `Q`,
> `Existing Price Lists` on `BK`/`BM`/`BO`. That shape is not rare. If a
> shortlisted function has conditions that depend on what an earlier M3 call
> returned, expect the pipeline path.

---

## 2. Call chaining and dependencies

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| <BasedOnTransaction> (run after success) | ✅ **Translated** — basedOnTransaction | ✅ **Translated** — call-status gate | `ir.Transaction.based_on`, `pipeline_ir.CALL_STATUS_OK` | — |
| <BasedOnTransaction TransactionFailed="true"> (get-or-create) | ✅ **Translated** — runOnFailure | ✅ **Translated** | `ir.Transaction.based_on_failed`, `pipeline_ir.CALL_STATUS_FAILED` | — |
| Dependency on a call not in this pipeline (an <Export>, or unresolved) | — | 🚫 **Reported only** — runs whenever its own filters allow, warned | `classic_to_pipeline.translate_classic_to_pipeline (the based_on resolution branch)` | — |
| API-chained input (one call's output feeding another's input) | ✅ **Translated** — including the synthetic-output-column fix for fields never mapped to a <Cell> | ✅ **Translated** | `ir.InputField.source_kind == "API_CHAIN"`, `build.build_live_workflow (_next_synthetic_column)` | — |
| Same (program, transaction) appearing twice in one block | ✅ **Translated** — resolved to the nearest preceding occurrence | ✅ **Translated** | `build.build_live_workflow (per-occurrence id_by_ir_id)` | — |
| A forward reference (a call depending on a later one) | — | ⛔ **Refused** | `classic_to_pipeline.ClassicPipelineError` | — |

---

## 3. Grouping

Confirmed Classic semantics (2026-09-10, from a real run): the `<StartRun>`
call fires **once per group**, on the group's **first row**; group breaks are
detected by comparing only to the **previous** row, so the sheet must be
sorted by the group columns; its `<Cell>` result lands on that first row alone
with no fill-down.

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| <ImportHeading GroupColumns> + <StartRun> | 🚫 **Reported only** — not translated - the call runs ONCE PER ROW | ✅ **Translated** | `ir.Workflow.group_columns`, `ir.Transaction.runs_once_per_group` | Live documents a 'Group by' option on the M3 API step, and every captured transaction carries hasGrouping/extendedGrouping - but always false/[], so neither the populated JSON nor its runtime semantics (once per group? previous-row break or all equal values?) is confirmed. One hand-built Group-by workflow, captured and run on a sorted and an unsorted sheet, settles whether native can carry this. Until then: the pipeline path. |
| ...where the grouped call only READS | ⚠️ **Degraded** — same answer per row, more HTTP calls; the output column carries a value on every row where Classic left it blank | ✅ **Translated** | `build._READ_ONLY_TRANSACTION_PREFIXES` | — |
| ...where the grouped call may WRITE | 🚫 **Reported only** — ONE RECORD PER ROW instead of one per group - flagged, do not run unchecked | ✅ **Translated** | `build._READ_ONLY_TRANSACTION_PREFIXES` | — |
| How far a once-per-group call's result reaches | — | ✅ **Translated** — carried to the whole group; CONFIRMED 2026-09-11 against a real run | `classic_to_pipeline.GROUP_RESULT_SCOPE` | — |
| More than one <ImportHeading> per block | ⚠️ **Degraded** — the parser reads one per block, warned | ⚠️ **Degraded** — same | `source_classic_xml.peek_classic_xml` | never observed in a real file - check by hand if you meet one |

---

## 4. File and block structure

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| One <Export> + one <Import> | ✅ **Translated** — one workflow | ✅ **Translated** — the Import half | `source_classic_xml._parse_raw_units` | — |
| Several blocks in one file | ✅ **Translated** — fans out to ONE WORKFLOW PER BLOCK | ✅ **Translated** — per unit | `source_classic_xml.parse_classic_xml_units`, `source_classic_xml.peek_classic_xml` | Live carries a single sheet, data row and filter gate per workflow while Classic states those per block. Structural. |
| Run ORDER between a file's blocks | ❓ **Unconfirmed** | ❓ **Unconfirmed** | *none* — nothing in the code enforces this | one real Classic run observing whether a file's blocks run sequentially or independently |
| An <Export>-only block | ✅ **Translated** — this is the path for it | ⛔ **Refused** — refused, pointing back at build.py | `classic_to_pipeline.ClassicPipelineError` | by design |
| An <Expand> block (reads one sheet, writes another) | 🚫 **Reported only** — not converted; named in the scan with both sheets and its call count | 🚫 **Reported only** — rebuild it by hand as an m3Expand Code step on the TARGET sheet that fetches its own source data | `source_classic_xml.EXPAND_TAG`, `source_classic_xml.CONVERTED_BLOCK_TAGS`, `source_classic_xml.peek_classic_xml` | Live is treated as having no Expand (product manager, 2026-10-16): a workflow reads and writes only its own sheet. Auto-translation would mean resolving each source-sheet column to the M3 call that fills it, then emitting an m3Expand spec. |
| Several <GetApi>/<LookupApi> calls in one <Export> | ✅ **Translated** — every one, chained on the list call's output | — | `source_classic_xml.ENRICHMENT_TAGS` | Only the first <GetApi> was read until 2026-10-16 (CustomerB); the native chain of three GETs on one LIST is built but not yet run in Live. |
| First <Export> and first <Import> on different sheets | ✅ **Translated** — two workflows; paired into one only when they use the same sheet | ✅ **Translated** | `source_classic_xml._sheets_pair`, `source_classic_xml._block_units` | — |
| A mixed Export/Import block on the pipeline path | — | ⚠️ **Degraded** — Export calls reported and left out; run the native half first | `classic_to_pipeline.translate_classic_to_pipeline` | by design |
| M3 field metadata (type, length, mandatory) behind every mapped field | ✅ **Translated** — live fetch from the target connection first; any transaction it has no answer for is filled from the offline M3 field catalog (m3-field-catalog.sqlite) and WARNED as another tenant's data; then the static stub / a TODO placeholder. Mandatory INPUT fields the source never maps are reported | — the pipeline path writes its own payloads and never looks metadata up - so an unmapped mandatory field is NOT reported there | `metadata.fetch_live_metadata`, `metadata.fill_from_catalog`, `metadata.unmapped_mandatory_warning` | the catalog does not record which tenant it was built from; M3 type D -> NUMERIC is still unconfirmed; the pipeline path could run the same mandatory check |

---

## 5. Excel / sheet layout

The pipeline path has **no Excel step at all** — VXL Live sends the rows it
has selected, so sheet and start row are decided there.

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| A <Cell> with a constant Value (no *) | 🚫 **Reported only** — not written; named in the scan | 🚫 **Reported only** | `source_classic_xml._parse_m3api`, `source_classic_xml.peek_classic_xml` | An Excel step only writes M3 output - put the value in the design file. Was read as an M3 field named after the constant until 2026-10-16. |
| SourceSheetName / TargetSheetName differ | ⚠️ **Degraded** — Live has one sheetName; target wins, warned | — no Excel step on this path | `build.build_live_workflow` | — |
| InputStartRow / OutputStartRow differ | ⚠️ **Degraded** — Live has one dataRow; output wins and the input side is off, warned | — no Excel step on this path | `build.build_live_workflow` | — |
| Data starting at row 1 or 2 | ⚠️ **Degraded** — header rows clamped to 1, warned | — no Excel step on this path | `build.build_live_workflow` | Live expects two header rows above the data row |
| MessageColumn (per-row status, block and per-call) | 🚫 **Reported only** — nothing generated | ⚠️ **Degraded** — every failed call is written to the block's column as '<call>: <M3 error text (code)>'; a call with its own MessageColumn also writes its text to that column (calls sharing one are joined with ' | '); blank on success - not Classic's own wording. Output lands in the table's own columns by header name (confirmed in real runs 2026-10-01) | `ir.Workflow.message_column`, `ir.Transaction.message_column`, `pipeline_ir.M3RowPipelineStep.message_column`, `pipeline_ir.CodeCall.message_column` | one confirmed Excel-step equivalent |
| <Cell> output columns | ✅ **Translated** — ordinary excel_1 columns, read at every level (ImportDetail, StartRun, ...) | ⚠️ **Degraded** — the Code step returns only the table's body rows, keyed by the table's header names (read at runtime from the header row above InputStartRow, or --header-row), with each Cell written under its column; rows above InputStartRow are never called and blank rows are skipped. Confirmed in real runs 2026-10-01. Still unconfirmed: the leading-column trim shift | `ir.Cell`, `pipeline_ir.M3RowPipelineStep.sheet_cells`, `pipeline_ir.M3RowPipelineStep.input_start_row`, `pipeline_ir.M3RowPipelineStep.header_row` | a run that exercises the leading-column trim |
| MaxRows | ✅ **Translated** — maxRows | 🚫 **Reported only** — not translated - the row count is whatever the sheet sends | `ir.Transaction.max_rows` | — |
| Leading blank columns in the sheet | — | ⚠️ **Degraded** — VXL trims entirely-empty LEADING columns and re-indexes from "A", shifting every later column left | `classic_to_pipeline.row_key` | platform behaviour. Bites Import-only conversions hardest, since the columns the paired <Export> would have filled are blank. |

---

## 6. Per-call flags

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| IsCritical on <ImportDetail> / <ImportHeading> | ❓ **Unconfirmed** — reported only | ❓ **Unconfirmed** — reported only | `ir.Transaction.is_critical` | ONE real Classic run with a deliberately failing critical call. The name suggests "abort on failure" - but whether it aborts the row, the block or the whole run decides whether a half-written row is left behind. Not guessable. |

---

## 7. Beyond Classic XML

| Construct | Native | Pipeline | Enforced by | To change it |
|---|---|---|---|---|
| Live JSON as a source (clone + retarget) | ✅ **Translated** | — | `source_live_json.clone_and_retarget` | — |
| Worded prompt as a source | 🚫 **Reported only** — stub only | 🚫 **Reported only** — stub only | `source_prompt.parse_prompt` | needs an LLM-assisted parse step, not a deterministic parser. See docs/vince-app-generator-handoff.md |
| Fetch a workflow out of Live | ✅ **Translated** | — | `live_fetch.fetch_workflow` | — |
| Custom Table - create | ✅ **Translated** | — | `liveapi.create_table_meta` | — |
| Custom Table - read a definition | ⚠️ **Degraded** — probed from a candidate list and validated, not confirmed | — | `liveapi.META_READ_CANDIDATES`, `liveapi.get_table_meta` | one run with a real token settles it; then collapse the candidate list to the answer |
| Custom Table - write rows | 🚫 **Reported only** — not possible | 🚫 **Reported only** — not possible | *none* — nothing in the code enforces this | one confirmed row-write endpoint |
| Custom Tables in the IR / build | 🚫 **Reported only** — no IR or build support | 🚫 **Reported only** — no IR or build support | *none* — nothing in the code enforces this | a workflow writing to a table can only come from a source that already has the Table Updater step in it |
| Excel design-file upload | ✅ **Translated** | — | `liveapi.upload_design_file` | — |
| Design-file link-back (hasExcelDesignFile / excelDesignFileKey) | ✅ **Translated** — the server does not link an upload (confirmed 2026-09-29), so app import does what the builder does: GET, set the two fields, PUT /workflows, GET to verify. Confirmed working 2026-09-29: 4 of 4 uploaded files linked in a real CustomerA import | ✅ **Translated** — same - the link lives in workflowConfig, not in a step | `appimport._upload_design_file`, `liveapi.update_workflow` | — |
| App import - create | ✅ **Translated** | — | `appimport.run_app_import` | — |
| App import - update / diff / delete | 🚫 **Reported only** — create only. Re-importing does NOT duplicate: Live rejects a used workflow name (HTTP 409) and the import skips it, as it skips an existing table - but a changed definition is left as-is, not updated | — | `appimport._is_duplicate_name_conflict` | deciding what "update an app" should mean is unstarted. Lead, docs-only (product docs): an old-API PUT /workflows (overwrite, id in body) and GET /workflows?search=workflowAlias:<name> for finding a skipped workflow's id - neither called for real yet |
| App import - cross-workflow reference remapping | 🚫 **Reported only** — not done | — | *none* — nothing in the code enforces this | no confirmed field holds a cross-workflow reference |
| App import - table rows | 🚫 **Reported only** — not done | — | *none* — nothing in the code enforces this | same as Custom Table row writing above |
| App import - per-block path selection | 🚫 **Reported only** — the fallback: a block that needed the pipeline path but got no --ion-api-base-url (or is an <Export>-only block) is still built here, with the unsupported filter gate left off and a loud warning naming why | ✅ **Translated** — auto-routed per BLOCK, not per-file (a real CustomerA file splits 3 native-safe / 5 needing it across 8 blocks), narrowly: only when a block has a RowFilter native cannot express (build.native_filter_problem - since 2026-09-28 that means a range on a non-numeric value, e.g. CustomerA' '!'; a numeric range stays native). Needs --ion-api-base-url; the other existing native gaps (non-uniform per-transaction filters, an M3-written-column filter, grouping) do NOT trigger it. Predicted at SCAN time too, from a real parse, so the plan and the confirmation box shown before a real (non-dry) import see it - not only a dry run's results. SECOND trigger since 2026-09-29: a native body over app_classic.NATIVE_BODY_SIZE_LIMIT (150 KB) is rebuilt on the pipeline path, because Live refuses oversized workflows ('Item size to update has exceeded the maximum allowed size'); this one is decided at BUILD time only (it needs the built body), so it shows in the results, not the scan | `source_classic_xml.unrepresentable_filter_operators`, `app_classic.convert_classic_xml`, `source_classic_xml.peek_classic_xml`, `app_classic.NATIVE_BODY_SIZE_LIMIT` | widen the narrow trigger to the other native gaps, if that turns out to be worth the added Code-step surface - a separate, later decision |

---

## 8. Hard blockers — no path does these

Listed separately because no amount of converter work fixes them from here;
each needs a platform capability or a confirmed fact that doesn't exist yet.

1. **GT/GE/LT/LE against a non-numeric value (e.g. '!', hundreds in a real CustomerA file set)** — confirm what Classic compares a range as (text vs number). If '!' is confirmed as 'not blank', map it to Live's isNotBlank (captured) natively and the pipeline's notEmpty gate; a STRING range in Live is still untested
2. **Any other RowFilter* wrapper** — meet one in a real file, then confirm its semantics before mapping it
3. **IsLower (case-insensitive compare)** — confirm the flag's meaning, then find a confirmed case-insensitive Live operator
4. **IncludeBlank="true"** — confirm what it means in Classic - on a GT with an empty Value it currently reads self-contradictory
5. **GT/GE/LT/LE against an empty value** — settle the meaning on the Classic side first
6. **More than one <ImportHeading> per block** — never observed in a real file - check by hand if you meet one
7. **Run ORDER between a file's blocks** — one real Classic run observing whether a file's blocks run sequentially or independently
8. **An <Expand> block (reads one sheet, writes another)** — Live is treated as having no Expand (product manager, 2026-10-16): a workflow reads and writes only its own sheet. Auto-translation would mean resolving each source-sheet column to the M3 call that fills it, then emitting an m3Expand spec.
9. **A <Cell> with a constant Value (no *)** — An Excel step only writes M3 output - put the value in the design file. Was read as an M3 field named after the constant until 2026-10-16.
10. **Data starting at row 1 or 2** — Live expects two header rows above the data row
11. **MessageColumn (per-row status, block and per-call)** — one confirmed Excel-step equivalent
12. **Leading blank columns in the sheet** — platform behaviour. Bites Import-only conversions hardest, since the columns the paired <Export> would have filled are blank.
13. **IsCritical on <ImportDetail> / <ImportHeading>** — ONE real Classic run with a deliberately failing critical call. The name suggests "abort on failure" - but whether it aborts the row, the block or the whole run decides whether a half-written row is left behind. Not guessable.
14. **Worded prompt as a source** — needs an LLM-assisted parse step, not a deterministic parser. See docs/vince-app-generator-handoff.md
15. **Custom Table - read a definition** — one run with a real token settles it; then collapse the candidate list to the answer
16. **Custom Table - write rows** — one confirmed row-write endpoint
17. **Custom Tables in the IR / build** — a workflow writing to a table can only come from a source that already has the Table Updater step in it
18. **App import - update / diff / delete** — deciding what "update an app" should mean is unstarted. Lead, docs-only (product docs): an old-API PUT /workflows (overwrite, id in body) and GET /workflows?search=workflowAlias:<name> for finding a skipped workflow's id - neither called for real yet
19. **App import - cross-workflow reference remapping** — no confirmed field holds a cross-workflow reference
20. **App import - table rows** — same as Custom Table row writing above

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
