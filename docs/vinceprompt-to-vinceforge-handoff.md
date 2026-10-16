# VincePrompt → VinceForge handoff: App Builder is no longer a black box

**Direction of travel.** `docs/vince-app-generator-handoff.md` was written
VinceForge → generator. This one goes back the other way. On 2026-09-18 the
VincePrompt project probed a real tenant read-only and confirmed an App Builder
API surface that VinceForge's documents currently say does not exist.

**What changes for VinceForge:** several "unconfirmed / never invent this"
entries are now confirmed, one is now confirmed *and different from the guess*,
and there is a new silent-truncation trap that any future list-based feature
will hit. Sections 1 and 4 are the ones that change code; read those first.

Full evidence, exact JSON shapes, and the status-code calibration live in
`VincePrompt/docs/appbuilder-api-confirmed.md`. **Don't restate its contents
here** — same rule as §2 of the original handoff: two copies of a confirmed-facts
document drift and then nobody knows which is real. Point at it.

---

## 0. The method still holds, and it is what produced this

Nothing below came from documentation or inference. Every fact was a real GET
against `https://api.vince.live` with a TenantAdmin token, read back and
recorded. The probe harness (`VincePrompt/probe/probe.py`) hardcodes
`method="GET"` rather than taking it as a parameter, so a write to the tenant
was impossible by construction — worth copying if VinceForge ever adds a
read-only mode.

One caveat that survived contact with the API and deserves repeating: **a 200 is
still not proof.** See §4 — a 200 with a plausible body was wrong in a way that
produced a completely false conclusion.

## 1. App Builder: the "zero confirmed facts" rule is now obsolete

Currently stated in three places, all now stale:

| Where | What it says | Status |
|---|---|---|
| `docs/vince-app-generator-handoff.md` §4, §7 | "App Builder: zero confirmed facts of any kind. No prompt format, no schema, no API." | **Wrong** — there is an API |
| `docs/vince-live-api-reference-prompt.md` | same claim, canonical copy | **Update** |
| `converter/app_prompt.py` module docstring + `_BANNER` | "does NOT claim to produce a format App Builder expects" | **Narrow, don't delete** |
| skill `vince-live-workflow`, `references/auth-endpoints-and-unconfirmed.md` §8 | same claim | **Update** |

The confirmed surface is `/v1/ai-apps/` — note the name, which is why every
guess at `/v1/apps`, `/v1/app-builder/*` and friends missed it:

```
GET /v1/ai-apps/                                   # app library
GET /v1/ai-apps/{appId}/versions                   # versions + state
GET /v1/ai-apps/{appId}/versions/{versionId}       # resources[] + assets[]
GET /v1/ai-apps/{appId}/versions/{versionId}/roles # InApp Roles
GET /v1/ai-apps/labels
GET /v1/ai-apps/tenant-prompt
```

It was found by reading the definition of a tenant workflow that calls it — not
by guessing. That is the cheap discovery trick worth reusing: **grep real
workflow definitions for `https://` and see what the platform calls itself.**

### What is confirmed, and what is still genuinely unknown

**Confirmed:** apps, versions and their lifecycle state, the bound resource list
(`type` ∈ `WORKFLOW` | `CUSTOM_TABLE`, `resourceId`, and `name` which **is the
alias** used in prompts), generated asset filenames, InApp Role names.

**Still unknown, and still not to be invented:** the prompt itself. There is no
`/prompt`, `/messages`, `/chat` or `/history` sub-route on an app or a version —
all return 404. Build-session content does not appear to be retrievable at all.

So `app_prompt.py`'s banner should be **narrowed, not deleted**. The honest
wording is now roughly: *the resource and alias model is confirmed; the prompt
text format App Builder expects is still unconfirmed.* Everything the module
does today — faithful restatement plus real ids, template override via
`app-builder-prompt.md` — remains correct. What changes is that it can now name
resources the way App Builder actually models them (alias per bound resource)
instead of inventing a layout.

### The prompt is not the whole input

Product docs, corroborated by the API: an app is shaped by the **tenant prompt**,
the **bound resources**, the **build-session history**, and the message. A
generator that emits only prompt text is addressing one of four inputs. The
resource binding is the part that is machine-readable — and therefore the part
VinceForge can actually own.

## 2. Custom Tables: the definition-read gap is closed

`docs/vince-app-generator-handoff.md` §4 and §10.5 list "no confirmed endpoint
for reading a table's definition" as an open question, and `liveapi.py:130-131`
probes two candidates in order. **Both are now settled:**

- `GET /v1/custom-tables/meta/{tableName}` → **200, works.** VinceForge's first
  candidate was correct. `primaryKeys`, `updateType`, `rowCount`, `shardMax` and
  field metadata all come back.
- `GET /v1/custom-tables/meta?tableName={name}` → **200, but the parameter is
  ignored** — it returns the entire 270-table listing. This is exactly the
  failure mode the existing validator was written for: `liveapi.py` checks that
  the body actually names the table before accepting it, and **that guard is what
  stops this from silently succeeding.** Keep it.
- `GET /v1/custom-tables/meta` (no argument) returns **every** table definition in
  one call — cheaper than per-table reads when you need more than a couple.

Field metadata arrives in one of two shapes and code must handle both:
`columnConfig` (`{Field: {columnOrder, aliasName, dataType}}`, `dataType` ∈
`STRING` | `NUMERIC`) on older tables, or `schema` (JSON Schema) on newer ones.
In the probed tenant: 72 vs 198 respectively.

## 3. Workflow endpoints VinceForge does not have yet

`liveapi.py` currently knows `GET /workflows/{id}` and `POST /workflows`. These
are confirmed real and are what a resource-health feature needs:

```
GET /v1/workflows/?limit=200&startKey=<lastKey>   # list (see §4 - read it first)
GET /v1/workflows/{id}/stats
GET /v1/workflows/{id}/results
GET /v1/workflows/{id}/results/{execId}           # includes the TRIGGER payload
GET /v1/workflows/{id}/results/{execId}/data      # final-step output
```

Two things worth knowing before using them:

- The **list already carries `stats` inline** per workflow, so run counts for a
  whole tenant cost one paged call, not one call per workflow. Use
  `stats.numRunsSuccess` — the adjacent `numRunsSuccessful` read 0 even where
  `numRunsSuccess` was 243.
- `/results/{execId}` exposes `payload.trigger.body` and
  `payload.trigger.header`, i.e. a workflow's **input** contract, which no
  documentation mentions. Useful well beyond App Builder — it is a way to learn
  what a Classic-migrated workflow actually gets called with.

## 4. ⚠️ The new trap: `GET /v1/workflows/` silently truncates at 200

This one cost a wrong conclusion during the probe and will cost more later.

Plain `GET /v1/workflows/` returns `count: 200, items: 200` in a tenant holding
**1,990 workflows**. There is no `total` field, no error, and no indication that
anything was omitted.

- `size`, `pageSize`, `offset`, `page`, `nextToken` — **all silently ignored.**
- `limit` is the real page-size parameter.
- `startKey` is the real cursor, fed the previous response's `lastKey` (opaque
  base64 of `{"pk":"TENANT-…","sk":"WORKFLOW-…"}`). `lastKey`,
  `exclusiveStartKey`, `cursor`, `after`, `nextKey` are all ignored as parameter
  names.

The concrete damage: a first-pass scan cross-referencing 168 apps against the
unpaginated list reported **99 apps bound to workflows that no longer exist.**
The true number is 2. Every other "missing" workflow was simply on page 2+.

This is the same family as the Custom Tables search pagination bug already
documented in §4 of the original handoff — **treat every Live list endpoint as
truncating until proven otherwise, and verify a count against a known total
before believing it.** The tell here was implausibility (99/168), and the check
was a direct GET of one allegedly-missing id.

`?search=` exists on the workflow list but is unreliable:
`workflowAlias:B*` returns 200 with 154 items while `workflowAlias:A*` and
`active:true` both return 500. Prefer cursor paging.

## 5. Gateway status codes do not mean what they look like

Needed to interpret any future probe:

| Status | Actually means |
|---|---|
| `403 {"message":"Forbidden"}` | **Route does not exist.** `/zzz` returns this. |
| `404 {"message":"Not Found"}` | Route exists, sub-resource doesn't. |
| `404 "Entity does not exist: X"` | Route exists, that entity doesn't. |
| `204` | Route exists, nothing configured. |
| `500` | Often a path segment parsed as the wrong kind of id. |

Twelve guessed App Builder endpoints returned 403 and none of them meant
"permission denied". Reading 403 as a permission signal would have produced the
opposite of the truth — that the API existed but was closed off.

## 6. What this means for the merge plan

The original handoff §9 has VinceForge and the generator sharing only the app
folder contract. That still holds, with one addition.

VincePrompt runs the **opposite direction** — existing tenant resources → a
prompt, rather than a prompt → resources. But both need the same grounded read
layer: paged workflow list with stats, table meta with field types, output-shape
derivation from real executions, and the M3 code → human name translation that
VinceForge's `m3-api-catalog` (22,535 rows) and `metadata.py` already provide.

Recommendation: that read layer is the shared seam, not just the folder shape.
Concretely, `liveapi.py` gains the §3 endpoints with §4's paging done correctly
once, and VincePrompt consumes it rather than reimplementing it. The alternative
— two clients, two pagination bugs, two M3 catalogs — is how the 200-cap trap
gets rediscovered the hard way in six months.

Also worth noting for §10.1 of the original handoff: **Table Updater's JSON shape
is still uncaptured.** Nothing in this probe touched it. It remains the first
real blocker for any generated app that writes to its own tables.

## 7. Measured tenant baseline (2026-09-18)

Useful as a reality check on what real usage looks like, and as the evidence base
for why resource health is worth tooling:

| Measure | Value |
|---|---|
| Apps / workflows / custom tables | 168 / 1,990 / 270 |
| Resource bindings | 599 `WORKFLOW` + 117 `CUSTOM_TABLE` |
| Distinct workflows bound by any app | 437 of 1,990 |
| Bindings to workflows with <3 successful runs | 154, across 37 of 168 apps |
| Bound workflows with **no readable output at all** | 193 of 437 |
| Bindings to a workflow that no longer exists | 2 |
| Apps with zero resources | 27 of 168 |
| Apps using InApp Roles | 9 of 168 |
| Tenant prompt configured | No |

The 3-run rule is the documented one, but the sharper test is **3+ runs that
actually returned rows** — an execution returning an empty record set teaches
App Builder nothing about field names. Both are computable from §3's endpoints.

## 8. Still unconfirmed — do not build against these

- Any **write** path on `/v1/ai-apps/`. Deliberately not probed; the run was
  read-only against a production tenant.
- Asset **content** download (filenames confirmed, bytes not).
- Whether prompt or build-session text is retrievable at all.
- Whether `/v1/ai-apps/` itself paginates — it returned a bare array of 168 with
  no cursor, but given §4, assume it might until checked.
- Whether `tenant-prompt` returns a body when one is set (204 in this tenant).
- The per-table `columnConfig` → `schema` migration rule: both shapes exist
  side by side and nothing confirms which one a newly created table gets.
