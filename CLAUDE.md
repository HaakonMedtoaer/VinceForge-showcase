# VinceForge

A tool that builds Vince Live workflow JSON (the `POST /workflows` API shape)
from any of three sources: Classic XML export, an existing Live JSON export
(clone + retarget), or (not yet built) a worded prompt. It also imports a
whole **app** — a folder of workflows, Custom Tables and App Builder source
— into a target tenant in one go. Started as a single-file Classic→Live
migration script; now a small package plus a local web UI. No accounts, no
external database — state lives in plain JSON files next to the code.

## The one rule that matters more than anything else here

**Never guess at Live's JSON structure.** Every confirmed fact in this
project — field names, enum values, step types, the weird ones like
`type: "ADD"` being load-bearing or `apiId` pointing at the *last* CLIENT
occurrence while `ref` points at the *first* — came from building a real
example in Live's UI or hitting the real API and reading back what actually
came out, not from documentation or inference. Do not rediscover these from
scratch, "simplify" them away, or extend an enum by analogy. If something
isn't covered in the docs below, that means it's genuinely unconfirmed — say
so and go verify it (GET a real workflow, build a minimal test workflow by
hand and capture it, or POST a probe and read the real validation error),
don't fill the gap with a plausible guess.

## Where the actual knowledge lives

- **[vince-classic-to-live-migration-summary-MERGED-v2.md](vince-classic-to-live-migration-summary-MERGED-v2.md)**
  — the canonical, prose reference for every confirmed structural fact about
  Live's JSON (Classic-migration section) plus Transform/REST API/Code step
  types (newer section). Read this before touching `build.py` or adding a
  new step type.
- **[docs/vinceforge-capability-matrix.md](docs/vinceforge-capability-matrix.md)**
  — what THIS TOOL can and cannot carry, per construct, on each path, plus
  what closing each gap would take. Different axis from everything else here:
  the other docs describe the PLATFORM, this one describes our coverage of
  it. **Generated** — edit `docs/capability-matrix.json` (rows) or
  `docs/capability-matrix.tmpl.md` (prose), never the `.md`. See the
  Capability matrix section below.
- **[docs/migration-team-guide.md](docs/migration-team-guide.md)** — the same
  ground aimed at the Migration Team rather than at us: how to triage a
  customer's Classic files, what the common blockers mean in plain language,
  and what a clean conversion does NOT prove.
  Every capability claim in it cites a matrix row id, and
  `selftest_matrix.py` fails if one stops resolving.
- **[docs/vince-native-expand.md](docs/vince-native-expand.md)** — native
  Expand (Classic `<Expand>` in Live, API-only): the confirmed JSON shape, how to
  read `isExpandOperation` from the compiled `definitionAsl`, and what real runs did.
- **[schemas/live-workflow.schema.json](schemas/live-workflow.schema.json)**
  — the same knowledge as a formal JSON Schema, with the gotchas baked into
  `description`/`$comment` fields. Built specifically to hand to a
  tool-less Claude session (e.g. claude.ai) that needs to draft workflow
  JSON without access to this project's history.
- **[docs-unverified/](docs-unverified/)** — **do not trust.** Contains a
  "Vince Platform API" OpenAPI doc + notes that were provided as reference
  but turned out to reference a domain (`api.vincelive.dev`) with zero DNS
  records — almost certainly fabricated/hallucinated, not real. See its own
  README for the full story. If a genuine separate "Vince Platform API"
  (OAuth2 client_credentials, tenant-specific Cognito domains, a
  `<tenant-slug>.vince.live` base URI) ever gets confirmed for real, it
  should get its own properly-sourced doc — not a correction of this one.

## Vince Live workflow building

See `docs/vince-live-workflow-notes.md` for confirmed schema details, JSONata quirks,
and gotchas when authoring/editing Vince Live workflows via the API.

## M3 without the M3 API step (generic pipelines)

See **[docs/vince-generic-m3-pipeline.md](docs/vince-generic-m3-pipeline.md)**.
A second, independent way to build M3 workflows: Trigger → Transform →
REST API chains that POST to ION API's bulk `m3api-rest/v2/execute`
endpoint, with **no M3 API step and no Excel step**. Not a hack — it's
the shape Vince's own production customer workflows use (one does full M3
writes across seven Transform/REST pairs), sourced from real captured
examples, all provenance named in that doc.

Reach for it when the native path genuinely can't express the workflow —
above all **per-call filtering**: `GENERIC_FILTER` runs once, before the
whole M3 API step, so it cannot gate one M3 call while another runs
unconditionally, which is exactly what Classic functions with filters
deep in the function do. On this path a filter is a JSONata predicate on
the rows going into one specific call's payload, and it can read an
earlier call's own output.

**A Classic file can now be converted straight onto this path**:
`cli.py classic-pipeline` (`converter/classic_to_pipeline.py`). It emits a
single **Code step** rather than a chain of Transform/REST pairs, which is
a deliberate departure from that doc's original design - a bulk chain
cannot carry a Classic import, because index correlation between steps
dies as soon as a call is gated (and Classic gates nearly all of them), and
because `<BasedOnTransaction>` needs a per-call success verdict, which only
a single-transaction POST gets from the confirmed
`nrOfFailedTransactions`. Two facts it still has to assume are isolated one
function/constant each and named in every conversion's warnings; see the
gap list below.

What you give up: field-metadata validation, automatic pagination,
`excel_1` binding (use the `VXL` label instead), and much looser
save-time validation. Prefer `build.py`'s native path whenever it fits.

## App import (folder → tables + workflows + App Builder prompt)

See `docs/vince-app-import.md`. Point it at a folder holding an app's
workflow JSONs, Custom Table metas and App Builder source (Java/HTML/CSS/JS);
it creates the tables and workflows via the API and generates the App Builder
prompt, since App Builder has no import API. Three things it deliberately
does NOT do, each because the alternative would mean guessing: import table
rows (no confirmed row-write endpoint), remap cross-workflow references (no
confirmed field holds one), or assert a prompt format for App Builder (zero
confirmed facts). Read that doc before changing any of those.

An app folder may also hold **Classic XML exports**. Each is converted to a
workflow POST body, written back into the folder as
`workflows/<stem>.generated.json`, and then created by the ordinary workflow
path — one import handles a whole customer's worth of Classic files. A file
with several `<Export>`/`<Import>` blocks becomes **one workflow per block**
(see the gap list below), each named `"<base name> - <block's own Label>"`
(**updated 2026-09-16** — was the block Label alone; see below) - the first
keeps the plain `<stem>.generated.json` filename and the rest get numbered
suffixes, so design-file pairing and re-import overwriting still work. The
`.xml` is the source of truth and is re-converted (and its `.generated.json`
overwritten) every run; a dry run still writes those files, deliberately,
because convert-inspect-then-commit is the point of one. `converter/app_classic.py`
owns that stage and its docstring carries the reasoning.

**Updated 2026-09-16 — naming, an `Imported` tag, and Live Groups.** Each
Classic XML file's base name is now a real choice, not a byproduct: the CLI
prompts once per file (suggested from its stem, Enter to accept) and the web
UI's app-import panel shows one editable field per file in the scan/plan
display, both feeding `app_classic.convert_classic_xml`'s `base_names` param
(keyed by the XML's app-relative path) via `run_app_import(...,
base_names=...)`. `source_classic_xml.compose_workflow_name(base, function)`
composes the two halves; a file with no chosen name falls back to its own
stem. Every workflow created via app import (Classic-XML-derived or a plain
pre-existing `workflows/*.json`) also gets the `Imported` label
(`envelope.IMPORTED_LABEL`, composes with `VXL` rather than replacing it).
And whenever a source Classic XML file produced **2+ successfully created**
workflows, they're bundled into a real Live "Group" (`liveapi.create_group`,
`POST /groups` - see `docs/vince-live-api-reference-prompt.md` §1d) so they
open together in VXL Live, named the same as the workflows' shared base name.
Grouping runs after `_import_workflows` (`appimport._create_groups`, bucketed
by the new `AppWorkflow.group_key` field) since a Group needs real,
server-assigned workflow ids - and it is **app-import only**: every other
creation path here either only ever writes a local JSON file (never POSTs)
or, in the web UI's "Create in Live" button, posts one workflow at a time,
so there is never more than one sibling to group with outside app import.

**Updated 2026-09-16 — app import now auto-routes, per BLOCK, onto the
generic pipeline path when a block genuinely needs it.** Until then this
section said app import always used the native path and a file needing the
generic one had to be converted on its own - true at the time, but a real
CustomerA customer file set (hundreds of `GE`/`LT`/`LE` filters, `docs/`-worthy
enough it drove that day's GE/LE/LT support work above) made the gap
concrete: those files can't go through app import correctly at all without
this. The trigger is deliberately **narrow**:
`source_classic_xml.unrepresentable_filter_operators` routes a block to the
pipeline path only when it has a RowFilter native cannot express
(`build.native_filter_problem` - since 2026-09-28 a GE/GT/LT/LE on a
NON-numeric value, e.g. CustomerA' `'!'`; a numeric range is native) - not for the other existing native gaps
(non-uniform per-transaction filters, a filter on an M3-written column,
grouping), which still just report/`_needs_review` as before. Widening
that is a separate, later call. It lives in `source_classic_xml.py`, not
`app_classic.py` where it's used, so `peek_classic_xml` - the read-only,
target-free SCAN, not just the conversion `app_classic.py` does after -
can predict it too, with a real parse
(`source_classic_xml.parse_classic_xml_units`), not a second, drift-prone
copy of the same check.

This is per-BLOCK, not per-file, because filter needs genuinely differ
within one file - `Creation_CentralRef`'s 8 blocks split 3 native-safe /
5 needing the pipeline path. A routed block's generated Code step always
has `debug: false` (unlike `classic-pipeline`'s standalone CLI default of
`true`) - a real customer import is not a one-off diagnostic run, and
`debug: true`'s self-poisoning hazard (below) is not something to hand a
migration by default.

`app-import`/`run_app_import` takes a new, explicit, optional
`ion_api_base_url` argument (CLI: `--ion-api-base-url`) - separate from the
connection/tenant/environment ids and NOT read from a stored Target Tenant
preset, because it is genuinely per-customer (different systems mean
different ION API roots even within the same ids). A block that needs it
and doesn't get one still builds - on the native path, with the filter gate
left off and a loud `PIPELINE BUILD FAILED` warning naming why - never a
hard failure of the whole run. Every routed block's result carries
`buildPath: "pipeline"` and a warning naming the operator(s) - and this is
visible **before anything is scanned even attempts conversion**, not only
after: `peek_classic_xml(text, path=...)` predicts the same per-block
routing from a real parse and folds it into the SCAN's own warnings, which
is exactly what feeds both the plan (`app-scan`) and the confirmation box
shown before a real (non-dry) import - the one path that never ran a dry
pass first. `selftest_app_classic.py` covers the build-time routing (the
decision, both fallback cases, a mixed two-block file); the scan-time
prediction is covered in `selftest_classic.py` alongside the rest of
`peek_classic_xml`.

Unlike every other source here, app import preserves workflow names exactly
rather than auto-incrementing them — an app's parts refer to each other by
name. ("Exactly" now means the composed name above for Classic-XML-derived
workflows, still verbatim for a plain pre-existing `workflows/*.json` —
either way, still no auto-increment.)

**Corrected same day, later in the session: re-importing does NOT duplicate
workflows.** `POST /workflows` against an already-used name comes back
`HTTP 409: Workflow "X" already exists!` — Live enforces name uniqueness
itself; this project previously assumed the opposite ("nothing dedupes
them") from inference, not a real call, and that assumption was wrong.
`appimport._is_duplicate_name_conflict` recognizes that specific 409 shape
and `_import_workflows` treats it as a skip (`ok: true, skipped: true`),
the same treatment `_import_tables` already gave an existing table - not a
failure, nothing new created. One real, deliberately-unresolved gap this
creates: a skipped workflow has no `workflowId` (no confirmed way to look
up an existing workflow's id by name), so it can't be added to a Group its
siblings form this run - `_create_groups` names it explicitly in the
group's warning rather than the group silently coming up short.
`selftest_appimport.py` covers the classifier and both the skip and
still-fails paths.

**Nothing reaches a tenant without a confirmation box.** It lists what is
about to be created and shows the target ids next to the tenant the *token*
resolves to, flagging a mismatch in red — the failure mode that is otherwise
silent (see Auth below). The single-workflow "Create in Live" button has the
same box.

## App generator (separate project, not built here)

`docs/vince-app-generator-handoff.md` is a self-contained handoff for a
SEPARATE project that turns a worded prompt into a whole app. It carries the
architecture, the confirmed-fact discipline, the app-folder contract that is
the seam between the two projects, and the gaps a generator has to design
around. Written for a session with no access to this repo's history.

The merge target is `converter/source_prompt.py` (still a stub): the generator
eventually becomes this project's third source, emitting IR for `build.py`.
Until then the two projects share nothing but the app folder shape.

## Vince Live Custom Tables

See `docs/vince-custom-tables-guide.md` for confirmed endpoints (`/v1/custom-tables/meta`,
`/data/`, `/search/`, `/aggregated/`), request/response shapes, and hard-won gotchas
(immutable `primaryKeys`, silent UPSERT overwrites on a wrong key choice, the deprecated
`columnConfig` field, a flaky/misleading 400 on table creation). Confirmed real against
`api.vince.live` — this settles the "does Tables exist" question from earlier in this
project's history; see that file for the corroborating detail (a decoded `__id` value
matching its claimed encoding exactly). Not yet wired into `converter/` - no IR/build.py
support exists for tables, this is ground truth for a future integration, same as the
Transform/REST API/Code notes were before they got documented.

## Capability matrix (what this tool can carry, as data)

Added 2026-09-15. Answers "can VinceForge carry this Classic function or
proposed architecture, and if not what would change that" — for us, for the
Migration Team, and for anyone evaluating a customer's files before promising
anything.

**The matrix is DATA, and the prose is generated from it:**

```
docs/capability-matrix.json      the source of truth - one row per construct
docs/capability-matrix.tmpl.md   the prose, with {{TABLE:<Area>}} placeholders
  -> converter/render_matrix.py  -> docs/vinceforge-capability-matrix.md
  -> converter/selftest_matrix.py   the drift check
```

**Why it is shaped this way.** A capability doc that silently drifts from the
converter reads with exactly the authority it had when it was true, and people
plan customer migrations on it — the `docs-unverified/` failure mode again,
this time self-inflicted. So **every row names the code symbol that enforces
it** (or says `none` deliberately), and `selftest_matrix.py` checks it both
ways: named symbols must still exist, and warning/refusal sites in the code
must be claimed by a row. Drift is a test failure, not something nobody
notices for six months. Rendering is deterministic and LLM-free
for the same reason — that is the step where a model could quietly reword a
verdict.

**Editing rules** (the JSON's own `_comment` block repeats them): never
hand-edit the generated `.md`; never change a verdict to match what you
believe the tool does — check the code, then change both; and **row ids are
permanent**, because the Migration Team guide cites them.

**The pre-push hook** warns (never blocks) when the rendered doc is stale or
the drift check fails.


## Architecture

```
converter/
  ir.py                 generalized IR - Workflow/Transaction/InputField/Cell/RowFilter,
                         source-agnostic (populated by any of the three sources).
                         Two fields exist only to be REPORTED, never translated:
                         Transaction.is_critical (Classic's IsCritical - "abort on
                         failure", but on what scope is unconfirmed) and
                         RowFilter.include_blank. Transaction.per_input_row is the
                         one that changes behaviour: False marks a call that RETURNS
                         rows rather than consuming one (Classic's <Export>), which
                         build.py ignores and the pipeline translator needs
  ids.py                 Live-format ID generation (transaction instance ids, workflowId)
  state.py                workflow-name auto-increment (RUN/converter_state.json)
  token_store.py           OPT-IN local persistence for the web UI's bearer token
                           (RUN/webui_token.json, gitignored) plus a paste timestamp -
                           the one exception to "never stored", added because this
                           install is single-user. CLI stays paste-fresh-every-time
  metadata.py              M3 field metadata: live metadata-endpoint fetch (direction-aware -
                           a field's I and O descriptions can disagree), then the offline
                           m3-field-catalog.sqlite for anything live didn't answer (always
                           warned as another tenant's data), then a static stub. Also the
                           unmapped-mandatory-input check
  source_classic_xml.py    PARSE for Classic XML - also owns source-classification logic
                           (Classic's raw "*"-syntax -> IR's CONSTANT/EXCEL_COLUMN/CLIENT/API_CHAIN
                           vocabulary), since that interpretation is Classic-specific.
                           Reads EVERY <Export>/<Import> block and groups them into UNITS,
                           one workflow each (parse_classic_xml_units) - a multi-block file
                           cannot be one workflow, see _parse_raw_units. parse_classic_xml
                           returns the first unit, for the one-file-one-workflow callers
  source_live_json.py      Live JSON as a source: parse_live_json() -> IR (generic building
                           block), and clone_and_retarget() - a direct JSON transform (NOT
                           routed through the IR/build pipeline - GET and POST don't share a
                           body shape, see the function's own docstring)
  source_prompt.py         STUB ONLY - worded-prompt source not designed yet
  source_app_folder.py     an app FOLDER as a source - scan a directory, classify every
                           file by shape (workflow / table meta / table rows / Classic
                           XML / Excel design file / App Builder source) into an
                           AppBundle. Read-only: no HTTP, no targeting - which is why it
                           only IDENTIFIES Classic XML (label, call count, what the
                           parser will drop) and leaves converting it to app_classic.py,
                           which needs both. Design files pair with the one Classic XML in
                           their folder, else by filename stem, else a person picks
                           (match_design_files) - nothing in the workflow JSON names one
  app_classic.py           the Classic-XML-in-an-app-folder stage: parse -> route each
                           BLOCK onto native or generic-pipeline (see App import section
                           above) -> fetch metadata (native only) -> build ->
                           write workflows/<stem>.generated.json, then APPEND to
                           bundle.workflows so the normal import path creates it. No
                           special case at POST time - a built workflow is already a
                           shape clone_and_retarget accepts, whichever path built it
  appimport.py             app import orchestrator - Classic XML converted first, then
                           tables (workflows reference them by name), then workflows,
                           then Live Groups for any source Classic XML file whose blocks
                           became 2+ workflows (AppWorkflow.group_key correlates them),
                           then the prompt. Writes a timestamped artifact dir per run
                           under RUN/app-imports/
  app_prompt.py            renders the App Builder prompt from the bundle + what the
                           import actually did. Template-driven; a folder can override it.
                           The result is written BOTH to the run artifacts and (by default,
                           not on a dry run) into the app folder as
                           app-builder-prompt.generated.md - which scan_app_folder skips,
                           or it would inline itself into the next prompt, forever
  liveapi.py               HTTP client for the confirmed api.vince.live endpoints
                           (workflows POST/GET, groups POST - bundles workflows so they
                           open together in VXL Live, custom-tables meta/aggregated/
                           search, plus the two-step design-file upload - presigned-URL
                           GET then a raw PUT straight to files.vince.live, the one call
                           here that deliberately does NOT go through _request: no auth
                           header, raw bytes, not JSON).
                           Confirmed endpoints ONLY - notably no row-write, because none is
                           confirmed. The ONE exception is get_table_meta(), which probes a
                           short candidate list and VALIDATES the body before accepting it,
                           because no read-a-table-definition endpoint is confirmed either
  live_fetch.py            pull workflows and tables OUT of Live as app-folder-ready JSON,
                           the read side of app import: writes workflows/<name>.json (the
                           GET response verbatim) and tables/<name>.json (+ .rows.json).
                           Writing is opt-in per call - the web UI wires that to one shared
                           "also save into this folder" toggle, so a fetch defaults to
                           look-don't-touch. Table definitions have to be PROBED for, see
                           liveapi.get_table_meta
  runs.py                  the READ-ONLY "Runs" inspector on the execution-results API:
                           one execution -> a step trace in RUN order (Live lists history
                           latest-first) with rows in/out and a tally of M3's per-row
                           <txnId>_MSG_COL messages; and a whole-tenant sweep (paged
                           workflow list, skips numRuns:0, stops on a 401). Flags a
                           count<total listing as partial instead of guessing a paging param
  history.py               the run-history log (RUN/webui_history.json), shared by the web
                           UI and appimport - lifted out of webui.py so appimport can
                           append without importing the web UI, which imports it
  cleanup.py               delete workflows THIS tool created, picked from the history -
                           refuses anything not in it, in another tenant, or renamed; a
                           delete counts only when a follow-up GET answers 404
  classic_to_pipeline.py   ir.Workflow -> pipeline_ir.Pipeline: the CLASSIC -> GENERIC
                           PIPELINE translator, for the control flow build.py's native
                           path cannot express (a gate on a column an M3 call WROTE, and
                           <BasedOnTransaction> in either direction). Emits ONE
                           m3RowPipeline Code step, not a chain of m3Bulk pairs - see its
                           docstring for the two reasons the bulk shape cannot carry a
                           Classic import (index correlation dies the moment a call is
                           gated; a per-call success verdict needs a single-transaction
                           POST). REFUSES an <Export>-only block, pointing at build.py.
                           The project's two open Classic questions live here, one named
                           thing each: row_key() and GROUP_RESULT_SCOPE
  build.py                 BUILD stage for the M3-API path - IR -> Live JSON. This is
                           where almost all the hard-won structural knowledge lives
  envelope.py              the POST /workflows top-level envelope (everything outside
                           `definition`), shared by build.py and build_pipeline.py so the
                           two can't drift on hasError/workflowType/tags. Also where the
                           `Imported` label (IMPORTED_LABEL) is appended unconditionally -
                           source_live_json.clone_and_retarget does the same for the one
                           path that doesn't go through here (live-clone + app-import)
  pipeline_ir.py           IR for the GENERIC pipeline path - an ordered CHAIN of
                           Transform/REST API/Code steps, plus the hand-written JSON spec
                           parser. Separate from ir.py on purpose: ir.py models one M3 API
                           step holding many transactions, this models a chain where every
                           binding is an expression we write ourselves
  m3_rest.py               calling M3 over ION API's bulk execute endpoint - the confirmed
                           endpoint/body/response shapes, the JSONata generator for the
                           Transform half of each call (including gates), and the Code-step
                           generator for the sequential-per-row escape hatch
  expand_code.py           the m3Expand Code step: Classic's <Expand> rebuilt on ONE sheet
                           (Live has no Expand). Nested M3 list levels + cached lookups,
                           rows keyed by the sheet table's header names, params from the
                           trigger form. Pipeline step kind "m3Expand"; hand-written specs
  build_pipeline.py        BUILD stage for the generic path: Pipeline -> Live JSON. Two
                           passes, because an m3Bulk step expands into TWO Live steps and a
                           step can reference a later sibling's stepId
  selftest_classic.py      stdlib-only self-tests for the CLASSIC path - run it directly.
                           Guards what fails SILENTLY there: the multi-block unit split,
                           grouping reaching the IR with the StartRun ordered first (so
                           BasedOnTransaction resolves), <Cell> read at every level, the
                           read-vs-write wording on a group-scoped call, and the
                           M3-written-column filter guard. Synthetic fixtures for the
                           cases no real file has (above all a WRITING StartRun), plus a
                           build of RUN/synthetic and both real customer files
  selftest_pipeline.py     stdlib-only self-tests for the generic path - run it directly.
                           Guards the things that would fail SILENTLY (step types, the
                           Transform+REST pairing, no excel_1, per-row correlation)
  selftest_classic_pipeline.py
                           self-tests for the CLASSIC -> PIPELINE translator - run it
                           directly. Its subject is CONTROL FLOW, where every error is
                           silent: the get-or-create gate's direction, a filter resolving
                           to the right OCCURRENCE of a repeated call, a block filter
                           gating the row rather than each call, an unrepresentable filter
                           being dropped loudly. Its last three tests EXECUTE the generated
                           Code step under node against a fake M3, which is the only way to
                           check behaviour rather than shape; they are skipped with a
                           message when node is absent, so the file stays stdlib-runnable
  selftest_app_classic.py  self-tests for app-import's per-BLOCK native/pipeline routing
                           (app_classic._unrepresentable_filter_operators and the
                           branching in convert_classic_xml) - run it directly. Covers the
                           routing decision itself, a native-safe block staying untouched,
                           a routed block's debug:false and Code-step shape, the graceful
                           fallback to native when no ION API root is given, and a mixed
                           two-block file routing each block independently - the whole
                           reason this is per-block, not per-file (a real CustomerA file:
                           3 of 8 blocks native-safe, 5 needing it)
  matrix.py                shared loading/derivation for the CAPABILITY MATRIX - the
                           data file, the worst-of-two-paths verdict, and the symbol
                           parsing the drift check needs. No network, no LLM
  render_matrix.py         capability-matrix.json + .tmpl.md -> the generated
                           docs/vinceforge-capability-matrix.md. `--check` reports
                           staleness and writes nothing (that is what the pre-push hook
                           calls). Deterministic on purpose: this is the step where an
                           LLM could quietly reword a verdict

  selftest_matrix.py       stdlib-only self-tests for the MATRIX - run it directly. Its
                           subject is DRIFT, which is silent by nature: nothing about
                           editing build.py tells you a doc three directories away just
                           became wrong. Checks both directions (named symbols must still
                           exist; warning/refusal sites in the code must be claimed by a
                           row), that the rendered doc is current, and that every row id
                           the Migration Team guide cites still resolves
  cli.py                   CLI entry point (classic-xml / live-clone / pipeline /
                           pipeline-scaffold / app-scan / fetch-workflow / fetch-table /
                           app-import)
  webui.py                 local web UI (stdlib only, no Flask). Uses the Target panel's
                           bearer token to fetch real M3 field metadata on a Classic XML
                           conversion, same as the CLI - without it every field falls back
                           to metadata.py's two-entry static table and the created
                           workflow's Excel headers literally read "TODO: metadata not
                           found for <program>.<transaction>.<field>" with type UNKNOWN.
                           It warns, naming the fields, whenever any placeholder survives.
                           The Source panel has a separate "Classic XML -> pipeline"
                           tab: the same file, converted onto the generic path by
                           classic_to_pipeline instead of build.py. Deliberately NOT
                           auto-detected - the same XML suits both and the native path
                           is the one to prefer, so the generic one is a decision you
                           make rather than one the text makes for you.
                           Panels top to bottom:
                           Source | Target (collapsed to a summary - the ids rarely change,
                           the token does, so the token stays visible), Fetch from Live
                           (workflow + table side by side, sharing one save-into-folder
                           toggle), Generate/Create actions, Generated JSON, and Import an
                           app folder. Run history is a header button opening a paged modal.
                           Both write paths (Create in Live, Import app) go through a
                           review modal first: what will be created, the target ids, and
                           the token's own tenant beside them with a red flag on mismatch

RUN/                     run artifacts - state files + sample inputs/outputs, NOT source code
  app-imports/           one timestamped dir per app-import run: the plan, the exact POST
                         bodies sent, the generated prompt, rows that couldn't be imported
  synthetic/             HAND-AUTHORED converter fixtures - the one thing in RUN/ that is
                         NOT captured data. Never cite one as a source or infer behaviour
                         from it; they exist so a parser/build change can be checked
                         against every construct at once. See its own README
archive/                 the original single-file script, superseded, kept for reference only
schemas/                 formal JSON Schema (see above)
docs-unverified/         see above - do not trust
```

### Why the architecture looks like this

Classic XML's `_classify_source`-equivalent logic (interpreting `"*A"`,
`"*Field1"`, `"*PROGRAM.Txn.FIELD"`) lives in `source_classic_xml.py`, not
`build.py` — it's Classic-syntax-specific inference that Live JSON and a
worded prompt don't need (they state their sources explicitly). `build.py`
never interprets raw source syntax; every `InputField` it sees already has a
resolved `source_kind`. Cross-transaction references (`based_on`, API-chain
refs) use an IR-internal `ir_id` per transaction, not Live's real instance
id — `build.py` generates real ids once and maps `ir_id -> real id`. For a
`Transaction` parsed from Live JSON, `ir_id` is just Live's own real instance
id reused directly, so a round trip needs no remapping.

## Running it

Web UI (the normal way to use this):
```bash
cd converter
python webui.py
```
Then open `http://127.0.0.1:8420`. Nothing auto-starts this after a reboot —
you have to run it again yourself.

**The server reads the code once, at startup — restart it after editing any
`converter/*.py`, or you are testing the old code.** This bit once, badly: a
three-day-old server kept serving pre-change code through several restarts,
because `ThreadingHTTPServer` defaults `allow_reuse_address = 1` and on
Windows that lets a second process bind a port another one is already
serving — the new server prints its banner and looks fine while the browser
is still answered by the old one. `webui._Server` now sets it to `False`, so a
second start fails loudly and tells you how to find the stale PID. If the UI
ever seems to ignore a change you just made, check for two `webui.py`
processes before doubting the change.

Closing the terminal stops the server;
run history (`RUN/webui_history.json`) and the name counter
(`RUN/converter_state.json`) both persist across restarts since they're
files, not in-memory state.

CLI:
```bash
python converter/cli.py classic-xml <classic.xml> <connection_id> <tenant_id> <environment_id> <output.json> [bearer_token]
python converter/cli.py classic-pipeline <classic.xml> <connection_id> <tenant_id> <environment_id> <output.json> --base-url <ion_api_root> [--column-headers <map.json>]
python converter/cli.py live-clone <source.json> <connection_id> <tenant_id> <environment_id> <output.json>
python converter/cli.py pipeline <spec.json> <connection_id> <tenant_id> <environment_id> <output.json>
python converter/cli.py pipeline-scaffold <output.json>
python converter/selftest_pipeline.py   # not a CLI subcommand - run it directly
python converter/selftest_classic.py    # ditto, for the Classic XML path
python converter/selftest_classic_pipeline.py   # ditto, for Classic -> generic pipeline
python converter/selftest_matrix.py     # ditto, for the capability matrix (drift check)
python converter/selftest_app_classic.py   # ditto, for app-import's native/pipeline block routing
python converter/selftest_metadata.py   # ditto, for field metadata (merge, offline catalog, mandatory check)
python converter/selftest_runs.py       # ditto, for the Runs inspector (trace order, partial listings, sweep)
python converter/selftest_cleanup.py    # ditto, for deleting created workflows (every refusal path)
python converter/selftest_expand.py     # ditto, for the m3Expand Code step (Classic <Expand> on one sheet), run under node
python converter/cli.py runs <workflow_id> <bearer_token> [--exec <WFEXEC-id> | --latest]
python converter/cli.py runs-sweep <bearer_token> [--match <name text>] [--include-idle]
python converter/render_matrix.py       # regenerate the capability-matrix doc
python converter/render_matrix.py --check   # is it stale? writes nothing
python converter/cli.py app-scan <app_folder>
python converter/cli.py fetch-workflow <workflow_id> <bearer_token> [--into <app_folder>] [--out <file.json>]
python converter/cli.py fetch-table <table_name> <bearer_token> [--rows] [--into <app_folder>] [--out <file.json>]
python converter/cli.py app-import <app_folder> <connection_id> <tenant_id> <environment_id> [bearer_token] [--dry-run] [--no-folder-prompt] [--ion-api-base-url <ion_api_root>]
```

## Auth / targeting

- Real, confirmed API base: **`api.vince.live`**. Not `api.vincelive.dev`
  (see docs-unverified above) — that's a different, unconfirmed thing.
- Bearer token: a short-lived Cognito ID token captured manually from Live's
  own web UI (DevTools → Network tab). By default not stored anywhere in
  this project; the CLI's optional token arg expects a fresh paste every
  time. **Exception:** the web UI persists it locally, opt-in, via
  `converter/token_store.py` — `RUN/webui_token.json` (gitignored), with
  the paste timestamp so the UI can show how stale it is instead of just
  failing with a 401. Added because this install is single-user; a shared
  install should not turn this on lightly. Expect ~401s after an hour or so
  of session idle time - that's the token expiring, not a bug.
- **The token, not the tenantId you send, decides which tenant you act on -
  and pairing it with another tenant's connection id fails SILENTLY.** No
  401: the connection is just invisible, so metadata lookups answer
  `{"count": 0, "items": []}` and every field degrades to
  "TODO: metadata not found ...". Diagnosed the hard way; see the auth
  section of `docs/vince-live-api-reference-prompt.md`.
  `metadata.tenant_from_token()` reads the tenant from the token's claims,
  the web UI warns on a mismatch and refuses to create, and
  `cli.py probe-metadata ... [--explore]` prints the token's tenant next to
  the connection so the pairing is visible before you interpret anything.
- **The ION API root is a fourth target value, needed only by the generic
  pipeline paths** (`cli.py pipeline` / `classic-pipeline`, and the web
  UI's pipeline tabs). It is the ION API base INCLUDING the tenant
  segment, e.g.
  `https://mingle-ionapi.eu1.inforcloudsuite.com/<TENANT_SEGMENT>`, and a
  generated M3 bulk-execute endpoint is built from it. Nothing can derive
  it - region and tenant segment are both per-tenant - so copy it from a
  real REST API step in the target tenant. It lives in a Target Tenant
  preset alongside the three ids (`ION_API_BASE_URL` in `.env`), and a
  conversion without it still produces a pipeline but refuses to build
  Live JSON, with a message saying why.
- Default connection/tenant/environment: copy `.env.example` to `.env` and
  fill in your real ids (`CONNECTION_ID`/`TENANT_ID`/`ENVIRONMENT_ID`) -
  `.env` is gitignored, real ids don't belong in tracked source or docs.
  `converter/tenants.py` reads it to seed a "Default" Target Tenant preset
  in the web UI on first run. No `.env`? The tool still runs fine, you just
  start with zero presets and add one manually via "+ Add" instead.
  **Careful copying the whole example:** `tenants.py` seeds that preset
  whenever all three ids are non-empty, and the example's `xxxx` placeholders
  ARE non-empty - so a wholesale copy seeds a preset made of junk ids.

- A malformed connection/tenant/environment id does NOT fail validation at
  creation time but silently breaks M3 API step rendering in the builder
  UI - always sanity-check the format first if a created workflow renders
  broken.
- **Testing gotcha, observed 2026-09-11: the Excel add-in caches which
  workflows it can run, and needs a manual refresh to see a
  newly-created or just-edited one.** After that refresh, it can still
  take a couple of minutes before the new/changed version actually becomes
  available to run - not instant. A test run done too soon after creating
  or editing a workflow may silently execute a STALE version (or fail to
  find the workflow at all), which looks exactly like "my fix didn't
  work" from the outside. When a run's result contradicts what a code
  change should have done, rule this out - wait past the delay and retest
  clean - before concluding the code is wrong.

## Known gaps / not yet built

- **Worded-prompt source** — deliberately deferred. When picked up, it needs
  an LLM-assisted parse step (drafting the IR conversationally, confirming
  ambiguous fields) rather than a deterministic parser like the other two.
- ~~**Classic XML parser only reads the FIRST `<Export>` and FIRST
  `<Import>`**~~ — **FIXED 2026-09-10.** Every block is read and grouped
  into **units**, one workflow each, because Live carries a single sheet,
  data row and filter gate per workflow while Classic states those per
  block — and the real files disagree across blocks (`C05_MMS100` declares
  InputStartRow 3 and 4; "Existing Price Lists" gates one block on
  `BH == "OK"` and the other not at all). The first `<Export>` and first
  `<Import>` stay paired in unit 1, so a file with at most one of each
  converts exactly as before. `parse_classic_xml_units` returns them all,
  `parse_classic_xml` returns the first. App import creates each as its own
  workflow named after its own block Label, `cli.py classic-xml` writes
  numbered sibling files, and the web UI builds all of them and lets you
  pick which to create. Still unconfirmed: whether Classic runs a file's
  blocks as one sequential unit or as independent steps — the split is
  forced either way, but run order is an open question.
- ~~**ImportDetail-level `<Cell>` elements**~~ — **FIXED 2026-09-10**,
  along with `<StartRun>`-level ones. `<Cell>` is read wherever Classic
  writes one and becomes an ordinary `excel_1` column. This was the
  blocker for the mixed-filter case: a RowFilter names a column, and the
  producing call is the one whose Cell writes it.
- **Classic's grouping (`<ImportHeading GroupColumns>` / `<StartRun>`) is
  parsed but deliberately NOT translated.** Confirmed semantics (2026-09-10,
  from a real Classic run): the StartRun call fires once per group, on the
  group's first row, with group breaks detected by comparing only to the
  PREVIOUS row (so the sheet must be sorted by the group columns); its
  `<Cell>` result lands on that first row alone with no fill-down; and
  per-row `RowFilterEQ`s reading that column therefore pass on a group's
  first row and fail on every other row in the group. **Corrected
  2026-09-23:** this used to say Live's M3 API step has no group concept at
  all - that went past the evidence. Live documents a "Group by" option on
  the M3 API step (product docs, "Group by" under M3 API; PPS100/OIS100-style
  header/line examples), and every captured transaction already carries
  `hasGrouping`/`extendedGrouping` - always `false`/`[]`. Neither the
  populated JSON nor the runtime semantics (once per group? previous-row
  break like Classic, or all equal values?) is confirmed; one hand-built
  Group-by workflow, GET back and run on a sorted and an unsorted sheet,
  settles it. Until then `build.py` does not use it and reports the divergence
  (workflow-level warning plus a `_needs_review` on the call, telling a
  read apart from a possible write) instead of quietly emitting a per-row
  call. `ir.Workflow.group_columns` and `ir.Transaction.runs_once_per_group`
  carry it. **The generic pipeline path DOES translate it** (`cli.py
  classic-pipeline`): the generated Code step fires a once-per-group call on
  a group's first row, breaking against the previous row only. One half of
  the semantics is still an assumption there rather than a fact - whether a
  group's 2nd..nth rows run at all - and it lives in
  `classic_to_pipeline.GROUP_RESULT_SCOPE`. See the gap below.
- **Custom Tables** — partly integrated. App import creates tables via
  `liveapi.create_table_meta` and the web UI's app panel shows them, but
  there is still no IR support and no `build.py` logic for tables, so a
  workflow that writes to one can only come from a source that already has
  the Table Updater step in it. **Row writing is not possible at all** - no
  confirmed endpoint exists, and **reading a table's definition isn't
  confirmed either** - `fetch-table` probes a candidate list and validates
  what comes back rather than assuming. One run with a real token would
  settle it; write the answer into `docs/vince-custom-tables-guide.md` and
  collapse `liveapi.META_READ_CANDIDATES` to it (see
  `docs/vince-app-import.md`). A separate
  "Vince Platform API" (OAuth2 client_credentials, tenant-specific Cognito
  domains) is a DIFFERENT, still-unconfirmed thing - don't conflate the two.
  Don't design or build against `docs-unverified/`.
- ~~**REST API step's `"body"` mode**~~ — **CONFIRMED**, along with
  `version: 2`, `forEach` and `skipIfExpression`. See
  `docs/vince-live-api-reference-prompt.md` §1/§1b. Still open there:
  `forEach`'s real behaviour (documented but beta, and its own doc
  contradicts itself on the version number) and `skipIfExpression`'s
  expression syntax (name confirmed from the implementing ticket, syntax
  documented nowhere).
- ~~**`<Import>`-level `<RowFilterEQ>` is silently dropped**~~ — **FIXED.**
  Classic has TWO filter levels: one inside an `<ImportDetail>` (per call)
  and one as a *sibling* of the details, gating the whole block. Only the
  first was ever read, so a converted workflow ran for rows Classic would
  skip. `_parse_block_row_filters` now reads the block level into
  `ir.Workflow.row_filters` (direct children of `<Import>` only — a
  recursive search would re-sweep every per-detail filter), and `build.py`
  puts those straight into `generic_filter_1` with no uniformity check,
  because a whole-block gate is exactly that step's semantics. The
  per-transaction level keeps its existing uniform-or-flag rule.

- **`RowFilterGT`/`GE`/`LT`/`LE` and `RowFilter/@IncludeBlank`: parsed and
  reported on the native path, not translated there.** The operator set is
  open-ended — `_is_filter_wrapper` matches any `RowFilter*` element and
  keeps the suffix as the operator, so a wrapper we have not met yet arrives
  as a reported unknown instead of vanishing. What Live's native M3 API path
  can be *given* is the separate, much smaller set in
  `build.REPRESENTABLE_FILTER_OPERATORS` (EQ/NE only — the only operators
  whose JSON value we have captured. **Corrected 2026-09-28:** this used to say
  "a real platform limit". It isn't: Live's Generic Filter UI offers
  Equal, Not Equal, less than, less than inclusive, greater than, greater than inclusive, is blank, is not blank, contains, do not contain (Haakon, from the UI). **Their JSON
  is now CAPTURED** (`RUN/captures/generic-filter-all-operators.json`,
  `build.CAPTURED_FILTER_OPERATORS`): `lessThan`, `lessThanInclusive`,
  `greaterThan`, `greaterThanInclusive`, `isBlank`, `isNotBlank`,
  `stringContains`, `stringNotContains` - the blank pair saved as
  `source: "CONSTANT"`, `value: "''"`, with no trigger field. Still NOT emitted:
  every condition is saved `dataType: "STRING"`, so a range check may compare
  lexically (`"9" > "10"`), and the UI nests conditions left-deep
  (`{"all": [{"all": [a, b]}, c]}`) where `build.py` writes one flat `all`
  (flat is confirmed for two only - this also applies to EQ/NE filters with
  3+ conditions TODAY). One real run settles both, and could put most of the
  CustomerA GE/LE blocks back on the native path). Per condition the UI also
  offers source Client/Constant, data type number/string/boolean, and logic
  AND/OR (Haakon, 2026-09-28). **A second capture settled the saved shape**
  (`RUN/captures/generic-filter-datatypes-and-or.json`): dataType is
  `"NUMBER"`/`"STRING"`/`"BOOLEAN"` (NOT `NUMERIC` like the M3 step), repeated
  on the condition, the trigger field and `workflowConfig.inputs`; a
  condition's `logicalType` is the connector to the condition AFTER it, and the
  level joining the two becomes `"any"` for OR. `build.py` now emits the UI's
  left-deep nesting (`_nest_filter_conditions`, identical to before for 1-2
  conditions), and `source_live_json.parse_live_json` now walks the nesting -
  it used to read only the top level, turning a nested block into a condition
  and every non-`equal` operator into NE (no caller, so nothing was affected).
  STILL OPEN, needs one real RUN: whether a `NUMBER` range compares
  numerically, and a CONSTANT source with a real value (not captured). Only then
  should GT/GE/LT/LE join `REPRESENTABLE_FILTER_OPERATORS`, with dataType NUMBER).
  **Later the same day, both settled by real runs**: a `NUMBER` range compares
  numerically (C=50 passed `lessThan 100`), and a CONSTANT condition with a
  value (`value`=`defaultValue`=`"5000"`, no trigger field) is the fixed,
  non-editable filter matrix row `filter-hidden-value` was waiting for. Neither is
  wired into `build.py` yet. **And the execution-results API is CONFIRMED**
  (`docs/vince-live-api-reference-prompt.md` §1e): `GET
  /v1/workflows/<id>/results[/<WFEXEC-id>[/data]]` returns each run's real
  input rows and, per step, `recordsProcessed` plus that step's own output rows
  and M3's per-row message - the only way found so far to see where a run
  passed or dropped rows. Not built into VinceForge yet; an obvious "inspect
  runs" feature, and the basis for a post-import smoke test. Anything else
  leaves the gate off there and says so: a block-level one as a conversion
  warning, a per-transaction one as `_needs_review`, and `peek_classic_xml`
  names the operators from the read-only scan so they show in the
  app-import plan and confirmation box before anything reaches a tenant.
  **GT, GE, LT and LE all translate on the generic-pipeline path**
  (`classic_to_pipeline.FILTER_OPERATORS`, `pipeline_ir.GATE_OPERATORS` —
  GE/LT/LE added 2026-09-16, sourced from a real CustomerA customer file set
  whose `<Import>` blocks lean heavily on GE/LE, not just GT/EQ/NE; LT had
  been defined in `pipeline_ir.GATE_OPERATORS` for a while but never wired
  into the Classic translator until then). `IncludeBlank="true"` on a
  range operator with an empty `Value` is carried on
  `ir.RowFilter.include_blank` and repeated in the review note, but its
  meaning is not confirmed — it reads self-contradictory; don't guess it.
  App import still only ever uses the native path for Classic XML, so a
  file needing GE/LE/LT has to be converted via `classic-pipeline` on its
  own for now — see the App import section above.

- **A gate on a column an M3 call WRITES can never work natively — now
  detected.** Found 2026-09-11, straight out of `<Cell>` becoming
  readable. `GENERIC_FILTER` sits between the trigger and the M3 API step
  and filters **raw spreadsheet rows**, so a filter on a column a
  transaction *writes* reads an empty cell at the only moment it is
  evaluated. This is not an operator limitation — it holds for `EQ` as
  much as for `GT` — and splitting transactions across several M3 API
  steps would not help either, since no placement of a Generic Filter can
  see an M3 call's output. Both real customer files are this shape (C05
  gates on P/Q, Existing Price Lists on BK/BM/BO). Such a filter is now
  never emitted at either level and is reported with the column **and**
  the transaction that writes it. At block level that is a genuine bug
  fix rather than a better message: a block gate goes into
  `generic_filter_1` with no uniformity check, so one on an M3-written
  column used to be emitted and silently filter on an empty cell.

- ~~**Per-transaction/mixed Classic RowFilters have an answer but not an
  automated one**~~ — **BUILT 2026-09-11.** `converter/classic_to_pipeline.py`
  (`cli.py classic-pipeline`) converts a Classic file onto the generic path,
  where each call carries its own condition and a condition can read an
  earlier call's own result. It translates per-call and block-level
  `RowFilter`s, `<BasedOnTransaction>` in both directions (including the
  get-or-create form — "Add Price List" adds only when `GetPriceList`
  FAILED), grouping, and the whole of both real customer files. It emits one
  **Code step**, not a chain of `m3Bulk` pairs as the pipeline doc
  originally sketched, and the reasons are structural: index correlation
  between bulk steps dies the moment a call is gated (Classic gates nearly
  all of them, nine deep in one real block), and a per-call success verdict
  needs a single-transaction POST, since the bulk response's only confirmed
  failure signal is the aggregate `nrOfFailedTransactions`. `build.py`'s
  native path is still the default: prefer it whenever it fits, and note
  that an `<Export>`-only block is refused on the pipeline path on purpose.

- ~~**The Classic→pipeline translator rested on TWO unconfirmed facts,
  each isolated behind one named thing.**~~ — **BOTH CONFIRMED 2026-09-11.**
  Everything else in the translator is derived from the file; these two
  needed a real run.
  1. ~~**How a spreadsheet row is addressed**~~ — **CONFIRMED 2026-09-11**,
     against a real run in Live (`WFEXEC-08330923...`, workflow
     `Extract Price Lists 4`): a VXL-triggered spreadsheet row IS keyed by
     column LETTER, not header name, unlike every widget-triggered example
     on record. `classic_to_pipeline.row_key()` needed no change. That same
     run caught a REAL bug hiding behind this question's uncertainty:
     `pipeline_ir.M3RowPipelineStep.rows_js` defaulted to
     `"trigger.body.data"`, but a VXL trigger's `body` is a bare array, not
     `{data: [...]}` - so every generated row pipeline was silently
     processing zero rows. Fixed to `"trigger.body"`. Also fixed:
     `m3_rest.py`'s row-lookup comment and `docs/vince-generic-m3-pipeline.md`
     open question 4, which both still said this was unconfirmed.
     **Follow-on caveat, confirmed the same day in a second run:** the
     letter is not the worksheet's real column - VXL's read feature trims
     entirely-empty LEADING columns and re-indexes what's left starting at
     "A". An Import-only conversion is especially exposed: the columns its
     paired `<Export>` would have written sit blank (the Import side never
     touches them) and get trimmed, shifting every later column left by
     however many were empty. See the comment at `row_key()`.
  2. ~~**How far a once-per-group call's result reaches**~~ —
     `classic_to_pipeline.GROUP_RESULT_SCOPE`, default `"group"`.
     **CONFIRMED 2026-09-11**, in a later run on the same workflow once the
     `rows_js` bug above was fixed (`WFEXEC-3cca6a49...`): with two rows
     sharing one group key, the once-per-group `SelUsers` call fired on row
     1 and was correctly skipped on row 2 (`groupRepeatsSkipped: 1`) — and
     row 2's own `AUTCHKMI_ChkAuthority` still came back `"ok"`, because it
     read row 1's carried result instead of seeing `"notrun"`. That is
     exactly the "group" behaviour the constant claims. Not a coin toss to
     begin with: C05_MMS100's "Create RO" groups by `B C E O` (one
     destruction order) and its `MMZ101MI.AddLine` — one LINE per row — is
     gated on `P`, the column its once-per-group `ChkAuthority` writes;
     under the literal sheet reading (`"firstRow"`) exactly one line per
     order would ever be added, which the real run confirms is not what
     happens. `selftest_classic_pipeline.py` still demonstrates the
     divergence if the constant is flipped.

- **What `IsCritical` means is unconfirmed, so it is parsed and reported
  only.** New 2026-09-11: `<ImportDetail IsCritical>` / `<ImportHeading
  IsCritical>` were being dropped without a word, which in a file where
  nearly every detail declares it is exactly the kind of silent loss this
  project exists to avoid. `ir.Transaction.is_critical` now carries it and
  both paths report it. It is NOT mapped to behaviour: the name suggests
  "abort on failure", but whether that aborts the row, the block or the
  whole run decides whether a half-written row is left behind, and nothing
  confirms which. One real Classic run with a deliberately failing critical
  call settles it.
- **Excel "Design file" — upload BUILT, the link-back still open.** A Live
  workflow can carry an uploaded Excel sheet. Confirmed and implemented:
  `GET /v1/files/tenant/upload-url/<WORKFLOW-id>/designFile/<name>.xlsx`
  returns a presigned descriptor, then `PUT` the bytes to the returned
  `files.vince.live` URL with `Content-Type: */*` and no auth header (the
  signature is in the query string). App import pairs `<stem>.xlsx` with
  `<stem>.json` and uploads after each workflow is created, since the key
  embeds the id Live assigns. **ANSWERED 2026-09-29:** the server does NOT
  write `workflowConfig.hasExcelDesignFile`/`excelDesignFileKey` - all 4
  real CustomerA uploads came back "uploaded but NOT linked". **The builder's
  PUT is now captured** (`PUT /workflows {tenantId, workflowId, definition}`,
  a HAR capture of the builder), and app import does the same
  after each upload: GET, set the two fields, PUT, GET to verify
  (`appimport._upload_design_file`, `liveapi.update_workflow`). Not yet run
  by this tool against Live.

- **App scaffolding (tables + workflows + App Builder prompts via one
  Claude-driven flow)** — the *import* half now exists: point app import at
  a folder and it lands the tables and workflows and generates the prompt
  (`docs/vince-app-import.md`). What's still missing is the *authoring* half
  — having Claude draft that folder's contents from a description, which
  needs the worded-prompt source above first.
- **App import can only create, never update** — no diff against what's
  already in the target tenant, no delete, and no way to push a changed
  workflow DEFINITION over an existing one. Re-importing no longer
  duplicates workflows (see the naming/tagging/grouping entry above -
  Live rejects a duplicate name and this tool now skips it, same as
  tables); it just means a workflow whose source changed is left as-is in
  the tenant rather than updated. Deciding what "update an app" should
  mean is unstarted.

- **A row pipeline's `debug: true` self-poisons the sheet it runs
  against — CONFIRMED 2026-09-11** (`WFEXEC-7684f109...`). A Manual-trigger
  run bound to a spreadsheet range writes the Code step's return value
  back into the SAME source sheet, and since `debug: true`'s shape
  (`{records, _diagnostics, _aborted}`) isn't the bare array the write-back
  expects, it can't expand into a table - it dumps the whole return value
  as one giant JSON string into a single cell (observed: cell A2). That
  cell then gets read back in as row 1's data on the NEXT run: a
  self-poisoning feedback loop, not a cosmetic export-shape mismatch. This
  is very likely what explained the "mystery" stray rows earlier in the
  same session, before the mechanism was understood. `debug` must be
  turned off - and the polluted cell cleared by hand - before any run that
  reuses the sheet; see the warning in `build_pipeline.py` and the
  `M3RowPipelineStep.debug` comment in `pipeline_ir.py`.
  **A second bug, found while chasing this one, and FIXED in code same
  day:** flipping `"debug": true` to `"debug": false` directly in an
  already-pasted Live Code step and re-running did nothing - the generator
  used to pick between two fixed return statements in PYTHON at generation
  time, so the `debug` value in the deployed PLAN object was inert data the
  JS never read. The runtime JS now reads `PLAN.debug` itself
  (`m3_rest._RETURN_RECORDS`), verified by regenerating and finding the new
  ternary in the emitted code.
  **UNRESOLVED as of end of day:** a brand-new workflow built with this fix
  (`WORKFLOW-a8f1da15fb2995194a36d49840242826`, "Extract Price Lists 11")
  still showed the same self-poisoning symptom on its first run. Two
  explanations are live and neither is confirmed: (a) the code fix is
  incomplete or wrong in some way not yet identified, or (b) the test was
  a false negative - **the Excel add-in needs a manual refresh after a
  workflow is created or changed, and can take a couple of minutes past
  that refresh before the new version is actually available to run** (a
  separate, newly-noticed operational hazard, confirmed by direct
  observation but not yet tied definitively to this specific symptom).
  Do not trust a repeat of this test unless it deliberately waits well
  past that propagation delay first. What a genuinely clean (non-debug)
  run's write-back looks like given a proper bare array is therefore
  still not observed.
