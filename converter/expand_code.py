"""
The m3Expand Code step: what Classic's <Expand> did, rebuilt so a workflow
only touches the sheet it runs on.

WHY THIS EXISTS
---------------
Classic's <Expand> reads key rows from one sheet and writes the detail rows
for each key onto another (CustomerB: each price-list header -> its lines; each
line -> its quantity scales). Live is treated as having no Expand (product
manager, 2026-10-16): a workflow reads and writes only the sheet it runs on.
So the customer opens the TARGET sheet, fills in a selection on the trigger
form, and this step fetches the keys from M3 itself instead of from another
sheet, then fans out:

    level 1  a LIST call (e.g. OIS017MI.LstPriceList), filtered here
    level 2  a LIST call per level-1 record (OIS017MI.LstBasePrice)
    level 3+ LOOKUPS per record (MMS200MI.GetItmBasic, ...), first record
             only, cached on their input, failure noted but never drops the row

and returns one row per leaf, keyed by the sheet table's HEADER NAMES.

WHAT IT RESTS ON (each named, so a first run checks the right thing)
--------------------------------------------------------------------
Confirmed:
  * the Code step wrapper, authorizedAxios/getContext and the bulk execute
    request/response shapes (m3_rest.py, real captured examples);
  * `maxReturnedRecords` is a confirmed optional field of the bulk request;
  * a Code step's return value REPLACES the sheet table's body, columns
    matched by HEADER NAME; a key with no matching header becomes a NEW
    column (docs/vince-excel-addin-internals.md §4) - so the step refuses to
    run when the table it can see lacks a header it would write;
  * a run's trigger form values are in `trigger.header`, as strings
    (execution records, docs/vince-live-api-reference-prompt.md §1e);
  * a list call given a start key lists FROM that key on, in TEXT order
    (OIS017MI/LstPriceList PRRF=001 on DEMO_TENANT, 2026-10-16) - so the
    `where` filter is what bounds a range, and compares as text by default.
Not confirmed:
  * that the Code step's own `trigger` argument carries that same `header`
    (the row pipeline reads it the same way, but only rows have been seen
    there). The step throws, listing what it did see, when a required form
    value is missing;
  * dates come back as M3 writes them (YYYYMMDD text), not as Excel dates;
  * whether ION tolerates PLAN.concurrency parallel calls.

If it is too slow (a fan-out of thousands of calls), the agreed fallback is
Transform + REST API (bulk) rather than tuning this step.
"""

import json
from typing import Optional

from m3_rest import M3RestError, mi_bulk_endpoint

VALUE_KEYS = ("const", "param", "from")
WHERE_OPERATORS = ("eq", "ne", "ge", "gt", "le", "lt")
# How a `where` condition compares. TEXT by default, because that is M3's own
# order for a key: DEMO_TENANT's OIS017MI/LstPriceList returns price lists as
# 001, 01, 056A, ..., 1-NLDISPLY, 1001, 73 (2026-10-16). Comparing numbers
# whenever both sides looked numeric made 001..001 also keep "01" and 1..2 drop
# "1001". "number" is for genuinely numeric fields, chosen per condition.
WHERE_COMPARE = ("text", "number")
WHERE_KEYS = {"field", "op", "value", "as"}
DEFAULT_MAX_RECORDS = 10000
DEFAULT_CONCURRENCY = 4


class ExpandSpecError(ValueError):
    pass


def _check_value(value, where: str, refs_before: list, params: list) -> dict:
    """A value spec: {"const": x} | {"param": NAME[, "default": spec]} | {"from": "ref.FIELD"}."""
    if not isinstance(value, dict) or sum(key in value for key in VALUE_KEYS) != 1:
        raise ExpandSpecError(f"{where}: a value needs exactly one of {list(VALUE_KEYS)}, got {value!r}")
    if "param" in value and value["param"] not in params:
        raise ExpandSpecError(f"{where}: param {value['param']!r} is not declared in params {params}")
    if "from" in value:
        ref = str(value["from"]).split(".")[0]
        if "." not in str(value["from"]) or ref not in refs_before:
            raise ExpandSpecError(
                f"{where}: \"from\" must be \"<ref>.<FIELD>\" naming an EARLIER level, one of "
                f"{refs_before}; got {value['from']!r}")
    if "default" in value:
        if "param" not in value:
            raise ExpandSpecError(f"{where}: \"default\" only goes with \"param\"")
        _check_value(value["default"], where + ".default", refs_before, params)
    return value


def validate_expand_spec(raw: dict, where: str = "m3Expand") -> dict:
    """
    Check an m3Expand step spec and return it normalised. Everything that
    would otherwise fail at RUN time in the customer's Excel - a typo in a
    ref, an undeclared param - fails here instead.
    """
    params = raw.get("params") or []
    names = []
    for index, param in enumerate(params):
        if not isinstance(param, dict) or not param.get("name"):
            raise ExpandSpecError(f"{where}.params[{index}]: needs a \"name\"")
        names.append(param["name"])

    levels = raw.get("levels") or []
    if not levels:
        raise ExpandSpecError(f"{where}: needs at least one entry in \"levels\"")
    if not levels[0].get("each"):
        raise ExpandSpecError(f"{where}.levels[0]: the first level must be a list (\"each\": true) - "
                              "it is what the rows fan out from")
    refs = []
    for index, level in enumerate(levels):
        at = f"{where}.levels[{index}]"
        for key in ("ref", "program", "transaction"):
            if not level.get(key):
                raise ExpandSpecError(f"{at}: needs \"{key}\"")
        if level["ref"] in refs:
            raise ExpandSpecError(f"{at}: duplicate ref {level['ref']!r}")
        for name, value in (level.get("record") or {}).items():
            _check_value(value, f"{at}.record.{name}", refs, names)
        for w_index, cond in enumerate(level.get("where") or []):
            if not level.get("each"):
                raise ExpandSpecError(f"{at}: \"where\" only applies to a list level")
            if cond.get("op") not in WHERE_OPERATORS or not cond.get("field"):
                raise ExpandSpecError(f"{at}.where[{w_index}]: needs \"field\" and \"op\" in {list(WHERE_OPERATORS)}")
            if set(cond) - WHERE_KEYS:
                raise ExpandSpecError(f"{at}.where[{w_index}]: unknown key(s) {sorted(set(cond) - WHERE_KEYS)}")
            if cond.get("as", "text") not in WHERE_COMPARE:
                raise ExpandSpecError(f"{at}.where[{w_index}]: \"as\" must be one of {list(WHERE_COMPARE)}")
            _check_value(cond.get("value"), f"{at}.where[{w_index}].value", refs, names)
        refs.append(level["ref"])

    columns = raw.get("columns") or []
    if not columns:
        raise ExpandSpecError(f"{where}: needs \"columns\" - the sheet table's header names, in order")
    headers = []
    for index, column in enumerate(columns):
        at = f"{where}.columns[{index}]"
        if not column.get("header"):
            raise ExpandSpecError(f"{at}: needs \"header\" - the table header name it is written under")
        if column["header"] in headers:
            raise ExpandSpecError(f"{at}: duplicate header {column['header']!r}")
        headers.append(column["header"])
        if not column.get("message"):
            _check_value({key: column[key] for key in column if key in VALUE_KEYS + ("default",)},
                         at, refs, names)
    if sum(1 for column in columns if column.get("message")) > 1:
        raise ExpandSpecError(f"{where}: only one column can be the message column")
    return raw


_RUNTIME_JS = r"""
  // ---- VinceForge generated: m3Expand (Classic <Expand> on one sheet) ----
  // Generated by converter/expand_code.py. PLAN is data; the code below is a
  // fixed interpreter for it. Regenerate from the spec rather than editing.
  const authorizedAxios = require('authorizedAxios');
  const getContext = require('getContext');

  // Form values. CONFIRMED in execution records as trigger.header (strings);
  // that the Code step's own `trigger` carries it is the first thing a run
  // checks - a required value that is missing stops the run below.
  const formBag = (trigger && (trigger.header || trigger.headers)) || {};
  function param(name) {
    const wanted = String(name).toLowerCase();
    const key = Object.keys(formBag).find(function (k) { return String(k).toLowerCase() === wanted; });
    const value = key === undefined ? '' : formBag[key];
    return value === null || value === undefined ? '' : String(value).trim();
  }
  const missing = PLAN.params.filter(function (p) { return p.required && param(p.name) === ''; });
  if (missing.length) {
    throw new Error('Fill in ' + missing.map(function (p) { return p.label || p.name; }).join(', ') +
      ' before running. Nothing was sent to M3. (Form values this step received: ' +
      (Object.keys(formBag).join(', ') || 'none') + ')');
  }

  // The rows VXL sent (the sheet's used range, keyed by column letter). Only
  // used to check the table: every header this step writes must exist, or
  // VXL would append it as a NEW column. Skipped when no rows came inline.
  const sheetRows = trigger && Array.isArray(trigger.body) ? trigger.body : [];
  const written = PLAN.columns.map(function (c) { return c.header; });
  if (sheetRows.length) {
    const lower = function (v) { return String(v === null || v === undefined ? '' : v).trim().toLowerCase(); };
    const headerRow = sheetRows.find(function (row) {
      const cells = Object.keys(row || {}).map(function (k) { return lower(row[k]); });
      return cells.indexOf(lower(written[0])) !== -1;
    });
    const present = headerRow ? Object.keys(headerRow).map(function (k) { return lower(headerRow[k]); }) : [];
    const absent = written.filter(function (h) { return present.indexOf(lower(h)) === -1; });
    if (!headerRow || absent.length) {
      throw new Error('Sheet check failed: this workflow writes into an Excel TABLE whose header row ' +
        'holds ' + written.join(', ') + '. ' + (headerRow ? 'Missing: ' + absent.join(', ') :
        'No row holds ' + written[0] + '.') + ' Nothing was sent to M3. Run it on the right sheet, ' +
        'or fix the table headers.');
    }
  }

  const connection = await getContext('globals.connection', PLAN.connectionId);
  const axios = await authorizedAxios(connection);

  // At most PLAN.concurrency M3 calls in flight, across every level.
  let active = 0;
  const waiting = [];
  function limited(work) {
    return new Promise(function (resolve, reject) {
      const start = function () {
        active++;
        work().then(resolve, reject).finally(function () {
          active--;
          if (waiting.length) waiting.shift()();
        });
      };
      if (active < PLAN.concurrency) start(); else waiting.push(start);
    });
  }

  function resolve(spec, ctx) {
    if (!spec) return undefined;
    if ('const' in spec) return spec['const'];
    if ('param' in spec) {
      const value = param(spec.param);
      return value === '' && spec['default'] ? resolve(spec['default'], ctx) : value;
    }
    if ('from' in spec) {
      const parts = String(spec.from).split('.');
      const rec = ctx[parts[0]];
      // Trimmed: M3 can pad a field, and a padded key passed on to the next
      // call ("A1 ") misses its record and splits the lookup cache.
      // Constants are NOT trimmed - CustomerB's GetAlias sends ALWQ " " on purpose.
      const value = rec ? rec[parts[1]] : undefined;
      return value === null || value === undefined ? value : String(value).trim();
    }
    return undefined;
  }

  // Same verdict as the row pipeline: only the confirmed counters decide.
  function outcome(body) {
    if (!body || typeof body !== 'object') return 'unknown';
    if (body.wasTerminated === true) return 'failed';
    if (typeof body.nrOfFailedTransactions === 'number') return body.nrOfFailedTransactions > 0 ? 'failed' : 'ok';
    return 'unknown';
  }
  function failureText(verdict, body) {
    const found = [];
    ((body && body.results) || []).forEach(function (r) {
      if (r && r.errorMessage) {
        const text = String(r.errorMessage).replace(/\s+/g, ' ').trim();
        found.push(r.errorCode ? text + ' (' + r.errorCode + ')' : text);
      }
    });
    return verdict + ': ' + (found.length ? found.join('; ') : JSON.stringify((body && body.results) || body || {}).slice(0, 300));
  }

  const diagnostics = { calls: 0, cached: 0, failed: 0 };
  const cache = {};

  async function m3(level, record) {
    const payload = { program: level.program, transactions: [{ transaction: level.transaction, record: record }] };
    if (level.each) payload.maxReturnedRecords = level.maxRecords;
    if (level.selectedColumns) payload.transactions[0].selectedColumns = level.selectedColumns;
    diagnostics.calls++;
    try {
      const response = await limited(function () {
        return axios.post(PLAN.endpoint, payload, { headers: { 'content-type': 'application/json' } });
      });
      const data = (response && response.data) || {};
      const body = data.body || data;
      const verdict = outcome(body);
      const results = Array.isArray(body.results) ? body.results : [];
      const records = (results[0] && results[0].records) || [];
      if (verdict !== 'ok') diagnostics.failed++;
      return verdict === 'ok' ? { ok: true, records: records } : { ok: false, records: [], message: failureText(verdict, body) };
    } catch (error) {
      diagnostics.failed++;
      return { ok: false, records: [], message: 'error: ' + ((error && error.message) || 'request failed') };
    }
  }

  function text(value) {
    return value === null || value === undefined ? '' : String(value).trim();
  }
  // Text unless the condition says "as": "number" - text is M3's own key order.
  // Under "number" a blank or non-numeric value gives NaN: it matches no range.
  function compare(left, right, as) {
    const a = text(left), b = text(right);
    if (as === 'number') return a === '' || b === '' ? NaN : Number(a) - Number(b);
    return a < b ? -1 : (a > b ? 1 : 0);
  }
  // A condition whose value is blank (an optional form field left empty) is
  // not applied.
  function kept(level, rec, ctx) {
    return (level.where || []).every(function (cond) {
      const right = resolve(cond.value, ctx);
      if (text(right) === '') return true;
      const c = compare(rec[cond.field], right, cond.as);
      switch (cond.op) {
        case 'eq': return c === 0;
        case 'ne': return c !== 0;
        case 'ge': return c >= 0;
        case 'gt': return c > 0;
        case 'le': return c <= 0;
        case 'lt': return c < 0;
        default: return false;
      }
    });
  }

  function row(ctx, notes) {
    const out = {};
    PLAN.columns.forEach(function (column) {
      out[column.header] = column.message ? notes.join(' | ') : text(resolve(column, ctx));
    });
    return out;
  }

  // Depth first; children of a list run concurrently (bounded by `limited`)
  // but come back in M3's order.
  async function walk(index, ctx, notes) {
    if (index === PLAN.levels.length) return [row(ctx, notes)];
    const level = PLAN.levels[index];
    const record = {};
    Object.keys(level.record || {}).forEach(function (name) {
      const value = resolve(level.record[name], ctx);
      if (value !== undefined && value !== null) record[name] = String(value);
    });

    if (!level.each) {
      const key = JSON.stringify([level.program, level.transaction, record]);
      let answer = cache[key];
      if (answer) diagnostics.cached++;
      else { answer = m3(level, record); cache[key] = answer; }
      const done = await answer;
      const next = Object.assign({}, ctx);
      next[level.ref] = done.ok ? (done.records[0] || {}) : {};
      return walk(index + 1, next, done.ok ? notes : notes.concat([level.ref + ': ' + done.message]));
    }

    const done = await m3(level, record);
    if (!done.ok) return PLAN.messageColumn ? [row(ctx, notes.concat([level.ref + ': ' + done.message]))] : [];
    if (done.records.length >= level.maxRecords) {
      notes = notes.concat([level.ref + ': stopped at ' + level.maxRecords + ' records - narrow the selection']);
    }
    const children = done.records.filter(function (rec) { return kept(level, rec, ctx); });
    const nested = await Promise.all(children.map(function (rec) {
      const next = Object.assign({}, ctx);
      next[level.ref] = rec;
      return walk(index + 1, next, notes);
    }));
    return [].concat.apply([], nested);
  }

  let rows = await walk(0, {}, []);
  if (!rows.length) {
    // An empty return would leave nothing to tell the person why the table emptied.
    rows = [row({}, ['No records found for this selection'])];
    if (!PLAN.messageColumn) rows = [];
  }
  if (PLAN.debug) console.log('m3Expand diagnostics', JSON.stringify(diagnostics), 'rows', rows.length);
  return rows;
"""


def render_expand_js(raw: dict, connection_id: str, base_url: Optional[str] = None) -> str:
    """An m3Expand spec -> the Code step's `content` (a whole Node module)."""
    spec = validate_expand_spec(raw)
    resolved_connection = spec.get("connectionId") or connection_id
    if not resolved_connection:
        raise M3RestError("An m3Expand step needs a connection id - the ION API connection it calls through.")
    endpoint = mi_bulk_endpoint(spec.get("baseUrl") or base_url)

    levels = []
    for level in spec["levels"]:
        entry = {"ref": level["ref"], "program": level["program"], "transaction": level["transaction"],
                 "record": level.get("record") or {}}
        if level.get("each"):
            entry["each"] = True
            entry["maxRecords"] = int(level.get("maxRecords") or DEFAULT_MAX_RECORDS)
            if level.get("where"):
                entry["where"] = level["where"]
        if level.get("selectedColumns"):
            entry["selectedColumns"] = level["selectedColumns"]
        levels.append(entry)

    plan = {
        "connectionId": resolved_connection,
        "endpoint": endpoint,
        "concurrency": int(spec.get("concurrency") or DEFAULT_CONCURRENCY),
        "debug": bool(spec.get("debug", False)),
        "params": [{"name": p["name"], "label": p.get("label") or p["name"],
                    "required": bool(p.get("required", False))} for p in spec.get("params") or []],
        "messageColumn": next((c["header"] for c in spec["columns"] if c.get("message")), None),
        "levels": levels,
        "columns": spec["columns"],
    }
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    lines = []
    for key, value in plan.items():
        if key in ("levels", "columns"):
            lines.append("    " + json.dumps(key) + ": [\n" +
                         ",\n".join("      " + compact(item) for item in value) + "\n    ]")
        else:
            lines.append("    " + json.dumps(key) + ": " + compact(value))
    plan_literal = "{\n" + ",\n".join(lines) + "\n  }"
    return ("module.exports = async function (prevStep, allSteps, trigger) {\n"
            f"  const PLAN = {plan_literal};\n"
            f"{_RUNTIME_JS}"
            "};\n")


def expand_warnings(raw: dict) -> list:
    """What to say out loud about a built m3Expand step."""
    lists = [level for level in raw.get("levels") or [] if level.get("each")]
    notes = [
        "m3Expand Code step: the trigger form values are read from trigger.header, CONFIRMED in "
        "execution records but not yet seen inside a Code step - the first run shows it (a missing "
        "required value stops the run and lists the values it did receive).",
        "m3Expand output REPLACES the body of the sheet's Excel table, matched by header name. The "
        "sheet must hold a table whose header row carries: " +
        ", ".join(c["header"] for c in raw.get("columns") or []) + ". Columns left out are blanked.",
        "Dates are returned as M3 sends them (YYYYMMDD text), not converted to Excel dates.",
    ]
    if len(lists) > 1:
        notes.append(
            str(len(lists)) + " nested list levels: calls grow with the product of their record "
            "counts. If a run is too slow, the agreed fallback is Transform + REST API (bulk).")
    return notes
