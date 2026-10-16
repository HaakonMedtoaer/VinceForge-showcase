# M3 Field Catalog Builder — handoff

**Purpose of this document.** You are starting a new, separate, small project.
Its only job: call Infor M3 (via ION API) to enumerate every Program,
Transaction and Field M3 exposes, and land the result in a single SQLite
file. That file is a shared artifact — both VinceForge (this converter
project) and VinceGenerator (the separate prompt-to-app project, see
`vince-app-generator-handoff.md`) will read it, neither will build it. This
is **not** meant to be a recurring pipeline — a one-shot (or very
occasionally re-run) extraction, not a service.

Written to be read by a session with no access to VinceForge's history.

---

## 0. The rule that outranks every other decision here

**Never guess at M3's or ION API's behaviour — verify every call for real
before writing the loop that runs it thousands of times.** This project is
almost pure API-calling code; the entire risk is committing to an
assumption about pagination, batching, wildcards, or auth that turns out
wrong only after it's already run for an hour against a real tenant.
VinceForge's own history is full of exactly this failure mode (see §2) —
don't repeat it here. If something below is marked UNCONFIRMED, that is
an instruction to go test it with one real call before building on it,
not a gap to fill with a reasonable-sounding guess.

---

## 1. What's already confirmed, from real calls made this session

**`MRS001MI.LstAllTrans`** lists Programs and Transactions. Called with
`MINM` (Program name) wildcarded — the working query used a bare `*`,
matching how the existing `m3-api-catalog` Custom Table (see below) was
built. Confirmed to return **22,535 unique `(Program, Transaction)` pairs**
after dedup (24,154 raw rows before removing genuine duplicate keys), with
columns `Program`, `Transaction`, `Description`, `Type`, `Status`.

**`MRS001MI.LstFields`** lists Fields for one Program+Transaction+direction.
Confirmed real response for `MINM=CRS610MI`, `TRNM=LstByName`, `TRTP=O`:

```json
{
  "results": [
    { "transaction": "LstFields", "records": [
      { "MINM": "CRS610MI", "TRNM": "LstByName", "TRTP": "O",
        "FLNM": "CONO", "FLDS": "Company", "TXT1": "0",
        "FRPO": "16", "TOPO": "18", "LENG": "3", "TYPE": "N", "MAND": "0",
        "RGDT": "", "RGTM": "", "LMDT": "", "CHNO": "", "CHID": "" },
      { "...": "89 more rows, same MINM/TRNM/TRTP, one per field" }
    ] }
  ],
  "wasTerminated": false,
  "nrOfSuccessfullTransactions": 1,
  "nrOfFailedTransactions": 0
}
```

Field meanings (inferred from values, standard M3 field-dictionary naming —
not independently cross-checked against Infor's own docs this session):

| Field  | Meaning                                              |
|--------|-------------------------------------------------------|
| `MINM` | Program name (input to the call, echoed back)         |
| `TRNM` | Transaction name (input, echoed back)                 |
| `TRTP` | Direction: `I` (input) or `O` (output) — observed `O` only so far |
| `FLNM` | Field code (M3's short field name, e.g. `CONO`)       |
| `FLDS` | Field description/label (e.g. `"Company"`)            |
| `TXT1` | Unknown — observed `"0"` on every row, not investigated |
| `FRPO` / `TOPO` | From/to position in the fixed-width record layout |
| `LENG` | Field length                                          |
| `TYPE` | `A` (alphanumeric) or `N` (numeric) observed — `D` (date) known to exist elsewhere in M3 but not seen in this capture |
| `MAND` | Mandatory flag — observed `"0"` on every row in this sample, **but this sample was all output fields of a List call, where nothing is ever mandatory; a `"1"` has not actually been observed yet** — test against an input field before assuming the encoding |
| `RGDT`/`RGTM`/`LMDT`/`CHNO`/`CHID` | Registration/change-tracking metadata on the field *definition* itself — observed blank on every row; unclear if these are ever populated for standard delivered MI programs |

The response envelope (`results[].records[]`, `nrOfSuccessfullTransactions`,
`nrOfFailedTransactions`, `wasTerminated`) matches VinceForge's own
confirmed shape for ION API's bulk `m3api-rest/v2/execute` endpoint (see
`docs/vince-generic-m3-pipeline.md` in the VinceForge repo) — same request
shape (`{"program", "cono", "maxReturnedRecords", "transactions": [{"transaction", "record", "selectedColumns"}]}`),
same response shape. **How the specific test call above was actually made
(Postman against ION API directly with what credential, or captured from a
Vince Live workflow's REST API step test-run) was not recorded — pin this
down as your first real task (§3), because it decides your entire auth
story.**

**Confirmed limitation: `LstFields` does not accept a wildcard on
`TRNM`.** Passing `*` for the transaction name returns zero results —
tested directly. `MINM` and `TRNM` must both be concrete, real values.
**Not yet tested: whether `TRTP` accepts a wildcard** (to get input and
output fields in one call instead of two) — test this before assuming
either answer.

---

## 2. Why this needs care: two already-confirmed footguns in adjacent work

These aren't theoretical — they cost real time in VinceForge building the
existing `m3-api-catalog` table, and the field-level extraction is the same
shape of problem at 10-20x the row count.

1. **Primary key choice silently drops data with no error.** The existing
   `m3-api-catalog` table was first created with `primaryKeys: ["Program"]`
   alone. Since one Program has many Transactions, every write for a repeat
   Program silently overwrote the previous one (UPSERT semantics) — 24,154
   source rows settled at ~1,330. The fix was `["Program", "Transaction"]`.
   **For field-level data, your natural key is `(Program, Transaction,
   Direction, FieldName)` — get this wrong and rows vanish with no error at
   all**, just a suspiciously low final count. If you go anywhere near a
   Custom Table with this data, this is the first thing to get right; if
   you go straight to SQLite (recommended, see §4), it's still the right
   primary key / uniqueness constraint to enforce, so a bug shows up as a
   loud constraint violation instead of a silent overwrite.

2. **Vince's Custom Tables search endpoint has two confirmed pagination
   bugs** at the 22,535-row scale already: `from` has a minimum of 1 but
   maps straight to a 0-indexed skip, permanently losing the true first
   result of any query; and deep `from`/`size` paging silently drops or
   duplicates records for some large queries and not others (looks like
   unstable default sort order across requests). The workaround that
   worked: never page — query by prefix, cap `size` at 2000 (confirmed
   hard limit), and recursively split any prefix whose `total` exceeds one
   page. See `fetch_m3_api_catalog.py` in the VinceForge repo for the
   working implementation of this pattern. **This is direct motivation for
   §4's recommendation to avoid Custom Tables for the output of this
   project entirely** — a table 10-20x this size is not where you want to
   discover a third pagination edge case.

---

## 3. What you need to confirm before writing the real extraction loop

In order, because each depends on the last:

1. **How was the `LstFields` test call in §1 actually made, and what auth
   does it need?** Concretely: is this reachable by calling ION API's
   `m3api-rest/v2/execute` directly with your own credentials (a
   tenant-specific OAuth2 `client_credentials` flow — VinceForge's own docs
   flag this as real but not yet generically set up, needs a Token URI and
   Base URI that are per-tenant and not derivable, copy from a working
   example), or only reachable from inside a Vince Live workflow's REST API
   step (where a stored `connectionId` gets its auth attached automatically
   server-side, and you'd need to drive it via a workflow run rather than a
   standalone script)? This is the single decision that determines whether
   this project is a standalone script or something that has to trigger
   Live workflow runs.
2. **Does `TRTP` accept a wildcard?** One call per `(Program, Transaction)`
   instead of two roughly halves your total call count.
3. **Can multiple `LstFields` invocations be batched into one HTTP POST?**
   The bulk-execute body's `transactions` array can hold multiple entries
   under one shared `program` — and since every `LstFields` call here
   shares `program: "MRS001MI"` and `transaction: "LstFields"`, differing
   only in the `record` (the `MINM`/`TRNM`/`TRTP` values), these look like
   exactly the kind of calls that batch into one request. **No cap on how
   many entries fit in one `transactions` array is documented or confirmed
   anywhere in VinceForge** — find this out empirically, starting small
   (10, then 100, then higher) rather than assuming a number. This is the
   difference between ~22,500-45,000 individual HTTP calls and a much
   smaller number of large batched ones.
4. **Rate limits.** Not confirmed anywhere in VinceForge's history at any
   volume close to this. Watch for 429s or throttling once you're running
   real batches, and back off adaptively rather than assuming a fixed safe
   rate.

---

## 4. Recommended shape (revisit after §3's answers, don't commit blind)

- **Output: a single SQLite file**, not a Custom Table and not XML. Reasons:
  no live network/auth needed to *read* it afterward (both VinceForge and
  VinceGenerator just open a local file), trivial to index on
  `(Program, Transaction, Direction, FieldName)`, and it sidesteps both of
  §2's Custom Table risks entirely. A flat JSON/XML blob at this row count
  (low hundred-thousands, per the `LstByName` sample alone contributing 90
  rows for one transaction) would need to be fully loaded into memory for
  any lookup — SQLite doesn't have that problem and needless verbosity
  (XML tags around every field) buys nothing here.
- **Two tables**, so the file is self-contained:
  - `transactions`: `Program`, `Transaction`, `Description`, `Type`,
    `Status` — mirrors the existing `m3-api-catalog` Custom Table.
    VinceForge already has a local snapshot of this exact data —
    `m3-api-catalog-full.json` (~8.4MB, all 22,535+ rows, built by
    `fetch_m3_api_catalog.py`) — **ask for that file rather than
    re-fetching the Program/Transaction list from scratch**; it saves you
    from re-deriving something already extracted and saves a re-run of the
    already-known-buggy Custom Tables search endpoint.
  - `fields`: `Program`, `Transaction`, `Direction`, `FieldName`,
    `FieldLabel`, `Type`, `Length`, `Mandatory`, `FromPos`, `ToPos` (see
    §1's table for source column mapping). Skip the always-blank
    change-tracking columns (`RGDT`/`RGTM`/`LMDT`/`CHNO`/`CHID`) unless a
    real capture ever shows them populated — no point carrying dead weight.
  - Unique constraint on `fields(Program, Transaction, Direction, FieldName)`
    — per §2 point 1, make a key collision a loud error, not a silent
    overwrite.
- **Resumable by construction.** Whatever the real call-count turns out to
  be after §3, this will not complete in one bearer-token lifetime if you
  end up needing a manually-pasted short-lived token (~1 hour, per
  VinceForge's experience) rather than a longer-lived credential. Design
  the loop so `INSERT OR IGNORE`/upsert into SQLite doubles as the
  checkpoint: a re-run skips `(Program, Transaction)` pairs already present
  and picks up where an interrupted run left off, with no separate
  progress file needed.
- **Since this won't be repeated often**, don't over-build: no scheduling,
  no incremental-diff logic against a previous run, no service. A script
  you run by hand once (or rarely), that writes the SQLite file and reports
  what it fetched, is the right amount of engineering for a one-shot job.

---

## 5. Handoff mechanics — needs a decision, not filled in here

- **Where the finished SQLite file lives so both VinceForge and
  VinceGenerator can read it** is not yet decided — a shared folder,
  network location, or cloud storage path. Don't invent one; get this from
  the user before the extraction project needs to write its final output
  somewhere.
- **Whether this project's source code itself lives in a separate repo** or
  as a subfolder of one of the existing two — also not decided here,
  consistent with the instruction that this should be a small, separate
  project rather than folded into VinceForge's `converter/` package.
