"""
Converting the Classic XML files found in an app folder into workflow JSON,
as part of an app import.

This is the seam between the two halves of the project that had never met:
`source_classic_xml` + `build` know how to turn one Classic export into one
Live workflow, and `appimport` knows how to land a folder full of workflows
in a tenant. This module runs the first over every XML in a bundle and hands
the results to the second.

## What it does

For each Classic XML in the bundle, in scan order, and then for each
workflow that file yields:

  1. parse it (`parse_classic_xml_units`), which reads EVERY <Export> and
     <Import> block and groups them into one unit per workflow,
  2. fetch real M3 field metadata for its transactions, if a token is set,
  3. build the POST body (`build_live_workflow`) against the target ids,
  4. write it into the app folder as `workflows/<stem>.generated.json`,
  5. append it to `bundle.workflows` so the ordinary import path takes it
     from there.

## One XML can be several workflows

A Classic file states its sheet name, data row, message column and
block-level gate PER BLOCK, and Live carries one of each per workflow, so
a multi-block file cannot be one workflow - see
`source_classic_xml._parse_raw_units`. Each block therefore becomes its own
workflow here, named after its own `Label`, and every one of them is
reported separately in the plan, the confirmation box and the results.
Before 2026-09-10 only the first block was read at all; a real customer
file (`C05_MMS100...xml`) silently lost 4 of its 6 M3 calls that way.

The first block keeps the plain `<stem>.generated.json` filename and later
ones get a numbered suffix - see `_file_suffix` for why that asymmetry is
deliberate.

Step 5 is the point. A built workflow is already a POST body with
`definition.workflowConfig`, which is exactly what `looks_like_workflow`
recognises and what `clone_and_retarget` accepts, so a converted XML needs
no special case at creation time - it is imported by the same code, logged
to the same history and reported in the same lists as a workflow that
arrived as JSON. The only thing that marks it out is where it came from.

## Naming

Updated 2026-09-16: the workflow's NAME is now `"<base name> - <block
Label>"` (`source_classic_xml.compose_workflow_name`), not the block Label
alone - `base_names`, keyed by the XML's app-relative path, supplies the
base name per FILE (all of a file's blocks share it, only the block-label
half differs), suggested from the file's own stem when the caller doesn't
override it. This still deliberately does NOT go through
`state.next_labeled_name` - app import preserves names exactly (an app's
parts refer to each other by name), it's just that the name being preserved
is the composed one now. Re-running an import therefore rewrites the same
file rather than accumulating "Check Data 2", "Check Data 3" on disk. It
also means re-importing duplicates the workflow in the tenant, which is the
documented app-import behaviour for every source, not something new here.

The FILE is named after the XML's stem, not the label: labels are neither
unique nor filesystem-safe, and a stem traces straight back to its source.

## Dry runs DO write these files

A dry run writes the converted JSON into the app folder for real, unlike the
generated App Builder prompt, which it deliberately withholds. The
difference is that the prompt embeds run results - placeholder statuses and
absent workflow ids - so a dry-run copy would be actively misleading. A
converted workflow embeds none: it is a pure function of the XML, the target
ids and the field metadata, byte-identical to what a real import would
write. Dry-run-then-inspect is the natural way to review a migration before
committing it, and that only works if the files are actually there.

## Missing metadata is a warning, not a block

Without a usable token (or for a transaction the metadata endpoint doesn't
know), fields fall back to `metadata.M3_FIELD_METADATA`'s handful of static
entries and the rest get a column header reading "TODO: metadata not found
for ..." with type UNKNOWN. Such a workflow is still created - reported
loudly per file, in the run warnings, and in the run history - rather than
held back. Nothing is silently degraded, but the decision to keep or fix it
stays with whoever is running the migration.
"""

import copy
import json
import os

from build import build_live_workflow
from build_pipeline import build_pipeline_spec, build_pipeline_workflow, pipeline_warnings
from classic_to_pipeline import ClassicPipelineError, translate_classic_to_pipeline
from metadata import (fetch_metadata_for_workflow, fill_from_catalog, missing_metadata_fields,
                      unmapped_mandatory_warning)
from source_app_folder import (AppWorkflow, GENERATED_WORKFLOW_SUFFIX, WORKFLOWS_SUBDIR,
                               match_design_files)
from source_classic_xml import (compose_workflow_name, parse_classic_xml_units,
                                unrepresentable_filter_operators)


def _workflows_dir(root: str) -> str:
    """
    The folder's own workflows/ directory, matching however it is already
    spelled on disk. A folder fetched into by live_fetch has "workflows";
    one a person made by hand may have "Workflows". On Windows both resolve
    to the same directory anyway, so the only thing at stake is whether the
    path we report back matches what the user sees - but on a folder synced
    from a case-sensitive filesystem it would really be two directories.
    """
    for entry in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if entry.lower() == WORKFLOWS_SUBDIR and os.path.isdir(os.path.join(root, entry)):
            return os.path.join(root, entry)
    return os.path.join(root, WORKFLOWS_SUBDIR)


def xml_stem(xml_rel: str) -> str:
    """The XML's own filename, minus the extension - the default suggested base name."""
    stem = os.path.basename(xml_rel)
    if stem.lower().endswith(".xml"):
        stem = stem[: -len(".xml")]
    return stem


def _file_suffix(index: int, label: str) -> str:
    """
    What to insert before `.generated.json` for the index'th workflow a
    single XML produced.

    The first gets nothing, so a one-block file keeps the exact filename it
    always had - which is what design-file pairing matches on
    (`orders.xlsx` -> `orders.generated.json`) and what makes a re-import
    overwrite rather than accumulate. Later blocks get a numbered,
    label-derived suffix so they are traceable back to the block they came
    from without colliding.
    """
    if index == 0:
        return ""
    slug = "".join(c if c.isalnum() else "-" for c in (label or "block").lower())
    slug = "-".join(part for part in slug.split("-") if part)[:40]
    return "." + str(index + 1) + "-" + (slug or "block")


def _write_generated(root: str, xml_rel: str, body: dict, warnings: list, suffix: str = ""):
    """Write the converted workflow into the app folder. Returns (abs path, relative path) or (None, None)."""
    directory = _workflows_dir(root)
    stem = xml_stem(xml_rel)
    path = os.path.join(directory, stem + suffix + GENERATED_WORKFLOW_SUFFIX)
    try:
        os.makedirs(directory, exist_ok=True)
        existed = os.path.exists(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2)
        if existed:
            warnings.append("Overwrote the existing " + os.path.basename(path) +
                            " - the .xml is the source of truth, any hand-edits to it are gone.")
        return path, os.path.relpath(path, os.path.abspath(root)).replace(os.sep, "/")
    except OSError as e:
        warnings.append("Couldn't write the converted workflow into the app folder: " + str(e) +
                        " - it is still in the run artifacts and will still be imported.")
        return None, None


# Live refuses a workflow that is too big: `HTTP 400: Item size to update has
# exceeded the maximum allowed size` (OBSERVED 2026-09-29, CustomerA import
# RUN/app-imports/vl-20260929-141416). The real limit is unknown and is not a
# plain body-byte count - native bodies of 134 KB were accepted and 179 KB /
# 379 KB refused. A native block over this is rebuilt on the generic-pipeline
# path, whose compact Code step carries the same calls in far less (Haakon's
# call, 2026-09-29: getting it working beats staying native; easy to revert -
# raise this, or drop the check, once the real limit is known).
NATIVE_BODY_SIZE_LIMIT = 150_000
# Same limit, seen on Code-step bodies: 82 KB accepted, 116-128 KB refused.
# Above this a pipeline-built body is warned about, never silently sent.
PIPELINE_BODY_SIZE_WARN = 100_000


def body_size(body) -> int:
    """Bytes of a POST body as sent - what the size limit above is measured on."""
    return len(json.dumps(body).encode("utf-8"))


def convert_classic_xml(bundle, connection_id: str, tenant_id: str, environment_id: str,
                        bearer_token: str = "", write_to_folder: bool = True,
                        ion_api_base_url: str = "", base_names: dict = None,
                        header_row: int = None) -> list:
    """
    Convert every Classic XML in `bundle`, appending each result to
    `bundle.workflows` so the caller's normal import path picks it up.

    `base_names`, when given, maps a Classic XML's app-relative path (
    `entry.path`) to the base name the caller (or the person running the
    import) chose for it - every block from that file gets
    `"<base name> - <block Label>"` (`compose_workflow_name`). A file with
    no entry falls back to its own stem, so this is optional at every call
    site. Every `AppWorkflow` this produces also gets `group_key=entry.path`
    set, so `appimport` can bucket a file's blocks back together after
    creation and group them in Live.

    Returns one result dict per WORKFLOW, in scan order - which is one per
    XML for a single-block file and one per block otherwise, each carrying
    its own `block`/`blockCount`. A file that fails to convert yields a
    single failed result and is skipped; it does not abort the others, for
    the same reason a failed workflow POST doesn't - a partial import with
    a clear per-resource report beats a run that stops at the first
    problem.

    ## Native vs. generic-pipeline routing, per BLOCK

    Each unit is built on the native (M3 API step) path by default, same as
    always. A unit is instead routed onto the generic-pipeline path when
    `source_classic_xml.unrepresentable_filter_operators` finds a RowFilter native
    cannot express (a range on a non-numeric value today - see
    build.native_filter_problem) - and ONLY for that reason; see its
    own docstring for why this stays narrow. This is a per-BLOCK decision,
    not per-file: a real CustomerA file (`Creation_CentralRef`) has 8
    blocks, only 5 of which need it. Every routed block's result carries
    `buildPath: "pipeline"` and a warning naming the operator(s) and why -
    the confirmation box shows it before anything reaches a tenant.

    A routed block still falls back to native, loudly, in two cases: the
    unit turns out to be an `<Export>`-only block (the pipeline path has no
    Excel step and cannot use one at all), or `ion_api_base_url` was not
    supplied (the pipeline path cannot build without the target's ION API
    root, which nothing can derive - it is customer/tenant-specific). Both
    leave the affected filter gate(s) off, exactly as an unrouted block
    always has - this is a strict improvement, never a regression.

    A routed block's generated Code step always has `debug: false` (since
    2026-09-29 the standalone `classic-pipeline` default too) - because a real
    import is not a one-off diagnostic run: `debug: true`'s documented
    self-poisoning hazard (see CLAUDE.md) is not something to hand a
    customer migration by default.
    """
    results = []
    for entry in bundle.classic_xml:
        full = os.path.join(bundle.root, entry.path.replace("/", os.sep))

        base_name = (base_names or {}).get(entry.path) or xml_stem(entry.path)

        try:
            units = parse_classic_xml_units(full)
        except Exception as e:
            results.append({
                "path": entry.path,
                "label": entry.label,
                "name": entry.label,
                "transactionCount": entry.transaction_count,
                "warnings": list(entry.warnings),
                "criticalWarnings": list(entry.warnings),
                "ok": False,
                "status": "failed to parse",
                "error": type(e).__name__ + ": " + str(e),
            })
            continue

        for index, workflow in enumerate(units):
            # The scan's own findings travel with every workflow the file
            # produced, so they are visible in the confirmation box and the
            # results, not just in the plan.
            warnings = list(entry.warnings)
            # `critical` is the subset that has to travel up to the run-level
            # warning list. Everything else stays with the file it belongs to.
            # Without this split a folder of 80 XMLs repeats every note three
            # times - once per file, once per converted workflow, once at the
            # top - and the two that actually matter (calls silently dropped,
            # fields with no metadata) drown in the ones that don't.
            critical = list(entry.warnings)
            if len(units) > 1:
                split_note = (
                    "Block " + str(index + 1) + " of " + str(len(units)) + " in " + entry.path +
                    ", from Classic's " + repr(workflow.label or "unlabelled") + " block. Each "
                    "block becomes its own workflow because one Live workflow carries a single "
                    "sheet, data row and filter gate, and Classic states those per block."
                )
                warnings.append(split_note)
                critical.append(split_note)

            # Composed from the file's base name + this block's own function -
            # see the module docstring's "Naming" section. `original_label`
            # is kept for the result/warning text below (still the block's
            # own bare Label); `workflow.label` itself is overwritten with
            # the composed name BEFORE either builder runs, because both
            # build_live_workflow (native) and translate_classic_to_pipeline
            # (generic pipeline, via `label=wf.label` in classic_to_pipeline)
            # read the workflow name from this IR attribute, not from any
            # variable kept only in this function. Missing this reassignment
            # was a real bug: the composed name reached AppWorkflow.name and
            # every warning/history string, but the actual POST body still
            # carried the bare block Label - so re-running an import created
            # workflows named "Create Specific Area" instead of "<base name>
            # - Create Specific Area", colliding by name with unrelated
            # workflows and with themselves on every re-run.
            original_label = workflow.label or entry.label
            name = compose_workflow_name(base_name, original_label)
            workflow.label = name

            result = {
                "path": entry.path,
                "label": original_label,
                "name": name,
                "transactionCount": len(workflow.transactions),
                "block": index + 1,
                "blockCount": len(units),
                "warnings": warnings,
                "criticalWarnings": critical,
            }

            # --- native vs. generic-pipeline routing - see the function
            # docstring's "Native vs. generic-pipeline routing" section ---
            def try_pipeline(reason, native_consequence):
                """The block built on the generic-pipeline path, or None (with a loud note)."""
                try:
                    pipeline, pipe_warnings = translate_classic_to_pipeline(
                        workflow, base_url=ion_api_base_url or None,
                        connection_id=connection_id, debug=False,
                        header_row=header_row,
                    )
                except ClassicPipelineError as e:
                    note = ("PIPELINE PATH NOT AVAILABLE: this block " + reason + ", but it "
                            "can't go on the generic-pipeline path either (" + str(e) + "). Built "
                            "on the native path instead - " + native_consequence + ".")
                    warnings.append(note)
                    critical.append(note)
                    return None
                try:
                    built = build_pipeline_workflow(pipeline, connection_id, tenant_id, environment_id)
                except Exception as e:
                    note = ("PIPELINE BUILD FAILED (" + type(e).__name__ + ": " + str(e) + ") - "
                            "most likely no ION API root was supplied for this import. Built on "
                            "the native path instead - this block " + reason + ", so " +
                            native_consequence + ".")
                    warnings.append(note)
                    critical.append(note)
                    return None
                warnings.extend(list(pipe_warnings) + pipeline_warnings(pipeline))
                note = "Routed to the generic-pipeline path: this block " + reason + "."
                warnings.append(note)
                critical.append(note)
                return built

            # A routing file beside the XML (source_app_folder.ROUTING_SUFFIX)
            # overrides the automatic choice for the blocks it names.
            routed = (entry.routing or {}).get(original_label)
            if routed == "skip":
                note = ("Skipped: " + str(entry.routing_path) + " marks this block 'skip' - another "
                        "source in the folder replaces it. No workflow is created for it.")
                result.update(ok=True, skipped=True, status="skipped (routing file)",
                              buildPath="skipped", warnings=[note], criticalWarnings=[note])
                results.append(result)
                continue

            build_path = "native"
            body = None
            unrepresentable = unrepresentable_filter_operators(workflow)
            if routed == "pipeline":
                body = try_pipeline(
                    "is set to the Code-step path by " + str(entry.routing_path),
                    "the routing file's choice is NOT applied")
                if body is not None:
                    build_path = "pipeline"
            elif routed == "native":
                if unrepresentable:
                    note = ("Kept on the native path by " + str(entry.routing_path) + " although it uses "
                            "filter operator(s) " + ", ".join(unrepresentable) + " native cannot "
                            "express - those filter gate(s) are left off.")
                    warnings.append(note)
                    critical.append(note)
            elif unrepresentable:
                body = try_pipeline(
                    "uses filter operator(s) " + ", ".join(unrepresentable) +
                    " in a form the native path cannot express (see "
                    "build.native_filter_problem - e.g. a range on a non-numeric value)",
                    "the affected filter gate(s) are left off")
                if body is not None:
                    build_path = "pipeline"

            if build_path == "native":
                overrides = None
                if bearer_token and connection_id:
                    problems = []
                    try:
                        overrides = fetch_metadata_for_workflow(workflow, connection_id, bearer_token, problems)
                    except Exception as e:
                        warnings.append("Field metadata lookup failed (" + type(e).__name__ + ": " + str(e) +
                                        "). Converted with the static table only.")
                        overrides = None
                    for problem in problems:
                        warnings.append("Metadata lookup: " + problem)
                elif not bearer_token:
                    warnings.append("No bearer token, so no M3 field metadata was fetched.")
                elif not connection_id:
                    warnings.append("No connectionId, so no M3 field metadata could be fetched.")
                overrides = fill_from_catalog(workflow, overrides, warnings)
                warnings.extend(unmapped_mandatory_warning(workflow, overrides))

                try:
                    body = build_live_workflow(workflow, connection_id, tenant_id, environment_id,
                                               overrides, warnings)
                except Exception as e:
                    result.update(ok=False, status="failed to build",
                                  error=type(e).__name__ + ": " + str(e))
                    results.append(result)
                    continue

                missing = missing_metadata_fields(body)
                if missing:
                    shown = ", ".join(missing[:6]) + (" (+" + str(len(missing) - 6) + " more)" if len(missing) > 6 else "")
                    message = (
                        str(len(missing)) + " field(s) have no metadata: their Excel column header will read "
                        '"TODO: metadata not found ..." and their type will be UNKNOWN - ' + shown +
                        ". First thing to check: the token must belong to the SAME tenant as the connection id "
                        "(a cross-tenant token does not error, it just makes the connection invisible)."
                    )
                    warnings.append(message)
                    critical.append(message)
                result["missingMetadataFields"] = missing

                size = body_size(body)
                if size > NATIVE_BODY_SIZE_LIMIT:
                    rerouted = try_pipeline(
                        "is too large for Live on the native path (" + str(size // 1000) +
                        " KB; bodies this size were refused with 'Item size to update has exceeded "
                        "the maximum allowed size')",
                        "Live will very likely refuse to create it")
                    if rerouted is not None:
                        body, build_path = rerouted, "pipeline"
                        result.pop("missingMetadataFields", None)

            if build_path == "pipeline" and body_size(body) > PIPELINE_BODY_SIZE_WARN:
                note = ("POSSIBLY TOO LARGE FOR LIVE (" + str(body_size(body) // 1000) + " KB): "
                        "Code-step workflows of 116 KB and up were refused with 'Item size to "
                        "update has exceeded the maximum allowed size'; 82 KB was accepted.")
                warnings.append(note)
                critical.append(note)

            result["buildPath"] = build_path

            written_abs, written_rel = (None, None)
            if write_to_folder:
                # original_label, not workflow.label (now the composed
                # name) - the suffix's job is disambiguating BLOCKS within
                # one file, and a long base name would otherwise dominate
                # the 40-char slug truncation and collide across blocks.
                written_abs, written_rel = _write_generated(
                    bundle.root, entry.path, body, warnings, _file_suffix(index, original_label))
            generated_rel = written_rel or entry.generated_path

            # A pointer, not a copy: these warnings are already reported against
            # the XML itself, and repeating them here would double every line in
            # both the UI and the CLI output.
            note = "Converted from " + entry.path
            if len(units) > 1:
                note += " (block " + str(index + 1) + " of " + str(len(units)) + ")"
            if warnings:
                note += " - " + str(len(warnings)) + " conversion warning(s) listed with that file"
            bundle.workflows.append(AppWorkflow(
                path=generated_rel,
                name=name,
                source_workflow_id=None,
                data=body,
                warnings=[note + "."],
                group_key=entry.path,
            ))

            result.update(ok=True, status="converted", name=name,
                          generatedPath=written_abs, generatedRelPath=generated_rel,
                          written=bool(written_abs), body=body)
            results.append(result)

    # The scan paired design files against the workflows it could see; the
    # ones just converted from XML were not among them. Re-run the match so
    # `orders.xlsx` finds `orders.generated.json` too. Idempotent - already
    # paired files are skipped.
    if results:
        match_design_files(bundle)
    return results


def convert_pipeline_specs(bundle, connection_id: str, tenant_id: str, environment_id: str,
                           write_to_folder: bool = True, ion_api_base_url: str = "") -> list:
    """
    Build every hand-written pipeline spec in `bundle` (source_app_folder.
    AppPipelineSpec) with the TARGET's ids, and append each to
    `bundle.workflows` - the same contract as convert_classic_xml.

    Built here, never imported pre-built: a Code step carries its connection
    id and ION endpoint inside its source text, which clone_and_retarget does
    not touch, so a spec built for one tenant would run against another.
    `ion_api_base_url`, when given, replaces the spec's own baseUrl (specs
    are written with a `<TENANT>` placeholder). The workflow is created
    under the spec's own "name", verbatim; `groupWith` puts it in the Live
    Group of the Classic XML it names.
    """
    results = []
    for spec in bundle.pipeline_specs:
        data = copy.deepcopy(spec.data)
        if ion_api_base_url:
            data["baseUrl"] = ion_api_base_url
        warnings = list(spec.warnings)
        result = {"path": spec.path, "label": spec.name, "name": spec.name, "kind": "pipelineSpec",
                  "warnings": warnings, "criticalWarnings": list(spec.warnings)}
        try:
            body, build_warnings = build_pipeline_spec(data, connection_id, tenant_id, environment_id)
        except Exception as e:
            result.update(ok=False, status="failed to build", error=type(e).__name__ + ": " + str(e))
            results.append(result)
            continue
        warnings.extend(build_warnings)
        placeholder = [w for w in build_warnings if "placeholder" in w]
        result["criticalWarnings"].extend(placeholder)

        written_abs, written_rel = (None, None)
        if write_to_folder:
            path = os.path.join(bundle.root, spec.generated_path.replace("/", os.sep))
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(body, f, indent=2)
                written_abs, written_rel = path, spec.generated_path
            except OSError as e:
                warnings.append("Couldn't write the built workflow into the app folder: " + str(e) +
                                " - it is still in the run artifacts and will still be imported.")

        bundle.workflows.append(AppWorkflow(
            path=written_rel or spec.generated_path,
            name=spec.name,
            source_workflow_id=None,
            data=body,
            warnings=["Built from the pipeline spec " + spec.path +
                      (" - " + str(len(warnings)) + " build warning(s) listed with that file" if warnings else "") + "."],
            group_key=spec.group_with,
        ))
        result.update(ok=True, status="built", buildPath="pipeline", generatedPath=written_abs,
                      generatedRelPath=written_rel or spec.generated_path, written=bool(written_abs),
                      body=body)
        results.append(result)
    return results
