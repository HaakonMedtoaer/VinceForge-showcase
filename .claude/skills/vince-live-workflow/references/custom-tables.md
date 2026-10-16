# Vince Live Custom Tables — reference

Confirmed real end-to-end: a real table created, 24,154 source rows written
via a Table Updater workflow step, and read back — independently
corroborated by decoding a real `__id` value and finding it matched its
claimed encoding exactly.

Tables are a **standalone resource**, independent of any workflow. Reading
data never requires a workflow; only *writing* rows does (via a Table
Updater step — see §5).

## 1. Base URL

```
https://api.vince.live/v1/custom-tables/...
```

An older `/live/custom-tables/...` path appears in some external docs as a
legacy/alternate form — `/v1/` is the confirmed-working prefix. `tenantId`
is populated **server-side** from the authenticated session; do not send it
in request bodies.

## 2. Creating a table

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

| Field | Confirmed behaviour |
|---|---|
| `tableName` | Gets **slugified** — hyphens, not underscores |
| `primaryKeys` | **Immutable after creation.** See the warning below |
| `updateType` | `UPSERT` (update-or-create, the default choice) \| `REPLACE` (full snapshot replace) \| `APPEND` (non-unique keys allowed, logging-style) |
| `schema` | Standard JSON Schema. The current, confirmed way to define columns |
| `enforceSchema` | `false` = no write-time validation, index whatever is written. `true` = reject rows not matching `schema`. **Start with `false`** |
| `displayOrder` | Column order in the UI |

**`columnConfig` (an older `aliasName`/`dataType` shape seen in some
docs) is DEPRECATED** — a request using it was rejected in real testing.
Use `schema`.

### ⚠️ Choosing primary keys — the hard-won lesson

`primaryKeys` cannot be changed after creation, and **every write with a
duplicate key silently overwrites the previous row. No error.** A bulk load
can therefore "succeed" and land a fraction of the rows sent — this was a
real, confirmed failure (a table keyed on `["Program"]` alone silently
collapsed 24,154 source rows to ~1,330, one per unique Program, because
many Programs have several Transactions).

Before creating a table: identify the smallest field set that is genuinely
unique per source record. After loading: check the resulting row count
(§4) against the expected input count — that comparison is exactly how the
real bug above was caught. Nothing else signals it.

### ⚠️ A known flaky, misleading error — not fully understood

The exact same valid body has been rejected twice with a 400 whose
`validationError` listed required properties (`definition`, `tenantId`,
`workflowAlias`, `workflowId`) that belong to a **workflow** object, not a
table — apparently a misrouted validator. The identical body then succeeded
on retry with no changes. If this is hit, retry before assuming the body is
wrong; root cause not confirmed.

## 3. Updating and deleting

```
PATCH https://api.vince.live/v1/custom-tables/meta/{tableName}
```

Partial update of the same shape — use for `enforceSchema`, adding `schema`
fields, etc. **Not** for `primaryKeys` (immutable).

**Deletion has up to a 6-hour delay** before the table is actually removed,
and a same-named table **cannot be recreated until it completes** — even
though the data becomes inaccessible immediately.

## 4. Reading

**Point lookup by composite primary key:**

```
GET https://api.vince.live/v1/custom-tables/data/{tableName}?records.{key1}={v1}&records.{key2}={v2}
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

- `__`-prefixed metadata fields (`__id`, `__created`, `__modified`,
  `__modifiedBy`) are always present.
- **`__id` is base64 of `{tableName}#0#DATA#{pkValue1}#{pkValue2}...`** —
  confirmed by decoding a real value. Useful for sanity-checking a
  composite key is being applied as expected.
- Multiple records in one call:
  `records[0].Program=X&records[0].Transaction=Y&records[1].Program=A&...`
- URL length capped at 8192 bytes (per docs, not independently re-verified).

**By `__id`** — `GET .../data/{tableName}/{id}`. Documented and internally
consistent with the `__id` encoding above; not independently tested.

**By prefix** — `GET .../data/{tableName}?beginsWith={prefix}`. UPSERT
tables only, respects primary-key order, concatenate multi-key prefixes
with `#`. Documented, not independently tested.

**Bulk / paginated** — `GET /v1/custom-tables/search/{tableName}`. Supports
a scroll cursor (`?size=10&scrollMinutes=5` returns a `scrollId` plus the
first page; repeat with `?scrollId=...`; other params ignored once present).
Also accepts a Lucene-style query — see §6 for the real, hard-won behaviour.

**Row count** — `GET /v1/custom-tables/aggregated/{tableName}` →
`{"count": N, "aggregations": {}}`. The fast way to confirm a bulk write
landed the expected row count without paging. Confirmed to update in
near-real-time while a write is still in progress.

## 5. Writing rows — Table Updater step only

**There is no confirmed row-write endpoint.** The confirmed path for
getting rows into a table is a workflow's **Table Updater** step. **That
step's own JSON shape is NOT CONFIRMED** — no real capture of one exists.
If a design needs to write rows, say so plainly, note that a Table Updater
step is the mechanism, and that its exact `type`/`target`/stepConfig needs
a real capture from the builder before it can be emitted with confidence.
Reading is fully decoupled from writing — a reader needs only the
`tableName` and read auth, never the workflow that fills it.

The typical production shape is a scheduled write side and an independent
read side:

```
trigger_1 (SCHEDULE) → m3_api_1 → transform_1 → table_updater      [write]
GET /v1/custom-tables/data|search/{tableName}                      [read, independent]
```

## 6. ⚠️ The search endpoint's pagination is broken — work around it

The search endpoint is Elasticsearch-backed. Two separate pagination bugs
found empirically:

1. **`from` has a minimum of 1** and maps straight through to ES's native
   0-indexed `from` — the true first result of every query is therefore
   **permanently unreachable** via `from=1`. Confirmed by diffing a
   `from=1` response against the same request with `from` omitted.
2. **Multi-page fetches silently duplicate/lose records** for *some* large
   queries but not others, apparently because the endpoint's default sort
   order isn't stable across separate HTTP requests — no client-side
   offset math fixes this. An explicit `sort=` param appeared to be a
   no-op.

**Single-page fetches (no `from` at all) have never shown either problem.**
The working strategy for a whole large table: **query by prefix and
recursively split** any prefix whose reported `total` exceeds the page cap
(**2000** per request, confirmed hard cap). Query syntax:
`?q=Program:C*&size=2000` — confirmed for a single-letter prefix; the
multi-character form (`q=Program:CMS*`) is *assumed* to behave the same and
has **not** been independently spot-checked.

## 7. Reading a table's *definition* — NOT CONFIRMED

No endpoint is confirmed to return a table's `primaryKeys`/`schema`/
`updateType`. `POST`/`PATCH` write it; the reads above return rows and
counts only. **Do not assume `GET /v1/custom-tables/meta/{tableName}`
works** — it's the plausible candidate (`PATCH` on that exact path is
confirmed) but not a verified read. If probing it, validate that the body
actually names the table and carries a real meta field before believing a
200 — a 200 that isn't this table's definition is a real, previously-seen
failure mode (an auth error page and a `/data/` response can both return
200-looking bodies that aren't the definition).

## 8. Known UI quirk

The Table Management UI can show `Columns: 0` for a table created with a
full `schema` and holding real data — it appears to still read column
metadata from the deprecated `columnConfig` rather than `schema`. Cosmetic;
data and querying are unaffected.

## 9. The `m3-api-catalog` table — a real, useful lookup

A Custom Table named `m3-api-catalog` exists in at least one real Vince
tenant (not a universal guarantee — a different tenant may not have it):
22,535 rows of `Program`/`Transaction`/`Description`/`Type`/`Status`, keyed
on `["Program", "Transaction"]`, built by a workflow that itself calls
`MRS001MI/LstAllTrans`. If verifying a proposed M3 program/transaction name
against a real tenant, checking a table like this (if the target tenant has
one) turns "is this API real" from a hallucination risk into a lookup.
Don't assume it exists in every tenant without checking.
