---
name: vince-live-workflow
description: Use when verifying or drafting Vince Live / VXL Live workflow JSON (the POST /workflows body), Custom Table metas, or an App Builder handoff — e.g. "would this work in Vince Live", "build a Vince Live workflow that...", "draft the JSON for an M3 API step / Generic Filter / Transform / REST API / Code step / Table Updater", "convert this Classic/VXL function to Live", or any question mentioning Vince Live, VXL, api.vince.live, M3 API step, ION API bulk execute, JSONata Transform, or Custom Tables. Carries a portable set of platform facts confirmed by building real examples in Live's UI or hitting the real API — not documentation-derived guesses.
---

# Vince Live workflow verification & generation

Vince Live is Infor/Vince's low-code workflow platform (successor to VXL
Classic), reachable at `https://api.vince.live`. This skill packages facts
about its real JSON structure and API surface, every one of them confirmed
by building a real example in Live's own builder UI or by calling the real
API and reading back what actually came out — never by reading
documentation or guessing by analogy.

## The one rule that overrides everything else here

**Never invent a field name, enum value, endpoint, or step type by
analogy.** This skill's reference files are a curated, confirmed subset —
not a complete specification. If a design needs something not covered in
`references/`, that means it is genuinely unconfirmed. Say so explicitly,
in the output, and name how to actually settle it:

- **Build a minimal real example in Live's own builder UI** and read back
  what it saves (a GET of the workflow), or
- **Hit the real API directly** (`api.vince.live`) and read the real
  response or error — a wrong guess at an enum often produces a specific,
  useful 400 error, which confirms the guess was *wrong* but does not by
  itself reveal the *right* value.

A confirmed-looking HTTP 200 is not proof of correctness either — several
documented failures here pass creation silently and only break rendering
or execution later. Never present an unconfirmed guess as settled fact, and
never quietly drop this caveat to make an answer look more complete.

## Two things this skill does

### 1. Verify a proposed design

Given a description or a draft workflow/table JSON, decide whether it would
actually work on the real platform, and explain the real mechanics — not
just "yes/no".

1. Identify what the design actually needs: which step types, which
   platform behaviours (a per-call filter? a value that depends on an
   earlier M3 call's result? grouped rows? a Custom Table write?).
2. Check each need against `references/capability-matrix.md` first — it's
   organized by construct (conditional logic, chaining, grouping,
   Excel/sheet dependence) and gives a verdict with the *why*, not just a
   yes/no.
3. Cross-check the concrete JSON shape against
   `references/live-workflow.schema.json` (the formal schema, with every
   gotcha in a `description`/`$comment`) and `references/workflow-structure.md`
   (the sequencing rules and step catalogue).
4. For anything M3-specific, `references/m3-native-api-step.md` (the
   `m3_api_1` step) and `references/m3-generic-pipeline.md` (the
   Transform+REST API / Code-step alternative) have the deep mechanics,
   including exactly where the native path's `GENERIC_FILTER` step
   structurally cannot express something (it runs once, before the whole
   M3 API step, and only ever sees raw spreadsheet rows — never an M3
   call's own output).
5. For Custom Tables, `references/custom-tables.md`.
6. State the verdict as one of: **works as described** (say which step
   types/fields make it work), **works, but differs from what was
   described** (say exactly how), **doesn't work on this path — use the
   other path instead** (native vs. generic pipeline), or **unconfirmed —
   here's how to check**. Ground every claim in a specific fact from the
   reference files; don't just assert a verdict.

### 2. Generate workflow / table / app JSON from a description

1. Decide **native M3 API path vs. generic pipeline path** first (see
   `references/workflow-structure.md` §3 for the two canonical shapes, and
   `references/capability-matrix.md` for what forces the pipeline path —
   chiefly per-call filtering and any gate reading an earlier M3 call's own
   output). Prefer native whenever it fits; it has the only confirmed
   field-metadata validation and strict save-time validation.
2. Start from the matching worked template in `references/workflow-structure.md`
   §5/§6 and adapt it — don't build the envelope from memory. Keep every
   structural key, enum value, and nesting exactly as shown; only the
   business content (program/transaction names, field names, column
   letters, JSONata expressions, ids) changes.
3. Fill in the M3-specific parts using `references/m3-native-api-step.md`
   (transaction/input/output field shapes, the `type: "ADD"` gotcha, the
   `ref` asymmetry between API-chain and repeated-CLIENT-field cases) or
   `references/m3-generic-pipeline.md` (the Transform/REST API payload
   idiom, JSONata binding model, filtering tiers).
3a. **Every program/transaction name must be real - verify it, never invent
   it.** A name M3 doesn't know imports cleanly and then renders "Inputs (0)"
   on that card. A spec that names only M3 *tables* (MITPOP, OLICHR) or says
   "API: to be confirmed" is not a transaction name: map the table to a real
   MI transaction, check it (and each input/output field) against the metadata
   endpoint or the field catalog, and if none is known, **stop and ask** - never
   write `TBC_*`, a placeholder or a guessed verb. See
   `references/m3-native-api-step.md` §10 for the rules and a verified
   table-to-transaction list.
4. For a whole **app** (workflows + Custom Tables + an App Builder
   handoff), also use `references/auth-endpoints-and-unconfirmed.md` §4-§7
   for labels (`VXL`), Groups, and design-file attachment — and remember
   App Builder itself has **zero** confirmed facts; never fabricate a
   prompt/schema for it (state that plainly instead, per that file's §8).
5. Use real ids as placeholders in the exact confirmed shapes
   (`TENANT-<32 hex>`, `WORKFLOW-<32 hex>`, `CONNECTION-<32 hex>`,
   `ENVIRONMENT-<32 hex>`) and say plainly that Live assigns its own
   `workflowId`/transaction instance ids regardless of what's sent — never
   present an invented id as something the output should rely on.
6. Run the checklist in `references/workflow-structure.md` §7 before
   presenting the draft as finished.
7. **Explicitly flag every field or value not backed by a reference file**
   — inline, next to that field, not buried in a footnote — with the
   verification path from "The one rule" above. A generated draft with
   silent gaps is worse than one that visibly says "unconfirmed: X, verify
   by Y."

## Reference files

| File | Contents |
|---|---|
| `references/live-workflow.schema.json` | Formal JSON Schema for the whole `POST /workflows` body, gotchas embedded in `description`/`$comment` on every field. The single source of truth for exact shapes. |
| `references/workflow-structure.md` | Mental model, the closed step-type catalogue, sequencing rules, POST-vs-GET shape mismatch, two full worked templates (native + generic pipeline), pre-flight checklists. |
| `references/m3-native-api-step.md` | Deep reference for the `m3_api_N` step: transaction/input/output fields, `type: "ADD"`, the metadata endpoint, runtime field naming, why `GENERIC_FILTER` structurally can't do per-call or M3-output-dependent gating. |
| `references/m3-generic-pipeline.md` | The Transform+REST API (ION API bulk execute) alternative to the M3 API step: full JSONata binding model, payload-building idiom, three-tier filtering strategy, the single-Code-step variant for heavily per-row-gated control flow, silent-failure-mode list. |
| `references/custom-tables.md` | Custom Tables API: create/read/write, primary-key pitfalls, the (unconfirmed) Table Updater write path, broken search pagination and its workaround. |
| `references/capability-matrix.md` | Verdict tables for common conditional-logic/chaining/grouping constructs — what's possible on which path, and why. Best starting point for "would this design work." |
| `references/auth-endpoints-and-unconfirmed.md` | Auth model (the token decides the tenant, and a mismatch fails silently), full endpoint catalog, `VXL`/`Imported` labels, design-file upload, Groups API, and the consolidated list of what is NOT confirmed. |

## Style

Write findings the way the source material does: state what's confirmed
plainly, mark what isn't with `❓ **Unconfirmed**` or an equivalent, and
never soften that distinction to make output read more complete. When
citing a fact, it's fine to point at which reference file it came from —
that's a feature (checkable), not clutter.
