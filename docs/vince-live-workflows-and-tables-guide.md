# Vince Live — Workflows & Tables Guide

**Audience:** a Claude session working on Vince Live *somewhere else* — no
access to this repository, its history, or its tooling. Paste this in whole.

**Scope:** how a workflow is sequenced and shaped, how Custom Tables are
structured and queried, and everything confirmed about reaching **M3** from a
workflow (both routes).

Everything below was confirmed by building a real example in Live's own
builder UI, or by calling the real API and reading back what actually came
out. Anything not confirmed is marked **NOT CONFIRMED** in place. There are
no unmarked guesses in this document, and you should not add any.

---

## 0. Ground rules — read before writing a single line of JSON

1. **Never guess at Live's JSON structure, endpoint paths, or enum values.**
   If it isn't here, it is genuinely unconfirmed. Say so and go verify it
   (build a minimal example in the builder and capture it, or make a real
   call and read the real response/error) rather than inventing something
   plausible. Treat every enum in this document as a **closed list**, not a
   pattern to extend by analogy.
2. **A wrong guess usually produces a useful error.** e.g. `POST /workflows`
   with an invalid step `type` returns
   `"WorkflowDefinition is invalid! Errors (N): Property workflowConfig.steps stepId X has invalid type: Y"`.
   That tells you the value is wrong; it does **not** tell you the right one,
   so don't assume your first correction is right either.
3. **HTTP 200 is not proof.** A malformed `tenantId` / `connectionId` /
   `environmentId` passes creation silently and only breaks rendering later
   in the builder (surfacing as a frontend `TypeError`, not a server error).
   After creating anything: read it back, and confirm it *renders* and
   *executes*.
4. **Silence is the characteristic failure mode here.** Wrong primary keys
   silently overwrite rows. A cross-tenant token silently returns empty
   metadata. A Transform referencing the wrong binding silently returns
   blank. Design your checks around that, not around error handling.

---

## 1. Auth, base URL, and the tenant model

- **Confirmed API base: `https://api.vince.live`.** A different-looking
  domain is not a "variant to try" — one previously-supplied reference doc
  confidently described `api.vincelive.dev`, which has zero DNS records and
  was fabricated. Verify reachability before trusting any URL you have not
  personally confirmed.
- **Bearer token:** a short-lived Cognito **ID token**, captured by hand from
  Live's own web UI (DevTools → Network → any authenticated request). Sent as
  `Authorization: Bearer <token>`. It expires after roughly an hour of idle
  session and then every call returns `401 {"message":"Unauthorized"}`. That
  is expected — get a fresh one. Decode the token's own `exp` claim locally
  before blaming anything else; an expired token and a wrong token are
  indistinguishable at the call site.
- Normalise a pasted token before use: strip whitespace, surrounding quotes,
  a stray `Bearer ` prefix, and any internal newlines from a copy that
  wrapped. All three produce the same unexplained 401.

### ⚠️ The token decides the tenant — and a mismatch fails silently

**The token is the authority.** It decides who you are *and* which tenant you
act on. `tenantId`, `connectionId`, `environmentId` in a request body are
**payload, not routing** — references to resources that live inside the
token's tenant.

Consequences, all observed:

- Using a token from tenant A against tenant B's `connectionId` does **not**
  return 401. The call succeeds and the connection is simply invisible, so
  `/metadata/{connectionId}` answers `{"count": 0, "items": []}` — which is
  indistinguishable from "M3 cannot describe this transaction". This cost a
  long debugging session where every field fell back to a
  `TODO: metadata not found` placeholder and the tooling blamed the
  transaction name. Evidence: same connection, same query, `count: 0` on one
  token and real fields on a fresh one minutes later.
- Anything you create lands in the **token's** tenant, not the one named in
  your body.
- Custom Tables corroborates the model independently: `tenantId` is populated
  server-side from the session and is not sent at all. `GET /workflows/{id}`
  needs no ids either.

**Always compare the tenant in the token's own claims against the tenant you
think you are targeting before calling.** The tenant id is a claim of the
shape `TENANT-<32 hex>`; the claim *name* is not confirmed, so match on the
value's shape rather than guessing a key.

**Open question (cheap to settle):** whether `POST /workflows` ignores the
body's `tenantId` (as Custom Tables does) or validates it against the token.
It is required at creation, but a deliberately wrong one has never been sent.

### A second auth mechanism exists — real, but not generically usable

OAuth2 `client_credentials` via an **API Client** (Live UI → Administration →
Access Management → API Clients). Confirmed from a real "self" connection
configuration screen showing:

- `Token URI` of the shape `https://tenant-<hex>.auth.<aws-region>.amazoncognito.com/oauth2/token`
- `Base URI` of the shape `https://<tenant-slug>.vince.live/`

**Both are tenant-specific and not derivable from a formula.** Get them from
that tenant's own connection screen or from Vince support. Do not guess a
subdomain.

A separately-referenced "Vince Platform API" (Users/Roles/API Clients/Tags/
Schedules/Variables, OAuth2-secured) may or may not be the same surface.
**NOT CONFIRMED** — do not conflate it with `api.vince.live`.

---

# PART 1 — WORKFLOWS

## 2. The mental model

A workflow is **a single linear chain of steps**, rooted at exactly one
`TRIGGER` step, nested through `childSteps`. No branching has been seen in
any confirmed example. Each step's output becomes the next step's input;
every step is also addressable by id from any later step.

```
trigger_1 ──► step ──► step ──► step ──► (end)
```

Two things are worth internalising immediately:

1. **`stepId` is load-bearing, not cosmetic.** Later steps reference earlier
   ones by exact stepId (`$context.data.all.rest_api_1`). Renaming a step id
   breaks every reference to it.
2. **`childSteps: []` is a valid, saveable terminal state for any step type.**
   A workflow does not have to end in an Excel step, or in anything
   particular.

## 3. The step catalogue — a CLOSED list

Every confirmed `type` / `name` / `target` combination. Guessing here has
already failed for real: both `"TRANSFORM"` and `"REST_API"` were rejected by
validation before the real values were found.

| `type` | `name` | `target` | stepId convention |
|---|---|---|---|
| `TRIGGER` | `Trigger` | `workflow-trigger` | `trigger_1` |
| `API` | `M3 API` | `workflow-api` | `m3_api_1` |
| `EXCEL` | `Excel` | `workflow-excel` | `excel_1` |
| `GENERIC_FILTER` | `Generic Filter` | `workflow-generic-filter` | `generic_filter_1` |
| `TRANSFORMER_MORPH` | `Transform` | `workflow-transform` | `transform_N` |
| `GENERIC_API` | `Rest API` | `workflow-rest-api` | `rest_api_N` |
| `GENERIC_CODE` | `Code` | `workflow-code` | `code_N` |

Casing is exact — it is `"Rest API"`, not `"REST API"`.

A **Table Updater** step also exists and is the only confirmed way to write
rows into a Custom Table (see §11.7). **Its JSON shape is NOT CONFIRMED** —
no capture of one exists. If you need it, capture a real one from the
builder first.

A step node:

```json
{
  "stepId": "m3_api_1",
  "type": "API",
  "name": "M3 API",
  "retryAttempts": 0,
  "target": "workflow-api",
  "childSteps": [ /* the next stepNode, or [] */ ]
}
```

`retryAttempts` is `0` on every step **except** `trigger_1`, which omits the
field entirely — the only step type that does.

## 4. Sequencing rules — what may follow what

Confirmed constraints, each learned from a real failure or a real capture:

- **`trigger_1` is always the root**, always present, exactly one.
- **`excel_1` is M3-API-specific.** Its column entries can only reference an
  **M3 API transaction instance id** — it has no way to reference any other
  step type's output. Attaching it after a non-M3 chain
  (Transform/REST API/Code with no M3 API step) caused a real save failure in
  the builder. If you need spreadsheet I/O without an M3 API step, use the
  `VXL` label instead (§8).
- **`generic_filter_1` runs before the M3 API step** and filters the raw row
  data going into it. It applies to **the whole M3 API step**, not to one
  transaction — so it *cannot* gate one M3 call while another runs
  unconditionally. That limitation is the single most common reason to leave
  the native path (§12.7).
- **Transform → REST API pairs chain freely**, repeatedly. Seven such pairs
  in a row is a real, working production shape.
- Ordering *within* an M3 API step is separate from step ordering: its
  transactions carry their own `executionOrder` (§12.2).

### The two canonical sequences

**A. Native M3 path** — what the builder produces for a classic
spreadsheet-driven M3 job:

```
trigger_1 (CLIENT)
  └─ generic_filter_1        (optional, row filter, whole-step)
       └─ m3_api_1           (1..n M3 transactions, executionOrder 1..n)
            └─ excel_1       (optional, column bindings back to m3_api_1)
```

**B. Generic pipeline path** — what Vince's own production customer
workflows use when the native path can't express the logic:

```
trigger_1
  └─ transform_1        builds { "payload": { ...M3 bulk body... } }
       └─ rest_api_1    POSTs it to ION API's bulk execute endpoint
            └─ transform_2
                 └─ rest_api_2
                      └─ ...           (repeat as needed)
```

Path B has no M3 API step and no Excel step at all. One real example performs
full M3 **writes** this way across seven Transform/REST API pairs. See §13.

## 5. How data moves between steps (JSONata bindings)

Inside a Transform step's template:

| Binding | Means |
|---|---|
| `body` / `header` | plain paths into the **previous step's** output, no `$` prefix (e.g. `body.hello`) |
| `$context.data.trigger.body` | the payload the workflow was triggered with |
| `$context.data.all.<stepId>` | a **specific** earlier step's output, by exact step id |

**The binding convention differs per step type — do not carry it across:**

- An **M3 API** step's result is **the array directly**:
  `$context.data.all.m3_api_1` — no `.body`, no `.data`.
- A **REST API** step's result is wrapped: `{ headers, body }` — you need
  `.body` to reach the payload: `$context.data.all.rest_api_1.body`.

Quote/bracket field names containing spaces:
`$context.data.trigger.body[0]."Order Number"`.

### ⚠️ Two Transform traps that fail silently

1. **The builder's preview pane binds pasted sample JSON to bare
   `$.<stepId>`, not `$context`.** Only a real saved/executed run resolves
   `$context.data.all.<stepId>`. Write and test against `$.<stepId>` in the
   preview, then **swap to `$context...` before saving for real execution**.
   Forgetting the swap produces a blank result with no error — `$context` is
   simply undefined in that pane.
2. **Save-time validation is loose.** A malformed template can have its
   content silently disappear or reset rather than failing to save with a
   clear error. Re-read the workflow after saving.

### A note on multi-statement JSONata

An early test recorded that semicolon-chained variable-binding blocks —
standard, valid JSONata — fail to parse in this editor with
`Expected "}", got ";"`:

```jsonata
( $rows := ...; $findKey := function(...){...}; $rows[...] )
```

**That note is now in doubt.** Four real production customer workflows use
exactly this construct and work:

```jsonata
(
  $data := $context.data.trigger.body[0];
  { "payload": { "program": "OIS100MI", "transactions": $data.{ /* ... */ }[] } }
)
```

So either the original failure had another cause (a stray brace, or that
specific expression) or the editor changed. **Re-test before working around
it** — the workaround costs real legibility.

Confirmed working in this engine regardless: `$keys()`, `$substring()`,
`$filter()`, `$lookup()`, `$map()`, and inline `function($k){...}` definitions
used directly inside an expression.

## 6. The `POST /workflows` body

```
POST https://api.vince.live/workflows
GET  https://api.vince.live/workflows/{workflowId}
```

**GET and POST do not share a body shape.** GET returns server-computed
fields that POST's validation rejects outright — `stats`, `workflowArn`,
`definitionAsl`, `created`/`modified`/`createdBy`/`modifiedBy`, `overview`
(e.g. `stats.lastExecutionId: null` fails validation: the creation schema
wants a string or the key absent, never null). GET also **omits
`description` and `labels` entirely**, though creation requires them. To
clone a workflow, strip a GET response down to exactly the fields below —
never forward the whole body.

**No workflow PUT/PATCH endpoint is confirmed.** Only `POST /workflows` and
`GET /workflows/{id}`. A "hit Save" PUT was once observed in a HAR but its
URL and body shape were never recorded.

### Top-level envelope

```json
{
  "entity": "Workflow",
  "description": "",
  "hasError": false,
  "tenantId": "TENANT-<32 hex>",
  "workflowId": "WORKFLOW-<32 hex>",
  "workflowName": "My Workflow",
  "workflowAlias": "My Workflow",
  "active": true,
  "workflowType": "STANDARD",
  "labels": [],
  "tags": {
    "connectionId": ["CONNECTION-<32 hex>"],
    "apiEnvironment": "ENVIRONMENT-<32 hex>",
    "environments": ["ENVIRONMENT-<32 hex>"],
    "primaryEnvironment": "ENVIRONMENT-<32 hex>"
  },
  "definition": { "workflowConfig": {}, "stepConfig": {} }
}
```

- `hasError` is **client-computed**, not server-validated. Live's own UI
  sends `true` at creation and `false` once its own validation decides the
  workflow looks complete. Set it yourself.
- `workflowId` must be syntactically valid (`WORKFLOW-` + 32 hex) but **Live
  assigns its own id regardless** — always read the real id back from the
  response.
- `workflowType` must be `"STANDARD"`. `"EXPRESS"` produces a broken,
  half-provisioned workflow: it blocks re-creating the same id, is
  unreachable via GET, and is invisible in the workflow list.
- `tags.*` ids must be real and correctly formatted. A malformed one passes
  creation silently and breaks M3 API step rendering later.

### `definition.workflowConfig`

```json
{
  "name": "My Workflow",
  "constants": { "CONSTANT_<fieldName>": "<literal value>" },
  "inputs": {
    "<CLIENT field name or Filter_<Column>>": {
      "dataType": "STRING",
      "validation": { "required": true }
    }
  },
  "connectionContext": { "M3": "CONNECTION-<32 hex>" },
  "hasSpreadSheetInput": true,
  "hasExcelDesignFile": false,
  "hasTemplateFile": false,
  "workflowTags": {},
  "apiFieldReferences": {},
  "contexts": {},
  "clientConfig": {
    "title": "My Workflow",
    "description": "Description",
    "buttonText": "Execute",
    "formGroups": [{ "groupId": 1, "groupName": "fields" }],
    "fields": [ /* trigger fields, see §7.1 */ ]
  },
  "steps": [ /* exactly one root TRIGGER stepNode */ ]
}
```

- `constants` are keyed `CONSTANT_<fieldName>`, and an input field stores
  **that key** as its `value` — not the literal.
- `hasSpreadSheetInput` is true if any transaction has an EXCEL-sourced input
  **or** any output cell is mapped to a column.
- `clientConfig` **mirrors** the `trigger_1` stepConfig entry — the same
  title/description/buttonText/formGroups/fields appear in both places.
- `steps` is always a single-element array.

**Seen in a real capture but never emitted or tested at creation time**
(present in a GET is not the same as accepted in a POST):
`isControlSpreadSheetEnabled`, `isEnforceSheetVerificationEnabled`,
`saveBackup`, `notifyOnError`, and a `type: "STANDARD"` *inside*
`workflowConfig` alongside the confirmed top-level `workflowType`. The first
three are the builder's "Control Spreadsheet", "Enforce Sheet Verification"
and "Save backup of output" toggles.

Also observed: `clientConfig.title` stayed `"Untitled"` while
`workflowConfig.name` was set to a real name. **The builder does not keep
them in sync**, so a workflow can display an unhelpful trigger title with
nothing actually wrong.

## 7. Step configs (`definition.stepConfig`)

`stepConfig` is keyed by `stepId`. **The config shape is not uniform across
step types** — see the warning under §7.6.

### 7.1 Trigger — `trigger_1`

Two confirmed `triggerType` variants.

**`CLIENT`** — a form a user fills in. Its fields:

```json
{
  "fieldId": "ORNO",
  "fieldName": "ORNO",
  "label": "ORNO",
  "placeHolder": "ORNO",
  "dataType": "STRING",
  "length": "15",
  "mandatory": true,
  "type": "text",
  "formGroupId": 1,
  "elementPosition": 1,
  "apiId": "<txnInstanceId>"
}
```

`formGroupId` and `elementPosition` are **not cosmetic**. A field with no
`formGroupId` has no declared group to render under — the most likely
explanation for a real test workflow that showed zero trigger fields despite
the fields existing in the data. Declare the groups in `formGroups`
(`{"groupId": 1, "groupName": "fields"}`, plus
`{"groupId": 2, "groupName": "filters"}` when Generic Filter fields exist),
and restart `elementPosition` at 1 within each group. *(Whether Live wants
positions unique per-group or globally is **NOT CONFIRMED**; per-group is the
more natural reading.)*

**`SCHEDULE`** — confirmed from a hand-built workflow:

```json
"trigger_1": {
  "triggerType": "SCHEDULE",
  "expression": "cron(* 1 ? * * *)",
  "scheduleId": "SCHEDULE-<32 hex>",
  "startDate": "2026-09-07", "endDate": "2026-09-10",
  "startTime": "11:22", "endTime": "11:22",
  "timezone": "Europe/Oslo", "autoTimezone": true,
  "availableFields": [],
  "title": "Untitled", "description": "Description", "buttonText": "Execute",
  "fields": [], "formGroups": [], "tags": {}
}
```

`scheduleId` is **server-assigned** — creating a SCHEDULE trigger evidently
provisions a schedule resource, so don't invent one. The CLIENT-trigger
fields stay present and inert. A GET also returns a server-computed
`overview.triggerType` echoing this value.

### 7.2 Generic Filter — `generic_filter_1`

A hard AND-combined list applied to raw row data **before** the M3 API step:

```json
{
  "all": [
    {
      "clientKey": "Filter_BD",
      "operator": "equal",
      "dataType": "STRING",
      "logicalType": "AND",
      "from": "Row",
      "defaultValue": "OK",
      "source": "CLIENT",
      "fact": "BD",
      "value": "{{ header.Filter_BD }}"
    }
  ]
}
```

- `operator` ∈ `equal | notEqual`. No confirmed `OR` case (`logicalType` is
  `AND` in every example).
- `value` is **literally** that template string in the stored JSON — it is
  substituted at execution, not at save.
- It creates an **editable** trigger field with `defaultValue` as the
  default. **No confirmed fixed/hidden variant exists** — anyone running the
  workflow can see and change the filter value. A case-insensitive comparison
  flag is also unconfirmed.

### 7.3 M3 API — `m3_api_1`

See §12. This is the largest single block of confirmed structure.

### 7.4 Excel — `excel_1`

```json
{
  "sourceSheet": {
    "fields": {
      "sheetName": "Sheet1",
      "startColumn": "A", "endColumn": "H",
      "primaryHeaderRow": 2, "secondaryHeaderRow": 3, "dataRow": 4,
      "primaryHeaderStyle": {}, "secondaryHeaderStyle": {}, "dataRowStyle": {},
      "columns": { "<column letter>": { /* excelColumn */ } }
    },
    "cells": { "contents": {}, "styleInfo": {} },
    "sortOrder": []
  }
}
```

In every confirmed example `primaryHeaderRow = dataRow - 2` and
`secondaryHeaderRow = dataRow - 1`.

An **excelColumn**:

```json
{
  "fieldId": "<txnInstanceId>.output.ITDS",
  "type": "API",
  "stepId": "m3_api_1",
  "field": {
    "fieldId": "<txnInstanceId>.output.ITDS",
    "fieldName": "ITDS",
    "type": "API",
    "datatype": "STRING"
  },
  "primaryHeader":   { "type": "API", "value": "Item description", "fieldId": "<txnInstanceId>.output.ITDS" },
  "secondaryHeader": { "type": "API", "value": "ITDS",             "fieldId": "<txnInstanceId>.output.ITDS" }
}
```

`fieldId` is `"<m3ApiTxnInstanceId>.output.<field>"` for an export column, or
`"...input.<field>"` for an input-side column.

**`<column>-1` dual-role columns:** if a second field genuinely reads the same
Excel column (two transactions both reading column F as separate input
fields), it gets a second entry keyed `"F-1"` — confirmed field-for-field
against a real GET. The plain slot goes to whichever transaction is declared
first, and the `-1` entry's `secondaryHeader` is a **copy** of the plain
slot's own `secondaryHeader` (pointing at the *other* transaction), not
derived fresh.

### 7.5 Transform — `transform_N` (`TRANSFORMER_MORPH`)

```json
{ "template": "<JSONata expression as a plain string>", "trimAll": true }
```

Flat keys. See §5 for bindings and traps.

### 7.6 REST API — `rest_api_N` (`GENERIC_API`)

> ⚠️ **The asymmetry that is easiest to get wrong.** A REST API step's
> stepConfig is a **`content` STRING holding the config as JSON** — *not* the
> config's keys sitting flat in stepConfig. A `TRANSFORMER_MORPH` step's
> config in the very same workflow **is** flat. Do not generalise either
> shape onto the other step type.

```json
"rest_api_1": {
  "content": "{\n  \"version\": 2,\n  \"endpoint\": \"https://...\",\n  \"method\": \"post\",\n  \"headers\": {\n    \"content-type\": \"application/json\"\n  },\n  \"bodyPath\": \"$.payload\"\n}"
}
```

Confirmed by a GET of a hand-built workflow, and corroborated by that
workflow's compiled `definitionAsl`, which parses the string back out and
hands the proxy lambda (`vl-sls-connection-generic-rest-api-prod-proxy`)
`'stepConfig': {"content": {"version": 2, ...}}`. **A flat stepConfig leaves
the proxy with no content at all.** The builder writes the string with
2-space indentation.

The JSON *inside* `content`:

```json
{
  "version": 2,
  "connectionId": "CONNECTION-<32 hex>",
  "endpoint": "https://api.example.com/path",
  "method": "post",
  "headers": { "content-type": "application/json" },
  "bodyPath": "$"
}
```

- **`connectionId` is optional** — the builder's "Use connection" toggle, off
  when absent (a plain external URL with no stored Vince connection). When
  present, **Live looks up the stored connection and attaches its auth to the
  request automatically** — you never build a token yourself. Confirmed in
  product documentation and in four real customer workflows.
- **`bodyPath`** is a JSONata expression selecting the request body from the
  previous step's output: `"$"` = the whole previous output, `"$.body"` = just
  its body, `"$.payload"` = a named sub-object (what the real M3 examples
  use).
- **`body` (literal-body mode) is CONFIRMED** — an object, sibling to the
  others, carrying `{{ body.x }}` / `{{ header.x }}` placeholders substituted
  at execution from the request that triggered the workflow (the same `{{ }}`
  mechanism as the Generic Filter step). **Set either `bodyPath` or `body`,
  not both.**
- **`"version": 2` is confirmed and in current production use.** It is written
  first in every real example, and unlocks two features:
  - **`forEach`** — a JSONpath to an array (e.g. `"body.records"`). The step
    then makes one sequential call per record, and its output changes from a
    single `{headers, body}` object to an **array** of them. Inside a
    `forEach`, an `endpoint` placeholder reads the *current record* wrapped in
    `body` (`{{ body.id }}`), while `bodyPath` addresses the current record's
    root. Documented as **beta**, self-describedly "a little inconsistent" in
    variable naming, and its own doc's example says `"version": 3` while the
    instructions say `2`. **No captured example — test before building on it.**
  - **`skipIfExpression`** — skip the request when a condition isn't met. The
    property **name** is confirmed (from the ticket that implemented it). Its **expression syntax and
    truthiness semantics are documented nowhere**. Probe before relying on it.
- `"version": 3` is **accepted** — a hand-built step saved with it and
  compiled into a working state machine. What it changes versus 2 is
  undocumented. Production workflows all use 2.
- **Binary responses** are steered by a `content-disposition` *request*
  header: absent or `inline` returns `{type: "Buffer", data: [...]}` plus a
  `fileKey`; `attachment` returns `body: null` plus a `fileKey`; `base64`
  returns a base64 string.

### 7.7 Code — `code_N` (`GENERIC_CODE`)

```json
{ "content": "module.exports = async function (prevStep, allSteps, trigger) { /* ... */ };" }
```

Confirmed sandboxed `require()`-able modules: `authorizedAxios` and
`getContext` — a third way to reach a stored connection, alongside the REST
API step's own `connectionId`:

```js
module.exports = async function (prevStep, allSteps, trigger) {
  const authorizedAxios = require('authorizedAxios');
  const getContext = require('getContext');
  const connection = await getContext('globals.connection', CONNECTION_ID);
  const axios = await authorizedAxios(connection);
  const rows = trigger.body.data;
  // ...
};
```

Costs of a Code step: invisible in the builder UI, spends wall-clock on
sequential HTTP (mind the 29-second ceiling in §9), and no other real Vince
workflow works this way. Treat it as the escape hatch.

## 8. The `VXL` label — spreadsheet I/O with no Excel step

`excel_1` cannot be used outside an M3 API chain. Instead, **add the label
`VXL` to the workflow**. Confirmed wording: *"Create a new workflow and add
the label `VXL` to it - this enables the workflow to be visible in VXL
Live"*, alongside *"All workflows with M3 API and Excel steps are already
available in VXL Live"*.

In the JSON that is the top-level `labels` array (`"labels": ["VXL"]`) — the
label→`labels[]` mapping is a *reading* of that wording, so **confirm it
against a GET of a real VXL workflow** before relying on it.

The **final step's output must be export-shaped**, per confirmed hard rules:

- an array of objects,
- every object carrying the same property keys,
- `""` rather than `null` for a blank.

Ragged rows are the failure mode.

## 9. Running a workflow

A workflow is also a synchronous API:

```
POST https://api.vince.live/live/workflows/{workflowId}/sync
Header: vl-wf-control-return-data: true
Body:   { "orderNumber": "0010000386" }
```

- **Without the `vl-wf-control-return-data: true` header you get execution
  metadata, not the data.**
- It returns the **last step's** output.
- **AWS API Gateway caps this at 29 seconds** — a real limit on how much
  sequential work a Code step can afford.
- Request body and header values reach a REST API step as `{{ body.x }}` /
  `{{ header.x }}`.

**Human-readable URLs** come from Custom APIs:

```
POST /v1/custom-apis/control
{
  "endpoint": "...", "tenantId": "...", "basePath": "...",
  "config": {
    "target": { "resource": "WORKFLOW", "identifier": "WORKFLOW-...",
                "parameters": { "vl-wf-control-return-data": true } },
    "source": { "contentTypes": [ ... ], "httpMethod": "..." }
  }
}
→ GET /v1/custom-apis/live/{basePath}/{endpoint}
```

Not exposed in the UI. Permissions must be PATCHed onto a role as
`vrn:<TENANT>:fnd:custom-api:<basePath>_<endpoint>` — and a PATCH
**replaces** the role's permission array, so send the whole thing with your
addition appended.

**Large results:** execution results appear inline in the execution log for
small payloads, or as a separate `dataFileKey` reference (e.g.
`WFEXEC-<id>/dataFile.json`) once the payload crosses some size threshold —
observed at 24,154 records. `GET /live/worfklows/{workflowId}/download-url`
is documented as returning a download URL and was used successfully to
retrieve one. *(The misspelling `worfklows` is reproduced from the source as
recorded — verify the real path before using it.)*

## 10. Attaching a design file (the Excel sheet)

Confirmed from a real upload capture. Two steps, and the file **never passes
through `api.vince.live` itself**.

**1. Ask for a presigned upload URL:**

```
GET https://api.vince.live/v1/files/tenant/upload-url/{WORKFLOW-<32 hex>}/designFile/{filename}.xlsx
Authorization: Bearer <token>
→ 200
{
  "filename":  "WORKFLOW-<id>/designFile/<filename>.xlsx",
  "url":       "https://files.vince.live/TENANT-<id>/WORKFLOW-<id>/designFile/<filename>.xlsx?...signed...",
  "expiresAt": "2026-09-10T08:28:18.000Z",
  "method":    "PUT",
  "headers":   { "Content-Type": "*/*" },
  "isWildcard": false
}
```

The tenant is taken from the token — the returned `url` carries a
`TENANT-<id>` prefix the caller never sends. `method` and `headers` are
instructions *for step 2*, not a description of step 1. `expiresAt` is about
five minutes out.

**2. `PUT` the raw file bytes to the returned `url`** with
`Content-Type: */*` and **no `Authorization` header** — the signature is in
the query string.

**What lands in the workflow JSON.** A before/after GET differs by exactly
two fields in `definition.workflowConfig`:

```json
"hasExcelDesignFile": true,
"excelDesignFileKey": "WORKFLOW-<id>/designFile/<filename>.xlsx"
```

`excelDesignFileKey` is **byte-identical to the upload-url response's own
`filename` field** — read it back from there rather than reconstructing it.
`hasTemplateFile` stayed `false` and no `templateFileKey` appeared; that is a
separate, uncaptured feature.

**Sequencing consequence:** the key embeds the workflow id, and Live assigns
its own id at creation. **A design file can only be uploaded after the
workflow exists and its real id has been read back.** There is no way to
include one in the initial POST.

**Open:** what actually *writes* those two fields — the server on PUT
completion, or the builder saving the workflow afterwards — is not settled.
Upload, then **GET the workflow back and check**. If the fields are absent a
workflow update call is needed, and no workflow PUT/PATCH endpoint is
confirmed.

**Caution:** the same before/after diff also showed the Excel step's column
bindings changing and `clientConfig.formGroups` going from 3 entries to 0.
That is consistent with documented auto-mapping behaviour, but equally
consistent with the builder saving unrelated pending edits in the same
action. **Do not conclude that uploading a design file re-maps columns** on
this evidence — but do not assume the upload is purely additive either.

---

# PART 2 — CUSTOM TABLES

Confirmed real end-to-end: a real table created, 24,154 rows written via a
Table Updater workflow step, and read back. Independently corroborated by
decoding a real `__id` value and finding it matched its claimed encoding
exactly.

## 11. Table structure and API

### 11.1 Base

```
https://api.vince.live/v1/custom-tables/...
```

An older `/live/custom-tables/...` path appears in some docs as a
legacy/alternate form; **`/v1/` is the confirmed-working prefix**.
`tenantId` is populated **server-side** from the authenticated session — do
not send it in request bodies.

Tables are a **standalone resource** with their own `meta`, independent of
any workflow. Reading does not require a workflow at all.

### 11.2 Creating a table — the structure

```
POST https://api.vince.live/v1/custom-tables/meta
```

```json
{
  "tableName": "m3-api-catalog",
  "updateType": "UPSERT",
  "enforceSchema": false,
  "primaryKeys": ["Program", "Transaction"],
  "schema": {
    "type": "object",
    "properties": {
      "Program":     { "type": "string", "title": "Program" },
      "Transaction": { "type": "string", "title": "Transaction" },
      "Description": { "type": "string", "title": "Description" },
      "Type":        { "type": "string", "title": "Type" },
      "Status":      { "type": "string", "title": "Status" }
    }
  },
  "displayOrder": ["Program", "Transaction", "Description", "Type", "Status"]
}
```

Field by field:

| Field | Confirmed behaviour |
|---|---|
| `tableName` | Gets **slugified** — hyphens, not underscores |
| `primaryKeys` | **Immutable after creation.** See the warning below |
| `updateType` | `UPSERT` (update-or-create; the default choice) \| `REPLACE` (full snapshot replace) \| `APPEND` (non-unique keys allowed, logging-style) |
| `schema` | Standard JSON Schema. The current, confirmed way to define columns |
| `enforceSchema` | `false` = no write-time validation, index whatever is written. `true` = reject rows not matching `schema`. **Start with `false`** |
| `displayOrder` | Column order in the UI |

**`columnConfig` (older docs, an `aliasName`/`dataType` shape) is
deprecated** — a request using it was rejected in testing. Use `schema`.

### ⚠️ Choosing primary keys — the hard-won lesson

`primaryKeys` cannot be changed after creation, and **every write with a
duplicate key silently overwrites the previous row. No error.** A bulk load
can therefore "succeed" and land a fraction of the rows you sent.

Before creating: identify the smallest field set that is genuinely unique per
source record. After loading: **check the resulting row count against the
expected input count** (§11.5). That check is exactly how a real primary-key
bug was caught — nothing else signalled it.

### ⚠️ A known flaky, misleading error — NOT FULLY UNDERSTOOD

The exact same valid body was rejected twice with a 400 whose
`validationError` listed required properties (`definition`, `tenantId`,
`workflowAlias`, `workflowId`) that belong to a **workflow** object, not a
table — apparently a misrouted validator. The identical body then succeeded
on retry with no changes. **If you hit this, retry before assuming your body
is wrong.** Root cause not confirmed.

### 11.3 Creation response

```json
{
  "primaryKeys": ["Program", "Transaction"],
  "schema": { "...": "as sent" },
  "updateType": "UPSERT",
  "rowCount": 0,
  "enforceSchema": false,
  "timestamp": "1787745513650",
  "shardMax": 0,
  "displayOrder": ["Program", "Transaction", "Description", "Type", "Status"],
  "entity": "META",
  "tableAlias": "m3-api-catalog",
  "created": "2026-08-26T11:58:33.774Z",
  "modifiedBy": "USER-...",
  "modified": "2026-08-26T11:58:33.774Z",
  "tenantId": "TENANT-...",
  "tableName": "m3-api-catalog"
}
```

`tableName` is echoed back and also duplicated as `tableAlias`. `rowCount`
and `shardMax` start at 0 and can be polled to track ingestion.
`timestamp` is an internal epoch-ms version marker — not something you set.

### 11.4 Updating and deleting

```
PATCH https://api.vince.live/v1/custom-tables/meta/{tableName}
```

Partial update of the same shape — use it for `enforceSchema`, adding
`schema` fields, etc. **Not** for `primaryKeys`.

**Deletion has up to a 6-hour delay** before the table is actually removed,
and a same-named table **cannot be recreated until it completes** — even
though the data becomes inaccessible immediately.

### 11.5 Reading

**Point lookup by composite primary key — CONFIRMED:**

```
GET https://api.vince.live/v1/custom-tables/data/{tableName}?records.{key1}={v1}&records.{key2}={v2}
```

Real working example and its real response:

```
GET /v1/custom-tables/data/m3-api-catalog?records.Program=CRS610MI&records.Transaction=GetOrderInfo
```
```json
{
  "count": 1,
  "items": [
    {
      "__id": "bTMtYXBpLWNhdGFsb2cjMCNEQVRBI0NSUzYxME1JI0dldE9yZGVySW5mbw",
      "__created": "2026-08-26T12:00:45.908Z",
      "__modified": "2026-08-26T12:00:45.908Z",
      "Status": 44,
      "Type": "S",
      "Program": "CRS610MI",
      "Description": "Retrieve customer financial data",
      "Transaction": "GetOrderInfo"
    }
  ]
}
```

- Metadata fields prefixed `__` (`__id`, `__created`, `__modified`,
  `__modifiedBy`) are **always present**.
- **`__id` is base64 of `{tableName}#0#DATA#{pkValue1}#{pkValue2}...`** —
  confirmed by decoding the value above. Useful for sanity-checking that your
  composite key is actually being applied the way you think.
- Multiple records in one call:
  `records[0].Program=X&records[0].Transaction=Y&records[1].Program=A&...`
- URL length capped at 8192 bytes (per docs, not independently re-verified).

**By `__id`** — `GET .../data/{tableName}/{id}`. Documented and internally
consistent with the `__id` encoding above; not independently tested.

**By prefix** — `GET .../data/{tableName}?beginsWith={prefix}`. UPSERT tables
only, respects primary-key order, concatenate multi-key prefixes with `#`.
Documented, not independently tested.

**Bulk / paginated** — `GET /v1/custom-tables/search/{tableName}`. Supports a
scroll cursor (`?size=10&scrollMinutes=5` returns a `scrollId` plus the first
page; repeat with `?scrollId=...`, and other params are ignored once it is
present). It also accepts a Lucene-style query — see §11.6, which is where
the real, hard-won behaviour lives.

**Row count** — `GET /v1/custom-tables/aggregated/{tableName}`:

```json
{ "count": 22535, "aggregations": {} }
```

The fast way to confirm a bulk write landed the expected number of rows,
without paging. **Confirmed to update in near-real-time while a bulk write is
still in progress** — so a mid-run check will show a climbing number. The
Table Management UI's row count matches this endpoint.

### 11.6 ⚠️ The search endpoint's pagination is broken — work around it

The search endpoint is Elasticsearch-backed. Two separate pagination bugs
were found empirically:

1. **`from` has a minimum of 1 and maps straight through to ES's native
   `from`** (a 0-indexed skip count). The true first result of every query is
   therefore **permanently unreachable** via `from=1`. Confirmed by diffing a
   `from=1` response against an otherwise-identical request with `from`
   omitted entirely — omitting it recovers exactly the record `from=1` drops.
2. **Multi-page fetches silently duplicate/lose records** for *some* large
   queries but not others (`Program:C*` consistently lost ~239 records across
   repeated attempts; `Program:M*`, also large and multi-page, never did).
   This looks like the endpoint's default sort order not being stable across
   separate HTTP requests, which breaks deep `from`/`size` pagination in a way
   no client-side offset math can fix. An explicit `sort=` param appeared to
   be a **no-op**.

**Single-page fetches (no `from` at all, everything in one response) have
never shown either problem.** So: avoid multi-page fetches entirely. The
working strategy for pulling a whole large table is to **query by prefix and
recursively split** any prefix whose reported `total` exceeds the page cap:

- Page size cap: **2000** per request (confirmed hard cap).
- Query syntax: `?q=Program:C*&size=2000` — the single-letter prefix form was
  confirmed directly. The **multi-character** form (`q=Program:CMS*`) is
  *assumed* to behave the same way and has **not** been independently
  spot-checked. Verify it manually if results look wrong.
- Split adaptively, not from a hardcoded list: which prefixes are oversized
  drifts as the data grows. (Confirmed at one point in time: plain `C` needed
  splitting down to 4 characters for `CMS*` and `CRS*` specifically.)

### 11.7 Writing rows — Table Updater only

**There is no confirmed row-write endpoint.** The confirmed path for getting
rows into a table is a workflow's **Table Updater** step. Reading is fully
decoupled — a reader needs only the `tableName` and read auth, never the
workflow.

The typical production shape is a scheduled write side and an independent
read side:

```
trigger_1 (SCHEDULE) → m3_api_1 → transform_1 → table_updater      [write]
                                     (independent)
GET /v1/custom-tables/data|search/{tableName}                      [read]
```

**The Table Updater step's own JSON shape is NOT CONFIRMED** — capture a real
one from the builder before emitting one.

### 11.8 Reading a table's *definition* — NOT CONFIRMED

No endpoint is confirmed to return a table's `primaryKeys` / `schema` /
`updateType`. `POST` and `PATCH` write it; the reads above return rows and
counts only. **Do not assume `GET /v1/custom-tables/meta/{tableName}`
works** — it is the *plausible* candidate (PATCH on that path is confirmed),
not a verified one. If you probe it, **validate that the body actually names
the table and carries a meta field** before believing a 200. A confirmed
absence is worth as much as a confirmed endpoint — record whichever you find.

### 11.9 Known UI quirk

The Table Management UI can show **`Columns: 0`** for a table created with a
full `schema` and holding real data — it appears to still read column
metadata from the deprecated `columnConfig` rather than `schema`. Purely
cosmetic; data and querying are unaffected. Don't take it as a sign something
is broken.

---

# PART 3 — EVERYTHING CONFIRMED ABOUT M3

## 12. Route A — the native M3 API step

### 12.1 When to use it

Prefer it whenever it fits. It gives you field-metadata validation, automatic
pagination, `excel_1` binding, and much stricter save-time validation. You
leave it only when it genuinely cannot express the workflow — above all for
**per-call filtering** (§12.7).

### 12.2 The step config

`stepConfig["m3_api_1"]` is keyed by **transaction instance id** — format
`x` + 10 mixed-case alphanumeric characters, e.g. `xN9MwyGBpt`. **Live
generates this, not you.**

```json
{
  "xN9MwyGBpt": {
    "id": "xN9MwyGBpt",
    "stepId": "m3_api_1",
    "programName": "MMS200MI",
    "transactionName": "Get",
    "name": "Get",
    "source": "M3",
    "connectionId": "CONNECTION-<32 hex>",
    "executionOrder": 1,
    "extendedGrouping": [],
    "input":  { "<fieldName>": { /* InputField  */ } },
    "output": { "<fieldName>": { /* OutputField */ } },
    "type": "ADD"
  }
}
```

- `executionOrder` starts at **1**, no gaps, and matches execution order.
- **`type` is `"ADD"` in every reliable multi-transaction example, regardless
  of what the M3 transaction actually does** (list/get/update/add). Its
  absence is the strongest known cause of an M3 API step rendering completely
  inert in the builder. **Do not infer LIST/GET/ADD from the transaction
  name.**
- List-style transactions additionally carry `"maxRows": <int>`,
  `"defaultInputConfig": {}`, `"hasSpreadSheetInput": false`.
- A dependent transaction adds `"basedOnTransaction": "<producer's instance
  id>"` and `"runOnFailure": <bool>`. (A real get-or-create idiom exists in
  the wild: an `AddPriceList` call based on a `GetPriceList` call *failing*.)
- **A field referenced elsewhere via an API-chain `ref` must be declared in
  its producer's own `output`**, even with no real column mapping —
  synthesize a minimal declaration on an unused column letter if needed, or
  you get a runtime SQL error.

### 12.3 InputField

```json
{
  "name": "ITNO", "descr": "Item number",
  "type": "STRING", "length": "15", "mandatory": false,
  "fieldType": "I", "isMapped": true, "hasGrouping": false,
  "source": "EXCEL", "sourceType": "COLUMN", "value": "F"
}
```

- `type` ∈ `STRING | NUMERIC | UNKNOWN`. `UNKNOWN` is a safe placeholder —
  confirmed not to break rendering.
- `source` ∈ `CONSTANT | EXCEL | CLIENT | API`, and **`value`'s meaning
  depends on `source`**:

| `source` | What `value` holds |
|---|---|
| `CONSTANT` | the **key** into `workflowConfig.constants` (e.g. `"CONSTANT_ALWT"`) — **not** the literal |
| `EXCEL` | the spreadsheet column letter; also set `"sourceType": "COLUMN"` |
| `CLIENT` | the **real M3 field name**, never a placeholder — confirmed via a real execution error when a placeholder was sent as-is |
| `API` | `"<producerTxnId>.output.<fieldName>"`, plus `"ref": {"apiId": "<producerTxnId>", "name": "<fieldName>", "type": "OUTPUT"}` |

- **Repeated CLIENT fields** (the same field name on a 2nd or later
  transaction) need
  `"ref": {"apiId": "<id of the FIRST transaction to declare it>", "name": "<fieldName>", "type": "INPUT"}`.
  Without it, Live creates separate fields (`CONO`, `CONO_1`, `CONO_2`...)
  instead of one shared trigger value.

> Note the asymmetry worth memorising: for an **API** chain the `ref.apiId`
> points at the **producer**; for a repeated **CLIENT** field it points at the
> **first** transaction that declared the field.

### 12.4 OutputField

```json
{
  "name": "ITDS", "descr": "Item description",
  "type": "STRING", "length": "30", "mandatory": false,
  "fieldType": "O", "isMapped": true, "hasGrouping": false,
  "source": "EXCEL", "sourceType": "COLUMN", "value": "H"
}
```

### 12.5 The M3 field metadata endpoint

This is what powers "pick a transaction, watch its fields populate" in the
builder, and the **only** confirmed way to get real `descr` / `type` /
`length` / `mandatory` per field.

```
GET https://api.vince.live/metadata/{connectionId}?details=FIELDS%23{program}%23{transaction}
```

The `details` value is `#`-separated and **must be URL-encoded** (`%23`).

| `details` | Result |
|---|---|
| `FIELDS#<prog>#<txn>` | **Use this.** One item per direction that exists — `count: 2` for `MRS001MI/LstAllTrans` (I + O), `count: 1` for `BUS100MI/DelBudgetLines` (I only) |
| `FIELDS#<prog>#<txn>#<I\|O>` | One direction. Works, but doubles the calls for no benefit |
| `FIELDS#<prog>` | Every transaction in the program at once (`count: 24` for `BUS100MI`) — **but see trap 2** |
| `TRANSACTIONS#<prog>` | `count: 0` — not supported, or differently named |
| `PROGRAMS` | `count: 0` — same |
| *(omitted)* | HTTP 400 `Missing required request parameters: [details]` |

Response:

```json
{"count": 2, "items": [
  {"name": "I", "descr": "Input",  "data": [
    {"name": "CONO", "type": "N", "length": "3", "mandatory": "0", "descr": "Company"}]},
  {"name": "O", "descr": "Output", "data": [ /* ... */ ]}]}
```

**M3 type codes → Live's vocabulary:**

| M3 code | Live type | Confidence |
|---|---|---|
| `A` (alphanumeric) | `STRING` | confirmed |
| `N` (numeric) | `NUMERIC` | confirmed |
| `D` (date) | `NUMERIC` | **inferred** — M3 typically stores dates as numeric `YYYYMMDD`, but this has never been confirmed against a real Live field of type `D` |

`mandatory` is the **string** `"1"` / `"0"`, not a boolean.

**Two traps, both of which have cost real time:**

1. **An empty direction is normal, not a failure.**
   `BUS100MI/DelBudgetLines` returns 14 input fields and `count: 0` for
   output — because a delete returns nothing. Only treat a transaction as
   undescribed when it has **no fields in any direction**. Warning
   per-direction produces a scary false alarm on every Add/Upd/Del
   transaction.
2. **The whole-program shape cannot be attributed to transactions.**
   `FIELDS#<prog>` returns items carrying only `['data', 'descr', 'name']`,
   where `name` is just `"I"`/`"O"`. **Nothing identifies which transaction
   each item belongs to**, so despite being far fewer calls it is unusable
   for per-(program, transaction, field) keying. Position *might* correlate
   with transaction order — that is a guess, and untested.

**And remember §1:** when this endpoint describes *no* fields at all, suspect
a **token from a different tenant than the connection** first, and the
transaction name second.

### 12.6 M3 API step runtime behaviour

Everything here is confirmed by real execution.

**Field naming.** For transaction instance id `<id>`:

| Runtime key | Meaning |
|---|---|
| `<id>_X` | declared **output** field `X` |
| `<id>_I_X` | the **input echo** of `X`, when `X` is also an input on that transaction |
| `<id>_MSG_COL` | auto-injected per-row status/error message (`"OK"`, or a real error string) |
| `<id>_<id>_<programName>_<transactionName>_ROWNUM` | auto-injected; observed always `null`; purpose not investigated |

Real example, instance `xu3EO2azWPT`, field `TRNM` used as both input and
output:

```json
"xu3EO2azWPT_I_TRNM": "*",
"xu3EO2azWPT_TRNM": "LstClaimDet"
```

**Padding / incomplete rows.** Large results have been observed to include
rows where **every declared output field is `null`**. Filter them out using
the **mandatory input-echo field** (`_I_<name>`) — filtering on an output
field does not work, since those are exactly what is `null` on the bad rows.

**`maxRows: 0` means unlimited, not zero.** Confirmed by a run returning
24,154 records with `maxRows: 0`, versus an earlier run capped at exactly 100
before it was set. It is configurable per transaction in the builder
(transaction settings, not a workflow-level setting).

**Pagination is automatic.** The compiled state machine (`definitionAsl`,
returned alongside a GET) contains a `CheckForPendingApi` choice state that
loops back into the M3 API step whenever `hasPendingSteps = true` or
`partialCursor != null`, before continuing. So manual cursor/`forEach` looping
is usually unnecessary. **This is inferred from internal implementation
details that could change without notice** — re-verify if it ever seems to
stop working.

### ⚠️ Transaction instance ids are NOT stable

`xu3EO2azWPT` and friends are **regenerated by Live**, not preserved from what
you send. Confirmed to change:

- every time a workflow is recreated via `POST /workflows`, and
- apparently also across ordinary re-saves of an existing workflow.

**Implication:** a Transform template that hardcodes an instance-id prefix
(`xu3EO2azWPT_TRNM`) will silently break on the next save. Two options:

1. Re-edit the Transform after every save, reading the new ids back via GET.
2. **Match fields by suffix instead**, so the template never depends on an
   instance id. This is confirmed working in this engine:

```jsonata
$context.data.all.m3_api_1[$exists($lookup($, $filter($keys($), function($k){$substring($k, -7) = "_I_TRNM"})[0]))].{
  "Program":     $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_MINM"})[0]),
  "Transaction": $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_TRNM" and $substring($k, -7) != "_I_TRNM"})[0]),
  "Description": $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_TRDS"})[0]),
  "Type":        $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_SIMU"})[0]),
  "Status":      $lookup($, $filter($keys($), function($k){$substring($k, -5) = "_STAT"})[0])
}
```

Three things that bit during development of that expression:

- **Get the character count right.** `$substring($k, -N)` takes the last N
  characters; a wrong N silently returns no match — no error, just an empty
  result.
- **Row-filter on the input echo** (`_I_TRNM`), not an output field — that is
  what excludes padding rows. `_I_TRNM` is **7** characters, including both
  underscores.
- **Disambiguate shared suffixes.** `_TRNM` and `_I_TRNM` both end in
  `_TRNM`; without the explicit `and ... != "_I_TRNM"` exclusion the match is
  ambiguous and may pick either key.

### 12.7 The limitation that forces Route B

`GENERIC_FILTER` runs **once, before the whole M3 API step**. It therefore
**cannot gate one M3 call while another runs unconditionally** — which is
exactly what Classic workflows with filters deep inside a function do. There
is no per-transaction filter on this path. When you need one, go to Route B.

## 13. Route B — M3 with no M3 API step (ION API bulk execute)

Confirmed from product documentation plus **four real production customer
workflows**. This is not a hack — it is the shape Vince itself uses when the
native step can't express the logic. One real example performs full M3
**writes** this way (`AddBatchHead` → `AddBatchHeadChg` → `AddBatchLine` →
`Confirm`) across seven Transform/REST API pairs, with no M3 API step and no
Excel step.

### 13.1 The idiom

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

(Remember: this whole object lives inside a **`content` string** — §7.6.)

**Region and `<TENANT>` are tenant-specific and not derivable.** Copy them
from a real REST API step in that tenant. Observed real values include
`.../DEMO_TENANT/...` and `.../TEST_TENANT/...`.

With `connectionId` set, **Live attaches the connection's auth
automatically** — you never build an ION API token yourself.

### 13.2 The request body

The endpoint carries **no program/transaction in the URL**: one call carries
**many** M3 transactions, which is the entire point (M3 needs eight calls to
read one order).

```json
{
  "program": "MMS200MI",
  "cono": 790,
  "maxReturnedRecords": 100,
  "transactions": [
    {
      "transaction": "LstItmWhsByWhs",
      "record": { "WHLO": "001" },
      "selectedColumns": ["WHLO", "ITNO", "ITDS"]
    }
  ]
}
```

`cono`, `maxReturnedRecords` and `selectedColumns` are **optional**.

### 13.3 The response (confirmed against a real captured payload)

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

(`nrOfSuccessfullTransactions` is spelled exactly that way.)

### 13.4 Reading it from a later step

```jsonata
$context.data.all.rest_api_1.body.results.records.CUNO
/* ^ flattened across ALL transactions in the call */

$context.data.all.rest_api_1.body.results[transaction="GetHead"].records[0].CUNO
/* ^ one named transaction's records */
```

That `.body` is the REST API step's own `{headers, body}` wrapper. An M3 API
step's result is the array directly — **do not carry the convention across
step types**.

### 13.5 Building the payload from rows (a confirmed real Transform)

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

**The trailing `[]` forces an array even for a single row.**

### 13.6 Where filters go — three tiers, in order of preference

**Tier 1 — a JSONata predicate in the Transform. Use this.** The filter
belongs on the rows going into *that one call's* payload, so each call gets
its own and one call can be gated while the next runs over everything:

```jsonata
$data := $context.data.trigger.body[Approved = "OK"];
```

Gating on an **earlier call's output** — the case the native filter cannot
express — needs per-row correlation, because a prior step's
`...records.ALPG` is that field's *whole flattened sequence*, not this row's
value. The real "Claim register" workflow correlates by index:

```jsonata
$data := $filter($context.data.trigger.body, function($row, $i) {
  $context.data.all.rest_api_1.body.results.records[$i].ALPG = "1"
    and $lowercase($row.Approved) = "ok"
});
```

> ⚠️ **Index correlation is fragile by nature.** `records[$i]` lines up with
> row `$i` only while the prior call returned exactly one record per input
> row, in order, with nothing dropped — and `nrOfFailedTransactions` exists
> precisely because M3 can drop some. **Any row-filtering between two calls
> destroys the alignment.** When it matters, carry the key field (e.g.
> `ORNO`) into the payload and join on it instead of trusting the index.

**Tier 2 — `skipIfExpression` on a v2 REST API step.** Name confirmed;
**syntax and truthiness semantics unconfirmed** (§7.6). A Tier 1 predicate
does the same job with JSONata semantics you can test locally.

**Tier 3 — a Code step.** Only when a later call's *input* genuinely depends
on an earlier call's *output* for the same row (so the calls can't be
batched), or when you need per-row throttling or error handling. See §7.7 for
the costs.

### 13.7 What you give up by leaving the M3 API step

- field-metadata validation
- automatic pagination
- `excel_1` binding (use the `VXL` label instead — §8)
- much stricter save-time validation

**Prefer Route A whenever it fits.**

## 14. The M3 API catalog — turn "which transaction?" into a lookup

This is the single most valuable asset for anything that has to *choose* an
M3 transaction, because it converts hallucination into lookup.

A Custom Table **`m3-api-catalog`** exists in Live:

| | |
|---|---|
| Rows | **22,535** (deduped; 24,154 fetched before dedup) |
| Columns | `Program`, `Transaction`, `Description`, `Type`, `Status` |
| Primary key | `["Program", "Transaction"]` |
| `updateType` | `UPSERT` |

Query it with the endpoints in §11.5 — a point lookup by `Program` +
`Transaction` is one GET:

```
GET https://api.vince.live/v1/custom-tables/data/m3-api-catalog?records.Program=CRS610MI&records.Transaction=GetOrderInfo
```

**Check every program/transaction name against this catalog before emitting
it.** A name that isn't in it is either a hallucination or a genuinely new
API — either way, stop and confirm; don't ship it.

To pull the whole catalog, use the prefix-and-split strategy in §11.6 — naive
`from`/`size` paging loses records.

The catalog is itself produced by a workflow: `MRS001MI / LstAllTrans` in an
M3 API step, a Transform doing the suffix-matching in §12.6, and a Table
Updater writing the rows.

**The catalog gives you programs and transactions. It does NOT give you their
field lists** — that is the metadata endpoint's job (§12.5), and it is a
separate problem.

### Program/transaction names confirmed observed

Useful as worked examples; **this is not a substitute for the catalog**.

| Program / transaction | Where it was confirmed |
|---|---|
| `MRS001MI / LstAllTrans` | real execution; metadata `count: 2` (I + O). The transaction that *generates* the catalog |
| `BUS100MI / DelBudgetLines` | metadata: 14 inputs, **0 outputs** — the canonical "empty direction is normal" case |
| `BUS100MI` (whole program) | metadata `FIELDS#BUS100MI` → `count: 24` |
| `MMS200MI / Get` | real M3 API step capture |
| `MMS200MI / LstItmWhsByWhs` | real ION API bulk-execute body |
| `MMS301MI / LstStockTakeDet` | field metadata (`WHLO` STRING/3/mandatory "Warehouse"; `CONO` NUMERIC/3/mandatory "Company") |
| `CRS610MI / GetOrderInfo` | real Custom Tables row (`Type: "S"`, `Status: 44`) |
| `OIS100MI / GetHead`, `GetOrderHead` | real production Transform + bulk-execute response |
| `MMS120MI` | generic-pipeline spec example |
| `AddBatchHead` → `AddBatchHeadChg` → `AddBatchLine` → `Confirm` | the real seven-pair production **write** pipeline |

Names seen in **Classic XML exports** rather than confirmed against Live —
treat as leads, not facts: `MMZ101MI.AddHead` / `.AddLine`,
`OIS017MI.GetPriceList` / `.AddPriceList`, `MNS150MI.SelUsers`,
`AUTCHKMI.ChkAuthority`, and `CMS100MI.LstC05_OPRICH` (note a **custom
transaction name**, which is a real thing that exists).

---

## 15. Checklists

### Before sending a `POST /workflows`

- [ ] The token's tenant matches the tenant you are targeting.
- [ ] The token is not expired (check its own `exp` claim).
- [ ] `workflowType` is `"STANDARD"`.
- [ ] Every step `type`/`name`/`target` triple is from the table in §3.
- [ ] `retryAttempts: 0` on every step except `trigger_1`, which omits it.
- [ ] Exactly one root `TRIGGER`; the chain is linear via `childSteps`.
- [ ] Every M3 transaction has `type: "ADD"` and a gapless `executionOrder`
      starting at 1.
- [ ] Every API-chain `ref` target is declared in its producer's `output`.
- [ ] Repeated CLIENT fields carry a `ref` to the **first** declaring
      transaction.
- [ ] Each REST API step's config is inside a **`content` string**, not flat.
- [ ] Each Transform step's config **is** flat (`template`, `trimAll`).
- [ ] No Transform references a hardcoded transaction instance id.
- [ ] Every Transform uses `$context...`, not the preview pane's `$.<stepId>`.
- [ ] `excel_1` appears only after an M3 API step.
- [ ] Trigger fields have `formGroupId` and `elementPosition`, and the groups
      they name exist in `formGroups`.

### After creating anything

- [ ] Read the **real** id back from the response — yours was ignored.
- [ ] GET the workflow back and confirm it **renders** in the builder.
- [ ] Execute it and confirm it actually does what you expect.
- [ ] For a table load: check `aggregated/{tableName}` count against the
      expected input count (this is how silent primary-key overwrites are
      caught).

### When a lookup returns nothing

Diagnose in this order — the first two cost nothing and are the usual
culprits:

1. Token expired?
2. **Token from a different tenant than the connection?**
3. Transaction name genuinely wrong (check the catalog, §14)?
4. Genuinely an empty direction (a delete has no outputs — §12.5)?

---

## 16. What is NOT confirmed — do not build against these

- **The Table Updater step's JSON shape.** Blocks any workflow that writes to
  its own tables. Capture a real one first.
- **Any row-write endpoint for Custom Tables.** None exists that is confirmed.
- **Reading a table's definition** (`primaryKeys`/`schema`/`updateType`).
- **A workflow PUT/PATCH endpoint.** Only POST and GET are confirmed.
- **`forEach`** on a v2 REST API step — documented, beta, no captured
  example, and its own doc contradicts itself on the version number.
- **`skipIfExpression` syntax and semantics** — only the property name is
  confirmed.
- **What `"version": 3` changes** on a REST API step. It is accepted and
  compiles; production uses 2.
- **`labels: ["VXL"]`** as the JSON form of the VXL label — a reading of the
  documented wording, not a capture.
- **M3 type code `D` → `NUMERIC`** — inference, not a confirmed Live field.
- **The multi-character prefix query form** (`q=Program:CMS*`) on the search
  endpoint.
- **A separate "Vince Platform API"** (OAuth2 `client_credentials`,
  Users/Roles/API Clients/Tables/Tags/Schedules/Variables). The *mechanism*
  is real — confirmed via a genuine Access Management → API Clients screen
  and a real "self" connection's Token URI / Base URI — but both the token
  endpoint and base URI are tenant-specific and not derivable. **Do not guess
  a domain, and do not assume it is the same surface as `api.vince.live`.**
- **App Builder** — **zero** confirmed facts of any kind. No known prompt
  format, schema, or API shape. Do not invent one.

---

## 17. How to extend this document

Same methodology throughout: build a minimal real example in Live's UI or hit
the real API directly, capture what actually comes back, and write down only
what is confirmed — marking anything uncertain as such rather than presenting
a guess as fact.

A wrong guess rejected by Live's own validator (a real 400 with a specific
message) is useful signal. A plausible-sounding guess that happens to return
200 is **not** proof it is correct until the result is independently
confirmed — by rendering it, executing it, or decoding and verifying a
specific field the way the Custom Tables `__id` was verified above.

A **confirmed absence** is worth as much as a confirmed endpoint. Record it,
so the next person doesn't re-probe it.
