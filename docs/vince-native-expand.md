# Native Expand in Vince Live (confirmed 2026-10-16)

Classic's `<Expand>` reads each row of a SOURCE sheet, lists M3 records for it,
and writes them to a TARGET sheet. Live does this natively, in the M3 API step.
**It is not in the workflow builder UI, only in the JSON / API** (Haakon), so we
can build it, but a customer cannot adjust it themselves.

Everything below comes from real workflows: an earlier converter's CustomerB JSON (read-only, kept in
`RUN/runs/CustomerB/product/`) and three test workflows created and run in the
test tenant (demo M3, `RUN/runs/expand-test/`).

## How Live tells Expand from an ordinary export

Read the compiled `definitionAsl`, which every `GET /workflows/{id}` returns:
the `m3_api_1` and `excel_1` payloads merge
`{"isImportOperation":…, "isExpandOperation":…}` into the workflow config. That
flag, not the JSON you sent, says how Live will run it.

| Shape sent | Compiled as |
|---|---|
| transactions `isExpand: true`, first call's inputs `source: "EXCEL"` (sheet columns), excel_1 `sourceSheet` + `targetSheet` | `isExpandOperation: true`. Sheet inputs compile to `__contextBody.<id>_<FIELD>`, chained ones to `__contextData.<id>.<FIELD>` |
| transactions `isExpand: true` but inputs from the trigger form (`CLIENT`) | `isExpandOperation: false`: an ordinary export, so the flag is inert (CustomerB "Planning - Projected on-hand Balance … expand_1") |
| no `isExpand`, form inputs, a list chained from a list | ordinary export. The second list still fans out (T3) |

## JSON shape (as an earlier converter and our tests wrote it)

- Each transaction: `"isExpand": true`, `"type": "ADD"`, `"extendedGrouping": []`,
  `"hasSpreadSheetInput": false`.
- The first call's inputs: `"source": "EXCEL", "value": "<column letter>"`.
- A chained input: `"source": "API", "value": "<id>.output.<F>",
  "ref": {"type": "OUTPUT", "apiId": "<id>", "name": "<F>"}`.
- A constant: `"source": "RAW", "value": "<text>"`. **`RAW` with value `null`
  compiles to the STRING `'null'`**, so send a real value instead.
- `workflowConfig.hasSpreadSheetInput: true`.
- `excel_1.sourceSheet.fields`: `sheetName`, header rows, `dataRow`, and
  `columns` mapping letters to `<id>.input.<F>`.
- `excel_1.targetSheet.fields`: the same, with `columns` mapping to
  `<id>.output.<F>` plus a `MSG_COL` column (`field.datatype: "MESSAGE"`,
  `type: "MSG"`).

## What the runs showed

| Run | Shape | Result |
|---|---|---|
| T1 `WFEXEC-76352068…` | header sheet → `LstBasePrice` → 3 lookups → Lines | works. The add-in CREATED the target sheet when it didn't exist |
| T2 `WFEXEC-7834658c…` | 2 header rows → `LstBasePrice` → `LstGradSlsPrc` → `GetItmBasic` → Scales | works: two list levels in one Expand, 2 in → 4 out, each row with its own currency, in sheet order |
| T3 `WFEXEC-84edc8d3…` | form → `LstBasePrice` → `LstGradSlsPrc` → lookup (ordinary export) | works: 2 rows |
| T2 on `M01` `WFEXEC-e6d4f509…` | 1 header row, 257 lines | 272 rows in **1.3 s**. `&101` fanned into its 3 scale rows (qty blank/10/100 → 12.10/11.50/10.25). An item listed twice gave 2 rows, not 4. 258 rows failed on DEMO_TENANT data (see below) |

Behaviour worth knowing:
- **Every source row produces output rows, even when a call fails.** A failed
  level-2 call still gives one row: its data columns blank, the message column filled.
  Map identifying fields (price list, item) from the FIRST list, not the second,
  so a failed row still says which line it was.
- **A failed call does not stop the calls chained after it.** They run with blank
  inputs and add their own error ("Price list must be entered").
- **The step status reads FAILED even when every call answered OK.** It is not
  a usable signal; read the message columns.
- **Dates land as Excel serial numbers** (44805 = 2022-09-01), unformatted on a
  sheet without a design file.
- **Header rows:** Product put the field name in BOTH the primary (row 3) and
  the secondary (row 5) header, so it shows twice. Use a label + field name.
- **Native M3 steps write message columns.** The earlier belief that they
  don't was wrong.

DEMO_TENANT data, so not a workflow fault: of `M01`'s 257 lines, 245 failed. The
items look absent from the item master (spot check: `A005` gives `WIT0103` via the MCP server). `LstGradSlsPrc` then
answers "Item number … does not exist" / "not in price list", and the item lookup
fails after it. Only 14 rows came back complete (12 items). Haakon counted 11
lines on the sheet; that difference is not explained yet.

## Scales data facts (OIS017MI)

- A graduated price is an extra `OPRICL` row with `OMQTYL > 0`.
- `LstGradSlsPrc` always returns the base row (quantity 0) first, so a line never has zero
  scale rows.
- In DEMO_TENANT only `M01`/SEK/20210101/`&101` has real steps.
- `LstPriceList` given a price list lists FROM it onward, in text order. Native
  has no filter on M3 output, so **a From/To range must come from the sheet**
  (expand from the header sheet), not from a form.

## Open

1. A design decision: native Expand (API-only, not editable by the customer)
   vs the `m3Expand` Code step (also not customer-friendly).
2. The converter does not build native Expand yet. Classic `<Expand>` currently
   goes to `expand_code.py`. Building it means `isExpand` transactions plus `sourceSheet`/
   `targetSheet` (see the shape above).
3. Native imports for CustomerB P3/P4 (per-call message columns), and whether an
   Excel date cell reaches M3 as YYYYMMDD.
4. The web UI's delete refuses workflows created with an API Client token:
   that token has no tenant claim.
