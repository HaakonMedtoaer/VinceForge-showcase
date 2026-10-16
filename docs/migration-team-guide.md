# VinceForge — a guide for the Migration Team

**Who this is for:** anyone carrying a customer's VXL Classic functions into
Vince Live. You do not need to read JSON, know what a Generic Filter is, or
touch the code. It covers two points in a migration: shortlisting which
functions to migrate, and verifying what came across.

**Where the details live:** this page explains the *shape* of the problem and
how to triage. For "what about this specific construct", use the
**VinceForge Capability Matrix** ([vinceforge-capability-matrix.md](vinceforge-capability-matrix.md)) — it is generated from the
converter itself and is always current. Claims on this page are tagged with
the matrix row they come from, like `[filter-on-m3-written-column]`, so you
can look any of them up.

---

## 1. "A customer sent me their Classic files. Can we migrate them?"

About ten minutes, no tenant access needed, nothing is created anywhere.

**Start the tool.** In a terminal, from the project folder:

```
cd converter
python webui.py
```

Then open http://127.0.0.1:8420. It closes when you close the terminal, and
nothing starts it automatically after a reboot.

**Triage each file.** In the **Source** panel pick the **Classic XML** tab,
paste or load the file, and press **Generate**. Nothing reaches a tenant —
`Create in Live` is a separate button behind a confirmation box.

**Read three things, in this order:**

1. **How many workflows did it become?** One Classic file often becomes
   several. That is correct and unavoidable `[struct-multi-block]` — see §4.3.
2. **The warnings.** Plain sentences saying what could not be carried across.
   Each one names the construct and what happens instead.
3. **`_needs_review` in the generated JSON.** Same idea, attached to the
   specific M3 call it affects.

**Two things that look like failures and are not:**

- **`TODO: metadata not found for …`** — you generated without a bearer token,
  so field descriptions fell back to a stub table. It says nothing about
  whether the file can be migrated. Paste a token into the **Target** panel
  and generate again.
- **Warnings, but a workflow was produced.** A warning means "this differs
  from the source in a way you must look at", not "this failed".

**Record the outcome per function** in the customer's tracker. A function with no warnings is a safe shortlist candidate. A
function with warnings needs the judgement in §5 before you promise it.

---

## 2. What the tool actually does

It reads a Classic XML export and writes the equivalent Vince Live workflow,
then creates it in a tenant for you. It also handles a whole folder at once —
a customer's workflows, Custom Tables and App Builder source together — and
can pull existing workflows back out of Live.

What it is **not** is a black box that either works or doesn't. Classic and
Live are genuinely different products, and some things Classic does have no
equivalent in Live. When that happens the tool tells you, in words, rather
than quietly producing a workflow that behaves differently from the one the
customer has been running for years. **Those warnings are the product.** A
conversion nobody read the warnings on is not a migration, it is a guess.

---

## 3. What a clean conversion does and does not prove

Read this before telling a customer a function is migrated.

**It proves:** the shape of the function can be expressed in Live. Every call,
field and mapping has somewhere to go.

**It does not prove the workflow produces correct results.** That is only ever
settled by running it in the tenant against real M3 data, and there is no shortcut.

**When you do test it, mind the Excel add-in.** It caches which workflows it
can run. After a workflow is created or changed you must refresh it manually,
and even then it can take a couple of minutes before the new version is
actually available. **A test run done too soon silently executes the OLD
version.** That looks exactly like "the migration is broken" from the outside,
and has already cost us a day of chasing a bug that did not exist. If a run
contradicts what you expect, rule this out first: wait, refresh, run again.

---

## 4. The three things that most often block a migration

Most warnings you will see are one of these three. Each is a real difference
between the products, not a shortcoming we can patch away.

### 4.1 A condition that depends on an earlier M3 call

**By far the most common.** `[filter-on-m3-written-column]`

Classic functions routinely do this: call M3, write the answer into a column,
then use that column to decide whether the *next* call runs. "Check the user's
authority, and only create the order if the check came back OK."

Live's native filtering happens **once, before any M3 call runs**, and it can
only see the spreadsheet as the user submitted it. At that moment the column
holding the authority answer is still empty. So the condition cannot be
applied — not because of the operator used, and not something a different
arrangement of steps would fix.

The tool never guesses here. It refuses to apply the condition and tells you
which column and which call are involved.

**Both of our real reference files are this shape**, so expect it. It is the
single most likely reason a shortlisted function needs the alternative
approach in §5.

### 4.2 Grouping — one call per order, not per line

`[group-startrun]`

Classic can group rows and run a call once per group: one authority check per
destruction order, not one per line on it.

A converted workflow on the standard path runs every call once per row. (Live
documents a "Group by" option on its M3 step, but how it behaves is not yet
confirmed, so the tool does not use it.) For a
call that only *reads*, that is wasteful but harmless. **For a call that
writes, it is not:** you get one record per row where Classic created one per
group. The tool flags these individually and tells you whether the call looks
like a read or a write.

**Do not run a converted workflow with a grouping warning on a writing call
until someone has looked at it.** That is the one failure in this document
that creates bad data in a customer's M3 rather than just an error message.

### 4.3 One file becoming several workflows

`[struct-multi-block]`

A Classic file can contain several blocks. Live carries one sheet, one data
row and one filter gate per workflow, while Classic states those per block —
and real files genuinely disagree between their blocks. So each block becomes
its own workflow, named "<base name> - <block label>" — the base name is
asked for once per file at import time, suggested from the file name.

This is normal and correct. Two consequences for you:

- **Triage per block, not per file.** Blocks do not share a verdict. One of
  our reference files converts its first block perfectly cleanly and needs the
  alternative approach for its second.
- **Tell the customer.** What they think of as one function arrives as two or
  three workflows in Live. Better said during the demo than discovered after.

One thing we genuinely do not know: whether Classic runs a file's blocks as
one sequence or independently `[struct-block-run-order]`. If run order between
blocks matters for a function, flag it rather than assuming.

---

## 5. It's blocked. What now?

**There are two ways to build a workflow**, and the tool defaults to the
better one.

**The native path** is the default and what you get from the **Classic XML**
tab. It gives you validated M3 fields, a proper Excel sheet, and a workflow
that is readable and editable in Live's builder. Prefer it whenever it fits.

**The pipeline path** (**Classic XML → pipeline** tab) exists for exactly the
cases in §4.1 and §4.2. It can carry per-call conditions and conditions that
read an earlier call's result, and it handles grouping properly.

What it costs: no Excel step, no field validation, and the workflow appears in
Live's builder as a single block of code rather than something you can edit
visually. It also needs one extra piece of setup — the tenant's ION API root,
copied from a REST API step in that tenant. It is not a downgrade, it is a
different trade; several of Vince's own production customer workflows are
built this way.

**In practice:**

| What you saw | What to do |
|---|---|
| No warnings | Native path. Proceed. |
| Grouping warning, read-only call | Native path is fine. Note it in the tracker. |
| Grouping warning, **writing** call | Needs the pipeline path, or a decision. Escalate. |
| Condition on an M3-written column | Needs the pipeline path. Escalate. |
| `Refused` / the tool declined | Escalate — the file needs the other path or a look. |
| Anything you don't recognise | Escalate. Don't improvise. |

"Escalate" means hand it to whoever owns VinceForge, with the file and the
warning text. The warnings are written to be pasted straight into a message.

---

## 6. Things we know we don't know

Listed because you may hit them, and because guessing is worse than saying so.

- **`IsCritical`** `[flag-iscritical]` — Classic marks calls "critical", which
  sounds like "stop if this fails". Whether it stops the row, the block or the
  whole run is unconfirmed, and the difference decides whether a half-written
  order is left behind. The tool reports it and does not act on it. One
  deliberate test on the Classic side would settle it.
- **Custom Table rows** `[tables-write-rows]` — we can create a table's
  definition but cannot write rows into it. Table *data* has to be loaded
  another way.
- **Re-importing an app** `[appimport-update]` — there is no update. Importing
  the same app twice does not create duplicates (Live refuses a name it already
  has, and the tool skips it), but it does not update anything either: a
  workflow whose source file changed stays as it was in Live. Import once, fix
  forward.

For the current full list, filter the capability matrix on
**Verdict = Unconfirmed**.

---

## 7. Where to look things up

The [**VinceForge Capability Matrix**](vinceforge-capability-matrix.md),
alongside this page:

- **Blockers** — everything that does not carry across cleanly
- **Work queue** — what it would take to close each gap
- **By area** — browse by kind of construct

Every row names the piece of code that enforces it, and a self-test fails if
the two ever disagree — so the matrix cannot quietly go stale. This page can
lag behind it; **if they conflict, believe the matrix.**
