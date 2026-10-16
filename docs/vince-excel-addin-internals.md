# How the Vince Live Excel add-in reads and writes the sheet

**Source:** the add-in's own JavaScript, read on 2026-10-13. The main bundle was
supplied by Haakon (`RUN/runs/addin java.js`). The lazy-loaded chunks that hold
the Excel logic were fetched from `https://vxl.vince.live/assets/`:
`HomeView-6afb0a60.js` (all sheet I/O), `workflow-102964e6.js` (API calls) and
`LookupView-cb1ece64.js`. The hashes identify the version; a new add-in release
can change any of this.

This is read from code, not observed in a run. It explains every sheet symptom
seen so far (see "Matches what we saw" at the end), but where a point below has
not also been seen in a real run, it is a strong lead, not a confirmed fact.
Names like `Kn`, `E0` and `Zm` are minified and will change between builds; the
method names (`readExcelDataAsWorkflowInput`, `writeContentsToTable`, ...) are
stable.

## 1. Two completely different paths

Which path a run takes depends on the workflow, decided when it starts:

- **"M3 data" path.** The workflow has an Excel step (its `stepConfig` entry,
  normally `excel_1`, is the add-in's `excelConfig`) AND a step of type `API` or
  `EXCEL`. Reader: `readExcelDataAsWorkflowInput`. Writer: the column-mapped
  writers (§3).
- **Raw path.** No Excel step config, e.g. a Code-step workflow. Reader: a plain
  used-range dump (`Zm`). Writer: `writeRawDataToExcelTable` (§4).

Input is only read at all when `workflowConfig.hasSpreadSheetInput` is true.
Otherwise the trigger body is empty.

## 2. Reading input, M3 data path

- **Range:** the whole sheet from `A1` to the end of the used range, so the
  column letters are the REAL worksheet letters, with no trimming. Rows start at
  `sourceSheet.fields.dataRow` (default 1).
- **Only VISIBLE rows are sent.** Rows hidden by a filter or by hand are skipped.
  On write-back, results are placed on the visible rows they came from, so a user
  can pick which rows run by filtering the sheet. A trigger whose filter values
  are all `IGNORE_FILTER` changes this mapping (`isFilterIgnored`).
- **With an Excel table** on the sheet (only the FIRST table, `tables.items[0]`,
  is ever used), rows outside the table are not read, and trailing empty rows are
  dropped. **Without a table**, reading stops at the first fully empty row. The
  scan for visible rows also stops after 10 consecutive empty rows.
- **Validation:** any configured input column outside the table's column range
  fails validation before the run.
- **Each row** is an object keyed by column letter (`{"A": ..., "C": ...}`).
  Empty cells are LEFT OUT of the object, unless that field has `includeBlank:
  true`, in which case they are sent as `""`.
- **Values are the calculated values** (Office.js `range.values`), so a formula
  cell sends its result, never the formula.
- **Conversions:**
  - DATE columns are converted from Excel serial numbers to M3 date format.
  - DECIMAL/NUMERIC columns are ROUNDED to the number of decimals in the cell's
    number format (`getFormattedNumber`). What the user sees is what is sent; a
    cell formatted with 0 decimals sends an integer.
- **Fixed input cells** (`sourceSheet.cells.contents`, keyed like `B_2`, type
  `API`, an `.input.` fieldId) are read once and MERGED INTO EVERY ROW under the
  key `B2`. This is the native equivalent of Classic's Field1–5 parameters.

## 3. Writing output, M3 data path

`writeWorkflowDataToExcel`:

- **Target sheet.** If the Excel step has `targetSheet.fields.sheetName`, output
  goes to THAT sheet, which is created if it does not exist and activated.
  Otherwise output goes to the active sheet. `targetSheet` also carries its own
  `columns`, `cells`, `dataRow`, header rows and `sortOrder`. **We have never
  captured a `targetSheet` in a real workflow JSON** - build one in the UI and GET
  it before relying on the shape.
- **Column mapping.** Each configured column whose `fieldId` contains `.output.`
  becomes the response key `<txnId>_<FIELD>` (the `.output.` replaced by `_`).
  Columns whose `fieldId` is an input are not written. `WF_MSG_COL` and
  `ENV_MSG_COL` columns take the message fields.
  - DATE outputs are converted to Excel dates, with 0 becoming blank.
  - LONG outputs are written with a leading `'` so Excel keeps them as text.
- **Sorting.** `sortOrder` entries (by `preference`) sort the response rows
  before writing.
- **Output cells** (`cells.contents` with an `.output.` fieldId) get their value
  from the FIRST response row only.
- **Import with fewer result rows than input rows** (a filter dropped some). Each
  result row is matched back to an input row by comparing its `<txn>_I_<FIELD>`
  input echoes with the input row's values, first unused match wins. Rows with
  identical inputs are matched in order.

Then one of two writers:

- **Sheet has a table** (`writeContentsToTable`).
  - **Import workflow with no `targetSheet`:** each output column is written IN
    PLACE, row by row from the first body row. Other columns are untouched. The
    column letter is used as an INDEX into the table's columns, so this lines up
    with the sheet only when the table starts in column A. A column beyond the
    table's width is silently skipped.
  - **Any other case (a list, or a `targetSheet`):** all result rows are added,
    then **the table's previous body rows are deleted**. A listing REPLACES the
    whole table body. Nothing else can share that table.
- **No table** (`writeContentsToSheet`). Column by column, from row 1:
  - the primary header (and secondary header, if configured) goes in its row
  - values go from `dataRow` down
  - rows above `dataRow` that are not header rows are left as they are
  - **Nothing below the new data is cleared**, so a shorter list than last time
    leaves the old list's tail underneath it
  - columns that are not output columns are untouched

## 4. Raw path (Code-step and other non-Excel-step workflows)

- **Input** (`Zm`): `getUsedRange()` of the active sheet, keyed by column letter
  counted from the FIRST USED column, rows starting at the FIRST USED row. Empty
  leading columns AND empty leading rows are trimmed, and the letters shift
  accordingly.
  - There is no `dataRow`, no table awareness and no visible-row filtering:
    hidden rows and rows above or outside a table are all sent.
  - Empty cells ARE included (as `""`).
- **Output** (`writeRawDataToExcelTable`): the keys of all result rows together
  become the columns.
  - **With a table:** columns are matched by HEADER NAME. Table columns whose
    name is not a result key get blank values. Result keys with no matching
    column are appended as NEW table columns. Then the rows are added and the old
    body is deleted, so the table body is REPLACED.
  - **Without a table:** a new table is created at `A1` with the keys as headers.
- So a Code step must return EVERY row it should keep, with every column it
  should keep, keyed by the table's header names.

## 5. Trigger, completion and results (API)

- **Trigger:** `POST /live/workflows/<workflowId>/async` (api.vince.live), with
  the body `{body: <rows or null>, headers: {...}}`. The two request headers:
  - `vl-wf-control`: JSON `{executionParams: {..., skipSpreadSheetGeneration,
    inputSnapshotFileKey}, triggerFile?}`
  - `vl-wf-header`: the run's form values, base64-encoded if any character is
    non-ASCII

  A Group run passes `entity: "Group"`.
- **Large input:** when the JSON of the input rows is longer than **180,000
  characters**, the rows are uploaded as a file and `triggerFile` is sent instead
  of the body. The upload is `GET /v1/files/workflow/upload-url/<workflowId>/triggers/<ms>_<slug>.json`,
  then a PUT to the returned URL with a `content-type` header.
- **Workbook snapshot:** every run of an import workflow (one with spreadsheet
  input) uploads a snapshot of the WHOLE workbook first, the same way but as
  `.xlsx`. Its key is `inputSnapshotFileKey`.
- **Completion:** pushed over a GraphQL subscription
  (`graphql.vince.live/graphql`, event `...finished`), not polled.
- **Results:** `GET /v1/workflows/<workflowId>/results/<executionId>/data` with the
  bearer token. **If the response carries a `vl-redirect` header, the data is
  fetched from that URL with a plain GET (no auth header).** This is how large
  outputs (`result.dataFileKey`) are read. It is a different route from the
  `/v1/files/tenant/download-url/...` presign we tried, which gave S3
  `AccessDenied`.

## 6. Matches what we saw

- The CustomerA Code-step test (`MMS002_OpenWhs_1.5 - Open`) is fully explained
  by §4:
  - the input was the used range from row 1, keyed by letter
  - output was matched by header name, and our letter keys created new columns
    `A..DR`
  - without a table, `Table3` was created at `A1`
- The "leading empty columns are trimmed" observation from 2026-09-11 is the raw
  path (§4). The M3 data path reads from `A1` and does not trim.
- The 210-row run stored input as `triggerFile` because of the 180,000-character
  limit in §5.

## 7. What this changes for us

- **Empty leading rows are trimmed on the raw path (§4), so a sheet whose row 1
  is empty shifts every row.** DONE 2026-10-13: the generated Code step throws
  before any M3 call when its expected header row has content but no header over
  a column the step writes, and the conversion warning tells the user to keep
  row 1 non-empty.
- **`runs.py` can read large outputs.** DONE 2026-10-13:
  `liveapi.get_execution_data` follows `vl-redirect` with a plain GET. Not yet
  run against Live.
- **Leads to confirm with one UI-built workflow each:**
  - `targetSheet` (read one sheet, write to another)
  - fixed input cells merged into every row
  - row selection by filtering
