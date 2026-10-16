# Vince Classic → Vince Live Workflow Migration — Project Summary

## The goal
Build a tool that converts Vince Classic workflow XML exports into Vince Live workflows, so existing Classic workflows (which interact with Infor M3 APIs and a spreadsheet) can be imported into Live without hand-rebuilding them in the UI.

## The approach
A standalone Python script (`classic_to_live_converter.py`) parses a Classic XML file and outputs JSON matching Live's `POST /workflows` API shape. No browser extension needed — Live has a real REST API (`GET/POST/PUT/PATCH /workflows`), so the conversion happens entirely outside the browser.

**Current status: three workflows have now gone through end-to-end, one of them a genuinely new structural combination.**
1. **"List Lines"** — a stock-take list/import workflow. Converted, created via the API, rendered correctly, and executed successfully against real M3 data.
2. **"Mass Allocation"** (`classic_v1.xml`) — an import-only workflow with no Export block, multiple transactions sharing Excel input columns, and (newly discovered this session) row-level conditional logic via `RowFilterEQ`/`RowFilterNE`. Converted and rendered correctly; the conditional-logic gap is now partially solved (see below) rather than fully closed.
3. **"Check Data"** — a small two-transaction chained workflow (`Get` → `GetAlias` via `basedOnTransaction`), with `CONSTANT`-sourced inputs and a shared Excel input column between two *different* transactions' own input fields. Converted, created, rendered correctly in the builder UI (confirmed via screenshot — correct field labels, output counts, constant values displayed inline), and **executed** — the M3 API call fired without error. (Couldn't be checked against meaningful real data, since the connected M3 backend isn't a live customer system, but this is the first confirmed real *execution* of a dependency-chained, multi-transaction workflow, not just creation/rendering.)

---

## How we worked (methodology, worth preserving)
1. Convert the Classic XML with the script.
2. POST the result to Live's API via Postman (using a short-lived bearer token captured from the browser's DevTools Network tab).
3. Open the result in Live's UI and visually inspect it, and/or execute it.
4. When something broke, capture ground truth — a GET of a known-working workflow, a HAR file of Live's own UI performing the equivalent action, a browser console error, or a manually-built minimal test workflow in the UI — and diff it against what we generated, rather than guessing.
5. Fix the script, regenerate, retest.

This "diff against real captured data" approach is what resolved nearly every bug across all three workflows, including the two brand-new filter mechanisms discovered this session (see below). **Session 2's corollary still holds: double-check the raw input parameters (`connection_id`/`tenant_id`/`environment_id`) before diffing JSON structure** — a single-character typo in one of these can look exactly like a deep structural bug.

**A related principle that proved itself again this session:** when a person's own recollection of what a target system supports turns out to be incomplete or wrong (e.g. an initial "Classic-style filters aren't available in Live's normal UI" turned out to be incomplete — they *are*, just not exactly where expected), the fix is the same as always: build a small real example and capture it, rather than either party guessing further from memory.

---

## Confirmed facts about Live's workflow JSON structure

### Required top-level fields (via real validation errors + HAR capture of Live's own UI)
- `tenantId`, `workflowId` (client generates one to satisfy validation, but **Live assigns its own ID regardless** — always read the real ID back from the response)
- `workflowName`, `workflowAlias` (same value)
- `entity: "Workflow"`
- `description` (can be empty string)
- `labels: []`
- `active: true`
- `workflowType: "STANDARD"` — **critical**: sending `"EXPRESS"` at creation time produces a broken, half-provisioned workflow. `EXPRESS` has since also been observed as the type Live's own UI assigns to disposable manually-built test workflows saved via the builder wizard — so it may simply be a different creation path's default, not exclusively a "later transition" state as originally thought. Still, `"STANDARD"` remains the confirmed-correct value for anything created via the API directly.
- `hasError: false` — **this is a client-computed flag, not server-validated.** Live's own UI sends `true` at creation and only sends `false` once its own validation decides the workflow looks complete. We must set this ourselves since we bypass that UI validation.
- `tags: { connectionId: [...], apiEnvironment: <id>, environments: [<id>], primaryEnvironment: <id> }` — **critical**, and **critically, must be a real, correctly-formatted ID** (`ENVIRONMENT-<32 hex chars>`, `CONNECTION-<32 hex chars>`). A malformed ID here does not fail validation at creation time, but silently breaks the M3 API step's per-transaction field rendering in the builder UI — surfacing as `TypeError: Cannot read properties of undefined (reading 'outputs')` in `ApiItem.vue`. Always sanity-check these three IDs against their expected format before trusting a deeper diagnosis.

### `definition.workflowConfig` / `definition.stepConfig` structure
- Fixed chain of `trigger_1` → **(optionally `generic_filter_1` →)** `m3_api_1` → `excel_1` (see "Generic Filter" section below — this is no longer strictly a 3-step chain; a 4th step type is now confirmed and, in the safe case, built automatically).
- Every M3 transaction needs `"type": "ADD"` in every real example we've captured with multiple transactions built via the API — required for the step to render in the UI at all. A manually-built test workflow showed `"type": "LIST"`/`"GET"` in other contexts, so `type` may carry more semantic meaning than assumed, but `"ADD"` remains the safe default since it's what's worked in every case tested end-to-end (now three real workflows deep).
- Multiple transactions of the same name must get **unique instance IDs per occurrence**, not per (program, transaction) name.
- **`executionOrder` must start at 1 with no gaps.**
- **The first transaction in an M3 API step may need `maxRows`, `hasSpreadSheetInput`, and `defaultInputConfig` declared even when it isn't a list-style call.**
- **Trigger fields** (CLIENT-sourced inputs) need `formGroupId`, `elementPosition`, `apiId`, and `placeHolder` — missing `formGroupId` specifically caused zero fields to render.
- **A workflow with zero CLIENT-sourced trigger fields anywhere renders an empty Trigger tab** ("Workflow does not contain any fields that can be configured") — confirmed as expected/correct behavior, not a bug. "Check Data" (all `CONSTANT`/`EXCEL` inputs, no `CLIENT` fields) is a fresh confirmation of this: empty Trigger tab, correctly.
- **Repeated CLIENT-sourced fields** must link every occurrence after the first via `"ref": {"apiId": <first-declaring txn id>, "type": "INPUT", "name": <field>}`.
- **The `<column>-1` duplicate-column mechanism is CONFIRMED correct for two DIFFERENT transactions plainly reading the same Excel column as separate INPUT fields**, and this session extended that confirmation: "Check Data"'s `Get`/`GetAlias` both read `ITNO` from column F, and Live's real GET response came back with `F` and `F-1` exactly matching what the script generated, field-for-field, executing without error. **The plain (non `-1`) slot goes to whichever transaction is parsed/declared first**; the second occurrence gets the `-1` suffix.
- **CLIENT field values must be the real M3 field name**, not Classic's raw placeholder string (`"Field1"/"Field2"/"Field3"`).
- **Chained API references** must point to a field that is **actually declared in the producer's own `output` dict** — synthesize a minimal output declaration if Classic never exported it to a visible column.
- **Classic's `*<letter>` column-style input references should be redirected to a direct API-chain reference** whenever that column's name matches the Export block's own field name for that column.
- **Dual-role Excel columns** get a second entry keyed `"<column>-1"`.
- **`basedOnTransaction`/`runOnFailure`** — confirmed working not just at creation/rendering but now through real *execution* too ("Check Data": `GetAlias.basedOnTransaction = Get's id`, `runOnFailure: false`, executed without error).
- **Excel step layout** should be read directly from Classic's own `<Export TargetSheetName="...">` and `<LstApi OutputStartRow="...">` attributes.
- `LstApi`'s `MaxRows` attribute maps directly to the corresponding transaction's `maxRows` field, alongside `defaultInputConfig: {}` and `hasSpreadSheetInput: false`.
- **`hasSpreadSheetInput` (workflow-level) — FIXED this session.** It was previously computed from Export-block output cells only, so an import-only workflow with real Excel-column *inputs* (no Export block) incorrectly came out `false`. Now computed as `bool(any Export cells) OR bool(any EXCEL/COLUMN input candidates)` — covers both sides.
- **`type: "UNKNOWN"` on any field does NOT break rendering** — confirmed via a real working workflow.
- **`workflowConfig.apiFieldReferences: {}` (always empty) is confirmed fine.**

### A real M3 field metadata endpoint exists
`GET https://api.vince.live/metadata/{connectionId}?details=FIELDS%23{program}%23{transaction}%23{I|O}` returns real field metadata. Confirmed working via direct Postman testing and via the script's integration. M3 type codes confirmed: `A` → STRING, `N` → NUMERIC, `D` → NUMERIC. `AUTCHKMI`/`"Chk Authority"` has consistently returned no metadata across multiple attempts, and testing a corrected name (`"ChkAuthority"`) made no difference. **The name question is settled as of 2026-09-10 — `ChkAuthority`, one word, since an M3 transaction name is always one word — so the name is NOT the explanation for the empty metadata.** See "Known unresolved gaps" item 4 for what else it might be (a 403 on an expired token is one real possibility, observed for every transaction in a web UI run that day).

**Update (2026-09-08), from direct probing — see `docs/vince-live-api-reference-prompt.md` §1c for the full write-up:**
- **Drop the `#{I|O}` suffix.** `details=FIELDS#{program}#{transaction}` returns *every* direction in one call (`count: 2` for `MRS001MI/LstAllTrans`), halving the requests. `converter/metadata.py` now does this.
- **An empty direction is normal.** `BUS100MI/DelBudgetLines` has 14 input fields and zero output fields, because deletes return nothing. Only a transaction with no fields in *any* direction is genuinely undescribed — the old per-direction check reported a false failure on every Add/Upd/Del.
- **`FIELDS#{program}` (whole program) is not usable**, despite being one call for everything: its items carry only `name`/`descr`/`data` with `name` being `"I"`/`"O"`, and nothing identifies which transaction each belongs to.
- The `AUTCHKMI` mystery above is worth re-testing with this in mind: a check-style transaction plausibly has inputs and no outputs, so a per-direction probe would have reported "no metadata" for a transaction that actually has some.

---

## Generic Filter / M3 Filter — Classic's RowFilterEQ / RowFilterNE (major new finding this session)

Classic's row-level conditional logic:
```xml
<RowFilterEQ>
  <RowFilter Column="BD" Value="OK" Label="Deliverymethod filter" IsHidden="true" IsLower="false" />
</RowFilterEQ>
<RowFilterNE>
  <RowFilter Column="BA" Value="x" Label="x" IsHidden="true" IsLower="true" />
</RowFilterNE>
```
was initially believed to have no equivalent in Live's normal builder UI at all. **That belief was wrong** — Live has two distinct, confirmed, real native filter mechanisms, discovered by capturing a manually-built Live test workflow ("Filter tests - Input and output"):

1. **Generic Filter** (`generic_filter_1`, step type `GENERIC_FILTER`) — runs **before** the M3 API step, filtering raw Excel row data (`"from": "Row"`, `"fact": "<column letter>"`). This is the real structural match for `RowFilterEQ`/`RowFilterNE` — both gate on a raw import column's value.
   ```json
   "generic_filter_1": { "all": [
     { "clientKey": "Filter_BD", "operator": "equal", "fact": "BD", "from": "Row",
       "source": "CLIENT", "defaultValue": "OK", "value": "{{ header.Filter_BD }}",
       "dataType": "STRING", "logicalType": "AND" }
   ] }
   ```
   Each condition gets a paired `CLIENT` trigger field (`Filter_<Column>`) in its own form group (`groupId: 2, "filters"`), separate from the regular `"fields"` group (`groupId: 1`).

2. **M3 Filter** (`m3_filter_1`, step type `FILTER`) — runs **after** the M3 API step, filtering a transaction's own **output** rows post-call (confirmed: the fields filtered, e.g. `CUNM`/`CONO`, were genuine output fields of the transaction tested). This is a *different* feature and does not correspond to anything seen in Classic so far — not used in the converter.

### What's now implemented
`source_classic_xml.py` parses filters at **both** of Classic's levels, and `build.py` treats them differently because they mean different things:

- **Block-level** (a wrapper that is a direct child of `<Import>`, sibling of the `ImportDetail`s) → `ir.Workflow.row_filters` → straight into `generic_filter_1`, always. A whole-block gate and a Generic Filter both run exactly once, before everything, so there is nothing to check.
- **Per-transaction** (a wrapper inside one `<ImportDetail>`) → `ir.Transaction.row_filters` → built into `generic_filter_1` **only when every transaction in the workflow shares the exact same row filters** (including the trivial single-transaction case). This is a hard safety rule, not a simplification for convenience: **Generic Filter runs once, before the entire M3 API step — it cannot gate one transaction while letting another run unconditionally.**

At either level, an operator outside `REPRESENTABLE_FILTER_OPERATORS` (i.e. anything but EQ/NE) is never approximated: the gate is left off and reported — a block-level one as a conversion warning, a per-transaction one as `_needs_review` on that transaction. `peek_classic_xml` reports unrepresentable operators too, from the read-only scan, so they appear in the app-import plan and the confirmation box *before* anything reaches a tenant rather than only in the run log. Both levels feed one filter step, and since Live keys its filter fields by column (`Filter_<column>`), two filters on the same column would collide — identical ones collapse, a genuine conflict is warned about and only the first applied.

### The real unsolved case
`classic_v1.xml`'s actual shape — an always-run `AUTCHKMI` authority-check transaction whose output feeds a column that gates a *different*, later transaction (`AllocateOrdLine`/`DeAllocateOrLne`) — falls into the **unsafe** bucket: only some transactions carry filters, not all. Auto-translating to Generic Filter here would incorrectly also skip `AUTCHKMI`. This case is **deliberately deferred and flagged** (`_needs_review` on the specific affected transaction) rather than guessed at. Solving it properly would require the converter to split a workflow's transactions across **multiple M3 API steps**, each with its own Generic Filter — not yet built, and not yet confirmed as even a valid workflow shape in Live.

### Grouping — `<ImportHeading GroupColumns="...">` / `<StartRun>`

Found while planning the Classic→pipeline translation, by reading
`RUN/C05_MMS100_Destruction_Order_ADD_1.4.xml` — a whole Classic construct
the parser does not read at all today. Its second `<Import>` block is:

```xml
<Import Label="Create RO" InputStartRow="4" MessageColumn="AF" SourceSheetName="Excel-template">
  <ImportHeading GroupColumns="B C E O" IsCritical="true">
    <StartRun>
      <M3Api Program="AUTCHKMI" Transaction="ChkAuthority" MessageColumn="AG"> ... </M3Api>
      <Cell Value="*ALO1" Column="P" Type="decimal"/>
    </StartRun>
  </ImportHeading>
  <ImportDetail> AUTCHKMI.ChkAutTblRcd  → <Cell Value="*TX05" Column="Q"/> </ImportDetail>
  <ImportDetail> MMZ101MI.AddHead   <RowFilterEQ> P=1, Q=YES </RowFilterEQ> </ImportDetail>
  <ImportDetail> MMZ101MI.AddLine   <RowFilterEQ> P=1, Q=YES </RowFilterEQ> </ImportDetail>
</Import>
```

**Confirmed** (Vince's own VXL Classic tutorial listing, video 12 "How to
configure an import using the Grouping-feature in VXL",
<https://vincesoftware.com/vxl-with-me>): *"Grouping can be used, when you
want to use header data and line data in Excel and manage this in the same
VXL function"*. So grouping exists to let one sheet carry header rows and
line rows for the same logical document — order header + order lines.

**Confirmed by reading the file itself:**

- `ImportHeading` and `ImportDetail` are siblings under `<Import>`;
  `ImportHeading` is the only element carrying `GroupColumns`, and the only
  one containing a `<StartRun>`.
- `StartRun` holds an `M3Api` *and its own `<Cell>`* — the output-to-column
  mapping that gap 3 below is about.
- **Both filter columns are written by earlier M3 calls, not typed by a
  user.** `P` ← `*ALO1` from the StartRun's `AUTCHKMI.ChkAuthority`, `Q` ←
  `*TX05` from the first ImportDetail's `AUTCHKMI.ChkAutTblRcd`. So
  `RowFilterEQ P=1 AND Q=YES` is a gate on *an earlier call's output* —
  exactly the shape Generic Filter cannot express, and exactly what the
  generic-pipeline path's `{"step": "<ref>.ALO1"}` was built for.
- No `RunOnce`/`IsHeader`-style attribute exists anywhere in the file. The
  complete attribute inventory is: `Import`(Label, InputStartRow,
  MessageColumn, SourceSheetName), `ImportHeading`(GroupColumns,
  IsCritical), `ImportDetail`(IsCritical), `M3Api`(Program, Transaction,
  MessageColumn), `InputField`(Name, Value, Source, Label, Type, IsHidden,
  IsLowerCase, NoOfDecimals), `Cell`(Column, Value, Type),
  `RowFilter`(Column, Value, Label, IsHidden, IsLower),
  `BasedOnTransaction`(TransactionName, TransactionFailed).

**CONFIRMED 2026-09-10, by running a real Classic function with grouped
rows and observing behavior directly** (not inference from element names —
this project's usual bar):

1. **`StartRun` fires once per group, on the group's first row.** A new
   group is detected when the current row's `B`/`C`/`E`/`O` differ from the
   *previous row's* — the engine does not scan ahead or compare against a
   running set of already-seen groups. **The sheet must therefore be sorted
   by the group columns**, or rows belonging to the same logical group but
   separated by a different row will be misread as two separate groups
   (each re-firing `StartRun`). This is a real, silent failure mode for
   any sheet a translator or a user hands it unsorted.
2. **The `<Cell>` result is written only to the group's first row — no
   fill-down.** `P` (from the StartRun's `AUTCHKMI.ChkAuthority`) is
   populated on the row where the group break happened and stays **blank**
   on every subsequent row of that group.
3. **`ImportDetail` calls, and their `RowFilterEQ`s, run per row** —
   evaluated against that row's value in the filtered column.

> ### ⚠️ CONTRADICTION, OPEN as of 2026-09-10 — do not build on fact 3's consequence
>
> What happens to the *non-first* rows of a group is **unresolved**. Two
> statements from the same source on the same day disagree, and both are
> quoted here rather than reconciled by reasoning:
>
> - **First answer (mechanism):** "on every subsequent row P is empty, so
>   those details are getting filtered out." Taken at face value,
>   `MMZ101MI.AddHead`/`AddLine` fire only once per group, never on the
>   2nd..nth line.
> - **Second answer (intent):** "grouping will limit headers and run all
>   of the lines separately, before moving on to the next header. Any
>   workflow/function with no grouping would not do this." Taken at face
>   value, *every* line row in the group runs — which is also what the
>   feature is for (the VXL tutorial's "header data and line data in
>   Excel"), and what an order-header-plus-lines function needs to be
>   useful at all.
>
> **Update 2026-09-11:** this contradiction is about CLASSIC'S OWN ENGINE
> and remains formally unresolved as such — nobody has watched Classic
> itself run this scenario. What IS now resolved is the practical question
> that actually matters for this project: `classic_to_pipeline.py`'s
> `"group"` default (the translator's own re-implementation, not Classic's)
> was exercised in a real Live run and produces the sensible business
> outcome (every row in a group gets processed), settling which behaviour
> to BUILD even though Classic's own internal mechanism stays unquoted.
> See `classic_to_pipeline.GROUP_RESULT_SCOPE` and next-steps item 2.
>
> These cannot both hold. The obvious reconciliation — that "no fill-down"
> describes only what is *written to the visible sheet*, while the engine
> still evaluates the filter against the group's value for every row in
> the group — would make the second answer right and the first answer's
> *consequence* wrong. **That is a guess and is deliberately not adopted
> here.**
>
> What is NOT in doubt: facts 1 and 2 (once per group, on the first row,
> result written to that row only) and the sort-order dependency.
>
> Why it matters enough to block: a translator that reproduces "lines
> 2..n are filtered out" when Classic actually runs them would silently
> drop most of a customer's order lines. The opposite error — running
> lines Classic skips — is equally bad in the other direction. Settle this
> with one concrete run before writing the translator: a sorted sheet, one
> group of three line rows, and the answer to "did the line call fire for
> rows 2 and 3?"

**What this means for a translator.** Only the parts above that are not in
doubt: run the StartRun call once per group rather than once per row, and
either enforce or reproduce the sort-order dependency (compare only to the
previous row, as Classic does) rather than silently assuming a sorted
sheet. Whether the group's value reaches the 2nd..nth row's filter is the
open question above, and it changes which rows the line calls run for, so
the row-level gating cannot be written until it is answered.

The public product documentation covers VXL Live only; VXL Classic's
grouping is documented as a video, and
`https://vincesoftware.com/product-documentation/groups` (and
`/vxl-live-wf-groups`) is a **different, unrelated feature** — grouping
multiple *workflows* into a collection for role-based access, not grouping
spreadsheet rows.

### A second real file — `RUN/Existing Price Lists.xml` (what it adds)

Structure: **one `<Export>` and TWO `<Import>` blocks** ("Extract Price
Lists", "Add Price List", "Update Price List"). The parser reads the first
of each, so the whole "Update Price List" block — 9 M3 calls — is currently
dropped. Both Import blocks are ~10 `<ImportDetail>`s chained by
`BasedOnTransaction`, with an `<ImportHeading GroupColumns="B C D E">`
whose `<StartRun>` runs `MNS150MI.SelUsers`.

New, confirmed by reading the file:

- **`<RowFilterGT>` exists** — a third filter wrapper alongside
  `RowFilterEQ`/`RowFilterNE`. The operator set is therefore open-ended;
  do not assume EQ/NE is all there is. (`pipeline_ir.Gate` already
  supports `gt`, so the pipeline path can express it; `GENERIC_FILTER`'s
  confirmed operator set is only equal/notEqual, so it cannot.)
  The parser no longer matches wrapper names against a fixed EQ/NE list:
  `_is_filter_wrapper` takes any element whose tag starts with
  `RowFilter` and records the suffix as the operator, so a fourth wrapper
  arrives as a reported unknown rather than as silence.
  `build.REPRESENTABLE_FILTER_OPERATORS` is the separate question of what
  Live can be given, and today holds only `EQ`/`NE`.
- **`RowFilter` has an `IncludeBlank` attribute.** Seen as
  `<RowFilter Column="AM" Value="" IncludeBlank="true" .../>` inside a
  `RowFilterGT` — i.e. "greater than empty string, include blanks". What
  that combination is meant to select is **not confirmed** and reads
  self-contradictory; treat it as unknown. Carried on
  `ir.RowFilter.include_blank` and repeated in the review note that flags
  the filter, so it is visible to whoever handles it by hand — never
  acted on.
- **A `<RowFilterEQ>` can sit at `<Import>` level**, as a *sibling* of the
  `ImportDetail`s rather than inside one: `BH == "OK"`, labelled "PL1 &
  PL2 input valid. check". This is the whole-block gate — semantically an
  exact match for Live's `GENERIC_FILTER`, which also runs once before
  everything. Classic therefore has **two filter levels**, and they map
  cleanly onto two different Live mechanisms: block-level →
  `GENERIC_FILTER`, detail-level → the generic-pipeline path.
  **Now parsed and translated** (2026-09-10): `_parse_block_row_filters`
  reads the wrappers that are direct children of `<Import>` into
  `ir.Workflow.row_filters`, and `build.py` puts them straight into
  `generic_filter_1` — unconditionally, since a whole-block gate is
  exactly that step's semantics and needs no uniformity check. It was
  dropped entirely before that, so a converted workflow ran for rows
  Classic skips. Not confirmed: whether a trailing block-level filter
  could instead be intended for the immediately preceding `ImportDetail`.
  In this file it appears last, after the tenth detail; it is read as
  gating the block, which is what its position and Classic's own nesting
  imply.
- **`BasedOnTransaction TransactionFailed="true"`** is in real use as the
  get-or-create idiom: `OIS017MI.AddPriceList` is based on
  `OIS017MI.GetPriceList` *failing*. Already handled —
  `build.py` emits it as `runOnFailure`.
- **The same `(program, transaction)` really does occur twice in one
  block** — `AUTCHKMI.ChkAuthority` appears at positions 2 and 9, writing
  different columns (`BK`, `BO`). `BasedOnTransaction` by name is
  therefore ambiguous; `_to_ir`'s `seen_so_far` shadowing (nearest
  preceding occurrence wins) already resolves it the sensible way, and
  this file is the first evidence that the case is real rather than
  theoretical.
- **A `<StartRun>` need not write a `<Cell>`.** `MNS150MI.SelUsers` here
  has none — its only consumer is `BasedOnTransaction`, i.e. "did the user
  lookup succeed". So StartRun output reaches later calls two ways:
  through a column, or through success/failure.
- **`IsCritical="false"`** observed for the first time (on
  `OIS017MI.GetPriceList` in the Add block).
- Export side: `<LstApi MaxRows="10000" OutputStartRow="3">` on
  `CMS100MI.LstC05_OPRICH` — note a **custom transaction name containing
  an underscore**, which any name-parsing must tolerate.

Both files agree on the important thing: **the `StartRun` call is a read**
(`AUTCHKMI.ChkAuthority`, `MNS150MI.SelUsers`) — group frequency is now
confirmed above regardless, but this matters independently for the
write-case risk: a StartRun that *writes* would create N records per group
under this now-confirmed once-per-group firing, where a naive per-row
translation would create one per row instead.

### Still explicitly unconfirmed (flagged in the output, not silently assumed)
- **`IsHidden="true"`** — Classic means these filters to be fixed and invisible to whoever runs the workflow. Every real Live example uses `source: "CLIENT"`, which creates an *editable* trigger field with a default value instead. No confirmed non-editable/fixed-value variant exists yet — this is a real behavioral/UX difference from Classic's intent, flagged via `_needs_review` on the `generic_filter_1` block itself.
- **`IsLower="true"`** — presumably case-insensitive comparison; no confirmed counterpart in Live's shape, not applied.
- `dataType: "STRING"` / `length: "15"` on generated fields — only confirmed for string-like columns; no NUMERIC example tested yet.
- Multiple `<RowFilter>` children combining as AND within one wrapper — assumed, not confirmed (every real Classic example seen so far has exactly one child per wrapper).

---

## Generic step types beyond M3 API - Transform / REST API / Code (new session: multi-source workflow creator)

Separate from the Classic→Live migration work above: confirmed real structure for three step types that don't appear in any Classic XML but are needed for building more general Live workflows (e.g. from a worded prompt, or as a wider target for the Live-JSON-source path). Discovered via a customer-provided PDF documenting a real production workflow ("Display customer order lines" - Trigger → Transform → RestAPI ×3 → Transform, feeding a widget rather than an Excel export) plus a live-tenant test workflow built by hand and captured via GET, plus a direct POST probe against the real API - same "capture real data, don't guess" methodology as everything else in this doc.

### Confirmed `type`/`target`/`name` per step (workflowConfig.steps)
| Step | `type` | `target` | `name` |
|---|---|---|---|
| Transform | `TRANSFORMER_MORPH` | `workflow-transform` | `"Transform"` |
| REST API | `GENERIC_API` | `workflow-rest-api` | `"Rest API"` |
| Code | `GENERIC_CODE` | `workflow-code` | `"Code"` |

Note: guessing `TRANSFORM` and `REST_API` for `type` produced a real, useful validation error - `POST /workflows` returns `"WorkflowDefinition is invalid! Errors (2):\nProperty workflowConfig.steps stepId transform_1 has invalid type: TRANSFORM ,\nProperty workflowConfig.steps stepId rest_api_1 has invalid type: REST_API "` - confirming `type` is checked against a fixed server-side enum, though the error doesn't list valid values. `target` guesses (following the existing `workflow-<type>` pattern) were correct on the first try.

`retryAttempts: 0` applies to all three (same as every non-trigger step seen so far). They chain the same way `generic_filter_1` wraps `m3_api_1` - single-child linear nesting via `childSteps`, no new nesting mechanism needed.

**`excel_1` is an M3 API-specific companion step, not a generic chain terminator - confirmed directly.** Attaching it after a REST API step (with no `m3_api_1` step anywhere in the workflow) caused a real save problem in the builder UI; the customer had to delete it manually to get the workflow to save. Excel is meant to be used only in conjunction with an M3 API step (matches how `excel_1`'s own column entries are always `fieldId`s of the shape `"<m3ApiTxnId>.output.<field>"` / `"<m3ApiTxnId>.input.<field>"` - it has no way to reference a Transform/REST API/Code step's output at all, so pairing it with anything else was never a valid combination to begin with). A workflow built from Transform/REST API/Code steps should simply not include an `excel_1` step; the hand-built test workflow ending at `code_1` with empty `childSteps` confirms that's a valid, saveable terminal shape.

### Confirmed `stepConfig` content per step
- **Transform** (`transform_1`): `{"template": "<JSONata expression as a string>", "trimAll": true}`. The default/placeholder template Live generates for a fresh Transform step is itself informative about available bindings: `{"bodyFromLastStep": body, "bodyFromLastStepIfGenericRestAPI": body.body, "headerFromLastStep": header, "context": $context}` - i.e. `body`/`header` are plain paths into the previous step's output (no `$` prefix needed), while `$context` is a bound JSONata variable (matches the customer PDF's own usage: `$context.data.trigger.body[0]."Order Number"`). Verified end-to-end: a real Transform step with a literal-JSON-shaped template rendered and evaluated correctly in the builder's own Input/Output preview panel.
- **REST API** (`rest_api_1`): `{"endpoint": "<url>", "method": "post", "headers": {...}, "bodyPath": "<JSONata expression>"}`, `connectionId` OMITTED entirely when not using a stored connection - confirmed via the builder UI's own "Use connection" toggle, which was OFF for a connectionId-less step we created via the API. This matches the customer PDF's shape too (their example has `connectionId` set, `"Use connection"` would presumably be ON there). There's also a `"Body"` mode as an alternative to `"Body path"` (i.e. probably a `"body"` key alternative to `"bodyPath"` for literal-body-instead-of-referenced-body cases) - NOT yet confirmed, just visible as a second tab in the UI.
  - **UNRESOLVED, flagged rather than explained away:** a real POST with `endpoint`/`bodyPath` set was echoed back correctly by the API itself (confirmed via the POST response body), but the builder UI displayed `Endpoint URL` and `JSONata body path expression` as empty placeholders while `method`/`headers` - sibling fields in the same object - rendered correctly from the same stored JSON. Since it's a partial failure (2 of 4 fields) rather than all 4, this looks like a UI-read-path quirk rather than wrong key names, but this is NOT confirmed - don't build on the assumption it's cosmetic without checking further (e.g. hard refresh, or capturing the builder's own GET call in DevTools to see if it reads from a different shape than the raw `GET /workflows/{id}` response).
- **Code** (`code_1`): `{"content": "<full JS module source as a string>"}`. Confirmed via a real example the customer pasted in (`module.exports = async function (prevStep, allSteps, trigger) { ... }`) - NOT a Vince-authored example, so its business logic isn't meaningful, but the wrapping shape (a `content` string holding a Node.js module) is real. Two sandboxed `require()`-able modules are visible in that example: `authorizedAxios` and `getContext` (used as `getContext('globals.connection', CONNECTION_ID)` to pull a stored connection inside Code, then `authorizedAxios(connection)` to get an authenticated HTTP client) - a third way (beyond REST API's own `connectionId`/`"Use connection"` toggle) to reach a stored connection.

### Real risk, confirmed by direct observation (not yet reproduced/isolated)
**Save-time validation for these three step types is much looser than for M3 API steps.** Per direct testing: a malformed Transform template can have its content silently disappear/reset rather than failing to save with a clear error. Unlike the M3 API step (which is validated against real field metadata), there is currently no known safeguard here - treat Transform/REST API/Code content as much easier to silently corrupt than the M3-API-step content this doc's earlier sections cover.

### Still open
- The real allowed `type` enum beyond these three values (only learned by hitting real ones - no full list available).
- REST API's `"body"` mode shape (only `"bodyPath"` mode is confirmed).
- Whether `connectionId` is genuinely optional for ALL step types that support connections, or specific to REST API's toggle.
- The endpoint/bodyPath UI-display gap noted above.
- No IR/build.py support yet - this section is ground truth for a future generalization, not yet wired into the converter architecture.

---

## Known unresolved gaps

1. ~~`hasSpreadSheetInput` computed from Export-block cells only~~ — **FIXED this session.**
2. **`RowFilterEQ`/`RowFilterNE` — partially resolved.** Block-level filters and the uniform (whole-workflow) per-transaction case are now handled via Generic Filter. The per-transaction/mixed case (the real shape in `classic_v1.xml`) is still deferred — detected and flagged, not translated — and so is any non-EQ/NE operator. See above.
3. ~~**ImportDetail-level `<Cell>` elements remain unparsed**~~ — **FIXED 2026-09-10.** `<Cell>` is now read at every level Classic writes one: an `<Export>`'s LstApi/GetApi, an `<ImportDetail>`, and a `<StartRun>`. They become ordinary `ir.Cell`s and so ordinary `excel_1` columns, since "this call's output goes to this column" is the same statement wherever it appears. This is the mechanism the mixed-filter case runs on (a RowFilter names a column; the producing call is the one whose Cell writes it), so it was the blocker for that whole case. Immediate side effect on the two real files: `MMS200MI.Get` → H/J, `GetAlias` → I, `ChkAutTblRcd` → Q/BM, `ChkAuthority` → BK/BO/P now all exist in the built workflow instead of vanishing.
3b. ~~**`<ImportHeading>` / `<GroupColumns>` / `<StartRun>` are not parsed at all**~~ — **PARSED 2026-09-10, still not translatable.** `Workflow.group_columns` carries the group columns and the StartRun call arrives as a `Transaction` flagged `runs_once_per_group`. Parsing it fixed a real second-order bug: both real files' `<ImportDetail>`s reference the StartRun call by name through `<BasedOnTransaction>`, so while it went unparsed those references resolved to nothing ("Could not resolve dependency target MNS150MI.SelUsers", twice in "Existing Price Lists"). It is REPORTED, never translated — Live's M3 API step has no group concept, so `build.py` warns at workflow level and puts a `_needs_review` on the call itself, distinguishing a read (same answer, more HTTP calls) from a possible write (one record per row where Classic made one per group). Execution frequency is confirmed; see the grouping section above.
3c. **`Source="UserID"` was silently read as a spreadsheet column.** Found 2026-09-10 by parsing `<StartRun>`, whose `MNS150MI.SelUsers` call is the only place either real file uses it: `<InputField Name="FRES" Value="*USERID" Source="UserID"/>` fell through `_classify_source`'s `"*<letter>"` branch and became a column literally named `USERID`, which then reached `excel_1` as a bogus `USERID` / `USERID-1` column pair. Classic has at least three `Source` values (`Constant`, `UserID`, and whatever else exists); nothing in Live is confirmed to supply the running user's id, so it now degrades to a CLIENT trigger field and says so, and any OTHER unrecognised explicit `Source` does the same rather than inventing a column.
4. ~~**Whether `AUTCHKMI`'s real M3 transaction name is `"Chk Authority"` or `"ChkAuthority"`**~~ — **SETTLED 2026-09-10: it is `ChkAuthority`, one word.** Confirmed on the general rule that an M3 transaction name is always a single word, so the spaced form was never a candidate. This **removes** the standing theory that the name explained `AUTCHKMI`'s empty metadata — the name was right all along, so the empty results have another cause. One real candidate surfaced the same day: a metadata call in the web UI returned **HTTP 403 Forbidden** for every transaction on an expired token, which is a rejected token rather than the documented silent cross-tenant emptiness. Whether AUTCHKMI's historical blanks were ever 403s is not established; re-test with a known-fresh token before theorising further.
5. Field metadata: the live-metadata-fetch feature is confirmed working via direct testing; the static stub table remains a fallback only.
6. **Only tested on three workflows.** Other Classic workflow shapes may still surface new edge cases.

6b. **A gate on a column an M3 call WRITES can never work on the native path** — found 2026-09-11, the moment ImportDetail `<Cell>`s became readable (gap 3). `GENERIC_FILTER` sits between `trigger_1` and the M3 API step and filters **raw spreadsheet rows**, so a column that only gets a value when a transaction writes it is empty at the only moment the filter is evaluated. This is not an operator limitation: it holds for `EQ` as much as for `GT`. Nor is it about uniformity — splitting a workflow's transactions across several M3 API steps, the enhancement gap 2 keeps pointing at, would not help, because no placement of a Generic Filter can see an M3 call's output. Both real customer files are exactly this shape (`C05` gates on P and Q, `Existing Price Lists` on BK, BM and BO), which means the "real unsolved case" above was never really about non-uniform filters; the gating column's provenance was the obstacle all along. Such a filter is now never emitted at either level and is reported with the column *and* the transaction that writes it. At block level that is a behaviour fix, not just better wording: block gates bypass the uniformity check entirely, so one on an M3-written column was previously emitted and silently filtered on an empty cell. Detection only — expressing the shape still needs the generic-pipeline path, whose gates read an earlier call's output by design. Guarded by `converter/selftest_classic.py`.
7. **The Execute button in the Live builder previously stayed greyed out** on the first workflow even after successful execution via the Excel/VXL add-in path. "Check Data" was confirmed to execute successfully this session and the API call fired as expected — worth directly clarifying next time whether this was via the Execute button itself or another path, to know if this gap is actually closed.

## Suggested next steps

Reordered 2026-09-10, after two more real Classic files and the design-file
capture. Roughly most-value-first.

1. ~~**Fix the `<Import>`-level `<RowFilterEQ>` drop.**~~ **DONE
   2026-09-10.** Block-level filters are parsed into
   `ir.Workflow.row_filters` and built into `generic_filter_1`; the
   operator wrapper name is now read as an open set so `GT` and anything
   after it are reported rather than dropped; `IncludeBlank` is captured
   and surfaced, still not interpreted. Verified against
   `RUN/Existing Price Lists.xml`: its `BH == "OK"` block gate now
   reaches the built workflow, and its `RowFilterGT` is flagged at scan
   time and per transaction. What this does **not** fix is the
   per-transaction/mixed case — still item 3 below.
2. **Answer the grouping question — DONE, fully, 2026-09-11.**
   **Settled 2026-09-10:** `StartRun` fires once per group, on the group's
   first row, with group breaks detected by comparing only to the
   *previous* row (so the sheet must be sorted by the group columns), and
   its `<Cell>` result is written to that first row alone with no
   fill-down.
   **Settled 2026-09-11, against a real run** (`WFEXEC-3cca6a49...`, the
   same workflow item 8 built): whether the line-level calls run for a
   group's 2nd..nth rows. With two rows sharing one group key, the
   once-per-group `SelUsers` call fired on row 1 and was correctly skipped
   on row 2 (`groupRepeatsSkipped: 1`) — and row 2's own
   `AUTCHKMI_ChkAuthority` still came back `"ok"`, reading row 1's CARRIED
   result rather than `"notrun"`. That is the `"group"` behaviour this
   project committed to, confirmed rather than assumed.
   **Where it lives:** `classic_to_pipeline.GROUP_RESULT_SCOPE`, `"group"`.
   The reasoning that made it the default in the first place, now backed
   by a real run rather than just being stronger than a coin toss:
   C05_MMS100's "Create RO" groups by `B C E O` — one destruction order —
   and its `MMZ101MI.AddLine`, one LINE per row, is gated on `P`, the
   column its once-per-group `ChkAuthority` writes. Under the literal
   sheet reading exactly one line per order would ever be added, which the
   real run confirms is not what happens. `selftest_classic_pipeline.py`
   still pins the difference: flipping that constant makes the line call
   fire twice for four rows instead of four times.
3. ~~**Build the Classic → generic-pipeline translation.**~~ **DONE
   2026-09-11** — `converter/classic_to_pipeline.py`, `cli.py
   classic-pipeline`, guarded by
   `converter/selftest_classic_pipeline.py`. Both real customer files
   translate and build; every block of both, including the nine-call
   get-or-create chain in "Add Price List".

   **It emits ONE Code step (`m3RowPipeline`), not a chain of `m3Bulk`
   pairs as this item and the pipeline doc originally specified.** That
   was not a shortcut — the bulk shape cannot carry a Classic import, for
   two reasons that only became visible with the whole chain in front of
   you:
   - **Index correlation dies as soon as a call is gated.** A bulk step
     sends N rows as N transactions and reads an earlier step's answer for
     row *i* as `records[$i]`; that holds only while every step saw the
     same rows in the same order. Classic gates nearly every call, so
     each step sends a different subset, and "Add Price List" chains nine
     gated calls. By the third the index means nothing. A row pipeline
     holds one row at a time and has no index to misalign.
   - **`BasedOnTransaction` needs a per-call verdict; the bulk response
     only gives an aggregate one.** The confirmed failure signal is
     `nrOfFailedTransactions`. What a single *failed* result entry looks
     like inside a multi-transaction payload has never been captured — so
     inside a bulk payload the get-or-create idiom could only be guessed
     at. Posting exactly ONE transaction per request turns that same
     confirmed counter into a verdict on that one call. That is the whole
     reason this path can express `<BasedOnTransaction>` at all, and it
     is why `{"callStatus": ...}` is a Code-path-only value source.
     Record count is deliberately not the test: a Get that finds nothing
     and a write that succeeds and returns nothing both come back with no
     records, and treating those alike is how a get-or-create becomes a
     double-create.

   Also translated: per-call and block-level `RowFilter`s (the block one
   as a ROW gate — it skips the row, so it is checked once rather than
   copied onto every call), a filter resolved to the nearest earlier call
   whose `<Cell>` writes that column and to the right *occurrence* of a
   repeated transaction name, `IsLower` → `caseInsensitive`, grouping with
   previous-row break detection, and `Source="UserID"` → a trigger field.
   Refused on purpose: an `<Export>`-only block, which the native path
   renders better (it has no input row to iterate and this path cannot use
   `excel_1`). Reported and never approximated: an unseen `RowFilter*`
   operator, `GT` against an empty value, `IncludeBlank`, `IsCritical`,
   `MessageColumn`, `MaxRows`, and the sheet/start-row settings.

   **Both unknowns are now confirmed, each isolated one named thing:**
   `classic_to_pipeline.row_key()` and `classic_to_pipeline.GROUP_RESULT_SCOPE`
   (item 2's open half, `"group"`) — both CONFIRMED 2026-09-11, see item 8.
4. ~~**Parse `<ImportHeading>` / `<GroupColumns>` / `<StartRun>`, and
   `<Cell>` at ImportDetail and StartRun level.**~~ **DONE 2026-09-10** —
   see unresolved gaps 3, 3b and 3c above. The Cell-to-column mapping the
   mixed-filter case needs now exists in the IR; grouping is parsed and
   reported but deliberately not translated; and doing this surfaced the
   `Source="UserID"` misread as a bonus.
5. ~~**Read every `<Export>` and `<Import>` block, not just the first.**~~
   **DONE 2026-09-10.** Blocks are grouped into UNITS - one workflow each -
   because Live carries a single sheet, data row and filter gate per
   workflow while Classic states those per block, and the real files
   genuinely disagree across blocks (`C05_MMS100` declares InputStartRow 3
   and 4 and MessageColumn AA and AF; "Existing Price Lists" gates its Add
   block on `BH == "OK"` and its Update block not at all). The first
   `<Export>` and first `<Import>` stay paired in unit 1, preserving the
   shape every confirmed conversion was validated on, so a file with at
   most one of each converts exactly as before. `parse_classic_xml_units`
   returns them all; app import creates each as its own workflow named
   after its own block Label, the CLI writes numbered sibling files, and
   the web UI's Source panel builds all of them and lets you pick which to
   create. All 6 M3 calls of `C05_MMS100` and all 20 of "Existing Price
   Lists" now reach Live, where 2 and 11 did before.
   Not confirmed, and worth knowing before relying on it: whether Classic
   runs a file's blocks as one sequential unit (in which case N separate
   Live workflows have to be run in order by hand) or as independently
   runnable steps. The split is forced by what Live can express either
   way, but the run-order question has not been answered.
6. **Settle `IncludeBlank`, and route `RowFilterGT` somewhere it works.**
   Both are now parsed and reported (item 1), which is as far as the
   native path can go: `GENERIC_FILTER` does only equal/notEqual, so a
   `GT` forces the pipeline path — i.e. it is really item 3 wearing a
   different hat. `IncludeBlank`'s meaning is still unconfirmed and needs
   a Classic-side answer, not a converter change.
7. **Close the design-file loop** — capture the workflow `PUT` the builder
   issues after its upload (URL and body shape), so app import can write
   `hasExcelDesignFile`/`excelDesignFileKey` itself instead of uploading a
   file nothing references. The importer already detects and reports that
   state; see `docs/vince-live-api-reference-prompt.md`, "Design file".
8. **Run one generated generic pipeline in a real tenant** — **DONE
   2026-09-11.** Converted `RUN/Existing Price Lists.xml` with
   `cli.py classic-pipeline`, created `WORKFLOW-66f056716329623e08ff34880ef5c45e`
   ("Extract Price Lists 4") in the `Test-Tenant` tenant, and ran it
   from VXL with `debug: true` across three iterations as the sheet and
   code got fixed. Every open question this item named is now answered:
   - **Row shape — CONFIRMED.** `trigger.body` is a bare array of row
     objects keyed by COLUMN LETTER (open question 4 in
     `docs/vince-generic-m3-pipeline.md`, now resolved).
     `classic_to_pipeline.row_key()` needed no change. Caveat found the
     same day: VXL's read feature trims entirely-empty LEADING columns and
     re-indexes what's left starting at "A" - an Import-only conversion is
     especially exposed, since the columns its paired `<Export>` would
     have written sit blank and get trimmed.
   - **A real bug, caught in the first run:** `pipeline_ir.py`'s
     `rows_js` default was `"trigger.body.data"`; since `trigger.body` is
     the array itself (not `{data: [...]}`), every row pipeline was
     silently processing zero rows. Fixed to `"trigger.body"`.
   - **`firstPayload`/`baseURL`/`_status` — CONFIRMED**, once the bug
     above was fixed: real M3 calls posted, a real M3 validation error
     (`"Customer CH_CUST does not exist"`) came back and was correctly
     parsed via `nrOfFailedTransactions` into `_status: "failed"`.
   - **`groupsSeen`/`groupRepeatsSkipped` — CONFIRMED**, answering item 2's
     open half too: with two rows sharing one group key, the once-per-group
     `SelUsers` call fired on row 1, was skipped on row 2
     (`groupRepeatsSkipped: 1`), and row 2's own `AUTCHKMI_ChkAuthority`
     still succeeded — reading row 1's CARRIED result rather than
     `"notrun"`. That is the `"group"` behaviour `GROUP_RESULT_SCOPE`
     claims, confirmed rather than assumed.
9. Test a non-`CLIENT`-sourced (fixed/hidden) Generic Filter value in
   Live's UI, to see if anything matches Classic's `IsHidden="true"`.
10. ~~Resolve the `AUTCHKMI` transaction-name question directly.~~ **DONE
    2026-09-10** — `ChkAuthority`, one word. Which means the empty
    metadata needs a different explanation; see unresolved gap 4.
11. Directly confirm whether the Execute-button greyed-out issue is
    resolved or was worked around.
12. Keep feeding real Classic files through. Every one so far has produced
    at least one new element, attribute or idiom: the second added
    `RowFilterGT`, `IncludeBlank`, block-level filters, `IsCritical="false"`,
    `TransactionFailed="true"` in real use, and a transaction name with an
    underscore.

## Where things are
The working script is `classic_to_live_converter.py`. It also uses a small `converter_state.json` file (must stay alongside the script) to auto-increment workflow names.

**Handoff note:** starting from this point, the person is taking over running the script and managing Postman/Live interactions manually for a while, to build direct familiarity with the system rather than delegating each step. The script and methodology above should remain the reference point for that work. Future chats may pick up specific issues as they arise rather than continuing a single running thread.
