# Vince Live API Reference

A consolidated reference for interacting with Vince Live's real, confirmed APIs. Meant to be pasted as context into a Claude conversation that needs to build or troubleshoot against Vince Live without access to a codebase or prior history.

## Ground rules — read this before anything else

1. **Never guess at Live's JSON structure, endpoint paths, or enum values.** Everything below was confirmed by building a real example in Live's UI, or by hitting the real API and reading back what actually came out — not from documentation or inference. If something isn't covered here, that means it's genuinely unconfirmed. Say so, and go verify it (build a minimal test in Live's UI and capture it, or make a real API call and read the real response/error) rather than inventing something plausible.
2. **A wrong guess often produces a real, useful error rather than silence** — e.g. `POST /workflows` with an invalid step `type` returns `"WorkflowDefinition is invalid! Errors (N): Property workflowConfig.steps stepId X has invalid type: Y"`. Use that signal to correct course; it does not by itself reveal the correct value, so don't assume the first plausible-sounding correction is right either.
3. **IDs alone are not proof something works.** A malformed `tenantId`/`connectionId`/`environmentId` can pass creation silently and only break rendering later in the builder UI. A successful `POST` (HTTP 200) does not guarantee the workflow renders or executes correctly — confirm both.
4. Treat every enum/type value below as a **closed, confirmed list**, not a pattern to extend by analogy. New step types, new table field types, etc. need their own real captured example before you write code or JSON against them.

## Auth & base URL

- **Real, confirmed API base: `https://api.vince.live`.** A different-looking domain (e.g. `api.vincelive.dev`) is not a typo variant to try — verify DNS/reachability before trusting any URL you haven't personally confirmed.
- **Bearer token**: a short-lived Cognito ID token, captured manually from Live's own web UI (DevTools → Network tab, any authenticated request). Passed as `Authorization: Bearer <token>`. Expect it to expire after roughly an hour of session idle time (surfaces as `401 {"message":"Unauthorized"}`) — that's expected, not a bug; get a fresh one.
- **⚠️ The token decides the tenant — and a mismatched one fails SILENTLY.** The token, not any `tenantId` in a request body, determines which tenant you are acting on. Strongly suspected (not yet deliberately reproduced): using a token from tenant A against tenant B's `connectionId` does **not** return 401 — the call succeeds and the connection is simply invisible, so `/metadata/{connectionId}` answers `{"count": 0, "items": []}`. That is indistinguishable from "M3 cannot describe this transaction", and it cost a long debugging session in which every field fell back to `TODO: metadata not found ...` and the tooling blamed the transaction name. Evidence: the same connection+query returned `count: 0` on one token and real fields on a fresh one minutes later, with the query format unchanged. **To confirm it properly**, deliberately pair a known-good connection with a token from another tenant and check that you get 200-with-nothing rather than 401. `metadata.tenant_from_token()` reads the tenant out of the token's own claims so the pairing can be checked before a call; the web UI warns on a mismatch and refuses to create.
  - **The mental model:** the **token is the authority** — it decides who you are *and* which tenant you act on. `connectionId` / `tenantId` / `environmentId` are **payload, not routing**: references to resources that live inside that tenant (the connection is what the M3 API step calls through and what `/metadata/{connectionId}` reads; the environment is what makes the step render). Corroborated independently by Custom Tables (`tenantId` populated server-side from the session, not sent) and by `GET /workflows/{id}` needing no ids at all. Because they are references rather than routing, a mismatch cannot produce a routing error — it produces a workflow whose references do not resolve, which is the same silent failure already documented for a malformed connection/environment id.
  - **Open question:** whether `POST /workflows` ignores the body's `tenantId` (as Custom Tables does) or validates it against the token. It is required at creation, but a deliberately wrong one has never been sent. Cheap to settle: POST with a bogus `tenantId` and a good token, then GET it back and see whether it errored or silently substituted the token's tenant.
- **A second, distinct auth mechanism exists and is real**, but is not generically usable without tenant-specific discovery: OAuth2 `client_credentials` via an API Client (created under Administration → Access Management → API Clients in Live's UI). Confirmed via a real "self" connection configuration screen showing a `Token URI` of the shape `https://tenant-<hex>.auth.<aws-region>.amazoncognito.com/oauth2/token` and a `Base URI` of the shape `https://<tenant-slug>.vince.live/` — **both are tenant-specific**, not derivable from a formula or reused across tenants. If you need this mechanism, the values must come from that tenant's own "self"-style connection screen or from Vince support directly — do not guess a subdomain.
  - **CONFIRMED 2026-10-01 (Haakon, VinceForge "Test connection"): `api.vince.live` accepts the `client_credentials` ACCESS token** - `GET /workflows` answered 200 with an API Client's minted token (`converter/api_client.py`). **An API Client with no usable Role is refused**: before a role was assigned the same call answered `HTTP 400: Empty or invalid ROLE permissions` (a 400, not a 401/403, and the token itself was fine). Assign the client a Role with workflow permissions under Administration -> API Clients / Security. Still unchecked: which roles the full import needs (create workflows, tables, groups, file upload), and whether a minted token names its tenant in a claim.
- A separately-referenced "Vince Platform API" (documented endpoints for Users/Roles/API Clients/etc., OAuth2-secured) may or may not be the same thing as the above — treat as **unconfirmed** until independently verified against a real call. A prior reference document for this made confident claims about a base URL that turned out to have zero DNS records — a fabricated/hallucinated source, not real. Confirm before trusting any doc about this, including this one.

---

## 1. Workflows API

### Create / read

```
POST https://api.vince.live/workflows
GET  https://api.vince.live/workflows/{workflowId}
PUT  https://api.vince.live/workflows          <- update, CONFIRMED 2026-09-29
DELETE https://api.vince.live/workflows/{workflowId}   <- CONFIRMED 2026-09-29
```

**Delete** - confirmed by use: 16 workflows deleted, each then answering
`404` to a GET (`converter/cleanup.py`). Deleting a workflow does NOT remove
a Group it belongs to: re-importing under the same base name then fails the
Group create with `HTTP 409: Group <name> already exists!` (Groups have no
captured delete/list endpoint yet).

**Update = `PUT /workflows`** (no id in the path), body
`{"tenantId": ..., "workflowId": ..., "definition": {...}}` -> `200` with the
full workflow record back. Captured from the builder attaching a design file
(a HAR capture of the builder): it sent the `definition`
exactly as the preceding GET returned it, with only the edited fields changed.
Only that design-file edit has been confirmed; `liveapi.update_workflow`.

**There is a size limit on a workflow - OBSERVED 2026-09-29** (CustomerA app
import, `RUN/app-imports/vl-20260929-141416`): a too-large POST comes back
`HTTP 400: Item size to update has exceeded the maximum allowed size`. The
exact limit is unknown and is NOT a plain body-byte count: among Code-step
workflows an 82 KB body was accepted and 116-128 KB bodies refused; among
native M3 API workflows 134 KB was accepted and 179 KB / 379 KB refused. The
wording suggests a storage item limit on something derived from the body.
Keep generated steps compact (`m3_rest._compact_plan_literal`).

**GET and POST do NOT share a body shape.** GET returns server-computed fields (`stats`, `workflowArn`, `definitionAsl`, `created`/`modified`/`createdBy`/`modifiedBy`, `overview`) that POST's validation rejects outright (e.g. `stats.lastExecutionId: null` fails schema validation — the creation schema wants a string or the key absent, not null). GET also **omits `description`/`labels` entirely**, even though creation requires them. If cloning a GET response into a new POST, strip it down to only the fields below — don't forward the whole GET body.

### Top-level envelope (POST body)

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
  "definition": { "workflowConfig": { "...": "see below" }, "stepConfig": { "...": "see below" } }
}
```

Field notes:
- `hasError` is **client-computed**, not server-validated. Live's own UI sends `true` at creation and `false` once its own validation decides the workflow looks complete. Set it yourself.
- `workflowId` you send must be syntactically valid (`WORKFLOW-` + 32 hex chars) but Live assigns its **own** id regardless — always read the real id back from the response.
- `workflowType` must be `"STANDARD"` at creation. `"EXPRESS"` produces a broken, half-provisioned workflow (blocks re-creating the same id, unreachable via GET, invisible in the workflow list).
- `tags.apiEnvironment`/`environments`/`primaryEnvironment`/`connectionId` must be real, correctly-formatted ids. A malformed one passes creation silently and breaks M3 API step rendering later (surfaces as a frontend `TypeError`, not a server error).

### `definition.workflowConfig`

```json
{
  "name": "My Workflow",
  "constants": { "CONSTANT_<fieldName>": "<literal value>" },
  "inputs": { "<CLIENT field name or Filter_<Column>>": { "dataType": "STRING", "validation": { "required": true } } },
  "connectionContext": { "M3": "CONNECTION-<32 hex>" },
  "hasSpreadSheetInput": true,
  "hasExcelDesignFile": false,
  "hasTemplateFile": false,
  "workflowTags": {},
  "apiFieldReferences": {},
  "contexts": {},
  "clientConfig": {
    "title": "My Workflow", "description": "Description", "buttonText": "Execute",
    "formGroups": [{ "groupId": 1, "groupName": "fields" }],
    "fields": [ "...see triggerField shape below..." ]
  },
  "steps": [ "...single root TRIGGER stepNode, see below..." ]
}
```

- `constants` are keyed `CONSTANT_<fieldName>`; the input field itself stores that **key** as its `value`, not the literal.
- `hasSpreadSheetInput` = true if any transaction has an EXCEL-sourced input OR any output cell is mapped to a column.
### Design file (the Excel sheet attached to a workflow) — CONFIRMED

Captured 2026-09-10 from a real upload in the builder (request headers
screenshot, upload response, and a GET of the same workflow before and
after — `RUN/*design file*`). Two steps, and the file never passes through
`api.vince.live` itself:

**1. Ask for a presigned upload URL.**

```
GET https://api.vince.live/v1/files/tenant/upload-url/{WORKFLOW-<32 hex>}/designFile/{filename}.xlsx
Authorization: Bearer <token>
→ 200 application/json
{
  "filename":  "WORKFLOW-<id>/designFile/<filename>.xlsx",
  "url":       "https://files.vince.live/TENANT-<id>/WORKFLOW-<id>/designFile/<filename>.xlsx?...signed...",
  "expiresAt": "2026-09-10T08:28:18.000Z",     // ~5 minutes
  "method":    "PUT",
  "headers":   { "Content-Type": "*/*" },
  "isWildcard": false
}
```

The path segment after `upload-url/` is the storage key, and the tenant is
taken from the token — the returned `url` carries a `TENANT-<id>` prefix
the caller never sends. `method` and `headers` are instructions for step 2,
not a description of step 1.

**2. `PUT` the raw file bytes to the returned `url`** with
`Content-Type: */*` and **no `Authorization` header** — the signature is in
the query string. Note `expiresAt`: get the URL and use it immediately.

**What appears in the workflow JSON.** A GET of the same workflow before
and after differs by exactly two fields in `definition.workflowConfig`:

```json
"hasExcelDesignFile": true,
"excelDesignFileKey": "WORKFLOW-<id>/designFile/<filename>.xlsx"
```

`excelDesignFileKey` is **byte-identical to the `filename` field of the
upload-url response**, so read it back from there rather than
reconstructing it. `hasTemplateFile` stayed `false` and no
`templateFileKey` appeared — that is a separate, still-uncaptured feature.

**Sequencing consequence:** the key embeds the workflow id, and Live
assigns its own id at creation regardless of what you POST. So a design
file can only be uploaded **after** the workflow exists and its real id has
been read back from the creation response. There is no way to include one
in the initial POST.

**The one open question.** The capture covers the upload-url GET and the
workflow before/after, but not whatever wrote those two fields. Either the
server sets them when the PUT completes, or the builder saved the workflow
itself afterwards. The evidence is suggestive but not decisive: the
workflow's `modified` went from `2025-02-13T14:24:29Z` to
`2026-09-10T08:23:19Z`, matching the upload-url response's own `Date:
08:23:18 GMT` to the second — so *something* wrote the record at that
moment. Until it is settled, upload and then **GET the workflow back and
check** rather than assuming; if the fields are absent, a workflow update
call is needed. **ANSWERED 2026-09-29 (4 of 4 real uploads, CustomerA app
import):** the server does NOT set it. After a successful presigned upload
+ PUT, a GET of the workflow still has `hasExcelDesignFile: false` and no
`excelDesignFileKey` - the builder's own follow-up workflow PUT is what links
it. **That PUT is now captured** (`PUT /workflows`, see "Create / read"
above): after the upload the builder set exactly
`workflowConfig.hasExcelDesignFile: true` and
`workflowConfig.excelDesignFileKey: "<WORKFLOW-id>/designFile/<filename>"`
and nothing else. It also slugified the filename
(`Creation_CentralRef_1.4.xlsx` -> `creation-centralref-14.xlsx`);
VinceForge keeps the original name, which uploads fine - whether the slug
matters anywhere downstream is unknown. (Older note, kept for history:
**no workflow PUT/PATCH endpoint is confirmed** (only
`POST /workflows` and `GET /workflows/{id}`; a "hit Save" PUT was observed
in a HAR once and used to diff the creation payload, but its URL and body
shape were never recorded).

**A caution about what else the capture moved.** That same before/after
diff shows the Excel step's column bindings changing — three `output`
fields gained `source: "EXCEL"` / `sourceType: "COLUMN"` / a column letter,
one `input` binding and one `inputs` entry disappeared, and
`clientConfig.formGroups` went from 3 entries to 0. That is consistent with
the documented behaviour *"an existing spreadsheet can be imported. Vince
Platform will automatically map fields if the Excel file contains field
IDs"* — but it is equally consistent with the builder saving unrelated
pending edits in the same action. **Do not conclude that uploading a design
file re-maps columns** on this evidence. It does mean a design-file upload
is not safely assumed to be additive, which matters if VinceForge ever
uploads one onto a workflow it has just carefully built.

(The rest of that diff — ~90 changes under `definitionAsl` — is unrelated:
the tenant's Step Functions definition was regenerated between the two
captures with AWS's JSONata query language, `Arguments`/`Output` replacing
`Parameters`/`ResultPath`. `definitionAsl` is server-computed and already
stripped by `clone_and_retarget`.)

### Other `workflowConfig` fields seen in that capture but not yet emitted

Real, captured, and absent from everything this project builds. Not yet
tested at creation time — present in a GET, which is not the same as
accepted in a POST:

`isControlSpreadSheetEnabled`, `isEnforceSheetVerificationEnabled`,
`saveBackup`, `notifyOnError`, and `type: "STANDARD"` *inside*
`workflowConfig` (alongside the confirmed top-level `workflowType`). The
first three are the builder's "Control Spreadsheet", "Enforce Sheet
Verification" and "Save backup of output" toggles
([Components in Workflow builder](https://vincesoftware.com/product-documentation/components-in-workflow-builder)).
Also worth noting: in that workflow `clientConfig.title` was `"Untitled"`
while `workflowConfig.name` was `"HS - DEMO - BENCO"` — the two can
diverge, so the builder does not keep them in sync the way this project's
builder does.
- `clientConfig` mirrors the `trigger_1` stepConfig entry (same title/description/buttonText/formGroups/fields, duplicated in both places).
- `steps` is always a single-element array — one root `TRIGGER` step whose `childSteps` nests the rest of the chain **linearly** (one child at a time; no branching seen in any confirmed example).

### Step chain (`stepNode`, recursive via `childSteps`)

```json
{ "stepId": "m3_api_1", "type": "API", "name": "M3 API", "retryAttempts": 0, "target": "workflow-api", "childSteps": [ "..." ] }
```

Confirmed `type` / `name` / `target` combinations — **this is a closed list**, do not extend by analogy (a wrong `type` produces a real 400, e.g. guessing `"TRANSFORM"` and `"REST_API"` both failed validation before the real values below were found):

| `type` | `name` | `target` | stepId convention | Notes |
|---|---|---|---|---|
| `TRIGGER` | `Trigger` | `workflow-trigger` | `trigger_1` | Root, always present. No `retryAttempts` field (only step type that omits it). |
| `API` | `M3 API` | `workflow-api` | `m3_api_1` | Holds one or more M3 transactions — see below. |
| `EXCEL` | `Excel` | `workflow-excel` | `excel_1` | **M3-API-specific.** Attaching it after a non-M3 chain (Transform/REST API/Code with no M3 API step) caused a real save failure in the builder UI. Its own column entries can only reference an M3 API transaction's instance id — it has no way to reference any other step type's output. |
| `GENERIC_FILTER` | `Generic Filter` | `workflow-generic-filter` | `generic_filter_1` | Filters raw Excel row data **before** the M3 API step runs. Applies to the whole M3 API step, not a single transaction. |
| `TRANSFORMER_MORPH` | `Transform` | `workflow-transform` | `transform_N` | JSONata transform — see below. |
| `GENERIC_API` | `Rest API` | `workflow-rest-api` | `rest_api_N` | Generic REST call — see below. Exact name casing confirmed (`"Rest API"`, not `"REST API"`). |
| `GENERIC_CODE` | `Code` | `workflow-code` | `code_N` | Node.js code step — see below. |

`stepId` naming is directly load-bearing: Transform steps reference sibling steps by exact stepId (e.g. `$context.data.all.rest_api_1`). `retryAttempts` is `0` on every step except `trigger_1`. **`childSteps: []` is a valid, saveable terminal state for any step type** — a workflow does not need to end in `excel_1`.

### M3 API step (`stepConfig["m3_api_1"]`, or whichever stepId)

Keyed by transaction instance id (format: `x` + 10 mixed-case alphanumeric chars, e.g. `xN9MwyGBpt` — Live generates this, not you):

```json
{
  "xN9MwyGBpt": {
    "id": "xN9MwyGBpt", "stepId": "m3_api_1",
    "programName": "MMS200MI", "transactionName": "Get", "name": "Get",
    "source": "M3", "connectionId": "CONNECTION-<32 hex>", "executionOrder": 1,
    "extendedGrouping": [],
    "input": { "<fieldName>": "...InputField, see below..." },
    "output": { "<fieldName>": "...OutputField, see below..." },
    "type": "ADD"
  }
}
```

- `executionOrder` starts at 1, no gaps, matches execution order.
- `type` is confirmed as `"ADD"` in every reliable multi-transaction example **regardless of what the M3 transaction actually does** (list/get/update/add) — its absence is the strongest known cause of an M3 API step rendering completely inert in the builder. Don't infer LIST/GET/ADD from the transaction name.
- List-style transactions also carry `"maxRows": <int>`, `"defaultInputConfig": {}`, `"hasSpreadSheetInput": false`.
- A dependent transaction adds `"basedOnTransaction": "<producer's instance id>"` and `"runOnFailure": <bool>`.
- A field referenced elsewhere via an API-chain `ref` **must** be declared in its producer's own `output`, even with no real column mapping — synthesize a minimal declaration on an unused column letter if needed, or a runtime SQL error results.

**InputField:**
```json
{
  "name": "ITNO", "descr": "Item number", "type": "STRING", "length": "15", "mandatory": false,
  "fieldType": "I", "isMapped": true, "hasGrouping": false,
  "source": "EXCEL", "sourceType": "COLUMN", "value": "F"
}
```
- `type` ∈ `STRING | NUMERIC | UNKNOWN` (M3 codes A/N/D map to STRING/NUMERIC/NUMERIC; `UNKNOWN` is a safe placeholder, confirmed not to break rendering).
- `source` ∈ `CONSTANT | EXCEL | CLIENT | API`, and `value`'s meaning depends on it:
  - `CONSTANT` → the **key** into `workflowConfig.constants` (e.g. `"CONSTANT_ALWT"`), not the literal.
  - `EXCEL` → the spreadsheet column letter; also add `"sourceType": "COLUMN"`.
  - `CLIENT` → the **real M3 field name**, never a placeholder — confirmed via a real execution error when a placeholder was sent as-is.
  - `API` → `"<producerTxnId>.output.<fieldName>"`, plus `"ref": {"apiId": "<producerTxnId>", "name": "<fieldName>", "type": "OUTPUT"}`.
- **Repeated CLIENT fields** (same field name, 2nd+ transaction) need `"ref": {"apiId": "<id of the FIRST transaction to declare it>", "name": "<fieldName>", "type": "INPUT"}` — without it, Live creates separate fields (`CONO`, `CONO_1`, `CONO_2`...) instead of one shared trigger value.

**OutputField:**
```json
{
  "name": "ITDS", "descr": "Item description", "type": "STRING", "length": "30", "mandatory": false,
  "fieldType": "O", "isMapped": true, "hasGrouping": false,
  "source": "EXCEL", "sourceType": "COLUMN", "value": "H"
}
```

### Excel step (`stepConfig["excel_1"]`)

```json
{
  "sourceSheet": {
    "fields": {
      "sheetName": "Sheet1", "startColumn": "A", "endColumn": "H",
      "primaryHeaderRow": 2, "secondaryHeaderRow": 3, "dataRow": 4,
      "primaryHeaderStyle": {}, "secondaryHeaderStyle": {}, "dataRowStyle": {},
      "columns": { "<column letter>": "...excelColumn, see below..." }
    },
    "cells": { "contents": {}, "styleInfo": {} },
    "sortOrder": []
  }
}
```
- `primaryHeaderRow` = `dataRow - 2`, `secondaryHeaderRow` = `dataRow - 1` in every confirmed example.
- **`<column>-1` dual-role columns**: if a second field genuinely reads the same Excel column (e.g. two transactions both reading column F as separate input fields), it gets a second entry keyed `"<column>-1"` — confirmed correct field-for-field against a real GET response. The plain slot goes to whichever transaction is declared first.

**excelColumn:**
```json
{
  "fieldId": "<txnInstanceId>.output.ITDS",
  "type": "API", "stepId": "m3_api_1",
  "field": { "fieldId": "<txnInstanceId>.output.ITDS", "fieldName": "ITDS", "type": "API", "datatype": "STRING" },
  "primaryHeader": { "type": "API", "value": "Item description", "fieldId": "<txnInstanceId>.output.ITDS" },
  "secondaryHeader": { "type": "API", "value": "ITDS", "fieldId": "<txnInstanceId>.output.ITDS" }
}
```
`fieldId` is `"<m3ApiTxnInstanceId>.output.<field>"` for an export column, or `"...input.<field>"` for an input-side column. For a `-1` dual-role column, `secondaryHeader` is a **copy** of the plain slot's own `secondaryHeader` (pointing at the other transaction), not derived fresh.

### Generic Filter step (`stepConfig["generic_filter_1"]`)

Filters raw Excel row data before the M3 API step runs — a hard AND-combined list, applies to the whole step (cannot gate one transaction while another runs unconditionally):

```json
{
  "all": [
    {
      "clientKey": "Filter_BD", "operator": "equal", "dataType": "STRING", "logicalType": "AND",
      "from": "Row", "defaultValue": "OK", "source": "CLIENT", "fact": "BD",
      "value": "{{ header.Filter_BD }}"
    }
  ]
}
```
`value` is literally the template string shown, not runtime-substituted in the stored JSON. A `source: "CLIENT"` condition creates an **editable** trigger field with `defaultValue` as the default.

**Extended 2026-09-28** from three UI-built captures and eight real runs of one test workflow (`RUN/captures/generic-filter-*.json`, `RUN/filter-debug/`):

- `operator` ∈ `equal | notEqual | lessThan | lessThanInclusive | greaterThan | greaterThanInclusive | isBlank | isNotBlank | stringContains | stringNotContains` — every option the UI offers.
- `dataType` ∈ `STRING | NUMBER | BOOLEAN` (**`NUMBER`**, not the M3 step's `NUMERIC`); a CLIENT condition repeats it on its trigger field and in `workflowConfig.inputs.<clientKey>`.
- `source` ∈ `CLIENT | CONSTANT`. A **CONSTANT** condition stores its literal in both `value` and `defaultValue` (e.g. `"5000"`) and gets **no trigger field and no input** — a fixed filter the runner cannot edit. `isBlank`/`isNotBlank` are saved as CONSTANT with `value: "''"`.
- `logicalType` ∈ `AND | OR` is the connector to the condition **after** this one; the last condition's is inert. The UI nests left-deep, one level per condition after the second: `{"all": [{"all": [{"all": [a, b]}, c]}, d]}`, and the level joining a group to its successor is `"any"` when the group's last condition is `OR`.
- **Runtime, from real runs:** a `NUMBER` range compares **numerically** — rows with `C = 50` passed `C lessThan 100` (as text, `"50" < "100"` is false). VXL sends numeric sheet cells as JSON numbers; `header` values arrive as strings. Not yet tested: a `STRING` range over numeric cells.

### Trigger step (`stepConfig["trigger_1"]`) — the SCHEDULE variant

`triggerType: "CLIENT"` (a form the user fills in) is the variant used everywhere else in this document. A hand-built workflow confirmed a second one, and its extra fields:

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

- `scheduleId` is server-assigned — creating a SCHEDULE trigger evidently provisions a schedule resource, so don't invent one.
- The CLIENT-trigger fields (`title`/`buttonText`/`fields`/`formGroups`) stay present and inert.
- A GET also returns a server-computed `overview.triggerType` echoing this value.
- `clientConfig.title` stayed `"Untitled"` while `workflowName` was set — the builder does not sync them, so a workflow can display an unhelpful trigger title without anything being wrong.

### Transform step (`stepConfig["transform_N"]`, `type: "TRANSFORMER_MORPH"`)

```json
{ "template": "<JSONata expression as a plain string>", "trimAll": true }
```
JSONata bindings, confirmed from Live's own default template for a fresh Transform step:
- `body` / `header` — plain paths into the **previous step's** output, no `$` prefix (e.g. `body.hello`).
- `$context` — a bound JSONata variable for the whole run context, e.g. `$context.data.trigger.body[0]."Order Number"` (bracket/quote a field name containing spaces).
- To reference a **specific** earlier step by id rather than "the previous step": `$context.data.all.<stepId>...`. An M3 API step's result is the array directly (`$context.data.all.m3_api_1`, no `.body`); a REST API step's result is wrapped as `{ headers, body }` (needs `.body` to reach the payload) — don't assume the same binding convention across step types.
- The builder's own preview pane (pasting sample JSON into "Input") binds it to bare `$.<stepId>`, **not** `$context` — only a real saved/executed run resolves `$context.data.all.<stepId>`. Forgetting to swap before saving for real execution produces a silent blank result, no error.
- Multi-statement semicolon-chained variable-binding blocks (`( $x := ...; $y := function(){...}; ... )`) — valid JSONata — **fail to parse in this specific editor** (`Expected "}", got ";"`). Use a single nested expression instead.
- **Real observed risk**: save-time validation is loose. A malformed template can have its content silently disappear/reset rather than failing to save with a clear error.

M3 API step runtime field naming (relevant when a Transform reads M3 API output directly): for transaction instance `<id>`, a declared output field `X` appears as `<id>_X`; if `X` is also an input on the same transaction, its input echo appears as `<id>_I_X`. Padding/incomplete rows can appear with every output field `null` — filter using the mandatory input-echo field (`_I_<name>`), not an output field, to reliably exclude them. `maxRows: 0` means unlimited, not zero.

### REST API step (`stepConfig["rest_api_N"]`, `type: "GENERIC_API"`)

**The stepConfig is a `content` STRING holding the config as JSON — not the config's keys sitting flat in stepConfig.** Confirmed by a GET of a hand-built workflow:

```json
"rest_api_1": {
  "content": "{\n  \"version\": 2,\n  \"endpoint\": \"https://...\",\n  \"method\": \"post\",\n  \"headers\": {\n    \"content-type\": \"application/json\"\n  },\n  \"bodyPath\": \"$.payload\"\n}"
}
```

That same workflow's `definitionAsl` parses the string back out and hands the proxy lambda (`vl-sls-connection-generic-rest-api-prod-proxy`) `'stepConfig': {"content": {"version": 2, ...}}`, so **a flat stepConfig leaves the proxy with no content at all**. The builder writes the string with 2-space indentation.

**This asymmetry is real and easy to get wrong:** a `TRANSFORMER_MORPH` step's config in the very same workflow *is* flat (`{"template": ..., "trimAll": true}`). Do not generalise either shape to the other step type.

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
- `connectionId` is **optional** — confirmed via the builder's own "Use connection" toggle, off when absent (plain external URL, no stored Vince connection). When present, Live looks up the stored connection and **attaches its auth to the request automatically** — you never build a token yourself. Confirmed in product documentation and in four real customer workflows.
- `bodyPath` is a JSONata expression selecting the request body from the previous step's output (`"$"` = whole previous output, `"$.body"` = just its body, `"$.payload"` = a named sub-object, which is what the real M3 examples use).
- **`body` (literal-body mode) is now CONFIRMED** — an object, sibling to the others, carrying `{{ body.x }}` / `{{ header.x }}` placeholders substituted at execution from the request that triggered the workflow (the same `{{ }}` mechanism as the Generic Filter step's `{{ header.Filter_BD }}`). Set either `bodyPath` or `body`, not both.
- **`"version": 2` is CONFIRMED and in current production use.** It is written first in every real example ("append to top for easier readability") and unlocks two features:
  - **`forEach`** — a JSONpath to an array (e.g. `"body.records"`); the step then makes one sequential call per record, and its output changes from a single `{headers, body}` object to an **array** of them. Inside a `forEach`, an `endpoint` placeholder reads the *current record* wrapped in `body` (`{{ body.id }}`), while `bodyPath` addresses the current record's root. Documented as **beta**, with self-described "a little inconsistent" variable naming, and its own doc's example says `"version": 3` while the instructions say `2` — **no captured example; test before building on it.**
  - **`skipIfExpression`** — skip the request when a condition isn't met. The property **name** is confirmed; its **expression syntax and truthiness semantics are documented nowhere**. Probe before relying on it.
- Binary responses are steered by a `content-disposition` request header: absent/`inline` returns `{type: "Buffer", data: [...]}` plus a `fileKey`; `attachment` returns `body: null` plus a `fileKey`; `base64` returns a base64 string.
- ~~**Unresolved observation**: a real POST with `endpoint`/`bodyPath` set was echoed back correctly in the API response, but the builder UI displayed both as empty placeholders.~~ **RESOLVED** — that POST wrote the config as flat stepConfig keys, and the builder reads a REST API step's config out of the `content` string described above. Nothing was wrong with the key names; the wrapper was missing. (Why `method`/`headers` appeared to render anyway was never chased down and no longer matters.)
- `"version": 3` is **accepted** by the builder — a hand-built step saved with it and compiled into a working state machine. What it changes versus 2 is undocumented and unconfirmed; the `forEach` doc page hints v3 exists to make variable naming uniform. Production workflows all use 2.

### Code step (`stepConfig["code_N"]`, `type: "GENERIC_CODE"`)

```json
{ "content": "module.exports = async function (prevStep, allSteps, trigger) { /* ... */ };" }
```
Confirmed sandboxed `require()`-able modules: `authorizedAxios` and `getContext` —
```js
const connection = await getContext('globals.connection', CONNECTION_ID);
const axios = await authorizedAxios(connection);
```
— a third way to reach a stored connection, alongside REST API's own `connectionId`.

---

## 1b. Calling M3 WITHOUT the M3 API step (ION API bulk execute)

Confirmed from product documentation plus four real production customer workflows. This is how Vince itself builds M3 workflows needing logic the M3 API step can't express — including **per-call** filtering, which `GENERIC_FILTER` cannot do (it runs once, before the whole M3 API step). One real example performs full M3 **writes** this way (`AddBatchHead` → `AddBatchHeadChg` → `AddBatchLine` → `Confirm`) across seven Transform/REST API pairs, with no M3 API step and no Excel step.

The idiom is a repeated pair: a **Transform** that builds `{"payload": {...}}`, then a **REST API** step that posts it.

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

Region and `<TENANT>` are **tenant-specific and not derivable** — copy them from a real REST API step in that tenant. Observed real values: `.../DEMO_TENANT/...`, `.../TEST_TENANT/...`.

The endpoint carries no program/transaction: **one call carries many M3 transactions**, which is the point (M3 needs eight to read one order).

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

`cono`, `maxReturnedRecords` and `selectedColumns` are optional. Response, confirmed against a real captured payload:

```json
{
  "statusCode": 200,
  "body": {
    "nrOfSuccessfullTransactions": 7,
    "nrOfFailedTransactions": 0,
    "wasTerminated": false,
    "results": [ { "transaction": "GetOrderHead", "records": [ { "ORNO": "0010000386" } ] } ]
  }
}
```

Reading it from a later step:
```jsonata
$context.data.all.rest_api_1.body.results.records.CUNO       /* flattened across ALL transactions */
$context.data.all.rest_api_1.body.results[transaction="GetHead"].records[0].CUNO
```
That `.body` is the REST API step's own `{headers, body}` wrapper — an M3 API step's result is the array directly. Don't carry the convention across step types.

Building the payload from rows (confirmed real Transform):
```jsonata
(
  $data := $context.data.trigger.body;
  { "payload": { "program": "OIS100MI",
                 "transactions": $data.{ "transaction": "GetHead",
                                         "record": { "ORNO": OrderNumber } }[] } }
)
```
The trailing `[]` forces an array even for one row. A filter goes on `$data` as an ordinary predicate (`...body[Approved = "OK"]`) — that is how you gate one M3 call while the next runs unconditionally.

**Correlating one call's results with another's**: the real workflows join by index (`records[$i]`, via `$map`/`$filter` with the index parameter). That alignment holds only while the earlier call returned exactly one record per input row, in order — `nrOfFailedTransactions` exists precisely because M3 can drop some, and any row-filtering between two calls breaks it. Carry the key field and join on it when it matters.

Full write-up, spec format and generator: `docs/vince-generic-m3-pipeline.md`.

### The `VXL` label — spreadsheet I/O with no Excel step

`excel_1` cannot be used outside an M3 API chain (its columns only bind to `m3_api_1` instance ids, and attaching it elsewhere broke saving for real). Instead **add the label `VXL` to the workflow** — confirmed wording: *"Create a new workflow and add the label `VXL` to it - this enables the workflow to be visible in VXL Live"*, alongside *"All workflows with M3 API and Excel steps are already available in VXL Live"*. In the JSON that is the top-level `labels` array (`"labels": ["VXL"]`) — the label→`labels[]` mapping is a reading of that wording, so confirm it against a GET of a real VXL workflow.

The same `labels` array also carries `"Imported"` on every workflow VinceForge creates that reads from the spreadsheet (omitted when no input is sheet-fed, since 2026-10-08 - it was reported to break saving) (`converter/envelope.py::IMPORTED_LABEL`) — an ordinary second string in the same list, composing rather than replacing (e.g. `["VXL", "Imported"]`). Not confirmed to mean anything to the platform itself (unlike `VXL`, which gates real UI behavior) — it's this tool's own marker, added so a VinceForge-created workflow is distinguishable from a hand-built one.

The **final step's output must be export-shaped**, per confirmed hard rules: an array of objects; every object carrying the same property keys; `""` rather than `null` for a blank. Ragged rows are the failure mode.

### A workflow is also a synchronous API

```
POST /live/workflows/{workflowId}/sync
Header: vl-wf-control-return-data: true      <- without it you get execution metadata, not the data
Body:   { "orderNumber": "0010000386" }
```
Returns the **last step's** output. **AWS API Gateway caps it at 29 seconds** — a real limit on how much sequential work a Code step can afford. Request body/header values reach a REST API step as `{{ body.x }}` / `{{ header.x }}`.

Human-readable URLs come from **Custom APIs**: `POST /v1/custom-apis/control` with `{endpoint, tenantId, basePath, config: {target: {resource: "WORKFLOW", identifier: "WORKFLOW-...", parameters: {"vl-wf-control-return-data": true}}, source: {contentTypes, httpMethod}}}` → `GET /v1/custom-apis/live/{basePath}/{endpoint}`. Not exposed in the UI; permissions must be PATCHed onto a role as `vrn:<TENANT>:fnd:custom-api:<basePath>_<endpoint>` — and a PATCH **replaces** the role's permission array, so send the whole thing with your addition appended.

---

## 1c. M3 field metadata endpoint

What powers "pick a transaction, watch its fields populate" in Live's builder, and the only way to get real `descr`/`type`/`length`/`mandatory` per field. Confirmed by direct runs against a real connection.

```
GET https://api.vince.live/metadata/{connectionId}?details=FIELDS%23{program}%23{transaction}
```

The `details` value is `#`-separated and **must be URL-encoded** (`%23`). Confirmed behaviour of each shape:

| `details` | Result |
|---|---|
| `FIELDS#<prog>#<txn>` | **Use this.** One item per direction that exists — `count: 2` for `MRS001MI/LstAllTrans` (I + O), `count: 1` for `BUS100MI/DelBudgetLines` (I only) |
| `FIELDS#<prog>#<txn>#<I\|O>` | One direction. Works, but doubles the calls for no benefit |
| `FIELDS#<prog>` | Every transaction in the program at once (`count: 24` for BUS100MI) — **but see the trap below** |
| `TRANSACTIONS#<prog>` | `count: 0` — not supported, or differently named |
| `PROGRAMS` | `count: 0` — same |
| *(omitted)* | HTTP 400 `Missing required request parameters: [details]` |

Response shape:

```json
{"count": 2, "items": [
  {"name": "I", "descr": "Input",  "data": [
    {"name": "CONO", "type": "N", "length": "3", "mandatory": "0", "descr": "Company"}]},
  {"name": "O", "descr": "Output", "data": [ … ]}]}
```

`type` codes: `A` → STRING, `N` → NUMERIC, `D` → NUMERIC (`D` mapped by inference, not confirmed against a real Live field). `mandatory` is the string `"1"`/`"0"`.

**Two traps, both of which have cost real time:**

1. **An empty direction is normal, not a failure.** `BUS100MI/DelBudgetLines` returns 14 input fields and `count: 0` for output — because a delete returns nothing. Only treat a transaction as undescribed when it has **no fields in any direction**. Warning per-direction produces a scary false alarm on every Add/Upd/Del transaction.
2. **The whole-program shape cannot be attributed to transactions.** `FIELDS#<prog>` returns items carrying only `['data', 'descr', 'name']`, where `name` is just `"I"`/`"O"`. Nothing identifies *which* transaction each item belongs to, so despite being far fewer calls it is unusable for per-(program, transaction, field) keying. Position might correlate with transaction order — that is a guess, and untested.

Implemented in `converter/metadata.py::fetch_live_metadata` (one call per transaction). `cli.py probe-metadata … [--explore]` and `probe-metadata-program` interrogate it directly.

---

## 1d. Groups API

Bundles a set of existing workflows into a named "Group" so they open together in VXL Live — confirmed 2026-09-16 by a real create, captured verbatim (not guessed from the public product doc alone, though that corroborates the intent — see below).

```
POST https://api.vince.live/groups
```

Request body (real capture):

```json
{
  "groupName": "PIPO",
  "groupAlias": "PIPO",
  "entities": [
    {"type": "Workflows", "id": "WORKFLOW-5b13264663a889445da955c71f7acde3", "sortX": 0, "sortY": 1},
    {"type": "Workflows", "id": "WORKFLOW-ea765e309b61d35b08af99b4f486d6e6", "sortX": 0, "sortY": 2},
    {"type": "Workflows", "id": "WORKFLOW-b8eecf3536e3825d7b4642ee2f34c987", "sortX": 0, "sortY": 3}
  ],
  "tenantId": "TENANT-35d55a49989f4caa00a5745a4af59cc2"
}
```

Response (real capture, same request):

```json
{
  "groupId": "GROUP-520b19b7e14fb6340cc34a63fa8b8d0f",
  "tenantId": "TENANT-35d55a49989f4caa00a5745a4af59cc2",
  "entities": [ /* same shape as the request, echoed back */ ],
  "createdBy": "USER-98d0ca19add76638540bfc3691fc7ac4",
  "entity": "Group",
  "created": "2026-09-16T10:43:02.577Z",
  "modified": "2026-09-16T10:43:02.577Z",
  "groupName": "PIPO",
  "groupAlias": "PIPO"
}
```

Notes:

- `entities[].id` must be a real, already-created `WORKFLOW-...` id — a Group is built from workflows that already exist, not created alongside them.
- `sortX`/`sortY` look like a 2D layout position for the group's own listing UI; the captured example is a single column (`sortX: 0` throughout, `sortY` 1-based and sequential). Untested: what a non-zero `sortX` or a gap in `sortY` does.
- **`roles` did NOT appear in the captured request**, though `vincesoftware.com/product-documentation/groups` (read as raw HTML, not just an AI-summarized fetch) documents a "Roles" dropdown at creation time ("Only users assigned to these roles will have access to the group") — so role assignment is either a separate call, an omitted-when-empty field, or something else entirely. **Unconfirmed — don't guess it.** The rest of that same public doc corroborates the feature's intent and is worth citing for behavior, not for JSON shape: "Groups help manage workflow access by assigning a collection of workflows and roles to a named group" / "organize multiple workflows into a single collection, making it easier to manage and execute related workflows together" / "When a user selects the group in VXL Live, all workflows in that group are available for execution in sequence."
- No GET/list/update/delete endpoint for Groups has been captured yet — only this one create call. The product doc describes View/Edit/Delete actions on a "Groups Listing" page, so they almost certainly exist; treat their shapes as unconfirmed until captured the same way.

Implemented in `converter/liveapi.py::create_group`. `appimport.py` calls it once per source Classic XML file that produced 2+ successfully-created workflows — see CLAUDE.md's App import section.

---

## 1e. Execution results — what a run actually did (CONFIRMED 2026-09-28)

Real GETs against the user's tenant, with the ordinary Cognito ID token:

```
GET https://api.vince.live/v1/workflows/<WORKFLOW-id>/results                       -> {count, total, items:[{executionId, startTime, endTime, status, failed, error, ...}]}
GET https://api.vince.live/v1/workflows/<WORKFLOW-id>/results/<WFEXEC-id>           -> the whole execution record
GET https://api.vince.live/v1/workflows/<WORKFLOW-id>/results/<WFEXEC-id>/data      -> the final output rows ([] when nothing came out)
```

The execution record's `payload` holds the run's actual input — `trigger.header` (the trigger/filter field values, as strings) and `trigger.body` (the sheet rows, keyed by column letter, numbers as JSON numbers) — and `payload.result.history`, **one entry per step, latest step first**: `{stepId, success, duration, recordsProcessed, data, files}`. `data` is that step's own output, so `generic_filter_1.data` is exactly the rows the filter let through, and the M3 API step's rows carry `<txnId>_MSG_COL` with M3's per-row message (e.g. `"Customer 9 does not exist (CRS610MI/Add)"`). This is the only confirmed way to see where a run passed or dropped rows.

**Not** as the public docs' path list implies: `GET /v1/workflows/results/<WFEXEC-id>` (no workflow id) answers **404**, and the old `/live/worfklows/<id>/results/<exec>` answered **403** with this token.

Also seen on the captured records: the execution RECORD carries `endTime` but no `startTime` (the list items have both). Still unconfirmed: whether the results list pages (it answers `{count, total, items}`; `converter/runs.py` flags count < total as a partial listing rather than guessing a parameter), and the list's `status` vocabulary.

**Listing every workflow — `GET /v1/workflows/` truncates silently at 200.** Confirmed by the sibling VincePrompter project against a real 1,990-workflow tenant (its `docs/appbuilder-api-confirmed.md`), not re-verified here: no `total`, no error. `limit` is the page size and `startKey` (fed the previous page's `lastKey`) the cursor; `size`/`pageSize`/`offset`/`page`/`nextToken` are all ignored. List items carry `stats.numRuns`; there is also `GET /v1/workflows/{id}/stats` → `{numRuns, numRunsSuccess, …}`. `liveapi.list_workflows` is the one place this project lists workflows, and it pages.

## 2. Custom Tables API

Confirmed real via direct testing (creating a real table, writing 24,154+ rows via a Table Updater workflow step, reading it back). A **standalone** resource — a workflow only *writes* via a Table Updater step; reading does not require a workflow at all.

### Base

```
https://api.vince.live/v1/custom-tables/...
```
An older `/live/custom-tables/...` path appears in some docs as a legacy/alternate form — `/v1/` is the confirmed-working prefix in production. `tenantId` is populated server-side from the authenticated session; do not send it in request bodies.

### Create a table

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
      "Program": { "type": "string", "title": "Program" },
      "Transaction": { "type": "string", "title": "Transaction" }
    }
  },
  "displayOrder": ["Program", "Transaction"]
}
```
- `tableName` gets slugified (hyphens, not underscores).
- **`primaryKeys` is immutable after creation** — pick wrong and every write with a duplicate key **silently overwrites** the previous row (no error). Identify the smallest field set that's actually unique per source record before creating; test with a sample and check the resulting row count against expected input count.
- `updateType` ∈ `UPSERT` (update-or-create, default choice) | `REPLACE` (full snapshot replace) | `APPEND` (non-unique keys allowed, logging-style).
- `schema` (standard JSON Schema) is the current, confirmed-working way to define columns. **`columnConfig` (older docs, `aliasName`/`dataType` shape) is deprecated — a request using it was rejected in testing.**
- `enforceSchema: false` = no write-time validation (index whatever's written); `true` = reject rows not matching `schema`. Start with `false`.
- **Known flaky/misleading error, not fully understood**: the exact same valid body was rejected twice with a 400 whose `validationError` lists required properties (`definition`/`tenantId`/`workflowAlias`/`workflowId`) that belong to a *workflow* object, not a table — looks like a misrouted validator. The identical body succeeded on retry with no changes. If you hit this, retry before assuming your body is wrong; root cause not confirmed.
- `PATCH https://api.vince.live/v1/custom-tables/meta/{tableName}` updates config (`enforceSchema`, adding `schema` fields) — not `primaryKeys`.
- Deletion has up to a **6-hour delay** before actual removal; a same-named table can't be recreated until it completes, even though data becomes inaccessible immediately.

### Reading a table's definition — NOT CONFIRMED

No endpoint is confirmed to return a table's `primaryKeys`/`schema`/
`updateType`. `POST`/`PATCH` write it; the confirmed reads below return rows
and counts only. Do not assume `GET /v1/custom-tables/meta/{tableName}`
works — it is the *plausible* candidate (PATCH on that path is confirmed),
not a verified one. If you probe it, validate that the body actually names
the table and carries a meta field before believing a 200.

### Reading data

Point lookup by composite primary key:
```
GET https://api.vince.live/v1/custom-tables/data/{tableName}?records.{key1}={value1}&records.{key2}={value2}
```
Response items carry `__`-prefixed metadata (`__id`, `__created`, `__modified`, `__modifiedBy`) always. `__id` is base64 of `{tableName}#0#DATA#{primaryKeyValue1}#{primaryKeyValue2}...` — confirmed by decoding a real example. Multiple records in one call: `records[0].Program=X&records[0].Transaction=Y&records[1]...`. URL length capped at 8192 bytes (per docs, not independently re-verified).

By single key + `__id`: `GET .../data/{tableName}/{id}` — documented, not independently tested.
By prefix: `GET .../data/{tableName}?beginsWith={prefix}` — UPSERT tables only, respects primary key order, concatenate multi-key prefixes with `#` — documented, not independently tested.

Bulk/paginated: `GET https://api.vince.live/v1/custom-tables/search/{tableName}?size=10&scrollMinutes=5` — returns a `scrollId` plus first page; repeat with `?scrollId=...` for subsequent pages (other params ignored once present). Documented, not independently tested this way, but likely the right endpoint for syncing a whole table rather than point lookups.

Row count validation: `GET https://api.vince.live/v1/custom-tables/aggregated/{tableName}` → `{"count": N, "aggregations": {}}` — fast way to confirm a bulk write landed the expected row count, updates near-real-time during an in-progress write.

**Known UI quirk**: the Table Management UI can show `Columns: 0` for a table created with a full `schema` and real data — looks like it still reads column metadata from the deprecated `columnConfig` rather than `schema`. Cosmetic; data/querying unaffected.

---

## 3. Confirmed to exist, but not usable yet — don't build against these

- **A separate "Vince Platform API"** (OAuth2 `client_credentials`, Users/Roles/API Clients/Tables/Tags/Schedules/Variables) — the *mechanism* is real (confirmed via a genuine Access Management → API Clients screen and a real "self" connection's Token URI/Base URI), but both the token endpoint and base URI are tenant-specific and not derivable — do not guess a domain or assume this is the same thing as the `api.vince.live` surface documented above without independently verifying an actual call.
- **App Builder** — zero confirmed facts of any kind. Do not invent a prompt format, schema, or API shape for it.

## How to extend this document

Same methodology throughout: build a minimal real example in Live's UI or hit the real API directly, capture what actually comes back, and write down only what's confirmed — marking anything uncertain as such rather than presenting a guess as fact. A wrong guess at an enum/path value from Live's own validator (a real 400 with a specific message) is useful signal; a plausible-sounding guess that happens to return 200 is not proof it's correct until the result is independently confirmed (e.g. render/execute it, or decode/verify a specific field as done above for Custom Tables' `__id`).
