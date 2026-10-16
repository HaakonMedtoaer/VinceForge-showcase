# VinceForge

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
