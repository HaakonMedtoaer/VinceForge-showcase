# Auth, endpoint catalog, labels, and the consolidated "do not build against this" list

## 1. Auth and the tenant model

- **Confirmed API base: `https://api.vince.live`.** A different-looking
  domain (e.g. one ending `.dev`) is not a variant to try — a prior
  reference document confidently described `api.vincelive.dev`, which has
  zero DNS records and was fabricated. Verify reachability before trusting
  any URL not in this skill's reference files.
- **Bearer token:** a short-lived Cognito **ID token**, captured by hand
  from Live's own web UI (DevTools → Network → any authenticated request).
  Sent as `Authorization: Bearer <token>`. Expires after roughly an hour of
  idle session (`401 {"message":"Unauthorized"}` — expected, not a bug).
  Normalise a pasted token before use: strip whitespace, surrounding
  quotes, a stray `Bearer ` prefix, and internal newlines from a wrapped
  copy — all three produce the same unexplained 401.

### ⚠️ The token decides the tenant — and a mismatch fails SILENTLY

**The token is the authority.** It decides who you are *and* which tenant
you act on. `tenantId`, `connectionId`, `environmentId` in a request body
are **payload, not routing** — references to resources living inside the
token's tenant.

- Using a token from tenant A against tenant B's `connectionId` does **not**
  return 401 — the call succeeds and the connection is simply invisible, so
  `/metadata/{connectionId}` answers `{"count": 0, "items": []}`, which is
  indistinguishable from "M3 cannot describe this transaction." This has
  cost a real, long debugging session.
- Anything created lands in the **token's** tenant, not the one named in the
  request body.
- Custom Tables independently corroborates this model: `tenantId` is
  populated server-side and not sent at all; `GET /workflows/{id}` needs no
  ids either.
- **Open question, never deliberately tested:** whether `POST /workflows`
  ignores the body's `tenantId` (as Custom Tables does) or validates it
  against the token.

**Always compare the tenant in the token's own claims against the tenant
being targeted before treating any call's result as meaningful.** The
tenant id is a claim shaped `TENANT-<32 hex>`; match on the value's shape,
since the claim name itself is not confirmed.

### A second, real but not generically usable auth mechanism

OAuth2 `client_credentials` via an **API Client** (Live UI → Administration
→ Access Management → API Clients). Confirmed from a real "self" connection
screen: `Token URI` shaped
`https://tenant-<hex>.auth.<aws-region>.amazoncognito.com/oauth2/token`,
`Base URI` shaped `https://<tenant-slug>.vince.live/`. **Both are
tenant-specific and not derivable from a formula** — get them from that
tenant's own connection screen. A separately-referenced "Vince Platform
API" (Users/Roles/API Clients/Tags/Schedules/Variables, OAuth2-secured) may
or may not be the same surface — **NOT CONFIRMED**, do not conflate it with
`api.vince.live`.

## 2. Endpoint catalog (all confirmed real, as named)

| Purpose | Endpoint |
|---|---|
| Create a workflow | `POST https://api.vince.live/workflows` |
| Read a workflow | `GET https://api.vince.live/workflows/{workflowId}` |
| Run a workflow synchronously | `POST https://api.vince.live/live/workflows/{workflowId}/sync` (header `vl-wf-control-return-data: true` needed to get the data back, not just execution metadata; returns the **last step's** output; AWS API Gateway caps this at **29 seconds**) |
| Get a download URL for a large result | `GET https://api.vince.live/live/worfklows/{workflowId}/download-url` (misspelling `worfklows` reproduced as recorded — verify before using) |
| M3 field metadata | `GET https://api.vince.live/metadata/{connectionId}?details=FIELDS%23{program}%23{transaction}` — see `m3-native-api-step.md` §5 |
| Request a design-file (Excel) upload URL | `GET https://api.vince.live/v1/files/tenant/upload-url/{WORKFLOW-id}/designFile/{filename}.xlsx` — see §5 below |
| Custom Tables — create/update meta | `POST` / `PATCH https://api.vince.live/v1/custom-tables/meta[/​{tableName}]` — see `custom-tables.md` |
| Custom Tables — read data | `GET https://api.vince.live/v1/custom-tables/data\|search\|aggregated/{tableName}` — see `custom-tables.md` |
| Create a Group (bundles workflows for VXL Live) | `POST https://api.vince.live/groups` — see §6 below |
| Human-readable custom API URL | `POST /v1/custom-apis/control` → `GET /v1/custom-apis/live/{basePath}/{endpoint}` — see §7 below |

## 3. Sanity rule that applies to every call

**HTTP 200 is not proof of correctness.** A malformed `tenantId` /
`connectionId` / `environmentId` passes creation silently and only breaks
rendering later in the builder (a frontend `TypeError`, not a server
error). After anything is created: read it back, and confirm it *renders*
and — separately — *executes*. **A wrong guess at an enum/path value often
produces a real, useful 400** (e.g. an invalid step `type` returns
`"WorkflowDefinition is invalid! ... has invalid type: X"`) — that
confirms the value is wrong, but does **not** by itself reveal the correct
one; don't assume the first plausible correction is right either.

## 4. The `VXL` and `Imported` labels

`excel_1` cannot be used outside an M3 API chain (see
`m3-native-api-step.md`). For spreadsheet I/O with no M3 API step, add the
label `VXL` to the workflow — confirmed wording from product documentation:
*"Create a new workflow and add the label `VXL` to it - this enables the
workflow to be visible in VXL Live"*, and *"All workflows with M3 API and
Excel steps are already available in VXL Live"*. In JSON this is read as
the top-level `labels` array (`"labels": ["VXL"]`) — **this specific
JSON mapping is a reading of the documented wording, not itself an
independent capture; confirm against a real GET of a VXL workflow before
relying on it.**

The final step's output must be **export-shaped** for VXL/Excel purposes,
per confirmed hard rules: an array of objects, every object carrying the
same property keys, `""` rather than `null` for a blank. Ragged rows are
the failure mode.

`"Imported"` has also been seen as a second label, composing with `VXL`
(`["VXL", "Imported"]`) — it is a tooling-added marker (not confirmed to
mean anything to the platform itself, unlike `VXL` which gates real UI
behaviour). Safe to omit from a hand-drafted workflow; mention it only if
distinguishing tool-created workflows from hand-built ones is relevant.

## 5. Design file (Excel sheet) upload — CONFIRMED

Two steps; the file **never passes through `api.vince.live` itself**.

**1. Ask for a presigned upload URL:**
```
GET https://api.vince.live/v1/files/tenant/upload-url/{WORKFLOW-id}/designFile/{filename}.xlsx
Authorization: Bearer <token>
→ 200 { "filename": "...", "url": "https://files.vince.live/...", "expiresAt": "...(~5 min)", "method": "PUT", "headers": {"Content-Type": "*/*"}, "isWildcard": false }
```

**2. `PUT` the raw file bytes to the returned `url`** with
`Content-Type: */*` and **no `Authorization` header** — the signature is in
the query string.

**What lands in the workflow JSON:** a before/after GET differs by exactly
two fields in `definition.workflowConfig`:
```json
"hasExcelDesignFile": true,
"excelDesignFileKey": "WORKFLOW-<id>/designFile/<filename>.xlsx"
```
`excelDesignFileKey` is byte-identical to the upload-url response's own
`filename` — read it back from there. Sequencing consequence: the key
embeds the workflow id, and Live assigns its own id at creation, so a
design file can only be attached **after** the workflow exists and its
real id has been read back — there is no way to include one in the initial
POST.

**❓ Unresolved:** what actually *writes* `hasExcelDesignFile`/
`excelDesignFileKey` — the server on PUT completion, or a client-issued
workflow update afterwards (and no workflow `PUT`/`PATCH` endpoint is
confirmed at all). After uploading, GET the workflow back and check rather
than assuming either.

## 6. Groups API — bundling workflows for VXL Live

Confirmed by a real create call.

```
POST https://api.vince.live/groups
```
```json
{
  "groupName": "PIPO",
  "groupAlias": "PIPO",
  "entities": [
    {"type": "Workflows", "id": "WORKFLOW-<32 hex>", "sortX": 0, "sortY": 1}
  ],
  "tenantId": "TENANT-<32 hex>"
}
```
Response echoes the request plus a server-assigned `groupId`
(`GROUP-<32 hex>`), `createdBy`, `entity: "Group"`, `created`/`modified`.
`entities[].id` must be a real, already-created workflow id — a Group is
built from workflows that already exist. `sortX`/`sortY` look like a 2D
layout position (captured examples use a single column, `sortX: 0`
throughout, `sortY` sequential from 1) — untested what a non-zero `sortX`
or a gap in `sortY` does. **`roles`-based access assignment is documented
in product docs but did NOT appear in the captured create request** — how
role assignment actually happens is unconfirmed; don't invent a field for
it. No GET/list/update/delete endpoint for Groups has been captured.

## 7. Human-readable workflow URLs (Custom APIs)

```
POST /v1/custom-apis/control
{
  "endpoint": "...", "tenantId": "...", "basePath": "...",
  "config": {
    "target": { "resource": "WORKFLOW", "identifier": "WORKFLOW-...",
                "parameters": { "vl-wf-control-return-data": true } },
    "source": { "contentTypes": [ "..." ], "httpMethod": "..." }
  }
}
→ GET /v1/custom-apis/live/{basePath}/{endpoint}
```
Not exposed in the UI. Permissions must be PATCHed onto a role as
`vrn:<TENANT>:fnd:custom-api:<basePath>_<endpoint>` — and that PATCH
**replaces** the role's whole permission array, so send it in full with the
addition appended.

## 8. The consolidated "do not build against this" list

Everything here is either genuinely unconfirmed or explicitly known-untrue.
If a design needs one of these, say so plainly and name the verification
path (build a minimal real example and capture it, or hit the real
endpoint and read the response/error) rather than filling the gap.

- **The Table Updater step's JSON shape.** No real capture exists. Blocks
  any workflow-drafting task that needs to write Custom Table rows.
- **Any row-write endpoint for Custom Tables.** None is confirmed to exist
  at all; the Table Updater step is the only confirmed mechanism.
- **Reading a Custom Table's definition** (`primaryKeys`/`schema`/
  `updateType`) — no endpoint is confirmed; see `custom-tables.md` §7.
- **A workflow `PUT`/`PATCH` endpoint.** Only `POST /workflows` and
  `GET /workflows/{id}` are confirmed. A "hit Save" PUT was once observed
  in passing but its URL and body were never recorded.
- **`forEach`** on a `version: 2` REST API step — documented but beta, no
  captured example, and its own product doc contradicts itself on the
  version number.
- **`skipIfExpression` syntax and truthiness semantics** — only the
  property *name* is confirmed.
- **What `"version": 3` changes** on a REST API step. Accepted by the
  builder and compiles; what it does differently from `2` is undocumented.
  Production examples all use `2`.
- **`labels: ["VXL"]`** as the JSON form of the VXL label — a reading of
  documented wording, not itself a direct capture.
- **M3 type code `D` → `NUMERIC`** — inference (M3 stores dates as numeric
  `YYYYMMDD`), not confirmed against a real Live field.
- **The multi-character prefix query form** (`q=Program:CMS*`) on the
  Custom Tables search endpoint.
- **A separate "Vince Platform API"** — the *mechanism* (OAuth2
  client_credentials via an API Client) is real, but its token
  endpoint/base URI are tenant-specific and not derivable; do not assume it
  is the same surface as `api.vince.live`.
- **App Builder** — **zero** confirmed facts of any kind: no known prompt
  format, schema, or API shape. Never invent one. If a task involves
  producing something for App Builder, the honest output is a plain
  restatement of what was built/created (workflow ids, table names, field
  lists) under an explicit "this format is not confirmed to be what App
  Builder expects" caveat — not a fabricated structured format.
- **A widget-click trigger's `triggerType` value.** Only `CLIENT` (form
  submit) and `SCHEDULE` are confirmed; a widget-click trigger has been
  referenced in a customer's own separate documentation but not
  independently confirmed here.
- **Whether Live validates a `POST /workflows` body's `tenantId` against
  the token**, or silently substitutes the token's tenant the way Custom
  Tables does. Untested either way.
- **The Table Updater step is not the only capability gap** — anything not
  covered by this skill's reference files at all should be treated the
  same way: flagged, not guessed. This skill's files are a curated subset
  of confirmed facts, not a claim of completeness.
