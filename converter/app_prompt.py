"""
Generates the App Builder prompt - the third output of an app import,
alongside the tables and workflows that actually got created via the API.

## Read this before changing the wording

`docs/vince-live-api-reference-prompt.md` is explicit: **App Builder has
zero confirmed facts of any kind - do not invent a prompt format, schema,
or API shape for it.** This module honours that. It does NOT claim to
produce a format App Builder expects. What it produces is a faithful,
verbatim restatement of what's in the app folder plus the real ids of what
was just created - the information a human (or App Builder itself) needs,
with no invented structure asserted as required.

That's why the default template opens with an explicit "format not
confirmed" banner, and why any app folder can override the whole thing by
including its own `app-builder-prompt.md`. Once a real App Builder prompt
is confirmed, drop it in as a template and delete the banner - don't
"correct" this default toward a guess in the meantime.

## The "how to prompt" section IS sourced, just not from an API

Added 2026-09-16, from vincesoftware.com's own published App Builder
documentation (`/product-documentation/appbuilder-build-prompt`,
`/appbuilder-tables`, `/appbuilder-workflow`) - real vendor guidance on how
to WRITE a good prompt (reference resources by alias, name exact fields,
build incrementally, re-prompt after specific changes), not a claim about
what JSON/API shape App Builder expects. That distinction is why this is
safe to bake into the default template while the banner above stays: prose
advice about prompting an AI in natural language is a different kind of
fact than an unconfirmed schema, and doesn't need a real API call to
verify - reading the vendor's own docs is the correct source for it. See
`docs-unverified/README.md`'s 2026-09-16 update for a related but
DIFFERENT-in-kind finding from the same research pass (a stale API
endpoint in vincesoftware.com's own docs) - that one stayed out of any
generated output, because it *is* the schema/endpoint kind of claim this
module's rule is about.

## Templates

An app folder's own template (see source_app_folder.PROMPT_TEMPLATE_NAMES)
wins over the default. Substitution is plain `{{placeholder}}` string
replacement - no template engine, matching this project's stdlib-only rule.
Unknown placeholders are left alone rather than erroring, so a template can
carry literal braces.
"""

import datetime
import json

PLACEHOLDER_HELP = {
    "{{app_name}}": "App name (from app.json, else the folder name)",
    "{{app_description}}": "App description from app.json",
    "{{target}}": "Target connection / tenant / environment ids",
    "{{resource_setup}}": "Checklist: each created table/workflow with a suggested alias, "
                          "and the workflow 3-run warm-up reminder",
    "{{tables}}": "Markdown table of the Custom Tables created (or attempted)",
    "{{workflows}}": "Markdown table of the workflows created (or attempted), with their new ids",
    "{{not_imported}}": "Anything deliberately left out, e.g. seed rows",
    "{{sources}}": "Every App Builder source file, inlined verbatim in fenced blocks",
    "{{manifest}}": "The raw app.json, pretty-printed",
    "{{generated_at}}": "UTC timestamp of generation",
}

_BANNER = (
    "> **Format not confirmed.** This project has no confirmed facts about what App Builder\n"
    "> expects a prompt to look like, so nothing below is asserted as a required structure - it is\n"
    "> a faithful restatement of the app folder's contents plus the ids of what was actually\n"
    "> created in the target tenant. Drop an `app-builder-prompt.md` into the app folder to replace\n"
    "> this layout wholesale once you know what App Builder actually wants.\n"
)

# vincesoftware.com's own published prompt-writing guidance, 2026-09-16 - see
# the module docstring's "how to prompt" section for why this is safe to bake
# in despite the banner above (prose advice, not an asserted schema).
_PROMPTING_GUIDE = (
    "The advice below is vincesoftware.com's own published guidance for writing App Builder\n"
    "prompts - not a format this tool claims App Builder requires; see the banner above.\n\n"
    "- **Reference resources by the alias you give them below**, not by their raw table/workflow\n"
    "  name - e.g. \"from `invoice_table`\", not \"from the invoice table\".\n"
    "- **Name exact fields** to display, sort or filter by, instead of describing them - a vague\n"
    "  request can make the AI pick the wrong column.\n"
    "- **Build incrementally, not in one giant prompt.** Start with one page or feature, save,\n"
    "  then add the next one as a separate prompt. When iterating, describe just the change -\n"
    "  don't restate the whole app.\n"
    "- **Re-prompt (tell App Builder what changed) whenever**: a new table/workflow should be\n"
    "  used by the app; a field is added, renamed or removed on an existing resource; a\n"
    "  workflow's output changes (after re-running it 3+ times so App Builder picks up the new\n"
    "  shape); or a resource is deleted and needs cleanup. Skipped, App Builder keeps working from\n"
    "  a stale idea of the resource's shape - it may bind to fields that aren't really there.\n"
)

DEFAULT_TEMPLATE = """# App Builder brief: {{app_name}}

""" + _BANNER + """
{{app_description}}

Generated {{generated_at}}.

## Target

{{target}}

## Before you prompt: add these as resources

{{resource_setup}}

## How to prompt App Builder

""" + _PROMPTING_GUIDE + """
## Custom Tables

{{tables}}

## Workflows

{{workflows}}

## Not imported

{{not_imported}}

## App manifest

{{manifest}}

## App source

{{sources}}
"""


def _md_escape(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _suggest_alias(name: str, suffix: str = "") -> str:
    """
    A snake_case suggestion for the alias App Builder's Resources panel asks
    you to give each resource, e.g. `invoice_table` / `orders_workflow` -
    vincesoftware.com's own example pattern. Purely a naming-consistency
    convenience: the alias is free text typed into App Builder itself, never
    anything this tool sets or asserts is required - see the module
    docstring's "how to prompt" section.
    """
    slug = "".join(c if (c.isalnum() and c.isascii()) else "_" for c in (name or "").strip().lower())
    slug = "_".join(part for part in slug.split("_") if part) or "resource"
    if suffix and not slug.endswith(suffix):
        slug += suffix
    return slug


def _resource_setup_section(table_results: list, workflow_results: list) -> str:
    ok_tables = [r for r in table_results if r.get("ok") is not False]
    ok_workflows = [r for r in workflow_results if r.get("ok") is not False]
    if not ok_tables and not ok_workflows:
        return "_Nothing was created successfully in this app folder - nothing to add as a resource yet._"

    lines = []
    if ok_tables:
        lines.append("**Tables** (Resources panel -> Add resource -> Table - no warm-up needed, "
                     "the schema is known immediately):")
        for r in ok_tables:
            lines.append("- `{}` -> suggested alias `{}`".format(
                _md_escape(r.get("tableName")), _suggest_alias(r.get("tableName") or "", suffix="_table")))
    if ok_workflows:
        if lines:
            lines.append("")
        lines.append("**Workflows** (Resources panel -> Add resource -> Workflow - **run each one "
                     "3+ times with realistic data FIRST**, or App Builder may not bind to it at all, "
                     "or may bind to fields that aren't really there):")
        for r in ok_workflows:
            lines.append("- `{}` -> suggested alias `{}`".format(
                _md_escape(r.get("name")), _suggest_alias(r.get("name") or "")))
    return "\n".join(lines)


def _tables_section(results: list) -> str:
    if not results:
        return "_No Custom Tables in this app._"
    lines = ["| Table | Primary keys | Update type | Columns | Status |",
             "|---|---|---|---|---|"]
    for r in results:
        columns = ", ".join(r.get("columns") or []) or "-"
        lines.append("| `{}` | {} | {} | {} | {} |".format(
            _md_escape(r.get("tableName")),
            _md_escape(", ".join(r.get("primaryKeys") or []) or "-"),
            _md_escape(r.get("updateType") or "-"),
            _md_escape(columns),
            _md_escape(r.get("status")),
        ))
    return "\n".join(lines)


def _workflows_section(results: list) -> str:
    if not results:
        return "_No workflows in this app._"
    lines = ["| Workflow | New workflow id | Status |", "|---|---|---|"]
    for r in results:
        lines.append("| {} | `{}` | {} |".format(
            _md_escape(r.get("name")),
            _md_escape(r.get("workflowId") or "-"),
            _md_escape(r.get("status")),
        ))
    return "\n".join(lines)


def _not_imported_section(bundle, table_results: list) -> str:
    notes = []
    total_rows = sum(len(t.rows) for t in bundle.tables)
    if total_rows:
        per_table = ", ".join(
            "`{}` ({} rows)".format(t.table_name, len(t.rows)) for t in bundle.tables if t.rows
        )
        notes.append(
            "- **Custom table seed rows were NOT imported**: " + per_table + ". There is no confirmed API for "
            "writing custom-table rows; the confirmed path is a workflow's Table Updater step. The rows were "
            "written to the run artifacts alongside this prompt so nothing is lost."
        )
    skipped_assets = [a for a in bundle.assets if a.text is None]
    if skipped_assets:
        notes.append("- **Source files listed but not inlined below**: " + ", ".join(
            "`{}` ({})".format(a.path, a.skipped_reason) for a in skipped_assets
        ))
    # `ok` is None on a dry run (planned, not attempted) - only a real
    # False is a failure worth reporting here.
    failed = [r for r in table_results if r.get("ok") is False]
    if failed:
        notes.append("- **Tables that did not get created**: " + ", ".join(
            "`{}` ({})".format(r.get("tableName"), r.get("status")) for r in failed
        ))
    for workflow in bundle.workflows:
        for warning in workflow.warnings:
            notes.append("- `{}`: {}".format(workflow.path, warning))
    return "\n".join(notes) if notes else "_Everything in the folder was imported._"


def _sources_section(bundle) -> str:
    if not bundle.assets:
        return "_No App Builder source files in this folder._"
    blocks = []
    for asset in bundle.assets:
        blocks.append("### `{}`\n".format(asset.path))
        if asset.text is None:
            blocks.append("_{} ({:,} bytes) - not inlined: {}_\n".format(
                asset.path, asset.size, asset.skipped_reason))
            continue
        fence = "```" + (asset.language or "")
        # A file that itself contains a ``` fence would break out of the block.
        if "```" in asset.text:
            fence = "````" + (asset.language or "")
            blocks.append(fence + "\n" + asset.text.rstrip("\n") + "\n````\n")
        else:
            blocks.append(fence + "\n" + asset.text.rstrip("\n") + "\n```\n")
    return "\n".join(blocks)


def _target_section(connection_id: str, tenant_id: str, environment_id: str) -> str:
    return "\n".join([
        "| Field | Value |",
        "|---|---|",
        "| Connection ID | `{}` |".format(_md_escape(connection_id)),
        "| Tenant ID | `{}` |".format(_md_escape(tenant_id)),
        "| Environment ID | `{}` |".format(_md_escape(environment_id)),
    ])


def build_app_prompt(bundle, table_results: list, workflow_results: list,
                     connection_id: str, tenant_id: str, environment_id: str) -> str:
    """
    Render the App Builder prompt. `table_results`/`workflow_results` are
    appimport's per-resource result dicts, so the prompt reports what really
    happened (including failures) rather than what was planned.
    """
    template = bundle.prompt_template or DEFAULT_TEMPLATE
    manifest = ("```json\n" + json.dumps(bundle.manifest, indent=2) + "\n```") if bundle.manifest \
        else "_No `app.json` in this folder._"

    values = {
        "{{app_name}}": bundle.name,
        "{{app_description}}": bundle.description or "_No description given in `app.json`._",
        "{{target}}": _target_section(connection_id, tenant_id, environment_id),
        "{{resource_setup}}": _resource_setup_section(table_results, workflow_results),
        "{{tables}}": _tables_section(table_results),
        "{{workflows}}": _workflows_section(workflow_results),
        "{{not_imported}}": _not_imported_section(bundle, table_results),
        "{{sources}}": _sources_section(bundle),
        "{{manifest}}": manifest,
        "{{generated_at}}": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    rendered = template
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered
