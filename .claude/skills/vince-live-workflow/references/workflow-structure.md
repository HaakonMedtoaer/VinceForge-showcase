# Vince Live workflow structure — mental model + worked templates

Companion to `live-workflow.schema.json` in this same folder. That file has
the field-by-field gotchas; this file has the shape at a glance and two full
worked templates to start a draft from.

Everything here was confirmed by building a real example in Live's own UI or
by calling the real API and reading back what came out — not from
documentation or inference. If you need something not covered here or in the
other reference files, say so explicitly and describe how to verify it (see
`SKILL.md`) rather than inventing it.

## 1. The mental model

A workflow is **one linear chain of steps**, rooted at exactly one `TRIGGER`
step, nested through `childSteps`. No branching has been seen in any
confirmed example. Each step's output becomes the next step's input, and
every step is addressable by its exact `stepId` from any later step.

```
trigger_1 ──► step ──► step ──► step ──► (end)
```

Two facts worth internalising immediately:

1. **`stepId` is load-bearing, not cosmetic.** Later steps (Transform
   templates, mainly) reference earlier ones by exact `stepId`
   (`$context.data.all.rest_api_1`). Renaming one breaks every reference.
2. **`childSteps: []` is a valid, saveable terminal state for any step
   type.** A workflow does not have to end in `excel_1`, or in anything
   particular.

## 2. The step catalogue — a CLOSED list

Guessing here has failed for real: both `"TRANSFORM"` and `"REST_API"` were
rejected by Live's own validation before the correct values (below) were
found.

| `type` | `name` | `target` | `stepId` convention |
|---|---|---|---|
| `TRIGGER` | `Trigger` | `workflow-trigger` | `trigger_1` |
| `API` | `M3 API` | `workflow-api` | `m3_api_1` |
| `EXCEL` | `Excel` | `workflow-excel` | `excel_1` |
| `GENERIC_FILTER` | `Generic Filter` | `workflow-generic-filter` | `generic_filter_1` |
| `TRANSFORMER_MORPH` | `Transform` | `workflow-transform` | `transform_N` |
| `GENERIC_API` | `Rest API` | `workflow-rest-api` | `rest_api_N` |
| `GENERIC_CODE` | `Code` | `workflow-code` | `code_N` |

Casing is exact — `"Rest API"`, not `"REST API"`. `retryAttempts: 0` on
every step except `trigger_1`, which omits the field entirely.

A **Table Updater** step also exists (the only confirmed way to write rows
into a Custom Table) but **its JSON shape is NOT CONFIRMED** — no capture
exists. If a design needs one, say so plainly and flag it as needing a real
capture from the builder first; do not invent its `type`/`target`/stepConfig.

## 3. Sequencing rules — what may follow what

- `trigger_1` is always the root, always present, exactly one.
- `excel_1` is **M3-API-specific**. Its columns can only reference an M3 API
  transaction's instance id. Attaching it after a non-M3 chain
  (Transform/REST API/Code with no `m3_api_1`) caused a real save failure in
  the builder. For spreadsheet I/O without an M3 API step, use the **`VXL`**
  label instead (see `auth-endpoints-and-unconfirmed.md`).
- `generic_filter_1` runs **before** the M3 API step and filters raw row
  data going into the whole step — never one transaction only. This is the
  single most common reason a design needs the generic pipeline path
  instead (`m3-generic-pipeline.md`).
- Transform → REST API pairs chain freely, repeatedly. Seven such pairs in a
  row is a real, confirmed production shape.
- Ordering *within* an M3 API step is separate from step ordering — its
  transactions carry their own `executionOrder` (see `m3-native-api-step.md`).

### The two canonical sequences

**A. Native M3 path** — prefer this whenever it fits. Gives you
field-metadata validation, automatic pagination, `excel_1` binding, and much
stricter save-time validation.

```
trigger_1 (CLIENT)
  └─ generic_filter_1        (optional, row filter, whole-step)
       └─ m3_api_1           (1..n M3 transactions, executionOrder 1..n)
            └─ excel_1       (optional, column bindings back to m3_api_1)
```

**B. Generic pipeline path** — what Vince's own production customer
workflows use when the native path can't express the logic (per-call
filtering above all — see `m3-generic-pipeline.md`). No M3 API step, no
Excel step.

```
trigger_1
  └─ transform_1        builds { "payload": { ...M3 bulk body... } }
       └─ rest_api_1    POSTs it to ION API's bulk execute endpoint
            └─ transform_2
                 └─ rest_api_2
                      └─ ...           (repeat as needed)
```

One real production example performs full M3 **writes** this way across
seven Transform/REST API pairs. A variant of this path collapses the whole
chain into a **single Code step** instead, for control flow the
Transform/REST pair shape structurally cannot carry (per-row correlation
once any call in the chain is gated) — see `m3-generic-pipeline.md` §4.

## 4. What POST and GET do NOT share

**GET and POST do not share a body shape.** GET returns server-computed
fields (`stats`, `workflowArn`, `definitionAsl`,
`created`/`modified`/`createdBy`/`modifiedBy`, `overview`) that POST's
validation rejects outright (e.g. `stats.lastExecutionId: null` fails —
POST's schema wants a string or the key absent, never `null`). GET also
**omits `description`/`labels` entirely**, though creation requires them.
**To clone a workflow, strip a GET response down to exactly the fields in
the schema — never forward the whole GET body.**

No workflow `PUT`/`PATCH` endpoint is confirmed. Only `POST /workflows` and
`GET /workflows/{id}` exist as confirmed calls.

## 5. Worked template — native M3 path

Illustrative skeleton assembled strictly from confirmed field shapes (every
piece below is individually confirmed in `live-workflow.schema.json`) —
**not** a byte-for-byte capture of one specific real workflow. Treat the
placeholder ids/names as exactly that; keep every structural key, enum
value, and nesting exactly as shown.

```json
{
  "entity": "Workflow",
  "description": "",
  "hasError": false,
  "tenantId": "TENANT-00000000000000000000000000000000",
  "workflowId": "WORKFLOW-00000000000000000000000000000000",
  "workflowName": "Get Item Description",
  "workflowAlias": "Get Item Description",
  "active": true,
  "workflowType": "STANDARD",
  "labels": [],
  "tags": {
    "connectionId": ["CONNECTION-00000000000000000000000000000000"],
    "apiEnvironment": "ENVIRONMENT-00000000000000000000000000000000",
    "environments": ["ENVIRONMENT-00000000000000000000000000000000"],
    "primaryEnvironment": "ENVIRONMENT-00000000000000000000000000000000"
  },
  "definition": {
    "workflowConfig": {
      "name": "Get Item Description",
      "constants": { "CONSTANT_CONO": "100" },
      "inputs": {},
      "connectionContext": { "M3": "CONNECTION-00000000000000000000000000000000" },
      "hasSpreadSheetInput": true,
      "hasExcelDesignFile": false,
      "hasTemplateFile": false,
      "workflowTags": {},
      "apiFieldReferences": {},
      "contexts": {},
      "clientConfig": {
        "title": "Get Item Description",
        "description": "",
        "buttonText": "Execute",
        "formGroups": [{ "groupId": 1, "groupName": "fields" }],
        "fields": []
      },
      "steps": [
        {
          "stepId": "trigger_1",
          "type": "TRIGGER",
          "name": "Trigger",
          "target": "workflow-trigger",
          "childSteps": [
            {
              "stepId": "m3_api_1",
              "type": "API",
              "name": "M3 API",
              "retryAttempts": 0,
              "target": "workflow-api",
              "childSteps": [
                {
                  "stepId": "excel_1",
                  "type": "EXCEL",
                  "name": "Excel",
                  "retryAttempts": 0,
                  "target": "workflow-excel",
                  "childSteps": []
                }
              ]
            }
          ]
        }
      ]
    },
    "stepConfig": {
      "trigger_1": {
        "title": "Get Item Description",
        "description": "",
        "buttonText": "Execute",
        "triggerType": "CLIENT",
        "formGroups": [{ "groupId": 1, "groupName": "fields" }],
        "fields": [],
        "tags": {}
      },
      "m3_api_1": {
        "xAAAAAAAAA1": {
          "id": "xAAAAAAAAA1",
          "stepId": "m3_api_1",
          "programName": "MMS200MI",
          "transactionName": "Get",
          "name": "Get",
          "source": "M3",
          "connectionId": "CONNECTION-00000000000000000000000000000000",
          "executionOrder": 1,
          "extendedGrouping": [],
          "input": {
            "CONO": {
              "name": "CONO", "descr": "Company",
              "type": "NUMERIC", "length": "3", "mandatory": false,
              "fieldType": "I", "isMapped": true, "hasGrouping": false,
              "source": "CONSTANT", "value": "CONSTANT_CONO"
            },
            "ITNO": {
              "name": "ITNO", "descr": "Item number",
              "type": "STRING", "length": "15", "mandatory": true,
              "fieldType": "I", "isMapped": true, "hasGrouping": false,
              "source": "EXCEL", "sourceType": "COLUMN", "value": "F"
            }
          },
          "output": {
            "ITDS": {
              "name": "ITDS", "descr": "Item description",
              "type": "STRING", "length": "30", "mandatory": false,
              "fieldType": "O", "isMapped": true, "hasGrouping": false,
              "source": "EXCEL", "sourceType": "COLUMN", "value": "H"
            }
          },
          "type": "ADD"
        }
      },
      "excel_1": {
        "sourceSheet": {
          "fields": {
            "sheetName": "Sheet1",
            "startColumn": "A", "endColumn": "H",
            "primaryHeaderRow": 2, "secondaryHeaderRow": 3, "dataRow": 4,
            "primaryHeaderStyle": {}, "secondaryHeaderStyle": {}, "dataRowStyle": {},
            "columns": {
              "F": {
                "fieldId": "xAAAAAAAAA1.input.ITNO",
                "type": "API", "stepId": "m3_api_1",
                "field": { "fieldId": "xAAAAAAAAA1.input.ITNO", "fieldName": "ITNO", "type": "API", "datatype": "STRING" },
                "primaryHeader": { "type": "API", "value": "Item number", "fieldId": "xAAAAAAAAA1.input.ITNO" },
                "secondaryHeader": { "type": "API", "value": "ITNO", "fieldId": "xAAAAAAAAA1.input.ITNO" }
              },
              "H": {
                "fieldId": "xAAAAAAAAA1.output.ITDS",
                "type": "API", "stepId": "m3_api_1",
                "field": { "fieldId": "xAAAAAAAAA1.output.ITDS", "fieldName": "ITDS", "type": "API", "datatype": "STRING" },
                "primaryHeader": { "type": "API", "value": "Item description", "fieldId": "xAAAAAAAAA1.output.ITDS" },
                "secondaryHeader": { "type": "API", "value": "ITDS", "fieldId": "xAAAAAAAAA1.output.ITDS" }
              }
            }
          },
          "cells": { "contents": {}, "styleInfo": {} },
          "sortOrder": []
        }
      }
    }
  }
}
```

Notes on this template:
- `xAAAAAAAAA1` is a placeholder — Live generates the real instance id and
  will discard whatever you send there. Never build downstream logic that
  depends on a specific instance id you invented.
- `CONO` demonstrates `CONSTANT` source; `ITNO` demonstrates `EXCEL` source
  paired with an `excel_1` input column; `ITDS` demonstrates an `EXCEL`
  output column. A `CLIENT`-sourced field would additionally need an entry
  in `clientConfig.fields` / `trigger_1.fields` with `formGroupId`,
  `elementPosition`, and `apiId` pointing at this transaction.
- If a `RowFilterEQ`/`RowFilterNE`-equivalent gate is needed here, insert a
  `generic_filter_1` step between `trigger_1` and `m3_api_1` — see the
  `genericFilterStepConfig` def in the schema file.

## 6. Worked template — generic pipeline path (Transform + REST API)

For when the native path can't express the control flow. See
`m3-generic-pipeline.md` for the full binding model, payload idiom, and
filtering patterns this expands on.

```json
{
  "stepId": "trigger_1", "type": "TRIGGER", "name": "Trigger",
  "target": "workflow-trigger",
  "childSteps": [
    {
      "stepId": "transform_1", "type": "TRANSFORMER_MORPH", "name": "Transform",
      "retryAttempts": 0, "target": "workflow-transform",
      "childSteps": [
        {
          "stepId": "rest_api_1", "type": "GENERIC_API", "name": "Rest API",
          "retryAttempts": 0, "target": "workflow-rest-api",
          "childSteps": []
        }
      ]
    }
  ]
}
```

```json
"stepConfig": {
  "trigger_1": { "...": "as above, triggerType CLIENT or SCHEDULE" },
  "transform_1": {
    "template": "( $data := $context.data.trigger.body; { \"payload\": { \"program\": \"OIS100MI\", \"transactions\": $data.{ \"transaction\": \"GetHead\", \"record\": { \"ORNO\": OrderNumber } }[] } } )",
    "trimAll": true
  },
  "rest_api_1": {
    "content": "{\n  \"version\": 2,\n  \"connectionId\": \"CONNECTION-00000000000000000000000000000000\",\n  \"endpoint\": \"https://mingle-ionapi.<region>.inforcloudsuite.com/<TENANT>/M3/m3api-rest/v2/execute\",\n  \"method\": \"post\",\n  \"headers\": { \"content-type\": \"application/json\" },\n  \"bodyPath\": \"$.payload\"\n}"
  }
}
```

Note the asymmetry that's easiest to get wrong: `transform_1`'s stepConfig
is **flat** keys (`template`, `trimAll`); `rest_api_1`'s stepConfig is a
single `content` **string** holding the same-looking object as JSON text.
Do not generalise either shape onto the other step type.

The `<region>` and `<TENANT>` segments of the ION API endpoint are
tenant-specific and cannot be derived — they must be copied from a real REST
API step already in the target tenant, or asked for explicitly if drafting
without one.

## 7. Checklists

### Before treating a draft as ready to hand over

- [ ] The token's tenant matches the tenant being targeted (see
      `auth-endpoints-and-unconfirmed.md`).
- [ ] `workflowType` is `"STANDARD"`, not `"EXPRESS"`.
- [ ] Every step `type`/`name`/`target` triple is from the closed list in
      §2 — nothing invented by analogy.
- [ ] `retryAttempts: 0` on every step except `trigger_1`, which omits it.
- [ ] Exactly one root `TRIGGER`; the chain is linear via `childSteps`.
- [ ] Every M3 transaction has `type: "ADD"` and a gapless `executionOrder`
      starting at 1.
- [ ] Every API-chain `ref` target is declared in its producer's `output`.
- [ ] Repeated CLIENT fields carry a `ref` to the **first** declaring
      transaction (not the producer — see the `ref` note in the schema).
- [ ] Each REST API step's config is inside a `content` string; each
      Transform step's config is flat.
- [ ] No Transform references a hardcoded transaction instance id.
- [ ] Every Transform uses `$context...`, not the preview pane's
      `$.<stepId>` shorthand.
- [ ] `excel_1` appears only after an M3 API step, never after a
      Transform/REST API/Code chain.
- [ ] Every field/value not backed by something in this skill's reference
      files is explicitly flagged as unconfirmed, with a verification path.

### After a real creation (not something this skill can do, but tell the
user to do it)

- [ ] Read the real id back from the response — the one sent was ignored.
- [ ] GET the workflow back and confirm it renders in the builder.
- [ ] Execute it and confirm it does what's expected.
- [ ] Wait past the Excel add-in's refresh/propagation delay before
      re-testing a just-created or just-edited workflow (a stale-version
      false negative is a real, previously-observed failure mode).
