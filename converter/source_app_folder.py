"""
An app folder as a source: scan a directory on disk and sort what's in it
into the things an app is made of - Custom Table definitions, Live
workflows, Classic XML exports awaiting conversion, and App Builder source
files (Java/HTML/CSS/JS/...).

This module ONLY reads and classifies. It makes no HTTP calls and decides
nothing about targeting - appimport.py does that, so a scan is always safe
to run and is what the web UI shows as the import plan.

## Folder layout

The recommended layout, which is also what the docs describe:

    MyApp/
      app.json                    optional manifest (name, description, notes)
      tables/*.json               POST /v1/custom-tables/meta bodies, or
                                  captured creation/meta responses
      workflows/*.json            GET /workflows/{id} responses, or POST bodies
      *.xml                       Classic "VinceExcelConfig" exports - converted
                                  to workflows/<stem>.generated.json on import,
                                  and left in place (see app_classic.py)
      app/                        App Builder source: .java, .html, .css, .js, ...
      app-builder-prompt.md       optional prompt template (overrides the default)

...but the subfolder names are a convention, not a requirement. Every
`.json` anywhere under the root is classified by its SHAPE, not its path,
so a flat folder works too. Anything that isn't a recognised workflow or
table shape becomes an App Builder source asset if it's readable text -
including a `.xml` that ISN'T a Classic export, since plenty of apps carry
real XML config.

## What "rows" do here

A table file may carry seed rows (inline `rows`/`items`, or a sibling
`<table>.rows.json` / `<table>.data.json`). They are collected and counted
but NEVER imported: there is no confirmed API for writing custom-table
rows - the confirmed path is a workflow's Table Updater step. See
liveapi.py's module docstring. The importer reports them so they don't
vanish silently.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from pipeline_ir import looks_like_pipeline_spec
from source_classic_xml import parse_classic_xml_units, peek_classic_xml

# App Builder source files get inlined verbatim into the generated prompt,
# so a runaway file (a bundled minified vendor lib, a data dump someone
# dropped in the folder) would otherwise blow up the prompt. Cap per-file
# and in total, and say so in the output rather than silently truncating.
MAX_INLINE_ASSET_BYTES = 200_000
MAX_TOTAL_ASSET_BYTES = 1_000_000

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".idea", ".vscode", ".venv", "venv", "dist", "build"}

MANIFEST_NAMES = ("app.json", "vinceapp.json")

# What app import writes back into the folder (appimport.run_app_import).
# The scan MUST skip it: it is this tool's own output, and left unskipped it
# would be picked up as an App Builder source file and inlined verbatim into
# the NEXT generated prompt - which would then be inlined into the one after
# that, growing without limit. It is also deliberately not one of the
# PROMPT_TEMPLATE_NAMES below, so a generated prompt can never be mistaken
# for a hand-written template.
GENERATED_PROMPT_NAME = "app-builder-prompt.generated.md"
PROMPT_TEMPLATE_NAMES = (
    "app-builder-prompt.md", "app-builder-prompt.tmpl.md",
    "prompt.md", "prompt.tmpl.md",
)

# Where a converted Classic XML lands, and how it is recognised on the next
# scan. Same problem as GENERATED_PROMPT_NAME, different shape: the file this
# tool writes is a perfectly valid workflow JSON, so without a marker the next
# scan would import BOTH the XML (re-converted) and its own previous output,
# creating the workflow twice. The suffix is that marker - a generated file
# whose source XML is still present is skipped, because the XML is
# authoritative and gets re-converted every run.
WORKFLOWS_SUBDIR = "workflows"
GENERATED_WORKFLOW_SUFFIX = ".generated.json"

# A workflow's "Design file" - the Excel sheet uploaded to the workflow
# itself. Paired by the FOLDER it shares with a Classic XML first, by
# filename stem second (see match_design_files), because nothing in the
# workflow JSON names it: the link is only made at upload time, and the
# resulting `excelDesignFileKey` embeds a workflow id that does not exist
# until Live assigns one. See liveapi.upload_design_file.
DESIGN_FILE_EXTENSIONS = (".xlsx", ".xlsm", ".xltx", ".xls")


def _workflow_stem(filename: str) -> str:
    """
    The name a workflow file and its design file pair on.

    Drops the extension and, for a converted Classic XML, the `.generated`
    marker too - so `orders.xml` -> `workflows/orders.generated.json` still
    pairs with an `orders.xlsx` sitting next to either of them.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    if stem.lower().endswith(".generated"):
        stem = stem[: -len(".generated")]
    return stem


# A hand-written pipeline spec (`"vinceforge": "pipeline/1"`, see pipeline_ir)
# is a workflow SOURCE, like a Classic XML: app import builds it with the
# target's ids and ION API root and writes the result beside it. Named
# `<name>.spec.json` by convention; any spec-marked JSON counts.
SPEC_SUFFIX = ".spec.json"

# `<xml stem>.routing.json` beside a Classic XML says, per BLOCK (by its
# Classic Label), how to build it: "skip" (another source in the folder
# replaces it), "pipeline" (Code step), or "native". Blocks it does not name
# keep the automatic routing.
ROUTING_MARKER = "classic-routing/1"
ROUTING_SUFFIX = ".routing.json"
ROUTING_CHOICES = ("skip", "pipeline", "native")


def looks_like_routing(data) -> bool:
    return isinstance(data, dict) and data.get("vinceforge") == ROUTING_MARKER


def spec_stem(rel_path: str) -> str:
    """`workflows/P2-lines.spec.json` -> `P2-lines`."""
    stem = os.path.basename(rel_path)
    for suffix in (SPEC_SUFFIX, ".json"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def generated_spec_path(spec_rel_path: str) -> str:
    """Where a pipeline spec's built workflow is written: beside the spec."""
    folder = os.path.dirname(spec_rel_path)
    name = spec_stem(spec_rel_path) + GENERATED_WORKFLOW_SUFFIX
    return (folder + "/" + name) if folder else (WORKFLOWS_SUBDIR + "/" + name)


def generated_workflow_path(xml_rel_path: str) -> str:
    """
    Relative path of the JSON a given Classic XML converts to.

    Named after the XML's own filename, not its Classic <Import Label="...">:
    the label is not unique (two files in one folder can both be "Check
    Data") and not filesystem-safe, while the stem traces straight back to
    the source file. The workflow's NAME still comes from the label.
    """
    stem = os.path.basename(xml_rel_path)
    if stem.lower().endswith(".xml"):
        stem = stem[: -len(".xml")]
    return WORKFLOWS_SUBDIR + "/" + stem + GENERATED_WORKFLOW_SUFFIX

# Extension -> markdown fence language for the generated prompt.
ASSET_LANGUAGES = {
    ".java": "java", ".kt": "kotlin", ".groovy": "groovy",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".js": "javascript", ".mjs": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx",
    ".json": "json", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
    ".sql": "sql", ".py": "python", ".sh": "bash", ".properties": "properties",
    ".md": "markdown", ".txt": "", ".csv": "csv", ".ftl": "html", ".vm": "html",
}

# Fields the server computes and returns on a table meta - present if the
# file was captured from a creation/read response rather than hand-written.
# Sending them back is not part of the confirmed creation body, so they're
# dropped. Shape source: docs/vince-custom-tables-guide.md, "Response shape
# when creating a table - CONFIRMED".
TABLE_SERVER_FIELDS = {
    "rowCount", "shardMax", "timestamp", "entity", "tableAlias",
    "created", "createdBy", "modified", "modifiedBy", "tenantId",
}
# Confirmed rejected by the API - the guide records a request using it
# being refused, with `schema` as the current mechanism.
TABLE_DEPRECATED_FIELDS = {"columnConfig"}


@dataclass
class AppTable:
    path: str                        # relative to the app root
    table_name: str
    meta: dict                       # cleaned POST /v1/custom-tables/meta body
    rows: list = field(default_factory=list)
    rows_path: Optional[str] = None
    warnings: list = field(default_factory=list)


@dataclass
class AppWorkflow:
    path: str
    name: str
    source_workflow_id: Optional[str]
    data: dict                       # the raw file contents, untouched
    warnings: list = field(default_factory=list)
    design_file: Optional["AppDesignFile"] = None
    # The source Classic XML's app-relative path, when this workflow came
    # from one - lets appimport bucket sibling blocks from the same file
    # back together after creation, to group them in Live. None for a
    # plain pre-existing workflow JSON file (nothing to group it with).
    group_key: Optional[str] = None


@dataclass
class AppDesignFile:
    """An Excel sheet in the folder, to be uploaded to a workflow after creation."""
    path: str                        # relative to the app root
    filename: str                    # basename, becomes part of the storage key
    stem: str                        # what it pairs on
    size: int
    workflow_path: Optional[str] = None   # the workflow it is attached to, if that exists yet
    # What it is matched to: a Classic XML's path (its first-block workflow
    # gets the file once converted) or a plain workflow JSON's path.
    target: Optional[str] = None
    matched_by: Optional[str] = None      # "folder" | "name" | "manual"
    needs_choice: bool = False            # ambiguous - a person has to pick (or pick none)
    warnings: list = field(default_factory=list)


@dataclass
class AppClassicXml:
    """A Classic XML export found in the folder, not yet converted."""
    path: str                        # relative to the app root
    label: str                       # Classic's own <Export/Import Label="...">
    generated_path: str              # where its converted JSON goes
    transaction_count: int = 0       # M3 calls the parser will actually read
    total_transaction_count: int = 0 # M3 calls present in the file
    block_count: int = 1             # workflows it becomes - one per <Export>/<Import> block
    warnings: list = field(default_factory=list)
    # From a `<stem>.routing.json` beside it: block Label -> "skip" |
    # "pipeline" | "native". Empty = automatic routing for every block.
    routing: dict = field(default_factory=dict)
    routing_path: Optional[str] = None


@dataclass
class AppPipelineSpec:
    """A hand-written pipeline spec in the folder, built at import time."""
    path: str                        # relative to the app root
    name: str                        # the spec's own "name" - the workflow is created under it, verbatim
    data: dict
    generated_path: str
    # The Classic XML (app-relative path) whose Live Group the built workflow
    # joins - the spec's top-level "groupWith". Optional.
    group_with: Optional[str] = None
    warnings: list = field(default_factory=list)


@dataclass
class AppAsset:
    path: str
    language: str
    text: Optional[str]              # None when not inlined
    size: int
    skipped_reason: Optional[str] = None


@dataclass
class AppBundle:
    root: str
    name: str
    description: str = ""
    manifest: dict = field(default_factory=dict)
    tables: list = field(default_factory=list)
    workflows: list = field(default_factory=list)
    classic_xml: list = field(default_factory=list)
    pipeline_specs: list = field(default_factory=list)
    design_files: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    prompt_template: Optional[str] = None
    prompt_template_path: Optional[str] = None
    links: list = field(default_factory=list)      # observed references between bundle members
    warnings: list = field(default_factory=list)
    # A person's own design-file pairings: design file path -> target (a
    # Classic XML path or a plain workflow JSON path), or "" for "do not
    # upload". Beats every automatic rule in match_design_files.
    design_matches: dict = field(default_factory=dict)

    def summary(self) -> dict:
        """The shape the web UI and CLI both render as the import plan."""
        return {
            "root": self.root,
            "name": self.name,
            "description": self.description,
            "tables": [{
                "path": t.path,
                "tableName": t.table_name,
                "primaryKeys": t.meta.get("primaryKeys", []),
                "updateType": t.meta.get("updateType"),
                "columns": sorted((t.meta.get("schema") or {}).get("properties", {}).keys()),
                "rowCount": len(t.rows),
                "rowsPath": t.rows_path,
                "warnings": t.warnings,
            } for t in self.tables],
            "workflows": [{
                "path": w.path,
                "name": w.name,
                "sourceWorkflowId": w.source_workflow_id,
                "designFile": w.design_file.path if w.design_file else None,
                "warnings": w.warnings,
            } for w in self.workflows],
            "designFiles": [{
                "path": d.path,
                "filename": d.filename,
                "size": d.size,
                "workflowPath": d.workflow_path,
                "target": d.target,
                "matchedBy": d.matched_by,
                "needsChoice": d.needs_choice,
                "warnings": d.warnings,
            } for d in self.design_files],
            # Everything a design file can be matched to by hand.
            "designTargets": [{"key": key, "kind": kind}
                              for key, kind, _folder, _stem, _wf in _design_targets(self)],
            "classicXml": [{
                "path": x.path,
                "label": x.label,
                "generatedPath": x.generated_path,
                "transactionCount": x.transaction_count,
                "totalTransactionCount": x.total_transaction_count,
                "blockCount": x.block_count,
                "designFile": next((d.path for d in self.design_files if d.target == x.path), None),
                "routing": x.routing,
                "routingPath": x.routing_path,
                "warnings": x.warnings,
            } for x in self.classic_xml],
            "pipelineSpecs": [{
                "path": p.path,
                "name": p.name,
                "generatedPath": p.generated_path,
                "groupWith": p.group_with,
                "warnings": p.warnings,
            } for p in self.pipeline_specs],
            "assets": [{
                "path": a.path,
                "language": a.language,
                "size": a.size,
                "inlined": a.text is not None,
                "skippedReason": a.skipped_reason,
            } for a in self.assets],
            "promptTemplate": self.prompt_template_path,
            "links": self.links,
            "warnings": self.warnings,
        }


# --- shape classification -------------------------------------------------

def looks_like_workflow(data) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("definition"), dict)
        and isinstance(data["definition"].get("workflowConfig"), dict)
    )


def looks_like_table_meta(data) -> bool:
    if not isinstance(data, dict) or not data.get("tableName"):
        return False
    return any(k in data for k in ("primaryKeys", "schema", "updateType", "displayOrder", "columnConfig"))


def extract_rows(data):
    """
    Rows, if this file is one. Two shapes are accepted: a bare JSON array of
    objects, or a `GET /v1/custom-tables/data/...` response (`{"count": N,
    "items": [...]}`), since that's the confirmed way rows come back out of
    Live and therefore the likeliest thing to find in an exported app.
    Returns None when the file isn't rows at all.
    """
    if isinstance(data, list) and all(isinstance(r, dict) for r in data):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return None


def clean_table_meta(data: dict) -> tuple:
    """Strip server-computed and deprecated fields. Returns (meta, warnings)."""
    warnings = []
    meta = {}
    for key, value in data.items():
        if key in TABLE_SERVER_FIELDS or key.startswith("__"):
            continue
        if key in TABLE_DEPRECATED_FIELDS:
            warnings.append(
                "Dropped deprecated field 'columnConfig' - the API rejects it; `schema` is the current mechanism."
            )
            continue
        if key in ("rows", "items", "data"):
            continue  # collected separately, never sent
        meta[key] = value

    if not meta.get("primaryKeys"):
        warnings.append("No `primaryKeys` - required at creation, and immutable afterwards. Creation will fail.")
    if not meta.get("updateType"):
        warnings.append("No `updateType` - required at creation (UPSERT / REPLACE / APPEND).")
    return meta, warnings


# --- scanning -------------------------------------------------------------

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None


def _walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            full = os.path.join(dirpath, filename)
            yield full, os.path.relpath(full, root).replace(os.sep, "/")


def scan_app_folder(root: str) -> AppBundle:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError("Not a folder: " + root)

    bundle = AppBundle(root=root, name=os.path.basename(root.rstrip(os.sep)) or "App")

    json_files = []       # (rel, full, data) still to classify
    text_candidates = []  # (rel, full, text) -> assets
    pending_rows = {}     # stem -> (rel, rows)

    for full, rel in _walk(root):
        filename = os.path.basename(rel)
        lower = filename.lower()
        ext = os.path.splitext(lower)[1]

        if lower == GENERATED_PROMPT_NAME and "/" not in rel:
            continue  # this tool's own output - never an input, see the constant

        if lower in MANIFEST_NAMES and "/" not in rel:
            data = _load_json(full, rel, bundle)
            if isinstance(data, dict):
                bundle.manifest = data
                bundle.name = str(data.get("name") or data.get("appName") or bundle.name)
                bundle.description = str(data.get("description") or "")
            continue

        if lower in PROMPT_TEMPLATE_NAMES and "/" not in rel and bundle.prompt_template is None:
            text = _read_text(full)
            if text is not None:
                bundle.prompt_template = text
                bundle.prompt_template_path = rel
                continue

        if ext == ".json":
            data = _load_json(full, rel, bundle)
            if data is not None:
                json_files.append((rel, full, data))
            continue

        if ext in DESIGN_FILE_EXTENSIONS:
            # A design file is binary, so it would otherwise land in
            # text_candidates and be reported as an un-inlinable App
            # Builder source file - present in the plan, but never
            # uploaded, which is the failure mode worth avoiding.
            bundle.design_files.append(AppDesignFile(
                path=rel, filename=filename, stem=_workflow_stem(filename),
                size=os.path.getsize(full),
            ))
            continue

        if ext == ".xml":
            # A Classic export is a workflow SOURCE, not App Builder source.
            # Left in text_candidates it would be inlined verbatim into the
            # generated prompt and never converted - which is what happened
            # before this branch existed.
            text = _read_text(full)
            peek = peek_classic_xml(text, path=full) if text is not None else None
            if peek is not None:
                bundle.classic_xml.append(AppClassicXml(
                    path=rel,
                    label=peek["label"],
                    generated_path=generated_workflow_path(rel),
                    transaction_count=peek["transactionCount"],
                    total_transaction_count=peek["totalTransactionCount"],
                    block_count=peek.get("blockCount", 1),
                    warnings=list(peek["warnings"]),
                ))
                continue
            text_candidates.append((rel, full, text))
            continue

        text_candidates.append((rel, full, _read_text(full)))

    # Rows are collected in the first pass so a table file can pick up its
    # sibling rows file regardless of which one os.walk reached first.
    # Case-insensitive: os.walk reports the directory as it is spelled on
    # disk ("Workflows/"), while generated_workflow_path always says
    # "workflows/". On Windows those are the same directory, and treating
    # them as different would import the workflow twice.
    #
    # A multi-block source produces SEVERAL generated files - the first
    # block keeps the plain "<stem>.generated.json", every later one gets
    # a numbered "<stem>.N-slug.generated.json" (app_classic._file_suffix).
    # `AppClassicXml.generated_path` only ever names the first. Matching
    # against that ONE path let blocks 2+'s stale output from a PREVIOUS
    # run survive this exclusion entirely and fall through to be imported
    # again as ordinary "plain" workflows - not overwritten, not skipped,
    # silently duplicated under whatever name that earlier run gave them.
    # A prefix+suffix match on the stem, not one exact path, is what
    # "produced by re-converting this source" actually means.
    # Specs and routing files are sources, not workflows - pulled out before
    # the generated-output exclusion below, which needs the specs' paths.
    routing_files = []
    remaining = []
    for rel, full, data in json_files:
        if looks_like_pipeline_spec(data):
            group_with = data.get("groupWith")
            bundle.pipeline_specs.append(AppPipelineSpec(
                path=rel, name=str(data.get("name") or spec_stem(rel)), data=data,
                generated_path=generated_spec_path(rel),
                group_with=str(group_with) if group_with else None))
        elif looks_like_routing(data):
            routing_files.append((rel, data))
        else:
            remaining.append((rel, full, data))
    json_files = remaining
    _attach_routing(bundle, routing_files)
    for spec in bundle.pipeline_specs:
        if spec.group_with and not any(x.path == spec.group_with for x in bundle.classic_xml):
            spec.warnings.append(
                "groupWith names " + repr(spec.group_with) + ", which is not a Classic XML in this "
                "folder (paths are relative to the app folder) - the workflow is created ungrouped.")
            spec.group_with = None

    regenerated_prefixes = []
    for spec in bundle.pipeline_specs:
        regenerated_prefixes.append(spec.generated_path.lower()[: -len(GENERATED_WORKFLOW_SUFFIX)])
    for x in bundle.classic_xml:
        prefix = x.generated_path.lower()
        if prefix.endswith(GENERATED_WORKFLOW_SUFFIX):
            prefix = prefix[: -len(GENERATED_WORKFLOW_SUFFIX)]
        regenerated_prefixes.append(prefix)

    def _is_regenerated_output(rel: str) -> bool:
        rel_lower = rel.lower()
        return any(
            rel_lower == prefix + GENERATED_WORKFLOW_SUFFIX
            or (rel_lower.startswith(prefix + ".") and rel_lower.endswith(GENERATED_WORKFLOW_SUFFIX))
            for prefix in regenerated_prefixes
        )

    deferred = []
    for rel, full, data in json_files:
        if _is_regenerated_output(rel):
            # This run re-converts the XML that produced it, so the file on
            # disk is about to be overwritten - importing it as well would
            # create the same workflow twice.
            continue
        if looks_like_workflow(data):
            workflow = _make_workflow(rel, data)
            if rel.lower().endswith(GENERATED_WORKFLOW_SUFFIX):
                workflow.warnings.append(
                    "Looks like output from a previous Classic XML conversion, but no source .xml in this "
                    "folder produces it - importing it as an ordinary workflow. Delete it if it is stale."
                )
            bundle.workflows.append(workflow)
            continue
        if looks_like_table_meta(data):
            deferred.append((rel, full, data, "table"))
            continue
        rows = extract_rows(data)
        if rows is not None:
            pending_rows[_rows_stem(rel)] = (rel, rows)
            continue
        deferred.append((rel, full, data, "unknown"))

    for rel, full, data, kind in deferred:
        if kind == "table":
            bundle.tables.append(_make_table(rel, data, pending_rows))
        else:
            # Not a workflow, not a table, not rows - treat it as App Builder
            # source (an app config, a fixture, whatever) rather than dropping it.
            bundle.assets.append(_make_asset(rel, _read_text(full), os.path.getsize(full)))
            bundle.warnings.append(
                rel + ": not a recognised workflow or table shape - treated as an App Builder source file."
            )

    for rel, full, text in text_candidates:
        bundle.assets.append(_make_asset(rel, text, os.path.getsize(full)))

    _apply_total_asset_cap(bundle)

    claimed = {t.rows_path for t in bundle.tables if t.rows_path}
    for stem, (rel, _rows) in pending_rows.items():
        if rel not in claimed:
            bundle.warnings.append(
                rel + ": looks like table rows, but no table named '" + stem +
                "' was found in this folder - ignored."
            )

    match_design_files(bundle)

    _detect_links(bundle)

    if not bundle.tables and not bundle.workflows and not bundle.classic_xml:
        bundle.warnings.append("No workflows, tables or Classic XML files found - nothing would be created in Live.")

    return bundle


def _load_json(full: str, rel: str, bundle: AppBundle):
    text = _read_text(full)
    if text is None:
        bundle.warnings.append(rel + ": couldn't read as UTF-8 text - skipped.")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        bundle.warnings.append(rel + ": not valid JSON (" + str(e) + ") - skipped.")
        return None


def _rows_stem(rel: str) -> str:
    """`tables/prices.rows.json` -> `prices`; `tables/prices/rows.json` -> `prices`."""
    filename = os.path.basename(rel)
    stem = filename[: -len(".json")] if filename.lower().endswith(".json") else filename
    for suffix in (".rows", ".data", "-rows", "-data"):
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    if stem.lower() in ("rows", "data"):
        parent = os.path.basename(os.path.dirname(rel))
        return parent or stem
    return stem


def _make_workflow(rel: str, data: dict) -> AppWorkflow:
    name = (data.get("workflowName")
            or data.get("definition", {}).get("workflowConfig", {}).get("name")
            or rel)
    warnings = []
    if not data.get("definition", {}).get("stepConfig"):
        warnings.append("No stepConfig - the workflow has no steps to import.")
    return AppWorkflow(path=rel, name=str(name), source_workflow_id=data.get("workflowId"),
                       data=data, warnings=warnings)


def _make_table(rel: str, data: dict, pending_rows: dict) -> AppTable:
    meta, warnings = clean_table_meta(data)
    table_name = str(data.get("tableName"))

    rows, rows_path = [], None
    inline = data.get("rows") if isinstance(data.get("rows"), list) else None
    if inline is None and isinstance(data.get("items"), list):
        inline = data["items"]
    if inline:
        rows, rows_path = inline, rel
    else:
        for stem in (table_name, _rows_stem(rel)):
            if stem in pending_rows:
                rows_path, rows = pending_rows[stem]
                break

    if rows:
        warnings.append(
            str(len(rows)) + " seed row(s) found but NOT imported - there is no confirmed API for writing "
            "custom-table rows (the confirmed path is a workflow's Table Updater step). They're written to "
            "the run artifacts so nothing is lost."
        )
    return AppTable(path=rel, table_name=table_name, meta=meta, rows=rows, rows_path=rows_path, warnings=warnings)


def _make_asset(rel: str, text: Optional[str], size: int) -> AppAsset:
    language = ASSET_LANGUAGES.get(os.path.splitext(rel.lower())[1], "")
    if text is None:
        return AppAsset(path=rel, language=language, text=None, size=size,
                        skipped_reason="not UTF-8 text (binary?) - listed but not inlined")
    if size > MAX_INLINE_ASSET_BYTES:
        return AppAsset(path=rel, language=language, text=None, size=size,
                        skipped_reason="larger than {:,} bytes - listed but not inlined".format(MAX_INLINE_ASSET_BYTES))
    return AppAsset(path=rel, language=language, text=text, size=size)


def _apply_total_asset_cap(bundle: AppBundle) -> None:
    """Inline smallest-first, so one huge file can't crowd out everything else."""
    total = 0
    for asset in sorted(bundle.assets, key=lambda a: a.size):
        if asset.text is None:
            continue
        if total + asset.size > MAX_TOTAL_ASSET_BYTES:
            asset.text = None
            asset.skipped_reason = "total inlined source would exceed {:,} bytes".format(MAX_TOTAL_ASSET_BYTES)
            continue
        total += asset.size
    bundle.assets.sort(key=lambda a: a.path)


def _attach_routing(bundle: AppBundle, routing_files: list) -> None:
    """
    Pair each `<stem>.routing.json` with the Classic XML `<stem>.xml` in the
    same folder, and check every block Label it names really is a block of
    that file - a typo would otherwise silently leave a block on automatic
    routing (for CustomerB: a duplicate native workflow).
    """
    for rel, data in routing_files:
        folder = os.path.dirname(rel)
        stem = os.path.basename(rel)
        stem = stem[: -len(ROUTING_SUFFIX)] if stem.lower().endswith(ROUTING_SUFFIX) else spec_stem(rel)
        xml = next((x for x in bundle.classic_xml
                    if os.path.dirname(x.path) == folder
                    and _workflow_stem(x.path).lower() == stem.lower()), None)
        if xml is None:
            bundle.warnings.append(rel + ": a routing file, but there is no " + stem + ".xml beside it - ignored.")
            continue
        blocks = data.get("blocks")
        if not isinstance(blocks, dict) or not blocks:
            bundle.warnings.append(rel + ': a routing file needs a "blocks" object (block Label -> ' +
                                   "/".join(ROUTING_CHOICES) + ") - ignored.")
            continue
        try:
            labels = [unit.label for unit in parse_classic_xml_units(os.path.join(bundle.root, xml.path))]
        except Exception:
            labels = []
        routing = {}
        for label, choice in blocks.items():
            if choice not in ROUTING_CHOICES:
                bundle.warnings.append(rel + ": block " + repr(label) + " has " + repr(choice) +
                                       ", expected one of " + ", ".join(ROUTING_CHOICES) + " - ignored.")
            elif labels and label not in labels:
                bundle.warnings.append(rel + ": no block labelled " + repr(label) + " in " + xml.path +
                                       " (its blocks: " + ", ".join(repr(l) for l in labels) + ") - ignored.")
            else:
                routing[label] = choice
        xml.routing = routing
        xml.routing_path = rel
        skipped = [label for label, choice in routing.items() if choice == "skip"]
        xml.block_count = max(xml.block_count - len(skipped), 0)
        xml.warnings.append(
            "Routing from " + rel + ": " + "; ".join(repr(l) + " -> " + c for l, c in routing.items()) +
            (". Skipped blocks create no workflow." if skipped else "."))


def _design_targets(bundle: AppBundle) -> list:
    """
    Everything a design file can be matched to, as
    (key, kind, folder, stem, workflow_path) - a Classic XML (key = its own
    path; its FIRST block's workflow receives the file once converted) or a
    plain workflow JSON (key = its path). Workflows converted from an XML
    are that XML, not targets of their own.
    """
    targets = []
    for xml in bundle.classic_xml:
        targets.append((xml.path, "classicXml", os.path.dirname(xml.path),
                        _workflow_stem(xml.path), xml.generated_path))
    for workflow in bundle.workflows:
        if workflow.group_key is None:
            targets.append((workflow.path, "workflow", os.path.dirname(workflow.path),
                            _workflow_stem(workflow.path), workflow.path))
    return targets


def match_design_files(bundle: AppBundle) -> None:
    """
    Decide which workflow each design file is uploaded to.

    Nothing in the workflow JSON names its design file (`excelDesignFileKey`
    is set only after upload and embeds a workflow id Live has not assigned
    yet), so the pairing comes from where the files sit, in this order:

      1. `bundle.design_matches` - a person's own choice, always wins.
      2. FOLDER: the design file's folder holds exactly one Classic XML and
         no other design file - that XML is its function, whatever the two
         files are called. Real customer folders do not keep the names in
         step (CustomerA: "Classic_MMS001_MassUpload.xml" next to
         "MMS001_MassUpload_1.0.xlsx"), so the folder is the
         primary signal, not the name.
      3. NAME: an exact filename-stem match - among the XMLs in its folder
         when there are several, else across every workflow in the bundle
         (how a plain workflows/<name>.json pairs with <name>.xlsx).
      4. Anything else is AMBIGUOUS: `needs_choice` is set, nothing is
         uploaded, and the web UI / CLI ask a person to pick. Uploading a
         sheet onto the wrong workflow is worse than not uploading it.

    A Classic XML's design file goes on its FIRST block's workflow (the
    plain `<stem>.generated.json` - the real `<Export>` if the file has one),
    the sheet a person fills in to START the function.

    Recomputed from scratch on every call: it runs once at scan time and
    again after Classic XML conversion has added the workflows the XML
    targets resolve to, and the answer must not depend on which pass it is.
    """
    targets = _design_targets(bundle)
    by_key = {t[0]: t for t in targets}
    workflows_by_path = {w.path: w for w in bundle.workflows}
    for workflow in bundle.workflows:
        workflow.design_file = None

    designs_per_folder = {}
    for design in bundle.design_files:
        folder = os.path.dirname(design.path)
        designs_per_folder[folder] = designs_per_folder.get(folder, 0) + 1

    claimed = {}   # target key -> design path

    def attach(design, key, how):
        if key in claimed:
            design.needs_choice = True
            design.warnings.append(
                "Would go to " + key + ", which " + claimed[key] + " is already matched to - "
                "choose which workflow this one belongs to.")
            return
        claimed[key] = design.path
        design.target, design.matched_by = key, how
        workflow = workflows_by_path.get(by_key[key][4])
        if workflow is not None:
            workflow.design_file = design
            design.workflow_path = workflow.path

    # Hand-picked ones first, so an automatic match never claims a target
    # a person already assigned.
    ordered = sorted(bundle.design_files,
                     key=lambda d: (d.path not in bundle.design_matches, d.path))
    for design in ordered:
        design.target = design.matched_by = design.workflow_path = None
        design.needs_choice = False
        design.warnings = []
        folder = os.path.dirname(design.path)

        if design.path in bundle.design_matches:
            choice = bundle.design_matches[design.path]
            if choice == "":
                design.warnings.append("Not uploaded - chosen by hand.")
                continue
            if choice in by_key:
                attach(design, choice, "manual")
                continue
            design.warnings.append(
                "The hand-picked match " + repr(choice) + " is not a Classic XML or workflow in "
                "this folder any more.")

        xmls_here = [t for t in targets if t[1] == "classicXml" and t[2] == folder]
        if len(xmls_here) == 1 and designs_per_folder[folder] == 1:
            attach(design, xmls_here[0][0], "folder")
            continue
        pool = xmls_here or targets
        named = [t for t in pool if t[3] == design.stem]
        if len(named) == 1:
            attach(design, named[0][0], "name")
            continue

        design.needs_choice = True
        if xmls_here:
            design.warnings.append(
                "Its folder holds " + str(len(xmls_here)) + " Classic XML file(s) and " +
                str(designs_per_folder[folder]) + " design file(s), and none is named '" +
                design.stem + "' - choose which workflow it belongs to.")
        elif named:
            design.warnings.append(
                "Matches " + str(len(named)) + " workflows by name (" +
                ", ".join(t[0] for t in named) + ") - choose which one it belongs to.")
        else:
            design.warnings.append(
                "No Classic XML shares its folder and no workflow is named '" + design.stem +
                "' - choose which workflow it belongs to, or none.")


def _detect_links(bundle: AppBundle) -> None:
    """
    Report - not rewrite - references between members of the bundle.

    Two kinds are worth flagging before an import:
      * a workflow whose JSON mentions another bundle workflow's source
        WORKFLOW- id. Cross-workflow references get NEW ids in the target
        tenant, and nothing here knows the confirmed field that holds such
        a reference, so it can't be remapped - that has to be a human's
        call, made against the real workflow in Live.
      * a workflow whose JSON mentions a bundle table by name. Table
        references are by name, so they should survive a tenant move
        provided the table is created first (which the importer does).
    """
    by_source_id = {w.source_workflow_id: w for w in bundle.workflows if w.source_workflow_id}
    table_names = sorted({t.table_name for t in bundle.tables if t.table_name})

    for workflow in bundle.workflows:
        blob = json.dumps(workflow.data)
        for source_id, other in by_source_id.items():
            # Compare ids, not object identity: a GET response always
            # contains its own workflowId, so identity alone would let two
            # copies of the same exported workflow "reference" each other.
            if source_id == workflow.source_workflow_id or source_id not in blob:
                continue
            bundle.links.append({
                "kind": "workflow-ref",
                "from": workflow.path,
                "to": other.path,
                "detail": "'" + workflow.name + "' references '" + other.name + "' by its source id " + source_id,
            })
            workflow.warnings.append(
                "References another workflow in this app by its OLD id (" + source_id + "). That id changes on "
                "import and is not remapped - no confirmed field holds this reference, so fix it by hand in Live."
            )
        for table_name in table_names:
            if '"' + table_name + '"' in blob:
                bundle.links.append({
                    "kind": "table-ref",
                    "from": workflow.path,
                    "to": table_name,
                    "detail": "'" + workflow.name + "' references table '" + table_name +
                              "' by name (names survive the move)",
                })
