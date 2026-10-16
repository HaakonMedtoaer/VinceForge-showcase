# Vince Live Transform Step — JSONata Guide

Everything confirmed about writing JSONata inside a Vince Live **Transform**
step: the binding model, how to read each upstream step type's output, the
production payload-building idiom, filtering and gating, and the silent
failure modes that eat the most time.

Self-contained — no prior knowledge of the VinceForge codebase needed.
Everything here was confirmed against a real execution, a real `GET` of a
saved workflow, a real builder-UI test, or a captured production customer
workflow. Anything unconfirmed is marked as such rather than smoothed over.
**Don't extend it by analogy** — if a construct isn't listed, it isn't
confirmed, and the failure modes here are mostly silent.

---

## 1. The step and its config shape

A Transform step in `workflowConfig.steps`:

```json
{
  "stepId": "transform_1",
  "type": "TRANSFORMER_MORPH",
  "name": "Transform",
  "retryAttempts": 0,
  "target": "workflow-transform",
  "childSteps": [ ... ]
}
```

Its entry in `stepConfig` is **flat**:

```json
"transform_1": {
  "template": "( $data := $context.data.trigger.body; { \"payload\": ... } )",
  "trimAll": true
}
```

`template` is a plain string holding one JSONata expression — not nested
JSON, not an array of statements.

⚠️ **Do not generalise this shape to other step types.** A REST API step
(`GENERIC_API`) in the same workflow wraps its whole config inside a
`content` **string** instead:

```json
"rest_api_1": { "content": "{\n  \"version\": 2,\n  \"endpoint\": \"...\"\n}" }
```

Confirmed by a `GET` of a hand-built workflow: the compiled state machine
parses that string back out. A flat REST API config saves cleanly and then
fails at run time with nothing reaching the proxy. Transform is flat; REST
API is wrapped. Neither convention carries across.

---

## 2. The binding model

This is where most time gets lost. Three separate things are in play and
they don't behave alike.

| Binding | What it is | Notes |
|---|---|---|
| `body`, `header` | Bare paths into the **previous** step's output | No `$` prefix. E.g. `body.hello` |
| `$context` | Bound variable for the whole run context | Needs the `$`. The only way to reach a *specific* earlier step |
| `$context.data.trigger.body` | All trigger rows (array) | |
| `$context.data.trigger.body[0].FIELD` | One trigger field | `[0]` = first row; the confirmed form for a single-value trigger input |
| `$context.data.all.<stepId>` | A named earlier step's output | Shape depends on the step type — see §3/§4 |

Live's own default template for a fresh Transform step is itself the
evidence for the first two, and a useful thing to keep around while
debugging:

```jsonata
{
  "bodyFromLastStep": body,
  "bodyFromLastStepIfGenericRestAPI": body.body,
  "headerFromLastStep": header,
  "context": $context
}
```

### Trap 1 — output shape differs per upstream step type

The single most common mistake:

```jsonata
$context.data.all.m3_api_1                        /* M3 API step: the array DIRECTLY, no .body */
$context.data.all.rest_api_1.body.results         /* REST API step: {headers, body} wrapper, .body REQUIRED */
```

An M3 API step's result is the row array itself. A REST API step's result
is wrapped as `{ headers, body }`. Carrying either convention across step
types yields empty results with no error.

### Trap 2 — the preview pane binds differently than real execution

The builder's Transform preview pane, when you paste sample JSON into its
**Input** box, binds that data to bare **`$.<stepId>`** — **not**
`$context`. Only a real saved-and-executed workflow resolves
`$context.data.all.<stepId>`.

**Practical sequence:** write and test against `$.<stepId>` in the preview
pane, then swap to `$context.data.all.<stepId>` before saving for real.
Forgetting the swap produces a **blank result with no error** — it isn't a
syntax problem; `$context` is simply undefined in that pane.

### Field names needing quotes

A property name that isn't a bare identifier must be quoted:

```jsonata
$context.data.trigger.body[0]."Order Number"      /* space in the name */
$context.data.trigger.body[0].OrderNumber         /* bare name, no quotes needed */
```

This is real and common — captured trigger rows are keyed by **header
name**, and real header names contain spaces.

---

## 3. Reading an M3 API step's output

### Runtime field naming

For a transaction with instance id `<id>` inside an M3 API step:

| Runtime key | What it is |
|---|---|
| `<id>_X` | Declared **output** field `X` |
| `<id>_I_X` | **Input echo** — present when `X` is also an input on that transaction |
| `<id>_MSG_COL` | Per-row status/error message (e.g. `"OK"`) |
| `<id>_<id>_<program>_<transaction>_ROWNUM` | Observed always `null`; purpose uninvestigated |

Real example, instance `xu3EO2azWPT`, field `TRNM` used as both input and output:

```json
"xu3EO2azWPT_I_TRNM": "*",
"xu3EO2azWPT_TRNM": "LstClaimDet"
```

### Instance ids are not stable — plan around it

The instance id is **regenerated by Live**, not preserved from what you
send. Confirmed to change on every `POST /workflows`, and apparently also
across ordinary re-saves of an existing workflow.

So a template hardcoding `xu3EO2azWPT_TRNM` **silently breaks** on the next
save. Two options: re-read the new id via `GET` after every save, or match
fields dynamically by suffix so no id appears in the template at all.

### Dynamic field matching (avoids hardcoded ids)

Confirmed working, using only `$keys` / `$filter` / `$substring` / `$lookup`:

```jsonata
$context.data.all.m3_api_1[$exists($lookup($, $filter($keys($), function($k){$substring($k, -7) = "_I_TRNM"})[0]))].{
  "Program":     $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_MINM"})[0]),
  "Transaction": $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_TRNM" and $substring($k, -7) != "_I_TRNM"})[0]),
  "Description": $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_TRDS"})[0]),
  "Type":        $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_SIMU"})[0]),
  "Status":      $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_STAT"})[0])
}
```

Three things to get right:

1. **`$substring($k, -N)` takes the last N characters.** Count them
   exactly — `_MINM` is 5, `_I_TRNM` is 7 (both underscores count). A
   wrong count returns no match, **silently**, with an empty result and no
   error.
2. **Disambiguate colliding suffixes.** `_TRNM` and `_I_TRNM` both end in
   `_TRNM`, so the output-field match needs the explicit exclusion
   `and $substring($k, -7) != "_I_TRNM"` or it may pick either key.
3. **Filter rows on the input echo, not an output field** — see below.

### Padding / incomplete rows

Large M3 API results have been observed to include rows where **every
declared output field is `null`** — padding, or an artifact of how the
underlying call completes.

Filter these out using the mandatory **input echo** field
(`_I_<fieldName>`). Filtering on an output field does not work, because
those output fields are exactly what's `null` on the bad rows. That's why
the pattern above gates on `_I_TRNM`.

---

## 4. Reading a REST API (bulk M3) step's output

Confirmed response shape from the ION API bulk `execute` endpoint:

```json
{
  "statusCode": 200,
  "body": {
    "nrOfSuccessfullTransactions": 7,
    "nrOfFailedTransactions": 0,
    "wasTerminated": false,
    "results": [
      { "transaction": "GetOrderHead", "records": [ { "ORNO": "0010000386" } ] },
      { "transaction": "GetOrderValue", "records": [ { } ] }
    ]
  }
}
```

Read it from a later step:

```jsonata
$context.data.all.rest_api_1.body.results                                    /* array of results */
$context.data.all.rest_api_1.body.results.records.CUNO                       /* FLATTENED across all results */
$context.data.all.rest_api_1.body.results[transaction="GetHead"].records[0].CUNO   /* one transaction, first record */
```

⚠️ **`results.records.FIELD` flattens across every result in the payload.**
That's what most real examples do, and it's fine while one payload carries
one transaction. As soon as a payload carries several, the flattened form
**silently mixes them** — select the transaction explicitly with
`results[transaction="X"]` first.

Note `nrOfFailedTransactions`: M3 can drop transactions, which matters for
index correlation (§6).

---

## 5. Building a bulk M3 request payload

The confirmed production idiom — used by four real customer workflows,
including one doing real writes across seven Transform/REST pairs:

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

- **`$data.{...}` maps N input rows to N M3 transactions inside ONE call.**
  One HTTP call carrying many transactions is the whole point of this path.
- **The trailing `[]` forces an array even for a single row.** Without it
  JSONata yields a bare object, which is not what M3 accepts. Easy to omit
  and it only breaks on single-row input.
- Inside `$data.{...}`, the current row is the context — so a row property
  is just `OrderNumber`, unquoted (or `"Order Number"` quoted).

**Several transactions per row** — use an array literal instead, and let
JSONata flatten the per-row arrays:

```jsonata
"transactions": $data.[
  { "transaction": "GetHead",  "record": { "ORNO": OrderNumber } },
  { "transaction": "GetLines", "record": { "ORNO": OrderNumber } }
]
```

**Interaction with `bodyPath`.** The REST API step selects its request body
out of the Transform's output:

- Transform returns `{ "payload": {...} }` → `"bodyPath": "$.payload"`
- Transform returns the bulk body unwrapped → `"bodyPath": "$"`

Both are confirmed in production (most workflows wrap in `payload`; one
returns it unwrapped). They must agree, or M3 receives the wrong shape.

Optional payload fields, all confirmed: `cono`, `maxReturnedRecords`, and
per-transaction `selectedColumns`. `selectedColumns` is worth using on
reads — without it M3 returns every field of the record (one captured
`LstLine` response had ~140).

---

## 6. Filtering and gating

The reason to write filters in JSONata at all: Live's native
`GENERIC_FILTER` step runs **once, before an entire M3 API step**, so it
cannot gate one call while another runs unconditionally. A JSONata
predicate sits on the rows going into *one specific* call's payload, so
each call gets its own — and it can read an **earlier call's output**,
which the native filter cannot express at all.

### Plain predicate (prefer this)

```jsonata
$data := $context.data.trigger.body[Approved = "OK"];
```

### Index-correlated gate (when gating on an earlier call's output)

A prior step's `...records.ALPG` is that field's **whole flattened
sequence**, not this row's value. To get this row's value you must
correlate by index, which a plain predicate can't do (no index available) —
use a `$filter` callback:

```jsonata
$data := $filter($context.data.trigger.body, function($row, $i) {
  $context.data.all.rest_api_1.body.results.records[$i].ALPG = "1"
    and $lowercase($row.Approved) = "ok"
});
```

Note that inside the callback the row is a **parameter**, so row properties
need the `$row.` prefix — unlike a plain predicate or `$data.{...}`, where
the row is the context and the prefix must be omitted.

⚠️ **Index correlation is fragile by nature.** `records[$i]` lines up with
row `$i` only while the prior call returned exactly one record per input
row, in order, with nothing dropped — and `nrOfFailedTransactions` shows M3
can drop some. Any filtering *between* the two calls destroys the
alignment. When correctness matters, carry the key field (e.g. `ORNO`) into
the payload and join on it instead of trusting the index.

### Operator → JSONata rendering

The renderings below are what a generator emits; they're equally useful as
a hand-writing reference.

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

`gt`/`lt` wrap both sides in `$number()` deliberately — M3 values arrive as
strings often enough that a raw `>` would compare lexically.

**Case-insensitive comparison** has no native equivalent in Live's filter
step; in JSONata wrap both sides in `$lowercase()` (lower-casing a literal
at authoring time is equivalent and cheaper).

---

## 7. Value sources — a vocabulary for the four real places a value comes from

Useful whether or not you use a generator; these are the four sources any
M3 payload field actually draws from, plus a raw escape hatch:

| Source | Renders as |
|---|---|
| literal | `901` |
| current input row | `OrderNumber` (or `"Order Number"`) |
| trigger field | `$context.data.trigger.body[0].CONO` |
| an earlier call's records | `$context.data.all.rest_api_1.body.results.records.ALPG` |
| one named transaction's records | `$context.data.all.rest_api_1.body.results[transaction="GetHead"].records.CUNO` |
| raw JSONata | verbatim, e.g. `'-' & ORQT` |

**Reference earlier steps carefully.** Hardcoding `rest_api_1` works until
a step is inserted ahead of it, at which point every stepId shifts and the
reference silently points at the wrong call.

---

## 8. Editor and engine quirks

### Multi-statement blocks — status: contested, re-test before working around

An earlier test recorded that semicolon-chained variable-binding blocks —
standard, valid JSONata — **fail to parse** in the Transform editor:

```jsonata
( $rows := ...; $findKey := function(...){...}; $rows[...] )
```
→ `Expected "}", got ";"`

**That finding is now in doubt.** Four real production customer workflows
use exactly this construct and work, and the reference JSONata
implementation parses it without complaint:

```jsonata
(
  $data := $context.data.trigger.body[0];
  { "payload": { "program": "OIS100MI", "transactions": $data.{ ... }[] } }
)
```

So either the original failure had a different cause (a stray brace, or
something specific to that expression) or the editor changed. **Treat it as
suspect, not as a constraint** — the single-nested-expression workaround
costs real legibility. Re-test if a template ever refuses to save.

### Confirmed working in this engine

`$keys()`, `$substring()`, `$filter()`, `$lookup()`, `$exists()`,
`$not()`, `$string()`, `$number()`, `$contains()`, `$lowercase()`,
`$merge()`, inline `function($k){...}` definitions used directly inside an
expression, array predicates, `$data.{...}` mapping, the trailing `[]`
array-forcing idiom, and `in` / `!=` / `=` / `&` operators.

### `{{ }}` substitution is NOT JSONata

Vince also has a separate `{{ }}` templating layer, used for values from a
workflow's own HTTP invocation (`{{ body.orderNumber }}`,
`{{ header.someInfo }}`) and by the Generic Filter step
(`{{ header.Filter_BD }}`). It's a different mechanism at a different
layer — don't mix the two syntaxes inside a Transform template.

### Silent failure modes — the full list

Every one of these produces an empty/blank result rather than an error:

1. Using `$context` in the preview pane (undefined there — use `$.<stepId>`).
2. `.body` on an M3 API step's output, or its absence on a REST API step's.
3. A wrong character count in `$substring($k, -N)`.
4. A colliding suffix match without an explicit exclusion.
5. Omitting the trailing `[]` on `$data.{...}` with single-row input
   (yields an object where M3 wants an array).
6. `records.FIELD` flattening across multiple transactions in one payload.
7. A hardcoded instance id after a re-save regenerated it.
8. `bodyPath` disagreeing with the Transform's actual output shape.

Also: **a malformed template has been observed to silently reset rather
than fail to save.** Save-time validation on this step type is much looser
than on the M3 API step. Re-open the step after saving and confirm your
template is still there.

---

## 9. If the Transform is the final step

For the output to be exportable to a spreadsheet or table, the last step
must return:

- an **array of objects**;
- with **every object carrying the same property keys**, even where a
  value is missing;
- using `""` rather than `null` for a blank.

Ragged rows are the failure mode: a result set where some rows errored and
some were skipped naturally has different keys per row. Square them up
before returning.

---

## 10. Debugging checklist

In the order that resolves things fastest:

1. **Is it the preview pane or a real run?** If preview: use
   `$.<stepId>`. If real: `$context.data.all.<stepId>`.
2. **Right wrapper for the upstream step type?** M3 API → no `.body`;
   REST API → `.body`.
3. **Dump the context.** Temporarily set the template to `{ "ctx": $context }`
   (or Live's default template, §2) and run it — that shows the real
   shape rather than the assumed one.
4. **Check for a regenerated instance id** if a previously-working template
   went blank after a save.
5. **Count your `$substring` characters** if a suffix match returns nothing.
6. **Re-open the step** to confirm the template didn't silently reset.
7. **Test the expression outside Live.** The reference JSONata
   implementation (jsonata-js / the online JSONata Exerciser) parses and
   evaluates the same expressions, which separates "my JSONata is wrong"
   from "this engine/editor behaves differently".

For end-to-end testing, a workflow is also callable as an API:
`POST /live/workflows/{workflowId}/sync` with header
`vl-wf-control-return-data: true` waits for the run and returns the **last
step's output** — the fastest way to see what a Transform actually produced.
Capped at 29 seconds by API Gateway.

---

## 11. Confirmed vs. not

**Confirmed:** the `template`/`trimAll` flat config shape; `body`/`header`/
`$context` bindings; the preview-pane `$.<stepId>` binding; per-step-type
output wrapping (M3 API array vs REST API `{headers, body}`); M3 runtime
field naming including input echoes and `_MSG_COL`; instance-id
instability; the dynamic suffix-matching pattern; padding rows and the
input-echo filter; the bulk response shape and `results`/`records` paths;
the `$data.{...}[]` payload idiom and the trailing-`[]` requirement; both
`bodyPath` modes; index correlation via `records[$i]`; the operator
renderings; the `{{ }}` layer being separate; the export-shape rules;
`/sync` + `vl-wf-control-return-data` + the 29s ceiling.

**Not confirmed:**

1. **Whether semicolon-chained blocks actually fail** in the editor — §8,
   two contradictory pieces of real evidence.
2. **`skipIfExpression`** on a v2 REST API step — the property *name* comes
   from the implementing ticket; the expression syntax and truthiness
   semantics are documented nowhere. Probe before relying on it; a JSONata
   predicate does the same job with semantics you can test locally.
3. **`forEach`** on a REST API step — documented but beta, its own doc
   contradicts itself on the version number, and there's no captured
   example.
4. **Trigger row shape for spreadsheet input.** Every captured example is
   widget-triggered, with rows as objects keyed by header name. Whether a
   spreadsheet's rows arrive keyed by header name or by column letter is
   not confirmed — one real run settles it.
5. **`trimAll`'s actual behaviour.** `true` is the only value ever seen;
   what it trims was never investigated.
6. **Whether the M3 API step's automatic pagination has any equivalent** on
   the REST path. As far as anything confirmed goes it does not —
   `maxReturnedRecords` and your own paging are all there is.
