# Full M3 workflows without the M3 API step

How to build a Vince Live workflow that talks to M3 and filters rows
**without using the M3 API component or the Excel component at all** —
Trigger → Transform → REST API chains that call M3's bulk API directly,
with the conditional logic living in JSONata (or, at the far end, in a
Code step).

**Why this exists.** Live's native row-level filter (the `GENERIC_FILTER`
step) runs **once, before the entire M3 API step**. It cannot gate one M3
transaction while letting another run unconditionally. Classic functions
do exactly that all the time — an authority check that always runs, whose
*output* then decides whether a later update runs for that row. That
shape is unrepresentable in Live's native path, and is the case the
Classic converter currently flags and defers (see the migration summary's
"The real unsolved case"). On this path it's an ordinary predicate.

**This is not a workaround.** It's the shape Vince's own production
customer workflows already use. Everything below was taken from real
captured examples, not inferred — provenance is named per fact.

---

## Provenance

Confirmed from Vince Live's product documentation and real workflows:

| Source | What it establishes |
|---|---|
| Product docs → How-to → **"Multiple M3 API calls in 1 REST API call"** | The bulk endpoint, the request body, a full real response payload, the synchronous workflow API, Custom APIs |
| Product docs → Workflow Components → **"Rest API"** and **"Rest API v2"** | `body` vs `bodyPath` modes, `connectionId` auth behaviour, `version: 2`, `forEach`, binary handling |
| Product docs → How-to → **"Export data from REST API to VXL Live"** | The `VXL` label, and the hard rules for what a final step must return to be Excel-exportable |
| Four real customer workflows: **"Display customer order lines"**, **"Claim register"**, **"Package Lost"**, **"Display customer credit notes"** | Four real end-to-end workflows in this exact shape, reads *and* writes |
| The ticket that implemented the feature | The name `skipIfExpression` |
| A live-tenant URL supplied directly | The single-transaction GET form and its query options |

**"Package Lost"** is the strongest single reference: `Trigger →
(Transform → Rest API) × 7`, performing real M3 **writes**
(`AddBatchHead` → `AddBatchHeadChg` → `AddBatchLine` → `Confirm`), with no
M3 API step and no Excel step anywhere in it.

---

## The confirmed idiom

Two steps, repeated once per M3 call:

```
Transform   builds  {"payload": {"program": ..., "transactions": [...]}}
REST API    posts   that payload to M3's bulk execute endpoint
```

### The REST API step

⚠️ **The stepConfig wraps this in a `content` string** — the config's keys
do **not** sit flat in stepConfig:

```json
"rest_api_1": { "content": "{\n  \"version\": 2,\n  \"endpoint\": \"...\",\n  \"bodyPath\": \"$.payload\"\n}" }
```

Confirmed by a GET of a hand-built workflow, whose compiled
`definitionAsl` parses that string back out and hands the proxy lambda
`'stepConfig': {"content": {...}}`. A flat stepConfig leaves the proxy
with nothing, and the builder UI shows empty fields. A `TRANSFORMER_MORPH`
step's config in the same workflow **is** flat
(`{"template": ..., "trimAll": true}`) — don't generalise either shape
across step types. VinceForge handles the wrapping
(`build_pipeline._rest_content`).

The JSON *inside* `content`:

```json
{
  "version": 2,
  "connectionId": "CONNECTION-<32 hex>",
  "endpoint": "https://mingle-ionapi.<region>.inforcloudsuite.com/<TENANT>/M3/m3api-rest/v2/execute",
  "method": "post",
  "headers": { "content-type": "application/json" },
  "bodyPath": "$.payload"
}
```

- The endpoint is the **bulk** `execute` path — no program or transaction
  in the URL; they go in the body. One HTTP call carries many M3
  transactions, which is the entire point (M3 needs eight transactions to
  read one order).
- `connectionId` is what makes this work: Live looks up the stored
  connection and **attaches its auth to the request automatically**. You
  do not build an ION API token yourself.
- Host region and `<TENANT>` are both tenant-specific. Copy them from a
  real REST API step in the target tenant — there is nothing to derive
  them from. Real observed values: `.../DEMO_TENANT/...`,
  `.../TEST_TENANT/...`.

### The Transform step

```jsonata
(
  $data := $context.data.trigger.body;
  {
    "payload": {
      "program": "OIS100MI",
      "transactions": $data.{
          "transaction": "GetHead",
          "record": {
            "ORNO": OrderNumber
          }
      }[]
    }
  }
)
```

- `$data.{...}[]` maps N input rows to N M3 transactions **inside one
  call**. The trailing `[]` forces an array even for a single row.
- `bodyPath: "$.payload"` then selects the payload out of the Transform's
  output. `bodyPath: "$"` also works if the Transform returns the bulk
  body unwrapped (the "Display customer order lines" workflow does that;
  the others wrap in `payload`).

### Request body fields (all confirmed)

```json
{
  "program": "MMS200MI",
  "cono": 790,
  "maxReturnedRecords": 100,
  "transactions": [
    { "transaction": "LstItmWhsByWhs",
      "record": { "WHLO": "001" },
      "selectedColumns": ["WHLO", "ITNO", "ITDS"] }
  ]
}
```

`cono`, `maxReturnedRecords` and `selectedColumns` are optional.
`selectedColumns` is worth using on reads — without it M3 returns every
field of the record (the captured `LstLine` response has ~140).

### Response shape (confirmed against a real captured payload)

```json
{
  "statusCode": 200,
  "body": {
    "nrOfSuccessfullTransactions": 7,
    "nrOfFailedTransactions": 0,
    "wasTerminated": false,
    "results": [
      { "transaction": "GetOrderHead", "records": [ { "ORNO": "0010000386", "...": "..." } ] },
      { "transaction": "GetOrderValue", "records": [ { "...": "..." } ] }
    ]
  }
}
```

Read it from a later step as:

```jsonata
$context.data.all.rest_api_1.body.results                        /* the array of results   */
$context.data.all.rest_api_1.body.results.records.CUNO           /* flattened across all   */
$context.data.all.rest_api_1.body.results[transaction="GetHead"].records[0].CUNO
```

That `.body` is the REST API step's own `{headers, body}` wrapper — an M3
API step's result is the array directly, with no `.body`. Don't carry the
convention across step types.

---

## Where the filters go

Three tiers, in order of preference.

### Tier 1 — a JSONata predicate in the Transform (use this)

The filter belongs on the rows going into **that one call's** payload, so
each call gets its own and one call can be gated while the next runs over
everything:

```jsonata
$data := $context.data.trigger.body[Approved = "OK"];
```

Gating on an **earlier call's output** — the case the native filter can't
express — needs per-row correlation, because a prior step's
`...records.ALPG` is that field's *whole flattened sequence*, not this
row's value. Correlate by index, the way the real "Claim register"
workflow does with `records[$i]`:

```jsonata
$data := $filter($context.data.trigger.body, function($row, $i) {
  $context.data.all.rest_api_1.body.results.records[$i].ALPG = "1"
    and $lowercase($row.Approved) = "ok"
});
```

This is what VinceForge generates for you (see below), including the
choice between the plain-predicate and `$filter` forms.

⚠️ **Index correlation is fragile, by nature.** `records[$i]` lines up
with row `$i` only while the prior call returned exactly one record per
input row, in order, with nothing dropped — and `nrOfFailedTransactions`
tells you M3 can drop some. As soon as anything filters rows *between*
two calls, the alignment is gone. When it matters, carry the key field
(e.g. `ORNO`) into the payload and join on it instead of trusting the
index.

### Tier 2 — `skipIfExpression` on a v2 REST API step

`version: 2` also advertises "*allows skipping the request if a condition
is not met*". The property is **`skipIfExpression`** — name confirmed from
the ticket that implemented it. The
**expression syntax and truthiness semantics are documented nowhere and
are not confirmed.** Probe before relying on it; a Tier 1 predicate does
the same job with plain JSONata semantics you can test locally.

### Tier 3 — a Code step (the escape hatch)

Only when a later call's *input* genuinely depends on an earlier call's
*output* for that same row, so the calls can't be batched — or when you
need per-row throttling or error handling. Costs: invisible in the
builder UI, spends wall-clock on sequential HTTP (mind the 29s ceiling
below), and no other real Vince workflow works this way.

Confirmed shape:

```js
module.exports = async function (prevStep, allSteps, trigger) {
  const authorizedAxios = require('authorizedAxios');
  const getContext = require('getContext');
  const connection = await getContext('globals.connection', CONNECTION_ID);
  const axios = await authorizedAxios(connection);
  const rows = trigger.body;   // a bare array for a VXL trigger - CONFIRMED
                                // 2026-09-11; `.data` was this doc's own
                                // unconfirmed guess and read as undefined
  // ...
};
```

---

## Spreadsheet in and out, with no Excel step

`excel_1` is **not** an option here: its columns can only reference an
`m3_api_1` transaction instance id, and attaching it to a non-M3 chain
broke saving in the builder for real.

Instead:

1. **Add the label `VXL` to the workflow.** Product documentation:
   *"Create a new workflow and add the label `VXL` to it — this enables
   the workflow to be visible in VXL Live"*, alongside *"All workflows
   with M3 API and Excel steps are already available in VXL Live"*.
   **Confirmed in the JSON** by a GET of a hand-built, VXL-tagged
   workflow: it is the plain top-level `labels` array,
   `"labels": ["VXL"]`. In a spec, `"vxl": true` (or just put it in
   `"labels"`).
2. **Make the final step return export-shaped data.** Confirmed hard
   requirements:
   - an **array of objects**;
   - **every object carries the same property keys**, even where a value
     is missing;
   - use `""` rather than `null` for a blank.

   Ragged rows are the failure mode to watch: a result set where some rows
   errored and some were skipped naturally has different keys per row.
   VinceForge's Code-step generator squares the keys up for exactly this
   reason.

The workflow then exports into whatever medium invoked it — a sheet if it
came from VXL Live, a table if it's writing a table, and so on.

---

## A workflow is also an API

Confirmed, and directly useful for testing this path:

```
POST /live/workflows/{workflowId}/sync
Header: vl-wf-control-return-data: true
Body:   { "orderNumber": "0010000386" }
```

`/sync` waits for the workflow and returns the **last step's output**.
Without that header you get execution metadata instead of the data.
**AWS API Gateway caps this at 29 seconds** — a real constraint on how
much sequential HTTP a Code step can afford.

Values from the request body reach a REST API step's `endpoint`/`body` as
`{{ body.orderNumber }}`, and headers as `{{ header.someInfo }}` — the
same `{{ }}` substitution the Generic Filter step uses for
`{{ header.Filter_BD }}`.

Human-readable URLs come from Custom APIs
(`POST /v1/custom-apis/control`, grouped by `basePath`, permissioned via
a role's `vrn:...:fnd:custom-api:<basePath>_<endpoint>`).

---

## Building one with VinceForge

A **pipeline spec** is hand-written JSON that describes the chain; the
builder emits the Live workflow.

```bash
python converter/cli.py pipeline-scaffold my-app.json      # starter spec
python converter/cli.py pipeline my-app.json <connection_id> <tenant_id> <environment_id> out.json
python converter/selftest_pipeline.py                      # structural self-tests
```

In the web UI: paste a spec into **Source** (it auto-detects), or press
**Scaffold**. Warnings appear above the generated JSON.

### Spec shape

```json
{
  "vinceforge": "pipeline/1",
  "name": "Mass allocation with authority gate",
  "baseUrl": "https://mingle-ionapi.eu1.inforcloudsuite.com/DEMO_TENANT",
  "vxl": true,
  "trigger": {
    "buttonText": "Allocate",
    "hasSpreadSheetInput": true,
    "fields": [{ "name": "WHLO", "label": "Warehouse", "length": "3" }]
  },
  "steps": [
    { "kind": "m3Bulk", "ref": "authority", "program": "AUTCHKMI",
      "calls": [{ "transaction": "ChkAuthority",
                  "record": { "ITNO": { "row": "ItemNumber" } } }] },

    { "kind": "m3Bulk", "program": "MMS120MI",
      "gates": [{ "left": { "step": "authority.ALPG" }, "op": "eq", "right": { "const": "1" } }],
      "calls": [{ "transaction": "AllocateOrdLine",
                  "record": { "WHLO": { "trigger": "WHLO" },
                              "ITNO": { "row": "ItemNumber" } } }] },

    { "kind": "transform", "template": "..." }
  ]
}
```

**Step kinds:** `m3Bulk` (expands to the confirmed Transform + REST API
pair), `transform`, `restApi`, `code`, `m3RowPipeline` (Tier 3).

**Value sources** — the same four places `ir.py` names for the M3-API
path, stated explicitly instead of inferred from Classic's `*`-syntax,
plus a raw escape hatch:

| Spec | Meaning | Renders as |
|---|---|---|
| `{"const": 901}` | literal | `901` |
| `{"row": "OrderNumber"}` | a property of the current input row | `OrderNumber` |
| `{"trigger": "CONO"}` | a trigger field | `$context.data.trigger.body[0].CONO` |
| `{"step": "authority.ALPG"}` | a field from an earlier call's records | `$context.data.all.rest_api_1.body.results.records.ALPG` |
| `{"step": "authority:GetHead.CUNO"}` | …from one named transaction | `...results[transaction="GetHead"].records.CUNO` |
| `{"jsonata": "'-' & ORQT"}` | raw JSONata | verbatim |

**Reference earlier steps by `ref`, not by stepId.** `ref` resolves to the
real `rest_api_N` at build time. Hardcoding `rest_api_1` works until you
insert a step, at which point every id shifts and the reference silently
points at the wrong call.

**Gate operators:** `eq`, `ne`, `empty`, `notEmpty`, `contains`, `gt`,
`lt`, `in`, `notIn`, each with optional `caseInsensitive` — Classic's
`RowFilterEQ`/`RowFilterNE` map to `eq`/`ne`, and `IsLower="true"` finally
has a real home in `caseInsensitive` (Live's native filter has no
equivalent, and only does equal/notEqual). Use `filterExpression` for a
raw JSONata predicate instead.

### `m3RowPipeline` only: per-row state

A row pipeline runs the calls in order **for one row at a time**, so it can
say things the bulk pairs cannot. Its extra keys:

| Key | On | Meaning |
|---|---|---|
| `rowGates` | the step | gates checked once per row, before any call - the row is skipped whole. May not reference a call; nothing has run yet |
| `groupColumns` | the step | the columns whose combined value defines a group. A break is detected against the **previous** row only, so the rows must arrive sorted |
| `groupResultScope` | the step | `"group"` (default - a once-per-group call's result is carried to the whole group) or `"firstRow"` |
| `columnHeaders` | the step | optional column-letter → header-name map for `{"row": ...}` lookups |
| `oncePerGroup` | a call | fires on each group's first row only |

and one extra value source, which only this path can offer:

| Spec | Meaning |
|---|---|
| `{"call": "authority.ALO1"}` | a field of that call's first record, **for this row** |
| `{"callStatus": "authority"}` | that call's outcome for this row: `"ok"` / `"failed"` / `"skipped"` / `"notrun"` |

`callStatus` is read from the confirmed `nrOfFailedTransactions` on a
response to a **single-transaction** POST, which is the only place that
counter is a per-call verdict. It is what makes Classic's
`<BasedOnTransaction>` — including the get-or-create form — expressible at
all. See "Translating Classic into a pipeline" below.

With `debug: true` the step returns `{records, _diagnostics}`;
`_diagnostics` carries the real first row and its keys, the exact first
payload sent, the connection's base URL, and counts of skipped rows,
skipped calls, groups seen and unknown outcomes. That is the fastest way to
answer the open questions at the bottom of this file.

**CONFIRMED HAZARD, 2026-09-11:** turning it off is not optional cleanup,
it is required before any run whose sheet you intend to reuse. A
Manual-trigger run bound to a spreadsheet range writes the step's return
value back into the SAME source sheet - and since `debug: true`'s shape
isn't a bare array, the write-back can't expand it into a table, so it
dumps the whole thing as one giant JSON string into a single cell
(observed: cell A2). That cell is then read back in as row 1's data on the
NEXT run - a self-poisoning feedback loop. Run once with `debug: true` to
read the diagnostics, then turn it off AND clear whatever cell it wrote
into before running again on the same sheet.

**A second, related bug found the same day while chasing this one:**
flipping `"debug": true` to `"debug": false` directly inside an
already-pasted Live Code step and re-running did nothing - the dump kept
happening. Cause: the generator used to pick between two fixed return
statements in PYTHON at generation time, so the `debug` value sitting in
the deployed PLAN object was inert data the JS never read - only
regenerating with `debug=False` and re-pasting the whole step actually
changed anything. **Fixed in code**: the runtime JS itself now reads
`PLAN.debug` (`return PLAN.debug ? {records, _diagnostics, _aborted} :
records;`), verified by regenerating and finding the new ternary in the
emitted JS.

**UNRESOLVED as of end of day 2026-09-11**: a brand-new workflow built
with this fix (`WORKFLOW-a8f1da15fb2995194a36d49840242826`, "Extract Price
Lists 11") still self-poisoned its sheet on its first run. Two
explanations are live, neither confirmed: the fix is incomplete in some
way not yet identified, or the test was a false negative from a separate,
newly-noticed hazard - **the Excel add-in needs a manual refresh after a
workflow is created/changed, and can take a couple of minutes past that
refresh before the new version is actually available to run.** Don't
trust a repeat of this test without deliberately waiting well past that
delay first.

---

## Translating Classic into a pipeline

**Built, 2026-09-11** — `converter/classic_to_pipeline.py`, driven by
`python cli.py classic-pipeline <classic.xml> <connection_id> <tenant_id>
<environment_id> <output.json> --base-url <ion_api_root>`, guarded by
`converter/selftest_classic_pipeline.py`. It converts the same file as
`classic-xml` and produces a workflow one block at a time, exactly the same
fan-out.

### It emits a Code step, not a chain of Transform+REST pairs

The design this section used to carry — one `m3Bulk` step per
`<ImportDetail>` — does not survive contact with the two real files.
Writing the translator against them showed two independent reasons, and
both are properties of the *bulk* shape rather than of the translation:

1. **Correlation collapses as soon as anything is gated.** A bulk step
   sends N rows as N transactions and reads an earlier step's answer for
   row *i* as `records[$i]`. That holds only while every step saw the same
   rows in the same order. A Classic chain gates nearly every call, so each
   step sends a *different* subset — and "Add Price List" chains nine
   calls, each gated on the one before. By the third step the index means
   nothing. A row pipeline holds one row at a time, so there is no index to
   misalign.

2. **`<BasedOnTransaction>` needs a per-call verdict, and the bulk response
   only gives an aggregate one.** The confirmed failure signal is
   `nrOfFailedTransactions`; what a single *failed* result entry looks like
   inside a multi-transaction payload has never been captured. Posting
   exactly **one** transaction per request — which is what the row pipeline
   does — turns that aggregate counter into a verdict on that one call,
   using only confirmed fields. That is the difference between expressing
   the get-or-create idiom and guessing at it. Record count is deliberately
   *not* used as the test: a Get that finds nothing and a write that
   succeeds and returns nothing both come back with no records, and
   treating those alike is how a get-or-create turns into a double-create.

Point 2 is why `{"callStatus": "<ref>"}` exists in `pipeline_ir` and why it
is a Code-path-only value source. The trade is stated in
`pipeline_warnings()` on every conversion: a Code step is invisible in the
builder UI, spends wall clock on sequential HTTP (mind the 29s ceiling),
and is the escape hatch rather than the idiom. Prefer `build.py`'s native
path whenever it can express the file.

### What maps to what

| Classic | Becomes |
|---|---|
| one `<ImportDetail>` | one call in the row pipeline, `ref` = `PROGRAM_Transaction` with an occurrence suffix (`AUTCHKMI.ChkAuthority` legitimately appears twice in one block) |
| `<ImportHeading><StartRun>` | `oncePerGroup` on that call, plus `groupColumns` on the step |
| `RowFilterEQ` / `NE` / `GT` | a `gate` with `eq` / `ne` / `gt`; `IsLower="true"` → `caseInsensitive`, which the native filter has no equivalent for |
| several `<RowFilter>` in one wrapper | AND, which is what `gates` already means |
| an **`<Import>`-level** filter | a **row** gate — it skips the row, so it is checked once before any call rather than repeated onto each |
| a filter on a column an earlier `<Cell>` writes | `{"call": "<that call's ref>.<FIELD>"}`, resolved to the **nearest earlier** writer |
| a filter on a column nothing writes | `{"row": "<column>"}` |
| `BasedOnTransaction TransactionFailed="false"` | `{"callStatus": "<ref>"} eq "ok"` |
| `BasedOnTransaction TransactionFailed="true"` | `{"callStatus": "<ref>"} eq "failed"` — the get-or-create idiom |
| `InputField Value="*<letter>"` | `{"row": "<letter>"}` |
| `Source="Constant"` | `{"const": ...}` |
| `Source="UserID"` (and any other unknown `Source`) | a trigger field, reported |
| `*PROGRAM.Transaction.FIELD` | `{"call": "<ref>.FIELD"}`; when the producer is an `<Export>` call it is not in the pipeline, so it falls back to the column that block writes the same field to — reported |
| a call's `<Cell>` fields | `selectedColumns` on that call, together with whatever later gates read from it |

### What it refuses, and what it only reports

An `<Export>`-only block is **refused**, with a message pointing at
`cli.py classic-xml`. It queries M3 and writes rows *into* the sheet: no
input row to iterate, no filters, no chaining, and this path cannot use
`excel_1` at all — so the native path is strictly better for it. In a
*mixed* unit (the first `<Export>` and first `<Import>` share unit 1) the
Export calls are left out and named.

Reported, never approximated: an unseen `RowFilter*` operator; `GT` against
an empty `Value` (the real file's `IncludeBlank="true"` case, which reads
self-contradictory and would compare every value against zero if rendered
literally); `IncludeBlank` itself; a filter that is *not* `IsHidden` and so
was editable in Classic; `IsCritical`; `MaxRows`; and the sheet/start-row
settings.

### What it returns: the input rows, with the `<Cell>` columns written in

**Changed 2026-09-29.** The step used to return one record per call per row
(`_row`, `_call`, `_status`, `_message`, `I_<field>`, every returned field).
Written back into the source sheet, that flooded it with columns on every
CustomerA test. A Classic conversion now uses the row pipeline's
`output: "sheet"`: the INPUT rows are handed back in the shape they arrived
in (same count, order and keys). Each `<Cell>` field is written under its
column's key, and only on a row where that call actually ran (a
once-per-group call's cell lands on the group's first row alone, as in
Classic). `MessageColumn` gets the row's failed calls as
`<call>: <message>` joined by ` | `, and is blank on success. The call log
is only returned under `_calls` when `debug` is on. The config lives in the
step's `PLAN` (`output`, `sheetCells`, `messageColumn`), so it can be edited
in a pasted step.

**The sheet must be formatted as an Excel table** for VXL to know where to
read and write (Haakon, 2026-09-29). Still unconfirmed on a table:
- whether its rows arrive keyed by header name rather than letter (the
  lookup tries the letter, then `columnHeaders`, then case-insensitively,
  and writes to whichever key the row already uses);
- whether a column blank on every input row, which is added as a new key,
  is written back;
- whether the leading-empty-column trim still shifts the letters.

One run of a converted Import block against a table, reading
`_diagnostics.rowKeys`, settles all three.

### The two unresolved facts, and where they live

Both are now settled.

- **`classic_to_pipeline.row_key()`** — **CONFIRMED 2026-09-11**, against a
  real VXL-triggered run (`WFEXEC-08330923...`): a spreadsheet row IS keyed
  by column letter, not header name (open question 4 below, now resolved).
  Column letters stay as the property names; a caller may still pass a
  letter→header map (`--column-headers <map.json>`) for the rare sheet that
  needs one. That same run also caught a real bug: `rows_js` defaulted to
  `trigger.body.data`, but a VXL trigger's `body` is a bare array, not
  `{data: [...]}` — every row pipeline was silently processing zero rows.
  Fixed in `pipeline_ir.py`. A follow-on caveat surfaced in a later run the
  same day: VXL's read feature trims entirely-EMPTY LEADING columns and
  re-indexes what's left starting at "A" — an Import-only conversion is
  especially exposed, since the columns its paired `<Export>` would have
  written sit blank and get trimmed.
- **`classic_to_pipeline.GROUP_RESULT_SCOPE`** — how far a once-per-group
  call's result reaches. **CONFIRMED 2026-09-11**, in a later run on the
  same workflow once the `rows_js` bug was fixed (`WFEXEC-3cca6a49...`):
  with two rows sharing one group key, the once-per-group `SelUsers` call
  fired on row 1 and was correctly skipped on row 2
  (`groupRepeatsSkipped: 1`) — and row 2's own `AUTCHKMI_ChkAuthority`
  still came back `"ok"`, because it read row 1's carried result instead of
  seeing `"notrun"`. Default stays `"group"`. The reasoning, for the
  record: C05_MMS100's "Create RO" groups by `B C E O` (one destruction
  order) and its `MMZ101MI.AddLine` — one **line** per row — is gated on
  `P`, the column its once-per-group `ChkAuthority` writes; under the
  literal sheet reading (`"firstRow"`) exactly one line per order would
  ever be added, which the real run confirms is not what happens.

Every conversion still reports both in its warnings, since a different
sheet or Classic file is still worth a sanity check.

---

## Confirmed vs. not

**Confirmed:** the bulk endpoint path and method; the request body fields;
the full response shape; `version: 2`; both `bodyPath` and `body` modes;
`connectionId` attaching auth automatically; `{{ body.x }}` /
`{{ header.x }}` substitution; the four step types' `type`/`name`/`target`;
linear `childSteps`; `childSteps: []` as a valid terminal; the `VXL` label
and the export-shape rules; `/sync` + `vl-wf-control-return-data` + the
29s ceiling; Custom APIs; `authorizedAxios`/`getContext` in a Code step.

**Not confirmed — verify before building on it:**

1. **`skipIfExpression` syntax** — name only (Tier 2 above).
2. **`forEach`** — documented, but beta, self-described as having
   inconsistent variable naming, and its own doc's example says
   `"version": 3` while the instructions say `2`. No captured example.
3. ~~**The `VXL` label's exact JSON home**~~ — **CONFIRMED** as top-level
   `labels[]` by a GET of a hand-built VXL workflow. The three speculative
   placements this doc used to hedge with are gone from the code.
4. ~~**Trigger row shape for VXL/spreadsheet input**~~ — **CONFIRMED
   2026-09-11**, against a real VXL-triggered run: `trigger.body` is a bare
   ARRAY of row objects keyed by COLUMN LETTER, unlike every
   widget-triggered example on record, which is keyed by header name
   (`OrderNumber`, or `"Order Number"` with a space) and shaped
   `{data: [...]}`. The Code-step generator's lookup order (letter, then
   any supplied column→header mapping, then case-insensitively) and its
   `_diagnostics.firstRow` / `_diagnostics.rowKeys` reporting stay as a
   safety net for a widget-triggered pipeline built on this same generator,
   not as a hedge against this being wrong. See
   `classic_to_pipeline.row_key()`. The `rows_js` default that reads the
   array itself is `trigger.body`, not `trigger.body.data` — that `.data`
   suffix was this doc's own unconfirmed guess (§ code example below) and
   silently zeroed every row pipeline's input until this run caught it.
   **A follow-on caveat, confirmed in a second run the same day:** the
   letter is NOT the worksheet's real column — VXL's read feature trims
   entirely-EMPTY LEADING columns from the used range and re-indexes what
   is left starting at "A". Two runs on the same sheet nailed it down: data
   typed into worksheet column A arrived as JSON key "A"; the same data
   moved to worksheet column B (with A left blank) arrived as JSON key "A"
   too — a reproducible shift from changing exactly one thing (whether
   column A had content). An Import-only conversion is especially exposed
   to this: the columns its paired `<Export>` would have written (which the
   Import side never touches) sit blank and get trimmed, shifting every
   Import column that follows them left by however many leading columns
   were empty.
5. **The single-transaction GET form** — real URL supplied, but no
   captured Vince workflow uses it. Prefer the bulk POST.
6. **Whether the M3 API step's automatic pagination has any equivalent
   here.** It does not, as far as anything confirmed goes: that
   `CheckForPendingApi` loop is specific to the M3 API step. On this path,
   `maxReturnedRecords` and your own paging are all there is.

**A standing contradiction worth resolving:**
`docs/vince-live-workflow-notes.md` §7 records that multi-statement
semicolon-chained JSONata (`( $x := ...; ... )`) **fails to parse** in the
Transform editor. Every real production workflow above uses exactly that
form successfully, and the reference JSONata implementation parses what we
generate. So either the earlier failure had a different cause or the
editor changed. Treat §7 as suspect rather than as a constraint, and
re-test if a template ever refuses to save.

---

## What you give up by leaving the M3 API step

State it plainly before choosing this path:

- **No field metadata validation.** The M3 API step is checked against
  real M3 field metadata; a bulk payload is not. A wrong field name
  surfaces at run time, in M3's own error.
- **No automatic pagination.**
- **No `excel_1` binding** — the VXL label plus an export-shaped final
  step instead.
- **Much looser save-time validation.** A malformed Transform template has
  been observed to silently reset rather than fail. Re-open and check
  after saving.
- **Filters are invisible in the builder.** A JSONata predicate is legible
  only inside the Transform. That's the trade for filters the native step
  cannot express at all.

---

## Next real-data steps

In the order that buys the most:

1. ~~**GET a real VXL-labelled workflow.**~~ **Done** — that one GET
   settled the VXL label *and* caught the `content`-wrapper bug in the
   generator, which would have produced workflows that saved cleanly and
   then failed at run time with no content reaching the proxy lambda.
   Worth remembering as an argument for building one by hand *first*.
2. **Create one generated pipeline in a real tenant and execute it.**
   `RUN/pipeline-examples/order-lines-readback.json` is a deliberate
   rebuild of a known-good production workflow — if it renders and runs,
   the generator is sound; if it doesn't, diff it against the real one.
3. **Run one spreadsheet-triggered pipeline** and record the real row
   shape (open question 4). A Classic conversion
   (`cli.py classic-pipeline`) is the cheapest way to get one: it prints
   the assumption, and the run's `_diagnostics.rowKeys` answers it.
3b. **Settle whether a group's 2nd..nth rows run** — the other half of the
   Classic translation's assumptions, and the more dangerous one, since
   the wrong answer means either one line per order or none. The test is
   in `classic_to_pipeline.GROUP_RESULT_SCOPE`.
4. **Probe `skipIfExpression`** with a trivially true and a trivially
   false expression and see which requests fire (open question 1).
5. Write each answer into this file and into
   `docs/vince-live-api-reference-prompt.md`, and collapse the
   defensive branches in `converter/m3_rest.py` that exist only because
   the answer was unknown.
