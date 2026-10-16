# The native M3 API step — deep reference

Everything confirmed about `m3_api_N` transactions, field metadata, and the
step's runtime behaviour. Prefer this path whenever it fits: it is the only
one with field-metadata validation, automatic pagination, `excel_1`
binding, and strict save-time validation. Leave it only when it genuinely
can't express the logic — see `m3-generic-pipeline.md` for when and how.

## 1. Transaction shape recap

`stepConfig["m3_api_1"]` (or whichever stepId) is keyed by a **Live-assigned
transaction instance id** — format `x` + 10 mixed-case alphanumeric
characters, e.g. `xN9MwyGBpt`. You do not generate this for real; a
placeholder you invent is replaced on save. Field-level shape is in
`live-workflow.schema.json`'s `transaction`/`inputField`/`outputField`
defs — this file covers the parts that need more explanation than a schema
comment can hold.

## 2. `type: "ADD"` — the one gotcha most likely to break silently

Every reliable multi-transaction example uses `"type": "ADD"` **regardless
of what the M3 transaction actually does** — list, get, update, or add.
Its absence is the strongest known cause of an M3 API step rendering
**completely inert** (no transactions visible at all) in the builder UI.
A manually-built single-transaction test workflow showed `LIST`/`GET` in a
different context, so `type` may carry more meaning than currently
understood — but `ADD` is the confirmed-safe default. **Never infer
LIST/GET/ADD from the transaction name** when drafting.

## 3. `source` and what `value` means per source

| `source` | What `value` holds |
|---|---|
| `CONSTANT` | the **key** into `workflowConfig.constants` (e.g. `"CONSTANT_ALWT"`) — never the literal |
| `EXCEL` | the spreadsheet column letter; also set `"sourceType": "COLUMN"` |
| `CLIENT` | the **real M3 field name**, never a placeholder — confirmed via a real execution error when a placeholder was sent as-is |
| `API` | `"<producerTxnId>.output.<fieldName>"`, plus `"ref": {"apiId": "<producerTxnId>", "name": "<fieldName>", "type": "OUTPUT"}` |

**Repeated CLIENT fields** (the same field name declared on a 2nd or later
transaction) need
`"ref": {"apiId": "<id of the FIRST transaction to declare it>", "name": "<fieldName>", "type": "INPUT"}`.
Without it, Live creates separate fields (`CONO`, `CONO_1`, `CONO_2`...)
instead of recognising one shared trigger value.

**Memorise the asymmetry:** for an API chain, `ref.apiId` points at the
**producer**. For a repeated CLIENT field, `ref.apiId` points at the
**first** transaction to declare it. These are opposite directions and easy
to swap by accident.

**A field referenced via an API-chain `ref` must be declared in its
producer's own `output`**, even with no real column mapping — synthesize a
minimal declaration on an unused column letter if the source never mapped
it to a visible column. Skipping this produces a runtime SQL error, not a
save-time one.

## 4. Chaining transactions

- `basedOnTransaction`: the producing transaction's own **instance id**
  (never its name) — this transaction only runs after that one completes.
- `runOnFailure`: whether to run even if the based-on transaction failed.
- **Get-or-create idiom, confirmed in real use**: an `AddX` transaction
  `basedOnTransaction` a `GetX` transaction, with `runOnFailure: true` — "add
  only if the get failed."
- List-style transactions additionally carry `"maxRows": <int>`,
  `"defaultInputConfig": {}`, `"hasSpreadSheetInput": false`.
  `maxRows: 0` means **unlimited**, not zero — confirmed by a real run
  returning 24,154 records with it set.
- The same `(programName, transactionName)` pair really can occur twice in
  one step (confirmed in a real customer file, writing different output
  columns each time). When a later reference is ambiguous, the sensible
  resolution is "nearest preceding occurrence wins" — not independently a
  platform rule, but the behaviour this project's own converter settled on
  after hitting the case for real.

## 5. The M3 field metadata endpoint

The only confirmed way to get real `descr`/`type`/`length`/`mandatory` per
field — this is what powers "pick a transaction, watch its fields populate"
in the builder.

```
GET https://api.vince.live/metadata/{connectionId}?details=FIELDS%23{program}%23{transaction}
```

The `details` value is `#`-separated and **must be URL-encoded** (`%23`).

| `details` | Result |
|---|---|
| `FIELDS#<prog>#<txn>` | **Use this.** One item per direction that exists — `count: 2` for `MRS001MI/LstAllTrans` (I + O), `count: 1` for `BUS100MI/DelBudgetLines` (I only) |
| `FIELDS#<prog>#<txn>#<I\|O>` | One direction. Works, but doubles the calls for no benefit |
| `FIELDS#<prog>` | Every transaction in the program at once (`count: 24` for `BUS100MI`) — **the whole-program shape cannot be attributed to transactions** (its items carry only `name`/`descr`/`data`, `name` being `"I"`/`"O"` with nothing naming the transaction) — unusable for per-field keying despite fewer calls |
| `TRANSACTIONS#<prog>` / `PROGRAMS` | `count: 0` — not supported |
| *(omitted)* | HTTP 400 `Missing required request parameters: [details]` |

```json
{"count": 2, "items": [
  {"name": "I", "descr": "Input",  "data": [
    {"name": "CONO", "type": "N", "length": "3", "mandatory": "0", "descr": "Company"}]},
  {"name": "O", "descr": "Output", "data": [ /* ... */ ]}]}
```

M3 type codes: `A` → `STRING` (confirmed), `N` → `NUMERIC` (confirmed), `D`
→ `NUMERIC` (**inferred**, not independently confirmed against a real Live
`D` field — M3 typically stores dates as numeric `YYYYMMDD`). `mandatory` is
the **string** `"1"`/`"0"`, not a boolean.

**Two traps:**
1. **An empty direction is normal, not a failure.** `BUS100MI/DelBudgetLines`
   returns 14 input fields and `count: 0` for output — a delete returns
   nothing. Only treat a transaction as undescribed when it has no fields in
   **any** direction.
2. Position in the whole-program response *might* correlate with
   transaction order — untested, don't rely on it.

**When this endpoint describes no fields at all**, suspect a token from a
different tenant than the connection first (see
`auth-endpoints-and-unconfirmed.md`), and a wrong transaction name second.

## 6. Runtime field naming (relevant if a later Transform reads M3 output)

For a transaction with instance id `<id>`:

| Runtime key | Meaning |
|---|---|
| `<id>_X` | declared **output** field `X` |
| `<id>_I_X` | **input echo** of `X`, present when `X` is also an input on that transaction |
| `<id>_MSG_COL` | auto-injected per-row status/error message (`"OK"`, or a real error string) |
| `<id>_<id>_<program>_<transaction>_ROWNUM` | auto-injected; observed always `null`; purpose not investigated |

Real example, instance `xu3EO2azWPT`, field `TRNM` used as both input and
output:

```json
"xu3EO2azWPT_I_TRNM": "*",
"xu3EO2azWPT_TRNM": "LstClaimDet"
```

**Padding/incomplete rows.** Large results have been observed to include
rows where every declared output field is `null` — likely padding or a
pagination artifact. Filter these out using the **mandatory input-echo
field** (`_I_<name>`), never an output field — output fields are exactly
what's `null` on the bad rows.

## 7. Instance ids are NOT stable

`xN9MwyGBpt` and friends are **regenerated by Live**, not preserved from
what you send. Confirmed to change on every `POST /workflows`, and
apparently across ordinary re-saves of an existing workflow too.

**Implication for anything downstream that reads M3 API output** (a
Transform template, mainly): hardcoding an instance-id prefix
(`xu3EO2azWPT_TRNM`) silently breaks the next time the workflow is saved.
Prefer dynamic suffix-matching (see `m3-generic-pipeline.md` §3) so no
instance id ever appears in a template, or plan to re-edit the template
after every save.

## 8. Pagination

The M3 API step appears to paginate automatically at the workflow-engine
level (inferred from the compiled state machine's `CheckForPendingApi`
choice state looping back into the step while `hasPendingSteps`/
`partialCursor` indicate more data). Manual cursor handling is usually
unnecessary on this path. **This is inferred from internal implementation
details that could change without notice** — flag it as such if citing it,
and note that no equivalent exists at all on the generic pipeline path
(`maxReturnedRecords` and manual paging are all there is there).

## 9. Filter step limits — why some designs must leave this path

`generic_filter_1` (`GENERIC_FILTER`) sits between the trigger and the M3
API step, runs **once**, and only sees **raw spreadsheet rows**. Two
independent consequences, both structural (no configuration fixes them):

1. **It cannot gate one M3 call while another runs unconditionally** — it
   applies to the whole M3 API step, not a single transaction.
2. **It can never see a value an M3 call produced.** A column that only
   gets a value when a transaction writes it is still empty at the one
   moment the filter evaluates. This holds for `equal` as much as any other
   operator — it is not an operator limitation.

Its confirmed `operator` set is only `equal | notEqual`
(`GENERIC_FILTER`'s own confirmed limit — see the `filterCondition` schema
def). Any design needing `>`/`<`/`>=`/`<=`, a filter reading an earlier
call's own output, or true per-call gating needs the generic pipeline path
— see `m3-generic-pipeline.md`.

## 10. The program AND transaction must be real (the "Inputs (0)" failure)

Live renders a transaction's fields from the connection's live M3 metadata. A
`programName`/`transactionName` M3 doesn't know imports without any error and
then shows **"Inputs (0)"** with a warning triangle on that card: valid JSON,
useless workflow. This happened for real: a customer spec that named only
tables and said "API / transaction: to be confirmed" was turned into invented
names (`TBC_GetItemPOP`, `MMS015MI.GetAlternateUoM`).

Rules:

- **Never invent a transaction, and never write a placeholder** (`TBC_*`,
  "to be confirmed", a plausible-sounding verb). If no real transaction is
  known, stop and ask; do not emit the step.
- **Verify before emitting**: the transaction exists, every `input` name is an
  input of THAT transaction, every `output` is one of its outputs, every
  mandatory input is mapped (§5 for the endpoint). Examples of what a guess
  gets wrong: `MMS015MI.Get` and `OIS017MI.LstBasePrice` have no `CONO`;
  `LstBasePrice` has no `QTYL`; `MMS025MI.GetAlias` returns `ALUN` as an
  OUTPUT, not an input.
- The field catalog (`m3-field-catalog.sqlite`) is another tenant's data: a miss
  is "unverified", not "doesn't exist" - say so rather than guessing.
- A list call (`Lst*`) should be `type: "LIST"`, not the `ADD` fallback.
- Tenant-specific VALUES (alias type/qualifier for EANs, a charge id) are data,
  not metadata: ask for them, don't invent them.

Verified table -> transaction map (catalog, 2026-10-08):

| Data | Transaction | Key inputs | Notes |
|---|---|---|---|
| Item master MITMAS | `MMS200MI.GetItmBasic` | ITNO | ITDS, ILEN/IWID/IHEI, VOL3, NEWE/GRWE, UNMS, ITCL, EVGR. Run-confirmed: ILEN/IWID/IHEI are **decimetres** (x100 = mm), numbers return as strings, UNMS = basic U/M (CRT on the tested items) |
| Item warehouse MITBAL | `MMS200MI.GetItmWhsBasic` | ITNO, WHLO | ISEG |
| Alternate U/M MITAUN | `MMS015MI.Get` | ITNO, AUTP, ALUN | COFA; no CONO |
| Alias / EAN MITPOP | `MMS025MI.GetAlias`, `LstAlias` | ITNO, ALWT (+ALWQ) | alias type/qualifier = tenant data |
| Base price lines OPRICL | `OIS017MI.LstBasePrice` | PRRF, CUCD, FVDT (+CUNO) | list; SAPR, SACD (price quantity), SPUN, LVDT; no scale quantity. Run-confirmed: price 0.1675 per PCS, SACD 1000 |
| Scale prices | `OIS017MI.LstGradSlsPrc` | PRRF, CUCD, FVDT, ITNO all mandatory (+CUNO) | one row per quantity step: QTYL, PCOF (factor vs the base row), SAPR. Run-confirmed: SAPR is per 1000 pcs on the tested list (167.5); a step at QTYL 20000 had PCOF 0.97999 -> 239.51 from 244.4 |
| Price list headers OPRICH | `OIS017MI.LstPriceList` | PRRF, CUCD, CUNO, FVDT (all optional) | PRRF etc. are a START key, not a filter: M3 lists FROM that key on, in TEXT order (001, 01, 056A, 1-NLDISPLY, 1001, 73 ...). Bound a From/To range yourself, comparing as text - a numeric compare makes 001..001 also match "01". A list in an object access group the user lacks gives `OI01703` on `LstBasePrice`. Checked on DEMO_TENANT 2026-10-16 |
| Discount lines / scale lines | `OIS800MI.LstDiscLine`, `LstScaleLine` | DISY, DIPO, FVDT, PREX, OBV1 all mandatory in the catalog (+OBV2-5) | LstDiscLine gives OBV1-5, CMPN, DITP; LstScaleLine gives LIMT, DISP, DIAM, ITNO, ORQT, SAPR. A Classic function called LstDiscLine WITHOUT OBV1 - whether M3 really refuses that is unconfirmed. PREX must be a priority that exists in the model (`WPRE903`) |
| General code CSYTAB | `CRS175MI.GetGeneralCode` | STCO, STKY | TX40, TX15 |
| Customer item OCUSIT | `MDBREADMI.LstOCUSIT00` | CUNO (+ITNO) | POPN; needs MDBREADMI in the tenant |
| Line charge rates OLICHR | `CRS275MI.GetLineChrgRate` | CRID, VFDT (+OBV1-3) | CRAM, CHPD, CRFA; CRID = tenant data (tested tenant: for `OBV1` NO1 the charge id equals the item group `OBV2`) |
| All line charge rates | `CRS275MI.LstLineChrgRate` | CONO, CRID (both optional) | OBV1-3, VFDT, LVDT, CRAM, CHPD, CRFA, CUCD, FRLC, ACR9 and **CRID - always request it**. CRID blank = every charge, thousands of rows: run it ONCE, never inside a per-item run. Charge unit: `LstLineChrgHead` (CHUN: PCS = CRAM per CHPD pieces, GR = factor in CRFA) |
| Calculated price (check only) | `OIS320MI.GetPriceLine` | CUNO, ITNO, ORDT, ORQA, CUCD, PRRF | the price engine: SAPR (rounded to 2 decimals), discounts DIP/DIA, NETP; check a price, don't read list prices with it |
| Customer charge model / statistic id 2 | `CRS610MI.GetOrderInfo` | CUNO | CHSY, FRE2 (NOT in `GetBasicData`; that has CFC1-9 free fields) |
| Customer CRS610 basics | `CRS610MI.GetBasicData` | CUNO | name, address, CFC0-9 |

**Also set each field's `type`/`length` (and the Excel column's `field.datatype`)
from metadata** (catalog Type `A`->STRING, `N`/`D`->NUMERIC). Leaving `"UNKNOWN"`
makes the Excel design's data row literally read UNKNOWN in every column.
