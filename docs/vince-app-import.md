# App import — folder → tables + workflows + an App Builder prompt

Imports a whole **app** into a target tenant from a folder on disk: its
Custom Tables and workflows go in via the API, and the part that has no API
— the App Builder app itself — comes out as a generated prompt.

Web UI: the **Import an app folder** panel. CLI: `app-scan` / `fetch-workflow` /
`fetch-table` / `app-import`.

## What an app folder looks like

```
MyApp/
  app.json                     optional manifest: name, description, anything else
  tables/*.json                POST /v1/custom-tables/meta bodies, or captured meta responses
  tables/*.rows.json           optional seed rows (see "Rows are never imported")
  workflows/*.json             GET /workflows/{id} responses, or POST bodies
  workflows/*.xlsx             a workflow's Excel "Design file" (see below)
  *.xml                        Classic "VinceExcelConfig" exports (see below)
  app/                         App Builder source: .java, .html, .css, .js, ...
  app-builder-prompt.md        optional prompt template, replaces the default layout
```

**The subfolder names are a convention, not a requirement.** Every `.json`
anywhere under the root is classified by its *shape*, so a flat folder works
identically:

| Shape | Classified as |
|---|---|
| `definition.workflowConfig` is an object | workflow |
| has `tableName` **and** one of `primaryKeys` / `schema` / `updateType` / `displayOrder` | table meta |
| a bare array of objects, or `{"count": N, "items": [...]}` | table rows |
| a `.xml` whose root is `VinceExcelConfig` (or that has an `Export`/`Import` block containing `M3Api`) | Classic XML, converted on import |
| `.xlsx` / `.xlsm` / `.xltx` / `.xls` | a workflow's Design file |
| anything else readable as text | App Builder source file |

Rows attach to a table by filename stem: `prices.rows.json`,
`prices.data.json`, `prices-rows.json` or `prices/rows.json` all attach to
the table named `prices`, and rows inline on the table file itself (`rows`
or `items`) work too. A rows file matching no table is reported and ignored.

Anything unreadable as UTF-8 (images, jars) is listed in the plan but not
inlined into the prompt. Source files are capped at 200 KB each and 1 MB in
total, inlined smallest-first so one big file can't crowd out the rest;
anything skipped says so, in the plan and in the prompt.

## Design files (the Excel sheet attached to a workflow)

A Live workflow can carry an uploaded Excel sheet — the builder calls it a
**Design file**, and can import field mappings from it. Pairing
(`source_app_folder.match_design_files`, changed 2026-09-29) goes in this
order:

1. **A person's own choice** (web UI: the "Upload to" dropdown on each
   design file in the plan; CLI: a numbered prompt; API:
   `run_app_import(..., design_matches={design path: target or ""})`).
2. **Folder**: the sheet's folder holds exactly one Classic XML and no other
   design file, so that XML is its function, whatever either file is
   called. Real customer folders don't keep the names in step (CustomerA:
   `Classic_MMS001_MassUpload.xml` next to
   `MMS001_MassUpload_1.0.xlsx`).
3. **Name**: an exact filename-stem match. If the folder has several XMLs,
   only those are considered; otherwise every workflow in the bundle is,
   which is how `workflows/pricelist.json` pairs with `pricelist.xlsx`.
4. **Otherwise it asks.** The file is flagged `needsChoice` and isn't
   uploaded. A real (non-dry) import in the web UI refuses to start until
   each flagged file has a target or "do not upload". The same happens when
   two sheets would land on the same workflow.

```
f/Alpha.xml  +  f/whatever.xlsx        (folder: the only XML there)
f/One.xml, f/Two.xml  +  f/Two.xlsx    (name, among that folder's XMLs)
workflows/pricelist.json + pricelist.xlsx   (name)
```

A Classic XML's sheet goes on its FIRST block's workflow (the plain
`<stem>.generated.json`).

It pairs on where the files sit rather than on anything in the JSON,
because **the workflow JSON has no field naming its design file**. The link
is `excelDesignFileKey`, which is written only after upload and embeds a
workflow id that doesn't exist until Live assigns one. An unresolved sheet
is **not uploaded**: putting a sheet on the wrong workflow is worse than
not putting it anywhere.

### It uploads after creation, and cannot be dry-run

The storage key is `<workflowId>/designFile/<filename>`, and Live assigns
the workflow id itself at creation regardless of what is POSTed. So the
upload is a step *inside* the workflow phase, immediately after each
successful `POST /workflows`, using the id read back from the response.
There is nothing to dry-run against; a dry run lists the file and says so.

The upload itself is two confirmed calls (see
`docs/vince-live-api-reference-prompt.md`, "Design file"):
`GET /v1/files/tenant/upload-url/<key>` returns a presigned descriptor,
then the bytes go by `PUT` straight to `files.vince.live` — never through
`api.vince.live`, and with no `Authorization` header, since the signature
is in the query string.

### It verifies rather than assumes, on purpose

What the capture does **not** settle is who writes
`hasExcelDesignFile`/`excelDesignFileKey` into the workflow: the builder
issues a workflow `PUT` right after its own upload, so the client may own
that — and no workflow `PUT`/`PATCH` endpoint is confirmed for this tool to
call. So after uploading, the importer **GETs the workflow back and reads
those two fields**, and reports one of:

- `uploaded and linked` — the workflow references the file. Done.
- `uploaded but NOT linked` — the file is in storage and nothing points at
  it. Reported as a failure *with the key*, because that key is exactly
  what a manual fix needs. This outcome also answers the open question, and
  should be written into the API reference when it first occurs.
- `uploaded, link unverified` — the read-back call itself failed.

## Classic XML in an app folder

A folder can hold Classic exports alongside its JSON. On import each one is
converted to a workflow POST body and **written back into the folder** as
`workflows/<xml stem>.generated.json`, then created in Live by the ordinary
workflow path. The `.xml` files are never modified and never sent anywhere.

Why it lands as a file rather than being converted in memory: the converted
JSON is the thing you review, diff and hand-edit, and on a customer
migration it is also the artifact that says what was actually created. A
`.xml` that isn't a Classic export (an app's own config file, say) is still
treated as App Builder source, as before.

Four rules worth knowing:

- **The XML is the source of truth.** Every import re-converts it and
  overwrites its `.generated.json`, warning that it did. Hand-edits to the
  generated file do not survive; edit the XML, or rename the file so it
  stops matching.
- **A `.generated.json` whose source XML is present is never imported on its
  own** — it would create the workflow twice. If the XML is gone, the
  leftover file is imported as an ordinary workflow with a warning that it
  looks stale.
- **The workflow name is `"<your chosen base name> - <block's own Label>"`**
  (updated 2026-09-16 — was the block `Label` alone), still matching app
  import's names-are-preserved rule rather than the web UI's auto-increment:
  the composed name is what's kept, verbatim, not incremented. The base name
  is asked for once per source XML file (CLI: an `input()` prompt suggesting
  the file's stem; web UI: an editable field per file in the scan/plan
  display) - see `source_classic_xml.compose_workflow_name` and
  `app_classic.convert_classic_xml`'s `base_names` param. The *file* is still
  named after the XML's stem, because labels are neither unique nor
  filesystem-safe.
- **Every workflow app import creates that reads from the spreadsheet also gets the `Imported` label** (one with no sheet-fed input does not, 2026-10-08: the label broke saving in Live when no import field was mapped), and
  a source XML file whose blocks produced 2+ successfully-created workflows
  gets them bundled into a real Live Group (so they open together in VXL
  Live) named the same as their shared base name — see
  `docs/vince-live-api-reference-prompt.md` §1d for the confirmed
  `POST /groups` request/response this is built from.
- **A dry run still writes these files.** That is a deliberate exception to
  the rule that governs the generated prompt (which a dry run doesn't even
  generate, as of 2026-09-16 — see below): a prompt embeds run statuses and
  workflow ids, so a dry-run copy would lie, while a converted workflow is a
  pure function of the XML, the target ids and the field metadata —
  byte-identical to what a real import writes. Convert, inspect, then
  commit is the point of a dry run. It does mean the target ids matter on a
  dry run, so `check_target_ids` runs for one too when the folder holds XML.

### Two things it will warn you about, loudly

**Dropped M3 calls.** The Classic parser reads only the first `<Export>` and
the first `<Import>` in a file (a pre-existing limitation, see CLAUDE.md).
The scan now counts the `M3Api` calls it will actually read against the
total and says the difference out loud — *"4 of its 6 M3 call(s) will NOT be
converted"* — in the plan, in the confirmation box, and in the run
warnings. On a single conversion you would probably notice a half-built
workflow; in a folder of eighty being migrated for a customer, you would
not. Split the extra block into its own XML file.

**Fields with no metadata.** Without a usable token, or for a transaction
the metadata endpoint doesn't know, fields fall back to `metadata.py`'s
small static table and the rest get a column header reading
`TODO: metadata not found for ...` with type `UNKNOWN`. Such a workflow is
still converted and still created — reported per file, in the run warnings
and in the history — rather than held back. Nothing is silently degraded,
but keeping or fixing it is the operator's call.

Per-file warnings stay with the file. Only the two above are promoted to the
run-level warning list, so a folder of eighty XMLs doesn't bury them.

### A block can auto-route onto the generic-pipeline path

Added 2026-09-16. Each **block** (not file — see below) is built on the
native path by default, same as always, unless it has a `RowFilter`
native cannot express (`build.native_filter_problem`). **Since 2026-09-28
that means a range (`GE`/`GT`/`LT`/`LE`) on a non-numeric value** - e.g. the
CustomerA set's hundreds of `GE '!'` - because a numeric range is now emitted
natively as a run-confirmed `NUMBER` comparison. Such a block instead routes onto
the generic-pipeline path (`docs/vince-generic-m3-pipeline.md`), which needs
the target's ION API root: pass it as `--ion-api-base-url` on the CLI, or
the app-import panel's equivalent field in the web UI. Omit it and an
affected block still gets built — on the native path, with the filter gate
left off and a loud `PIPELINE BUILD FAILED` warning naming why — never a
hard failure of the whole run.

This is per-**block**, not per-file, because filter needs genuinely differ
within one file: a real CustomerA customer file (`Creation_CentralRef`,
8 blocks) splits 3 native-safe / 5 needing the pipeline path. Each routed
block's result carries a `buildPath: "pipeline"` marker and a warning
naming the operator(s) — and the SCAN itself predicts this too, from a real
parse, before any conversion happens: `peek_classic_xml`'s plan already
names which block(s) will route and why, so the confirmation box shown
before a real (non-dry) import — the one path that doesn't run a dry pass
first — sees it as well, not just a dry run's results. Routed blocks
always build with `debug: false` (as `classic-pipeline` itself does since
2026-09-29) — see CLAUDE.md's debug self-poisoning hazard for
why a real import shouldn't default to it. See
`source_classic_xml.unrepresentable_filter_operators` and CLAUDE.md's App
import section for the full reasoning, and `selftest_app_classic.py` /
`selftest_classic.py` for what's covered.

### Choosing per block: a routing file (added 2026-10-16)

A `<xml stem>.routing.json` placed beside a Classic XML overrides the automatic
choice for the blocks it names, using each block's Classic Label:

```json
{"vinceforge": "classic-routing/1",
 "blocks": {"Export basic prices": "skip", "Import Basic Prices": "pipeline"}}
```

The three choices are:
- `skip`: no workflow is created, because another source in the folder replaces the block.
- `pipeline`: the block is built as a Code step.
- `native`: the block stays native, even if its filters would otherwise route it.

A label that names no block in the XML is reported in the scan, not ignored.
If it were ignored, a typo would leave that block on automatic routing.

**Use `pipeline` for an import whose message columns matter.** The native
Excel step has no confirmed message-column equivalent, so a native import
writes no MessageColumn at all.

### Hand-written pipeline specs (added 2026-10-16)

A JSON file with `"vinceforge": "pipeline/1"` is a workflow SOURCE. By
convention it is named `*.spec.json`. One example is an `m3Expand` step
replacing a Classic `<Expand>`, which Live does not have.

App import builds each spec with the target ids and the import's ION API
root, which replaces the spec's own `baseUrl`. The result is written beside
the spec as `<stem>.generated.json`, and that generated file is never
imported again as a plain workflow.

Specs are never imported pre-built. A Code step carries its connection id
and endpoint inside its source text, which `clone_and_retarget` does not
touch. A pre-built spec would therefore run against whichever tenant it
was first built for.

Two more rules:
- The workflow is created under the spec's own `name`, unchanged.
- An optional top-level `"groupWith": "<xml path>"` puts the workflow in
  that XML's Live Group.

The first real use is `RUN/runs/CustomerB`.

## Building a folder by fetching out of Live

An app folder can be *built* by pulling from a source tenant rather than
hand-written. Both fetches on the page feed it:

| Fetch | Writes | CLI |
|---|---|---|
| the workflow **Fetch** (Source panel, by `WORKFLOW-...` id) | `workflows/<name>.json` | `fetch-workflow` |
| **Fetch table** (App Import panel, by table name) | `tables/<name>.json` (+ `.rows.json`) | `fetch-table` |

### The save-into-folder toggle

One checkbox next to the app folder path governs both: **"Also save fetched
workflows and tables into this folder"**.

- **Off** (the default) — a fetch only fills the Source box. Nothing touches
  the disk. Look without committing.
- **On** — the fetch *also* writes into the folder above and re-scans, so the
  import plan updates as the folder fills up.

The folder is deliberately the same field app import reads from, so the loop
is: point at a folder, toggle on, fetch the app's workflows and tables one by
one, retarget, Import app.

If the toggle is on but no folder path is set, the fetch stops and says so
rather than quietly downgrading to source-only. On the CLI the equivalent is
just `--into <app_folder>`: omit it and nothing is written.

A fetch that overwrites a file already in the folder says which one — a
re-fetch replacing its own earlier capture is normal, but silently replacing
something hand-edited would not be.

Workflows are written **verbatim** as Live returned them. Stripping the
GET-only fields is import's job (`clone_and_retarget`), not the capture's —
an app folder should hold what the API actually said.

**The endpoint it reads from is not confirmed.** Nothing in this project
confirms a way to read a Custom Table's *definition*: `POST` and `PATCH` on
`/v1/custom-tables/meta` are confirmed, and the confirmed reads
(`/data/`, `/search/`, `/aggregated/`) return rows and counts, never
`primaryKeys`/`schema`/`updateType`. So `liveapi.get_table_meta` **probes**,
in this order:

1. `GET /v1/custom-tables/meta/{tableName}` — the plausible one: `PATCH` on
   exactly this path is confirmed, so a read most likely lives here.
2. `GET /v1/custom-tables/meta?tableName={tableName}` — fallback.

and **validates** each response before accepting it: the body must contain
an entry naming this table *and* carrying at least one real meta field
(`primaryKeys` / `schema` / `updateType` / `displayOrder`). A 200 that isn't
this table's definition is rejected — the guide's own warning is that a
guess returning 200 is not proof it was right. An auth error page and a
`/data/` response both fail this check, which is the point.

If nothing is accepted, the fetch fails and prints every endpoint it tried
with the real status and verdict. **That log is the deliverable**: when one
of them turns out to work against a live tenant, write it into
`docs/vince-custom-tables-guide.md` as confirmed and collapse
`liveapi.META_READ_CANDIDATES` down to it. Don't leave the probe in place
once the answer is known.

The row count comes from the confirmed `aggregated` endpoint independently,
so a fetch still tells you the table is real and how big it is even when the
meta probe fails — and a 404 there is reported as "no such table" rather
than as a probe failure.

### Rows are opt-in

The **with rows** tickbox pages the table via
`GET /v1/custom-tables/search/{tableName}`, writing `tables/<name>.rows.json`
— which `scan_app_folder` picks up automatically as that table's rows. Two
things to know:

- That endpoint is **documented but marked untested** in the tables guide, so
  its response shape is treated as unknown: the paging loop looks for an
  `items` list or a bare array and *stops with a warning* if it finds neither,
  rather than guessing.
- Rows are captured verbatim, `__id`/`__created`/`__modified` included, and
  are capped at 50,000 rows / 200 pages with truncation reported. They still
  can't be *imported* (see below) — this is capture for reference and for
  loading via a Table Updater.

## What actually happens on import

1. **Scan.** Purely local — no HTTP, no token. This is the plan the UI shows.
2. **Classic XML**, converted and written into the folder (see above), then
   folded into the workflow list so step 4 treats it like any other
   workflow. This is the only step that runs on a dry run.
3. **Tables**, via the confirmed `POST /v1/custom-tables/meta`. Server-computed
   fields (`rowCount`, `shardMax`, `timestamp`, `entity`, `tableAlias`,
   `created`/`modified`/`modifiedBy`, `tenantId`) are stripped, so a file
   captured straight from a creation response can be replayed as-is. The
   deprecated `columnConfig` is dropped with a warning — the API rejects it.
4. **Workflows**, via `POST /workflows`, each one run through
   `source_live_json.clone_and_retarget(..., preserve_name=True)`: fresh
   transaction instance ids, retargeted connection/tenant/environment,
   GET-only fields stripped, **name kept exactly**.
5. **Design files**, uploaded per workflow immediately after its creation
   (the key needs the real id) and then verified by reading the workflow
   back — see above.
6. **Prompt**, rendered from what actually happened — including failures.

Tables go first on purpose: a workflow's Table Updater step references a
table **by name**, and names (unlike ids) survive a move between tenants, so
creating tables first is all the ordering that's needed.

Individual failures don't abort the run. Every resource is independent, and
a partial import with a per-resource report is more useful than one that
stops at the first error.

## The confirmation box

Nothing is written to a tenant until a review dialog has been shown and
accepted. It lists every table, workflow and Classic XML that is about to
be created, the connection/tenant/environment the workflow bodies carry,
and — separately — **the tenant the bearer token itself resolves to**.

Those last two get their own rows because they can disagree, and when they
do the token wins: it decides which tenant the create lands in, the
`tenantId` in the body does not, and a mismatched pair does not raise a 401.
It simply makes the connection invisible, so metadata lookups return
nothing and the workflow arrives in the wrong customer's tenant looking
half-built. A mismatch is flagged in red and spelled out.

The box always re-scans the folder first, so it describes what is on disk
at that moment rather than whatever the last press of Scan found. A dry run
gets the same box with its own wording — it sends nothing to Live, but it
does write converted XML into the folder, so the target ids still matter.

The single-workflow **Create in Live** button has the same box, reading its
target from the payload that is actually about to be POSTed rather than
from the Target panel (the two can differ if the fields were edited after
Generate), and repeating any warnings Generate produced.

## Rows are never imported

There is **no confirmed API for writing custom-table rows.** The confirmed
path is a workflow's Table Updater step (see
`docs/vince-custom-tables-guide.md`). So rows found in an app folder are
counted, written to the run artifacts as
`tables/<name>.rows-NOT-imported.json`, and reported in both the UI and the
generated prompt — never sent. Load them with a Table Updater workflow, or
confirm a real row-write endpoint first and add it to `liveapi.py`.

## Names are preserved, and that has a trade-off

Unlike every other source in this tool, app import does **not** run workflow
names through `state.next_labeled_name()`. An app is a coherent set whose
docs, prompts and cross-references use its real workflow names, and a silent
"Pick List 2" would break that.

**Updated 2026-09-16 — corrected, not just extended.** This section used to
say re-importing the same app gives you two workflows with the same name,
"nothing dedupes them." That was wrong, and confirmed wrong the hard way: a
real `POST /workflows` against a name that already exists in the tenant
comes back `HTTP 409: Workflow "X" already exists!` — Live enforces
workflow-name uniqueness itself, it does not silently allow a duplicate.
So re-running an app import is now the **normal, expected case**, not
something to avoid: a workflow whose name already exists is detected via
that 409 and reported `ok: true, skipped: true, status: "already exists in
target tenant - left untouched"` (`appimport._is_duplicate_name_conflict`),
the same treatment tables already got (below) — not a failure, and nothing
new is created. Duplicate names *within one folder* are still flagged in
the plan, since those would collide with each other on the same run
regardless of what already exists in the tenant.

**One real gap this creates, itself unresolved on purpose rather than
guessed at:** a skipped (already-existing) workflow has no `workflowId` in
the result — there is no confirmed endpoint to look up an existing
workflow's id by name — so it cannot be added to a Group
(`appimport._create_groups`) this run creates for its sibling blocks. That
member is named explicitly in the group's own warning rather than the group
just silently coming up smaller than expected; add it to the group by hand
in Live if it needs to be there.

Tables behave differently, because they can be checked: an existing table is
detected and left untouched rather than recreated (its `primaryKeys` are
immutable anyway, so recreating is not a thing that can happen).

## The App Builder prompt

`docs/vince-live-api-reference-prompt.md` is explicit: **App Builder has zero
confirmed facts of any kind — no prompt format, no schema, no API.** The
generator honours that. It produces a faithful restatement of the folder's
contents plus the real ids of what was just created, under a banner saying
the format is unconfirmed. It does not claim App Builder expects this shape.

### Where the prompt ends up

App Builder has no import API, so the prompt is a hand-copy job. It lands in
two places:

- **In the web UI** — an *App Builder prompt* box appears after an import,
  with a **Copy** button. That's the copy-into-App-Builder path.
- **On disk** — always `RUN/app-imports/<app>-<timestamp>/app-builder-prompt.md`,
  and (with **Prompt into folder** ticked, the default) also
  `<app folder>/app-builder-prompt.generated.md`, so it lives next to the app
  it describes. CLI opt-out: `--no-folder-prompt`.

Two rules keep that safe, and both matter:

1. **`scan_app_folder` skips `app-builder-prompt.generated.md`.** Without
   that, the generated prompt would be read back as an App Builder source
   file and inlined verbatim into the *next* prompt, which would then be
   inlined into the one after — growing every run. It's also deliberately
   not one of the template names, so a generated prompt can never be
   mistaken for a hand-written template.
2. **A dry run doesn't generate a prompt at all (2026-09-16 — previously it
   generated one but withheld it from the app folder).** A dry run creates
   nothing in Live, so a prompt built from it would be built entirely from
   placeholders — no real workflow ids, statuses that never happened. That
   was already true and already the reason the old behavior withheld it
   from the app folder; the only thing that changed is not spending the
   effort building a throwaway prompt at all. `promptPath`/`prompt` in the
   response, and the prompt box in the web UI, are simply absent on a dry
   run rather than present-but-worthless.

Overwriting an existing generated prompt is allowed but reported — a re-run
replacing its own output is routine, replacing something you edited is not.

The **New workflow id** column is the reason to wait for a *real* import
before copying the prompt: Live assigns its own ids, and a dry run has
nothing to hand App Builder there.

### Replacing the default layout

To replace it, drop an `app-builder-prompt.md` (or `prompt.md`) at the app
folder root. Substitution is plain `{{placeholder}}` replacement — no
template engine, and unknown placeholders are left alone:

| Placeholder | Content |
|---|---|
| `{{app_name}}` | App name from `app.json`, else the folder name |
| `{{app_description}}` | Description from `app.json` |
| `{{target}}` | Connection / tenant / environment ids, as a table |
| `{{resource_setup}}` | Checklist: each created table/workflow with a suggested alias, and the workflow 3-run warm-up reminder — vendor-sourced prompt-writing guidance, see `app_prompt.py`'s docstring |
| `{{tables}}` | The Custom Tables, with keys, columns and real status |
| `{{workflows}}` | The workflows, with the **new** ids Live assigned |
| `{{not_imported}}` | Rows, skipped files, failures, unresolved references |
| `{{sources}}` | Every App Builder source file, inlined verbatim |
| `{{manifest}}` | The raw `app.json` |
| `{{generated_at}}` | UTC timestamp |

When a real App Builder prompt gets confirmed, add it as a template and
delete the banner. Don't "improve" the default toward a guess before then.

## Cross-references are reported, not rewritten

Workflow ids change on import. If one workflow's JSON mentions another
bundle workflow's **old** `WORKFLOW-...` id, the scan flags it — and stops
there. No confirmed field holds a workflow-to-workflow reference, so
rewriting one would mean guessing where it lives, which is exactly what this
project's core rule forbids. Fix those by hand in Live.

Table references, being by name, are reported as informational links and
need no fixing.

## Run artifacts

Every run (dry runs included) writes a timestamped directory under
`RUN/app-imports/<app-slug>-<timestamp>/`:

```
plan.json                                  what the scan found
tables/<name>.meta.json                    the exact body POSTed
tables/<name>.rows-NOT-imported.json       rows that were left behind
workflows/<name>.post.json                 the exact body POSTed
app-builder-prompt.md                      the generated prompt (also copied into the app folder) - real runs only, see above
warnings.txt                               every warning, grouped by resource and deduped - see below
results.json                               per-resource outcomes
```

A dry run writes all of it except the responses (and the prompt, now - see
above), so "what would this send" is always inspectable before anything is
sent. Each created resource also appends a row to the shared run history
(`RUN/webui_history.json`), so app imports show up in the web UI's history
alongside single-workflow creates.

### Warnings are grouped, deduped, and summarized - not dumped flat

Added 2026-09-16, after a real CustomerA customer folder (21 workflows) made
the old flat warning list genuinely unreadable in the web UI. Two things
were actually going on, and the fix addresses both:

- **Real duplication, not just volume.** A multi-block Classic file's own
  file-level notes (its block count, which filter operators it uses, which
  blocks auto-route to the pipeline path) get copied onto every block's own
  warning list at conversion time (see `app_classic.convert_classic_xml`),
  so they were showing up once per BLOCK at the run level - a real
  8-block file repeated its 3 file-level notes 8 times each.
- **Genuine volume**, even after removing that duplication - dozens of
  distinct, legitimately different messages for a large folder.

`appimport._group_warnings_by_resource` groups the run-level `warnings`
list by the resource each message belongs to (split on the message's own
`"<path>: "` prefix) and deduplicates within each group, preserving order.
Nothing is discarded - `warnings` itself (the raw, undeduped list) is still
returned in full and still lands in `results.json` unchanged; only the
grouped, deduped VIEW of it - `warningGroups` in the response, and
`warnings.txt` on disk - is smaller. The web UI renders that grouped view
as one collapsed `<details>` per resource (closed by default, a click away,
nothing hidden) instead of a flat wall of `<div>`s, and a resource card's
own `warnings` list collapses the same way past 2 entries. The CLI still
prints everything inline as before (a scrollable terminal is a different
problem than a rendered page), but now also prints the `warnings.txt` path.

## CLI

```bash
python converter/cli.py app-scan       <app_folder>
python converter/cli.py fetch-workflow <workflow_id> <bearer_token> [--into <app_folder>] [--out <file.json>]
python converter/cli.py fetch-table    <table_name>  <bearer_token> [--rows] [--into <app_folder>] [--out <file.json>]
python converter/cli.py app-import     <app_folder> <connection_id> <tenant_id> <environment_id> [bearer_token]
                                      [--dry-run] [--no-folder-prompt] [--ion-api-base-url <ion_api_root>]
```

`app-scan` makes no HTTP calls and touches nothing. `app-import --dry-run`
sends nothing to Live, but it is **not** read-only on the folder: it still
converts any Classic XML and writes the `.generated.json` (see above).
`app-import` exits non-zero if any resource failed.

## Known gaps

- **Row import** — blocked on a confirmed row-write endpoint (above).
- **Cross-workflow reference remapping** — blocked on knowing which field
  holds such a reference (above).
- **Reading a table definition** — no confirmed endpoint; `fetch-table`
  probes and reports (above). Settle it with one real-token run.
- **App Builder itself** — no import API exists; the prompt is the handoff.
  If one is ever confirmed, it belongs in `liveapi.py` and the prompt step
  becomes a fallback rather than the only option.
- **Updating an existing app** — import only ever creates, never diffs or
  updates a workflow's DEFINITION in place; there's still no delete. What
  changed 2026-09-16: re-importing no longer duplicates workflows - Live
  itself rejects a duplicate name (409), and this tool now treats that as
  a skip (see "Names are preserved" above), the same as tables already
  were. So a workflow whose content changed in the source folder is left
  exactly as it was in the tenant on re-import, not updated and not
  duplicated - there's just no path to updating one in place yet.
