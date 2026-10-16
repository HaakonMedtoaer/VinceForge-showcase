# Vince Live Custom Tables — Reference Guide

Confirmed via real Postman calls against `https://api.vince.live` on 2026-08-26, cross-referenced with the product docs. Everything below marked **CONFIRMED** was tested and worked end-to-end (creating `m3-api-catalog`, writing 24,154 source rows via a Table Updater workflow step, reading it back). Anything not confirmed live is marked accordingly — don't treat those as verified fact.

## Base URL

```
https://api.vince.live
```

Older product docs reference `https://{{env-apiDomain}}/live/custom-tables/...` — that `/live/` prefix appears to be a legacy/alternate path. The confirmed-working prefix in production is `/v1/custom-tables/...`. Use `/v1/` unless you have a specific reason to test `/live/`.

`tenantId` is **not** required in the request body — it's populated server-side from the authenticated session automatically (confirmed: it did not need to be sent, and the response included it).

---

## Creating a table — CONFIRMED

```
POST https://api.vince.live/v1/custom-tables/meta
Content-Type: application/json
```

### Working request body (verbatim, tested)

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
            "Transaction": { "type": "string", "title": "Transaction" },
            "Description": { "type": "string", "title": "Description" },
            "Type": { "type": "string", "title": "Type" },
            "Status": { "type": "string", "title": "Status" }
        }
    },
    "displayOrder": ["Program", "Transaction", "Description", "Type", "Status"]
}
```

### Field notes

| Field | Required? | Notes |
|---|---|---|
| `tableName` | Yes | Gets slugified (spaces/special chars → hyphens). Confirmed slug pattern uses **hyphens**, not underscores. |
| `primaryKeys` | Yes | Array of field names that make a record unique. **Cannot be changed after creation** — if you need different keys, create a new table. See "Choosing primary keys" below. |
| `updateType` | Yes | `UPSERT` (update-or-create, default choice), `REPLACE` (full snapshot replace on update), `APPEND` (non-unique keys allowed, logging-style). |
| `schema` | Optional but recommended | Standard JSON Schema. Confirmed as the current/working way to define columns. |
| `enforceSchema` | Optional | `false` = no validation, index whatever's written. `true` = strict validation on write (rejects rows that don't match `schema`). Start with `false` unless you specifically need write-time validation. |
| `displayOrder` | Optional | Array of field names controlling column order in the Vince Live Table Management UI. |
| `columnConfig` | **Deprecated / do not use** | An older doc describes this as the way to define columns (with `aliasName`/`dataType`). In testing, a request using `columnConfig` was rejected — `schema` is the current mechanism. |
| `description` | Optional | Free-text table description. |

### ⚠️ Known flaky/misleading error behavior — NOT FULLY UNDERSTOOD

During testing, the exact same valid request body was rejected twice with:

```json
{
  "statusCode": "400",
  "validationError": "[object instance has properties which are not allowed by the schema: [...], object has missing required properties ([\"definition\",\"tenantId\",\"workflowAlias\",\"workflowId\"])]"
}
```

The required-properties list (`definition`, `tenantId`, `workflowAlias`, `workflowId`) looks like it belongs to a **workflow** object, not a table meta object — this may be a misrouted/misleading validator error rather than a real body problem. The identical body succeeded on a subsequent attempt with no changes. **If you hit this, retry before assuming your body is wrong.** Root cause not confirmed; do not build retry-suppression logic around this without further investigation.

### Choosing primary keys — hard-won lesson

Primary keys must uniquely identify a row for `UPSERT`. Get this wrong and every write with a duplicate key **silently overwrites** the previous row — no error, so it can go unnoticed until you check row counts.

Confirmed failure case: a table for M3 API transactions (`Program`, `Transaction`, `Description`, `Type`, `Status`) was created with primary key `["Program"]` alone. Since one Program routinely has multiple Transactions (e.g. `Add`, `Upd`, `Get`, `List` all under the same program), every write for a repeat Program clobbered the prior one. Source data had 24,154 rows; the table settled at only ~1,330 (one row surviving per unique Program).

Fix: recreate the table with the correct composite key, `["Program", "Transaction"]`. After the fix, the same 24,154-row source settled at 22,535 rows — the small gap from genuine duplicate Program+Transaction pairs in the source (not a bug).

**Lesson: before creating a table, identify the smallest set of fields that is actually unique per source record. If in doubt, test with a small sample and check the resulting row count against the expected input count — don't assume the first field that looks like an ID is sufficient.**

### Reading a table's config — NOT CONFIRMED

There is no confirmed way to read a table's *definition* back. Everything
confirmed above either writes it (`POST`, `PATCH`) or reads **data**
(`/data/`, `/search/`, `/aggregated/`) — none of the reads return
`primaryKeys`, `schema`, `updateType` or `displayOrder`.

This matters for anything that rebuilds a table in another tenant, which is
exactly what VinceForge's app import needs. `converter/liveapi.py` therefore
**probes** for it rather than assuming, trying `GET /v1/custom-tables/meta/
{tableName}` first (because `PATCH` on that exact path *is* confirmed) then
`GET /v1/custom-tables/meta?tableName={tableName}`, and validating the body
actually names the table and carries at least one meta field before
accepting it.

**If you run a fetch with a real token, capture the result here.** A working
endpoint should be written up as confirmed with its real response, and the
candidate list in `liveapi.META_READ_CANDIDATES` collapsed to it. If neither
works, record *that* — a confirmed absence is worth as much as a confirmed
endpoint, and stops the next person re-probing.

### Updating an existing table's config

```
PATCH https://api.vince.live/v1/custom-tables/meta/{tableName}
```

Use this to change `enforceSchema`, `schema` (add fields), etc. — **not** for `primaryKeys`, which are immutable. Body is a partial update of the same shape as creation.

### Deleting a table

Deletion has up to a **6-hour delay** before the table is actually removed. A new table with the same name **cannot** be created until the delay completes. Data becomes inaccessible immediately on delete request, even though physical deletion is delayed.

---

## Response shape when creating a table — CONFIRMED

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

Notes: `tableName` you sent is echoed back and also duplicated as `tableAlias`. `rowCount` and `shardMax` start at 0 and can be polled to track ingestion progress. `timestamp` is an internal epoch-ms version marker, not something to set yourself.

---

## Reading data — CONFIRMED (point lookup)

### By composite primary key

```
GET https://api.vince.live/v1/custom-tables/data/{tableName}?records.{key1}={value1}&records.{key2}={value2}
```

Example (confirmed working):

```
GET https://api.vince.live/v1/custom-tables/data/m3-api-catalog?records.Program=CRS610MI&records.Transaction=GetOrderInfo
```

Response:

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

Notes:
- Metadata fields prefixed `__` (`__id`, `__created`, `__modified`, `__modifiedBy`) are always present.
- `__id` is base64 of `{tableName}#0#DATA#{primaryKeyValue1}#{primaryKeyValue2}...` — confirmed by decoding. Useful for sanity-checking that the composite key is actually being applied as expected.
- For multiple records in one call: `records[0].Program=X&records[0].Transaction=Y&records[1].Program=A&records[1].Transaction=B`.
- URL length capped at 8192 bytes (per the product docs — not independently re-verified in this session, but no reason to doubt it).

### By single primary key + `__id`

```
GET https://api.vince.live/v1/custom-tables/data/{tableName}/{id}
```

Not tested this session but documented and internally consistent with `__id` structure above.

### By prefix ("begins with")

```
GET https://api.vince.live/v1/custom-tables/data/{tableName}?beginsWith={prefix}
```

Only for `UPSERT` tables; respects the order of primary keys; concatenate multi-key prefixes with `#`. Not tested this session — documented only.

---

## Bulk / paginated reads — documented, not tested this session

For pulling large tables (e.g. this 22,535-row catalog) rather than doing point lookups:

```
GET https://api.vince.live/v1/custom-tables/search/{tableName}?size=10&scrollMinutes=5
```

Returns a `scrollId` plus the first page. Repeat with `?scrollId=...` to get subsequent pages (other params are ignored once `scrollId` is present). This is likely the right endpoint for a Claude Code project to sync the whole catalog rather than looking up individual API programs one at a time.

---

## Validating row counts after a bulk load — CONFIRMED

```
GET https://api.vince.live/v1/custom-tables/aggregated/{tableName}
```

Example response:

```json
{
    "count": 22535,
    "aggregations": {}
}
```

This is the fast way to check "did my write actually land the number of rows I expect" without paging through the whole table. Confirmed to update in near-real-time while a bulk write (via a Table Updater workflow step) is still in progress — so if you check it mid-run, expect the number to still be climbing.

The Table Management UI in Vince Live also shows row count directly (`Table name | Rows | Columns | Actions`), confirmed to match the `aggregated` endpoint's count.

### Known UI quirk

The Table Management UI showed **Columns: 0** for a table that was created with a full `schema` object and has real data. This appears to be a cosmetic bug — the UI likely still reads column metadata from the deprecated `columnConfig` field rather than `schema`. Data and querying are unaffected; don't take a "0 columns" UI display as a sign something is actually broken.

---

## Architecture note for the Claude Code integration

- Tables are a standalone resource (their own `meta`), independent of any workflow. A workflow only *writes* to a table via a **Table Updater** step; reading does not require going through a workflow.
- This means the Claude Code project should read directly from `/v1/custom-tables/data/...` or `/v1/custom-tables/search/...` — no need to trigger or reference the workflow at all for read access.
- Keep the write side (M3 API step → Transform → Table Updater, on a schedule) and the read side (Claude Code project) fully decoupled. The Claude Code project just needs the `tableName` and read auth.
