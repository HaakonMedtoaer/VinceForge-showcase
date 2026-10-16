# Calling M3 without the M3 API step — the generic pipeline path

How to build a Vince Live workflow that talks to M3 and filters rows
**without the M3 API component or the Excel component at all** — Trigger →
Transform → REST API chains posting to M3's bulk execute endpoint, with
conditional logic living in JSONata (or, when even that can't carry the
control flow, in a Code step).

**This is not a workaround.** It's the shape Vince's own production
customer workflows use, including one performing real M3 writes across
seven Transform/REST API pairs with no M3 API step and no Excel step
anywhere in it. Confirmed from real captured production examples and direct
API probing — provenance is named per fact below.

**When to reach for this path** — see `m3-native-api-step.md` §9 for the
full reasoning: above all **per-call filtering** (`GENERIC_FILTER` runs
once, before the whole M3 API step, so it can't gate one call while another
runs unconditionally), and **gating on a value an earlier M3 call
produced** (impossible on the native path at any operator, structurally —
`GENERIC_FILTER` only ever sees raw spreadsheet rows).

What you give up by leaving the M3 API step: field-metadata validation,
automatic pagination, `excel_1` binding (use the `VXL` label instead — see
`auth-endpoints-and-unconfirmed.md`), and much looser save-time validation.
**Prefer the native path whenever it fits.**

## 1. The confirmed idiom — Transform + REST API pairs

A repeated pair: a **Transform** that builds `{"payload": {...}}`, then a
**REST API** step that posts it.

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

(This whole object lives inside a `content` **string** in the step's
stepConfig — see `live-workflow.schema.json`'s `restApiStepConfig`.)

**Region and `<TENANT>` are tenant-specific and cannot be derived.** Copy
them from a real REST API step in that tenant, or ask for them explicitly.
Real observed values: `.../DEMO_TENANT/...`, `.../TEST_TENANT/...`.

With `connectionId` set, Live attaches the connection's auth automatically
— never build an ION API token by hand.

### The request body

No program/transaction in the URL — one call carries **many** M3
transactions, which is the entire point (M3 can need eight calls to read
one order):

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

`cono`, `maxReturnedRecords`, and per-transaction `selectedColumns` are
optional. `selectedColumns` is worth using on reads — without it M3 returns
every field of the record (one captured response had ~140).

### The response

```json
{
  "statusCode": 200,
  "body": {
    "nrOfSuccessfullTransactions": 7,
    "nrOfFailedTransactions": 0,
    "wasTerminated": false,
    "results": [
      { "transaction": "GetOrderHead", "records": [ { "ORNO": "0010000386" } ] }
    ]
  }
}
```

(`nrOfSuccessfullTransactions` is spelled exactly that way — three L's.)
`nrOfFailedTransactions` is the confirmed way to know a call failed — this
is the only per-request-level failure signal; there is no confirmed
per-transaction-inside-the-request failure detail beyond it (see §4 for
what that means for a get-or-create idiom on this path).

## 2. The binding model (Transform step)

Three separate things, and they don't behave alike:

| Binding | What it is | Notes |
|---|---|---|
| `body`, `header` | Bare paths into the **previous** step's output | No `$` prefix, e.g. `body.hello` |
| `$context` | Bound variable for the whole run context | Needs the `$`. Only way to reach a *specific* earlier step |
| `$context.data.trigger.body` | All trigger rows (array) | |
| `$context.data.trigger.body[0].FIELD` | One trigger field | `[0]` = first row |
| `$context.data.all.<stepId>` | A named earlier step's output | Shape depends on the step type — see below |

### Trap: output shape differs per upstream step type

```jsonata
$context.data.all.m3_api_1                    /* M3 API step: the array DIRECTLY, no .body */
$context.data.all.rest_api_1.body.results      /* REST API step: {headers, body} wrapper, .body REQUIRED */
```

Carrying either convention across step types yields empty results with **no
error**.

### Trap: the preview pane binds differently than real execution

The builder's Transform preview pane, when sample JSON is pasted into its
Input box, binds it to bare `$.<stepId>` — **not** `$context`. Only a real
saved-and-executed workflow resolves `$context.data.all.<stepId>`. Write
and test against `$.<stepId>` in the preview, then swap to
`$context.data.all.<stepId>` before treating a template as final —
forgetting the swap produces a **blank result with no error**.

### Field names needing quotes

`$context.data.trigger.body[0]."Order Number"` — real captured trigger rows
are keyed by header name, and real header names contain spaces.

## 3. Reading an M3 API step's output from a Transform

Runtime field naming (see `m3-native-api-step.md` §6): `<id>_X` for output
field X, `<id>_I_X` for its input echo. Filter padding rows on the input
echo, never an output field. Instance ids are unstable across saves — avoid
hardcoding one.

**Confirmed dynamic suffix-matching pattern** (uses only `$keys`, `$filter`,
`$substring`, `$lookup` — no instance id appears anywhere):

```jsonata
$context.data.all.m3_api_1[$exists($lookup($, $filter($keys($), function($k){$substring($k, -7) = "_I_TRNM"})[0]))].{
  "Program":     $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_MINM"})[0]),
  "Transaction": $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_TRNM" and $substring($k, -7) != "_I_TRNM"})[0])
}
```

Get the character count in `$substring($k, -N)` exactly right — a wrong
count returns no match, **silently**, with an empty result and no error.
Disambiguate colliding suffixes (`_TRNM` also matches inside `_I_TRNM`)
with an explicit exclusion.

## 4. Reading a REST API (bulk M3) step's output

```jsonata
$context.data.all.rest_api_1.body.results.records.CUNO
/* ^ FLATTENED across ALL transactions in the call - fine for one transaction per payload,
     silently mixes results if the payload carries several */

$context.data.all.rest_api_1.body.results[transaction="GetHead"].records[0].CUNO
/* ^ one named transaction's records - use this once a payload carries more than one */
```

## 5. Building the payload from rows — the confirmed idiom

```jsonata
(
  $data := $context.data.trigger.body;
  { "payload": {
      "program": "OIS100MI",
      "transactions": $data.{ "transaction": "GetHead",
                              "record": { "ORNO": OrderNumber } }[]
  } }
)
```

- **`$data.{...}[]` maps N input rows to N M3 transactions inside ONE
  call.** The trailing `[]` forces an array even for a single row — easy to
  omit and it only breaks on single-row input, since JSONata otherwise
  yields a bare object.
- Inside `$data.{...}`, the current row **is** the context — a row property
  is unquoted (`OrderNumber`) unless it needs quoting for a space.
- Several transactions per row: use an array literal and let JSONata
  flatten the per-row arrays —
  `$data.[{"transaction": "GetHead", ...}, {"transaction": "GetLines", ...}]`.
- Whether the Transform's output is wrapped (`{"payload": {...}}`, needing
  `bodyPath: "$.payload"`) or the bulk body unwrapped (`bodyPath: "$"`) —
  **both are confirmed in production**; they must agree with each other or
  M3 receives the wrong shape.

## 6. Filtering and gating — three tiers, in order of preference

**Tier 1 — a JSONata predicate in the Transform. Use this.** Each call gets
its own filter, so one call can be gated while the next runs over
everything:

```jsonata
$data := $context.data.trigger.body[Approved = "OK"];
```

Gating on an **earlier call's output** (the case the native filter cannot
express at all) needs per-row correlation — a prior step's `...records.ALPG`
is that field's **whole flattened sequence**, not this row's value:

```jsonata
$data := $filter($context.data.trigger.body, function($row, $i) {
  $context.data.all.rest_api_1.body.results.records[$i].ALPG = "1"
    and $lowercase($row.Approved) = "ok"
});
```

Inside the callback, the row is a **parameter** (`$row.`), unlike a plain
predicate or `$data.{...}` where the row is the implicit context.

> ⚠️ **Index correlation is fragile by nature.** `records[$i]` lines up with
> row `$i` only while the prior call returned exactly one record per input
> row, in order, with nothing dropped — and `nrOfFailedTransactions` exists
> precisely because M3 can drop some. Any row-filtering **between** two
> calls destroys the alignment. When it matters, carry the key field (e.g.
> `ORNO`) into the payload and join on it instead of trusting the index.

**Operator → JSONata rendering** (a useful hand-writing reference, and the
full set VinceForge's own generator emits):

| Operator | JSONata |
|---|---|
| `eq` | `left = right` |
| `ne` | `left != right` |
| `empty` | `($not($exists(left)) or $string(left) = "")` |
| `notEmpty` | `($exists(left) and $string(left) != "")` |
| `contains` | `$contains(left, right)` |
| `gt` | `$number(left) > $number(right)` |
| `lt` | `$number(left) < $number(right)` |
| `in` | `left in right` |
| `notIn` | `$not(left in right)` |

`gt`/`lt` wrap both sides in `$number()` deliberately — M3 values often
arrive as strings, and a raw `>` would compare lexically. Case-insensitive
comparison has no native equivalent anywhere in Live; wrap both sides in
`$lowercase()` inside a Transform (there is no equivalent at all on the
native `GENERIC_FILTER` step).

**Tier 2 — `skipIfExpression` on a `version: 2` REST API step.** Name
confirmed (from an implementing ticket); **syntax and truthiness semantics
documented nowhere**. Probe before relying on it — Tier 1 does the same job
with semantics you can test locally.

**Tier 3 — a Code step.** Only when a later call's *input* genuinely
depends on an earlier call's *output* for the same row (so the calls can't
be batched), or per-row throttling/error handling is needed. See §7 for the
costs and §8 for when a whole chain collapses into one.

## 7. The `{{ }}` layer is a separate thing, not JSONata

Vince also has a `{{ }}` templating layer for values from the workflow's own
HTTP invocation (`{{ body.orderNumber }}`, `{{ header.someInfo }}`) and for
the Generic Filter step (`{{ header.Filter_BD }}`) and a REST API step's
literal-`body` mode. Don't mix the two syntaxes inside a Transform template.

## 8. When even Transform/REST API pairs aren't enough: a single Code step

The Transform/REST API bulk-pair shape (§1) breaks down for control flow
that is heavily per-row gated, for two structural reasons:

1. **Index correlation dies as soon as a call is gated.** A bulk step sends
   N rows as N transactions and reads an earlier step's answer for row *i*
   as `records[$i]`; that only holds while every step saw the same rows in
   the same order. Once a call is gated, each step sends a different
   subset, and by the third gated call in a chain the index means nothing.
2. **A dependency on one call's success/failure (a get-or-create idiom)
   needs a per-call verdict.** The bulk response's only confirmed failure
   signal is the aggregate `nrOfFailedTransactions` — what one failed
   result looks like *inside* a multi-transaction payload has never been
   captured. Posting exactly ONE transaction per request turns that same
   counter into a verdict on that one call.

The confirmed-working answer for that shape is a **single Code step** that
processes one row at a time, posting one M3 transaction per HTTP call and
reading `nrOfFailedTransactions` off each response as that call's
success/failure verdict. This is real production-proven engineering (a real
generator in VinceForge builds exactly this for Classic-to-Live migrations
with deep per-row conditional logic), not a documented Live feature with
its own captured example independent of that generator — treat the
specific JS shape as something to design fresh from the Code step contract
in `live-workflow.schema.json`, using this section's reasoning for *why*
one call per row is required, not as a captured template to copy verbatim.

Confirmed real hazard on this shape, if the workflow is bound to a
spreadsheet range and returns row-level diagnostics: a Code step returning
anything other than a bare array of plain row records (e.g. a debug
wrapper object) can get written back into the SAME source sheet as one
giant JSON string in a single cell, which is then read back in as row data
on the NEXT run — a self-poisoning loop. **The final return value must be
export-shaped**: a bare array of plain objects, `""` rather than `null` for
blanks, uniform keys across every object. If a Code step needs diagnostics
for debugging, gate them behind an explicit flag read at runtime (not baked
in at generation time) and make sure it is off before a design would run
unattended.

## 9. Silent failure modes — the full list

Every one of these produces an empty/blank result rather than an error:

1. Using `$context` in the preview pane (undefined there — use `$.<stepId>`).
2. `.body` on an M3 API step's output, or its absence on a REST API step's.
3. A wrong character count in `$substring($k, -N)`.
4. A colliding suffix match without an explicit exclusion.
5. Omitting the trailing `[]` on `$data.{...}` with single-row input.
6. `records.FIELD` flattening across multiple transactions in one payload.
7. A hardcoded instance id after a re-save regenerated it.
8. `bodyPath` disagreeing with the Transform's actual output shape.

Also: **a malformed Transform template has been observed to silently
disappear/reset rather than fail to save with a clear error.** Save-time
validation on Transform/REST API/Code steps is much looser than on the M3
API step.

## 10. Confirmed vs. not, on this path

**Confirmed:** the Transform/REST API pairing shape; the `content`-string
wrapping on REST API vs. flat Transform config; per-step-type output
wrapping; the bulk request/response shape including
`nrOfFailedTransactions`; the `$data.{...}[]` payload idiom and the
trailing-`[]` requirement; both `bodyPath` modes; index correlation via
`records[$i]`; the operator renderings above; the `{{ }}` layer being
separate; `version: 2` unlocking `forEach`/`skipIfExpression` by name.

**NOT confirmed — flag explicitly if a design depends on these:**
- `skipIfExpression`'s actual expression syntax and truthiness semantics.
- `forEach`'s real behaviour end-to-end (documented but beta; its own doc
  contradicts itself on the version number; no captured example).
- Whether a semicolon-chained multi-statement JSONata block
  (`( $x := ...; $y := function(){...}; ... )`) reliably parses in the
  Transform editor — real evidence is contradictory (an early test recorded
  a parse failure; four real production workflows use exactly this
  construct and work). Treat as suspect either way, not as a hard rule.
- `trimAll`'s actual behaviour beyond `true` being the only value seen.
- Whether the M3 API step's automatic pagination has any equivalent here —
  as far as anything confirmed goes, it does not; `maxReturnedRecords` and
  manual paging are all there is.
- Whether a spreadsheet-bound (rather than widget-bound) trigger's rows
  arrive keyed by column letter or by header name — confirmed **by column
  letter** for a VXL-triggered spreadsheet specifically (a real captured
  run), with a caveat: VXL's read feature trims entirely-empty **leading**
  columns and re-indexes what's left starting at `"A"`, which silently
  shifts every later column left. This is a real, previously-observed
  hazard for any design assuming a fixed column mapping onto a sheet whose
  leading columns might be blank.
