# VinceForge

A migration tool that turns a legacy integration product's configuration
files into working workflows on its successor platform, and creates them
in a customer's environment through the platform's API.

## The problem

Vince makes tools that connect Excel to **Infor M3**, an ERP system used by
manufacturers and distributors. Customers on the older product, **VXL
Classic**, are moving to the newer workflow platform, **Vince Live**. Each
customer has many Classic "functions" (XML files describing which M3 calls
to make and where the results go in a spreadsheet). Until now, every one of
them had to be rebuilt by hand in Live's visual builder.

Three things made automating this hard:

- **The target format was undocumented.** Live's workflow JSON isn't
  publicly specified in the detail you need to generate it, so it had to be
  worked out from real examples and real API responses.
- **The two products don't map one-to-one.** Filters, grouping, chained
  calls and multi-block files behave differently, and a careless conversion
  can run *silently* wrong: for example, writing to the ERP for rows the old
  tool would have skipped.
- **Mistakes land in a customer's production ERP.** Anything that writes
  needs guard rails.

## What it does

- **Converts** Classic XML into Live workflows. It picks between two
  strategies per block: the platform's native M3 step when that can express
  the logic, or a generated JavaScript step calling M3's REST API when it
  can't.
- **Imports whole apps.** It takes a folder of Classic files, workflow JSON,
  table definitions and Excel design files, and creates everything in a
  tenant in one run, grouped by source file.
- **Inspects runs.** It reads what workflows actually did in Live, step by
  step, or sweeps a whole tenant for failures.
- **Comes with a local web UI and a CLI.** Every write goes through a
  confirmation step that names the target tenant and checks that the
  credentials really belong to it.

## How it was built

I built it with **Claude Code** (Anthropic's AI coding agent) as a
development partner, under one rule: **the AI may never guess the target
format.** Every structural fact had to come from something built in the
platform's UI or a real API call. Anything unproven is reported loudly in
the output instead of being papered over. A few things that came out of
that rule:

- **Verified facts, written down.** The findings live in
  [docs/](docs/), a [JSON Schema](schemas/live-workflow.schema.json) with
  the gotchas in its descriptions, and a [portable Claude
  skill](.claude/skills/vince-live-workflow/SKILL.md) that other people's AI
  sessions can use. Material that turned out to be unreliable is kept
  separately in [docs-unverified/](docs-unverified/), with the story of why.
- **No silent loss.** Anything the converter can't carry becomes a warning
  or a `_needs_review` flag before anything is created. Several real bugs
  were found because a conversion that *looked* right was checked against
  an actual run.
- **A capability matrix that can't go stale.**
  [docs/vinceforge-capability-matrix.md](docs/vinceforge-capability-matrix.md)
  says, per construct, what converts, what degrades and what needs manual
  work. It is generated from data, every row names the code that enforces
  it, and a self-test fails if the code and the matrix disagree.
- **Tested.** 11 self-test suites (119 tests) run on the Python standard
  library. Generated JavaScript is executed under Node against a fake ERP,
  so the tests check behaviour, not just shape.
- **Guard rails for the AI as well.** A
  [pre-tool hook](.claude/hooks/agent_guard.py) blocks dangerous shell
  commands and asks for confirmation before deletions or writes to the live
  API. A [pre-push hook](.githooks/pre-push) has Claude audit each push's
  diff for leaked credentials and obvious bugs, and blocks the push if it
  finds one.

**Stack:** Python 3 standard library only (no `pip install`), about 19,500
lines including tests, plus about 7,800 lines of documentation. The web UI
is a single stdlib HTTP server with no framework.

> **Note:** this is a cleaned copy for showcase purposes. Customer files,
> real tenant identifiers and internal references have been removed or
> replaced with placeholders. Running it for real requires access to a
> Vince Live tenant and an Infor M3 environment.

---

## Technical overview

Builds **Vince Live** workflows (the `POST /workflows` JSON) from existing
material, and imports whole apps into a Live tenant. Built for migrating
customers from VXL Classic to Vince Live.

- **Classic XML → Live**: converts a VXL Classic function export into a Live
  workflow. Each `<Export>`/`<Import>` block becomes its own workflow.
- **Live JSON → Live**: clones an existing workflow and retargets it at
  another connection/tenant/environment.
- **App import**: takes a folder of workflows, Custom Table definitions,
  Classic XML and App Builder source. It creates the tables and workflows in
  one run, groups related workflows, and generates the App Builder prompt.
- **Runs inspector**: reads what workflows actually did in Live, either one
  run step by step or a health sweep of a whole tenant. Read-only.
- (Planned) **Worded prompt → Live**: not built yet.

Python 3 standard library only: no `pip install`, no database, no accounts.
State lives in plain JSON files under `RUN/`.

## Quick start

```bash
cp .env.example .env        # optional: fill in your default target ids
cd converter
python webui.py             # then open http://127.0.0.1:8420
```

The web UI is the normal way to use the tool. Paste a bearer token into the
Target panel. The token is a Cognito ID token copied from Live's own web UI
(DevTools → Network) and expires after about an hour.

**Restart `webui.py` after changing any `converter/*.py` file.** The server
only reads the code at startup. Reload the browser tab after a restart too.

## Command line

```bash
python converter/cli.py classic-xml      <classic.xml> <connection_id> <tenant_id> <environment_id> <out.json> [token]
python converter/cli.py classic-pipeline <classic.xml> <connection_id> <tenant_id> <environment_id> <out.json> --base-url <ion_api_root>
python converter/cli.py live-clone       <source.json> <connection_id> <tenant_id> <environment_id> <out.json>
python converter/cli.py pipeline         <spec.json>   <connection_id> <tenant_id> <environment_id> <out.json>
python converter/cli.py app-scan         <app_folder>
python converter/cli.py app-import       <app_folder> <connection_id> <tenant_id> <environment_id> [token] [--dry-run]
python converter/cli.py fetch-workflow   <workflow_id> <token> [--into <app_folder>]
python converter/cli.py fetch-table      <table_name>  <token> [--rows] [--into <app_folder>]
python converter/cli.py runs             <workflow_id> <token> [--latest | --exec <WFEXEC-id>]
python converter/cli.py runs-sweep       <token> [--match <name text>] [--include-idle]
```

Run `python converter/cli.py` with no arguments for the full usage text.
Only `app-import` (without `--dry-run`) writes to Live. The web UI's "Create
in Live" button writes too. Both show a confirmation first, naming the
target tenant and the tenant the token actually belongs to.

## Two ways to call M3

- **Native path** (`build.py`): Live's own M3 API step. Prefer it whenever it
  can express the function.
- **Generic pipeline path** (`classic_to_pipeline.py`, `build_pipeline.py`):
  a Code/Transform/REST chain posting to ION API's bulk execute endpoint. Use
  it for what the native path can't express, such as per-call filters, a
  filter on a column an earlier M3 call wrote, `<BasedOnTransaction>` and
  grouping. It needs the tenant's ION API root.

App import picks the path per block automatically. See
[docs/vinceforge-capability-matrix.md](docs/vinceforge-capability-matrix.md)
for what each path can and cannot carry.

## Tests

Every self-test runs on its own, stdlib only:

```bash
cd converter
for t in selftest_*.py; do python "$t"; done
```

A few Classic→pipeline tests execute the generated Code step under `node`.
They are skipped, with a message, when node isn't installed.

## Layout

```
converter/          the tool: sources -> IR -> builders, the Live API client, CLI, web UI, self-tests
docs/               confirmed platform facts, guides, and the capability matrix (source + generated)
schemas/            JSON Schema for a Live workflow, with the known gotchas in its descriptions
RUN/                run artifacts and local state (mostly gitignored); RUN/synthetic/ holds test fixtures
docs-unverified/    reference material found to be unreliable - do not build against it
archive/            the original single-file script, kept for reference
```

## Where to read more

| Doc | For |
| --- | --- |
| [vince-classic-to-live-migration-summary-MERGED-v2.md](vince-classic-to-live-migration-summary-MERGED-v2.md) | Confirmed structural facts about Live's workflow JSON |
| [docs/vince-live-api-reference-prompt.md](docs/vince-live-api-reference-prompt.md) | Confirmed API endpoints: workflows, groups, results, tables, auth |
| [docs/vinceforge-capability-matrix.md](docs/vinceforge-capability-matrix.md) | What this tool can carry, per construct (generated, do not edit) |
| [docs/migration-team-guide.md](docs/migration-team-guide.md) | Triage guide for the Migration Team |
| [docs/vince-app-import.md](docs/vince-app-import.md) | The app folder format and how import works |
| [docs/vince-generic-m3-pipeline.md](docs/vince-generic-m3-pipeline.md) | The generic pipeline path |
| [docs/vince-custom-tables-guide.md](docs/vince-custom-tables-guide.md) | Custom Tables API |
| [CLAUDE.md](CLAUDE.md) | Full working notes: current state, open questions, known gaps |

## Ground rules

- **Never guess at Live's JSON.** Every structural fact here came from a
  workflow built in Live's UI or a real API call. When something is
  unconfirmed, the code says so, in warnings and `_needs_review` flags,
  rather than filling the gap with a plausible guess.
- **The token decides the tenant, not the ids you type.** Pairing a token
  with another tenant's connection doesn't give a 401. It silently returns
  empty metadata. The UI checks for this and warns.
- **No real ids in tracked files.** Tenant, connection and environment ids
  belong in `.env` and in the gitignored files under `RUN/`.
