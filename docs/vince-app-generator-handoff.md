# Vince App Generator — architecture & knowledge handoff

**Purpose of this document.** You are starting a new project: turn a worded
prompt into a working Vince Live app — workflows, Custom Tables, and a prompt
for App Builder. This is everything VinceForge learned that you need, plus
the architecture that survived contact with the real API, plus the specific
places where a generator will be tempted to invent facts and must not.

Written to be read by a session that does **not** have VinceForge's history.
Sections 1 and 9 are the ones that change what you build; read those first.

---

## 0. The rule that outranks every other decision here

**Never guess at Live's JSON structure, endpoint paths, or enum values.**

Every confirmed fact in VinceForge came from building a real example in
Live's UI, or hitting the real API and reading back what actually came out.
None of it came from documentation or inference. The structure is full of
things no reasonable person would guess:

- `type: "ADD"` on an M3 transaction is load-bearing **regardless of what the
  transaction actually does** (List/Get/Update/Add). Its absence is the
  strongest known cause of an M3 API step rendering completely inert.
- A repeated CLIENT field's `ref.apiId` points at the **first** transaction to
  declare it, while an API-chain `ref` points at the **producer**. Two `ref`
  shapes, opposite directions, same field name.
- `workflowType` must be `"STANDARD"`. `"EXPRESS"` creates a half-provisioned
  workflow that is unreachable via GET, invisible in the list, and blocks
  re-creating the same id.
- GET and POST on `/workflows` do **not** share a body shape.

Three corollaries that matter more for a generator than for a converter:

1. **A 200 is not proof.** A malformed `tenantId`/`connectionId`/`environmentId`
   passes creation silently and only breaks rendering later in the builder UI,
   as a frontend `TypeError`, not a server error. A generator that checks only
   HTTP status will report success on broken output.
2. **A wrong guess usually produces a real, useful error.** `POST /workflows`
   with a bad step `type` returns
   `"WorkflowDefinition is invalid! Errors (N): Property workflowConfig.steps stepId X has invalid type: Y"`.
   Use that. But the error names what's wrong, not what's right — don't assume
   your first correction is correct either.
3. **Treat every enum as a closed list.** New step types, new field types, new
   table update types each need their own captured real example before you
   write code against them. An LLM generating "plausible" enum values is
   exactly the failure mode this project exists to avoid.

---

## 1. The seam: what the generator actually has to produce

**Do not have the generator emit Live workflow JSON.** VinceForge already has
a build stage that knows the gotchas in section 0, and an importer that
creates tables and workflows against the real API. The generator's job ends
before all of that.

Produce an **app folder**. That is the interchange contract, it already
exists, and it is what makes these two projects merge cleanly instead of
fighting over who owns the JSON knowledge.

### The app folder contract

```
MyApp/
  app.json                     manifest: name, description (free-form otherwise)
  tables/<name>.json           a POST /v1/custom-tables/meta body
  tables/<name>.rows.json      optional seed rows (captured, never imported — see §4)
  workflows/<name>.json        a workflow JSON document
  app/                         App Builder source: .java, .html, .css, .js, ...
  app-builder-prompt.md        optional template overriding the default prompt layout
```

Subfolder names are a **convention, not a requirement** — every `.json` is
classified by its *shape*, so a flat folder works identically:

| Shape | Classified as |
|---|---|
| `definition.workflowConfig` is an object | workflow |
| has `tableName` **and** one of `primaryKeys` / `schema` / `updateType` / `displayOrder` | table meta |
| a bare array of objects, or `{"count": N, "items": [...]}` | table rows |
| anything else readable as text | App Builder source file |

Given such a folder, VinceForge already: scans and reports a plan, creates
the tables (`POST /v1/custom-tables/meta`), creates the workflows
(`POST /workflows`) with fresh instance ids and retargeted connection/tenant/
environment, generates the App Builder prompt, and writes a full artifact
trail. You get all of that for free by matching the contract.

**This is the merge plan.** Short term the two projects share nothing but
this folder shape. Long term, the generator becomes VinceForge's third source
(§9).

---

## 2. What already exists — do not rebuild these

VinceForge is a stdlib-only Python package (no Flask, no dependencies) plus a
local web UI. Modules worth knowing about:

| Module | What it does |
|---|---|
| `ir.py` | Source-agnostic intermediate representation: `Workflow` / `Transaction` / `InputField` / `Cell` / `RowFilter` |
| `build.py` | **IR → Live workflow JSON.** ~580 lines, and where almost all the hard-won structural knowledge lives |
| `ids.py` | Live-format id generation (transaction instance ids, workflowId) |
| `source_classic_xml.py` | Classic XML → IR |
| `source_live_json.py` | Live JSON → IR, and clone-and-retarget as a direct transform |
| `source_prompt.py` | **A stub. This is your merge target.** |
| `source_app_folder.py` | Folder → `AppBundle` (the §1 contract, read side) |
| `appimport.py` | Creates an app folder's tables + workflows in a tenant |
| `app_prompt.py` | Renders the App Builder prompt |
| `liveapi.py` | HTTP client, confirmed endpoints only |
| `live_fetch.py` | Pulls workflows/tables out of a tenant into an app folder |

### Files to copy across on day one

- `docs/vince-live-api-reference-prompt.md` — the canonical confirmed-facts
  reference, written specifically to be pasted into a session with no
  codebase access. **Everything in section 3 below is a summary of this; that
  file is the source of truth.**
- `schemas/live-workflow.schema.json` — the same knowledge as a formal JSON
  Schema, gotchas baked into `description`/`$comment` fields.
- `docs/vince-custom-tables-guide.md` — Custom Tables, confirmed against the
  real API.
- `docs/vince-app-import.md` — the full app folder contract.

**Do not restate their contents in your new project.** Two copies of a
confirmed-facts document drift, and then nobody knows which is real. Copy the
files; point at them.

There is also a `docs-unverified/` directory in VinceForge. It contains an
OpenAPI doc for a "Vince Platform API" that references a domain with zero DNS
records — almost certainly fabricated. **Do not copy it, and do not build
against it.** It is kept only as a cautionary record.

---

## 3. The confirmed API surface, in brief

Base URL: `https://api.vince.live`. Auth: `Authorization: Bearer <token>`,
a short-lived Cognito ID token captured by hand from Live's own web UI
(DevTools → Network). Expect `401 {"message":"Unauthorized"}` after roughly an
hour idle — that's expiry, not a bug.

**Workflows**
- `POST /workflows` — create. `GET /workflows/{id}` — read. Different body shapes.
- One root `TRIGGER` step; `childSteps` nests the rest **linearly**. No branching
  in any confirmed example.
- Confirmed step types — a **closed list**: `TRIGGER`, `API` (M3), `EXCEL`,
  `GENERIC_FILTER`, `TRANSFORMER_MORPH` (Transform/JSONata), `GENERIC_API`
  (REST), `GENERIC_CODE` (Node.js).
- `stepId` is load-bearing: Transform steps reference siblings by exact stepId
  (`$context.data.all.rest_api_1`).

**Custom Tables**
- `POST /v1/custom-tables/meta` — create. `PATCH .../meta/{tableName}` — update
  config, but **not** `primaryKeys`, which are immutable.
- `GET /v1/custom-tables/data/{tableName}?records.{key}={value}` — point lookup.
- `GET /v1/custom-tables/search/{tableName}` — bulk read (see §4 for its bugs).
- `GET /v1/custom-tables/aggregated/{tableName}` → `{"count": N}`.
- `tenantId` is **not** sent in the body; the server fills it from the session.
- `updateType` ∈ `UPSERT` | `REPLACE` | `APPEND`.
- `columnConfig` is deprecated and **rejected** — `schema` (JSON Schema) is current.

**Primary keys are the highest-consequence table decision.** Get them wrong
and every write with a duplicate key silently overwrites the previous row, no
error. Real case: a 24,154-row source keyed on `["Program"]` alone settled at
~1,330 rows; the correct composite key `["Program", "Transaction"]` gave
22,535. A generator choosing primary keys from a natural-language description
will get this wrong sooner or later — see §8.

---

## 4. Confirmed gaps that constrain your design

These are not "not yet looked into". Each was actively investigated.

**No API for writing table rows.** Creating a table's definition is confirmed;
writing rows into it is not. The confirmed path is a workflow's **Table
Updater** step. This means a generated app that needs seed/reference data must
express that as a workflow, not as a data upload. VinceForge captures rows
found in an app folder, reports them, and refuses to send them.

**No confirmed endpoint for reading a table's definition.** `POST` and `PATCH`
write it; the confirmed reads return rows and counts, never `primaryKeys` /
`schema` / `updateType`. VinceForge probes `GET /v1/custom-tables/meta/{name}`
(plausible — `PATCH` on that exact path is confirmed) then
`GET /v1/custom-tables/meta?tableName={name}`, and **validates** the body
actually names the table and carries a meta field before accepting it. One run
with a real token would settle this; if you settle it, write it down.

**App Builder: zero confirmed facts of any kind.** No prompt format, no
schema, no API. Nothing is known about what it accepts. See §7.

**The search endpoint has two confirmed pagination bugs** (found empirically,
2026-08-31, while pulling 24,154 rows):
1. `from` has a minimum of 1 and maps to Elasticsearch's 0-indexed `from`, so
   the true first result of every query is **unreachable** via `from=1`.
   Omitting `from` entirely recovers exactly the record it drops.
2. Deep `from`/`size` paging **silently loses records** for some large queries
   and not others (`Program:C*` consistently lost ~239; `Program:M*`, also
   large, never did). The default sort order appears not to be stable across
   requests, which client-side offset math cannot fix.

The workaround that *did* work: never page. Query by prefix (`q=Program:C*`),
`size` ≤ **2000** (confirmed hard cap), and recursively split any prefix whose
`total` exceeds one page until every leaf fits in a single response. See
`fetch_m3_api_catalog.py`.

**A second auth mechanism is real but not generically usable**: OAuth2
`client_credentials` via an API Client, with a `Token URI` like
`https://tenant-<hex>.auth.<region>.amazoncognito.com/oauth2/token` and a
`Base URI` like `https://<tenant-slug>.vince.live/`. Both are **tenant-specific
and not derivable**. Do not guess a subdomain.

---

## 5. Recommended architecture for the generator

VinceForge's shape is `source → IR → build`, with source-specific
interpretation kept out of the build stage. That separation is what let a
third source be added without touching the JSON knowledge. Keep it.

The generator is a **source**. Its output is IR (or, before merging, an app
folder). It should never learn Live's JSON gotchas.

### Three stages, and the boundary that matters

```
prompt ──[LLM]──▶ draft spec ──[human confirm]──▶ IR ──[deterministic]──▶ app folder
        creative                  grounded                 zero creativity
```

The whole design rests on one line: **the LLM drafts, deterministic code
builds.** The moment an LLM is emitting Live JSON directly, every guarantee in
section 0 is gone, because a language model's core competence is producing
plausible structure — precisely the failure this codebase is built to prevent.

What the LLM is genuinely good for here, and should own:
- turning a vague description into a candidate set of tables, workflows and screens
- naming things, writing descriptions, drafting App Builder source
- asking the right clarifying questions
- proposing which M3 programs/transactions might fit — as **candidates to verify**, never as facts

What must be deterministic, or grounded in real data:
- every field name, program name and transaction name (§6)
- all Live JSON emission (`build.py`)
- primary key selection (§8) — propose, then verify against real cardinality
- anything that becomes an enum value

### The IR needs extending

Be aware before you plan: today's IR models **M3 API + Excel + Generic Filter
workflows only**. `Transaction` is `program`/`transaction`/`inputs`/`cells`/
`row_filters`/`based_on`. There is no IR representation for Transform, REST
API, Code, or **Table Updater** steps, and `build.py` cannot emit them.

That last one matters most: **a generated app that writes to its own tables
needs a Table Updater step, and neither the IR nor `build.py` supports one
today.** Its JSON shape is not in the confirmed docs either. Treat this as the
first real blocker to design around — capture a real Table Updater step from
Live's UI and write it up before building anything that depends on it.

---

## 6. Grounding: the M3 API catalog

This is the single most valuable asset for a generator, because it converts
"which M3 transaction does this?" from hallucination into lookup.

- A Custom Table `m3-api-catalog` exists in Live: **22,535 rows**, columns
  `Program`, `Transaction`, `Description`, `Type`, `Status`, primary key
  `["Program", "Transaction"]`.
- A full local snapshot lives in VinceForge as `m3-api-catalog-full.json`
  (~8.4 MB, 24,154 fetched rows before dedup) under
  `{tableName, fetchedCount, duplicateKeyCount, perLetterCounts, items[]}`.
- `fetch_m3_api_catalog.py` regenerates it, and encodes the pagination
  workaround from §4.

**Every program/transaction name a generated workflow references should be
checked against this catalog before it reaches the IR.** A name that isn't in
it is a hallucination or a genuinely new API — either way, stop and confirm,
don't emit it.

Field-level metadata is a separate problem: `metadata.py` fetches per-field
metadata from Live's own metadata endpoint given a bearer token, falling back
to a small static stub table. The catalog gives you programs and transactions;
it does **not** give you their field lists.

---

## 7. The App Builder half — be honest about this

There is no App Builder import API. The handoff is a prompt a human pastes in.
And App Builder has **zero confirmed facts** — no known prompt format, schema,
or interface.

VinceForge's approach, which you should keep: generate a faithful restatement
of the app folder's contents plus the real ids of what was created, under an
explicit banner saying the format is unconfirmed, and let any app folder
override the whole layout with its own `app-builder-prompt.md`. The default
uses `{{placeholder}}` substitution — `{{app_name}}`, `{{tables}}`,
`{{workflows}}`, `{{sources}}`, `{{not_imported}}` and friends.

The important discipline: **do not design a rich App Builder schema.** Anything
elaborate is invention presented as interface. When a real App Builder prompt
is confirmed, drop it in as a template and delete the banner.

Also note the ordering constraint this creates: the prompt is only fully
useful **after** a real import, because the workflow ids Live assigns are the
main thing App Builder needs from it. A dry-run prompt has placeholder ids and
is not the one to paste.

---

## 8. Validating generated output

A generator can produce confidently wrong output at a far higher rate than a
converter, so validation is not optional.

1. **Schema-check before sending.** `schemas/live-workflow.schema.json` catches
   structural errors locally, with no token and no side effects.
2. **Dry run.** VinceForge's app import has a dry-run mode that builds every
   POST body and writes it to an artifact directory without sending anything.
   That is your "show me what you would do" step.
3. **Never trust a 200.** Per §0: read the workflow back, and confirm it
   *renders* in the builder. Creation succeeding proves very little.
4. **Check table row counts against expected input counts** after any load. This
   is how the primary-key bug in §3 was caught — silently, only row counts
   revealed it. `GET /v1/custom-tables/aggregated/{tableName}` is the cheap check.
5. **Verify every program/transaction against the catalog** (§6) before emitting.
6. **Sanity-check the id formats.** `CONNECTION-`/`TENANT-`/`ENVIRONMENT-` + 32
   hex. Malformed ones pass creation and break rendering later.

---

## 9. The merge plan

**Phase 1 — separate projects, one contract.** Your generator emits an app
folder (§1). VinceForge imports it. Zero code coupling; the folder on disk is
the entire interface. You can build and test the whole generator without ever
touching Live's JSON.

**Phase 2 — merge as a source.** The generator becomes `source_prompt.py`,
which today is a deliberate stub whose docstring already anticipates exactly
this: *"this needs an LLM-assisted parse step (Claude drafting the IR
conversationally, asking clarifying questions about ambiguous fields and
confirming M3 program/transaction names) rather than a deterministic parser
like the other two sources."* It produces IR; `build.py` emits the JSON; the
existing web UI, targeting, history and artifact machinery all apply
unchanged.

For phase 2 to be possible, keep the generator's internal model **shaped like
the IR** — `Workflow` → `Transaction` → `InputField`, with source kinds drawn
from Live's own vocabulary (`CONSTANT`, `EXCEL_COLUMN`, `CLIENT`, `API_CHAIN`).
If your internal model diverges, the merge becomes a rewrite.

---

## 10. Settle these early

Roughly in order of how much they'd cost to get wrong late:

1. **The Table Updater step's JSON shape** (§5). Blocks any app whose workflows
   populate its own tables. Capture a real one from Live's UI.
2. **How primary keys get decided** (§3). The failure is silent data loss.
   Human confirmation, or verification against real cardinality — not an LLM's
   first guess.
3. **How much the LLM is allowed to decide unattended.** Where exactly is the
   human-confirm gate in §5? This is a product decision, and everything about
   the tool's trustworthiness follows from it.
4. **Whether `build.py` gets extended or the generator emits richer folders.**
   Extending the IR to cover Transform/REST/Code/Table-Updater is the bigger
   job but the one that makes phase 2 real.
5. **Reading a table definition** (§4). One run with a real token settles it.
   Cheap; do it first and write down the answer either way — a confirmed
   absence is worth as much as a confirmed endpoint.

---

## Appendix: the method, since it is the actual asset

When you hit something not covered by the confirmed docs:

1. Build a minimal real example in Live's UI, save it, `GET` it, and read what
   actually came out. This is how nearly everything here was learned.
2. Or `POST` a deliberate probe and read the real validation error.
3. Write down **only** what was confirmed, and mark everything else as
   unconfirmed rather than presenting a guess as fact.
4. A 200 is not confirmation. Independently verify — render it, execute it, or
   decode a field and check it matches its claimed encoding. (That last one is
   how Custom Tables' `__id` was confirmed to be base64 of
   `{tableName}#0#DATA#{pk1}#{pk2}`.)

The temptation in a generator project is stronger than in a converter, because
the LLM in the middle will always produce something plausible. Plausible is
the failure mode. Confirmed or flagged — nothing in between.
