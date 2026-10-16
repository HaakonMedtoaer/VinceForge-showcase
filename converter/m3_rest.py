"""
Calling M3 without Live's M3 API step: the ION API "bulk execute" surface,
driven from Transform + REST API steps.

PROVENANCE - this module is built on real, captured production examples
(Vince Live's product documentation: "Multiple M3 API calls in 1
REST API call", "Rest API" / "Rest API v2", "Export data from REST API to
VXL Live", plus four real customer workflows built in this shape:
"Display customer order lines", "Claim register", "Package Lost",
"Display customer credit notes"). Not inferred.

THE CONFIRMED IDIOM
-------------------
Every real example uses the same two-step pair, repeated:

  Transform  builds  {"payload": {"program": ..., "transactions": [...]}}
  REST API   posts   that payload to M3's bulk execute endpoint

    POST https://mingle-ionapi.<region>.inforcloudsuite.com
             /<TENANT>/M3/m3api-rest/v2/execute
    {"version": 2, "connectionId": "CONNECTION-...", "method": "post",
     "headers": {"content-type": "application/json"},
     "bodyPath": "$.payload"}

One POST carries MANY M3 transactions (that is the point - M3 needs eight
calls to read one order), and `connectionId` makes Live attach the
connection's auth automatically. "Package Lost" runs seven such pairs and
does real writes (AddBatchHead -> AddBatchHeadChg -> AddBatchLine ->
Confirm) with no M3 API step and no Excel step anywhere.

Bulk request body (confirmed):
    {"program": "MMS200MI",
     "cono": 790,                    # optional
     "maxReturnedRecords": 100,      # optional
     "transactions": [{"transaction": "LstItmWhsByWhs",
                       "record": {"WHLO": "001"},
                       "selectedColumns": ["WHLO", "ITNO", "ITDS"]}]}

Bulk response (confirmed, real captured payload):
    {"statusCode": 200,
     "body": {"nrOfSuccessfullTransactions": 7, "nrOfFailedTransactions": 0,
              "wasTerminated": false,
              "results": [{"transaction": "GetOrderHead", "records": [{...}]}]}}

So a later step reads `$context.data.all.<stepId>.body.results` - and
`.records` flattens across results, `[transaction="GetHead"].records[0]`
picks one out. Note the `.body`: that is the REST API step's own
{headers, body} wrapper, unlike an M3 API step whose result is the array
directly.

WHERE THE FILTERS GO - the reason this path exists
--------------------------------------------------
Live's GENERIC_FILTER step runs once, before the entire M3 API step, so it
cannot gate one M3 call while another runs unconditionally. Here the
filter is a JSONata predicate in the Transform that builds the next
payload, so each call gets its own - including a gate on an EARLIER
call's output, which is the Classic shape (an authority check that always
runs, gating a later update) that the native path can't express.

STILL NOT CONFIRMED (flagged, not papered over)
-----------------------------------------------
  * `skipIfExpression` on a v2 REST API step - the property NAME comes
    from the ticket that implemented it; the expression SYNTAX and its
    truthiness semantics are documented nowhere. Probe before relying.
  * `forEach` - documented (JSONpath to an array; output becomes an array
    of {headers, body}), but the same page's example says `"version": 3`
    while its instructions say add `"version": 2`, and it is marked beta
    with "variable naming is a little inconsistent". No captured example.
  * The single-transaction GET form
    (`.../execute/<PROGRAM>/<TRANSACTION>?FIELD=x&dateformat=YMD8...`) is
    real - one working URL was supplied from a live tenant - but no
    captured Vince workflow uses it; every real one uses the bulk POST.
"""

import json
import re
from typing import Optional

# --- Confirmed endpoint pieces ----------------------------------------

# Real bases seen in production examples, one per tenant. Region and
# tenant segment are both tenant-specific - nothing to derive them from.
ION_API_BASE_EXAMPLES = (
    "https://mingle-ionapi.eu1.inforcloudsuite.com/DEMO_TENANT",
    "https://mingle-ionapi.eu1.inforcloudsuite.com/TEST_TENANT",
)

MI_BULK_SUFFIX = "M3/m3api-rest/v2/execute"

# Query options from the one confirmed single-transaction GET URL. Only
# relevant to that form; the bulk POST takes none of these.
MI_GET_DEFAULT_OPTIONS = {
    "dateformat": "YMD8",
    "excludeempty": "false",
    "righttrim": "true",
    "format": "PRETTY",
    "extendedresult": "false",
}

# Confirmed REST API stepConfig values for an M3 bulk call.
M3_BULK_HEADERS = {"content-type": "application/json"}
M3_BULK_METHOD = "post"
M3_BULK_BODY_PATH = "$.payload"
M3_BULK_VERSION = 2


class M3RestError(ValueError):
    pass


def _require_base(base_url: Optional[str], what: str) -> str:
    if not base_url:
        raise M3RestError(
            f"{what} needs a baseUrl: the ION API root INCLUDING the tenant segment, shaped like "
            f"{ION_API_BASE_EXAMPLES[0]} . It can't be defaulted - the region is per-tenant and the "
            "tenant segment is per-tenant, so there is nothing to derive it from. Take it from a "
            "real REST API step in the target tenant."
        )
    return base_url.rstrip("/")


def mi_bulk_endpoint(base_url: Optional[str]) -> str:
    """The bulk execute endpoint every real Vince workflow posts to."""
    return f"{_require_base(base_url, 'An M3 bulk endpoint')}/{MI_BULK_SUFFIX}"


def mi_get_url(program: str, transaction: str, fields: Optional[dict] = None,
               options: Optional[dict] = None, base_url: Optional[str] = None) -> str:
    """
    The single-transaction GET form. Real, but not the idiom - prefer the
    bulk POST unless you specifically need one call expressed as a URL.
    """
    from urllib.parse import urlencode

    base = _require_base(base_url, "An MI GET URL")
    merged = {k: v for k, v in (fields or {}).items() if v is not None}
    merged.update(MI_GET_DEFAULT_OPTIONS if options is None else options)
    query = urlencode(merged)
    return f"{base}/{MI_BULK_SUFFIX}/{program}/{transaction}" + (f"?{query}" if query else "")


def results_path(step_id: str) -> str:
    """JSONata path to a bulk REST step's results array."""
    return f"$context.data.all.{step_id}.body.results"


def records_path(step_id: str, transaction: Optional[str] = None) -> str:
    """
    JSONata path to a bulk REST step's records.

    With no transaction this flattens records across every result - which
    is what every real example does (`...body.results.records.CUNO`). With
    one, it selects that transaction's own result first, which is what you
    want as soon as a payload carries more than one transaction, since the
    flattened form silently mixes them.
    """
    base = results_path(step_id)
    if transaction:
        return f'{base}[transaction="{transaction}"].records'
    return f"{base}.records"


# --- JSONata generation for the Transform half ------------------------

_BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _jsonata_name(name: str) -> str:
    """A property reference, quoted only when it has to be."""
    return name if _BARE_NAME.match(name) else f'"{name}"'


def _jsonata_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


def value_expression(value: dict, where: str, step_ids: Optional[dict] = None,
                     row_prefix: str = "", index_records: bool = False) -> str:
    """
    One value source -> a JSONata expression, evaluated in the context of
    the current row of the array being mapped over.

      {"row": "OrderNumber"}       -> OrderNumber
      {"row": "Order Number"}      -> "Order Number"
      {"const": 901}               -> 901
      {"trigger": "CONO"}          -> $context.data.trigger.body[0].CONO
      {"step": "rest_api_1.ORNO"}  -> $context.data.all.rest_api_1.body.results.records.ORNO
      {"step": "rest_api_1:GetHead.ORNO"}
                                   -> ...results[transaction="GetHead"].records.ORNO
      {"jsonata": "'-' & ORQT"}    -> passed through verbatim

    In the step form, the part before the "." is looked up in `step_ids`
    first (a spec's own `ref` for an earlier step), and only used as a
    literal Live stepId if it isn't a known ref. Referencing by ref is
    what you want: insert a step later and the real stepIds shift, which
    would silently repoint a hardcoded `rest_api_1` at the wrong call.

    The trigger form matches the confirmed real one
    (`$context.data.trigger.body[0]."Order Number"`); the step form
    matches `$context.data.all.rest_api_1.body.results.records.CUNO`.
    """
    if "row" in value:
        # row_prefix is "" when the expression is evaluated with the row as
        # context (inside `$data.{...}` or a `rows[...]` predicate), and
        # "$row." inside a $filter callback where the row is a parameter.
        return f"{row_prefix}{_jsonata_name(str(value['row']))}"
    if "const" in value:
        return _jsonata_literal(value["const"])
    if "trigger" in value:
        return f"$context.data.trigger.body[0].{_jsonata_name(str(value['trigger']))}"
    if "jsonata" in value:
        return str(value["jsonata"])
    if "step" in value:
        raw = str(value["step"])
        if "." not in raw:
            raise M3RestError(
                f"{where}: {{\"step\": {raw!r}}} needs a field - "
                '"<stepId>.<FIELD>" or "<stepId>:<Transaction>.<FIELD>"'
            )
        locator, field = raw.split(".", 1)
        transaction = None
        if ":" in locator:
            locator, transaction = locator.split(":", 1)
        locator = (step_ids or {}).get(locator, locator)
        # index_records correlates the prior call's Nth record with the Nth
        # input row - exactly what the real customer workflows do
        # (`records[$i].CUNO`). Without it, `records.FIELD` is the whole
        # flattened sequence of that field, which silently does the wrong
        # thing inside a per-row condition.
        index = "[$i]" if index_records else ""
        return f"{records_path(locator, transaction)}{index}.{_jsonata_name(field)}"
    raise M3RestError(f"{where}: unrecognised value source {value!r}")


def gate_expression(gate, where: str, step_ids: Optional[dict] = None,
                    row_prefix: str = "", index_records: bool = False) -> str:
    """
    One gate -> a JSONata boolean expression, for use as an array
    predicate on the rows being mapped into a payload.

    This is where a Classic RowFilterEQ/RowFilterNE lands, and unlike
    Live's GENERIC_FILTER (equal/notEqual only, always applied to the
    whole M3 API step) it applies to exactly one call and can read an
    earlier call's output.
    """
    left = value_expression(gate.left, where, step_ids, row_prefix, index_records)
    if gate.case_insensitive:
        left = f"$lowercase({left})"

    if gate.op == "empty":
        return f'($not($exists({left})) or $string({left}) = "")'
    if gate.op == "notEmpty":
        return f'($exists({left}) and $string({left}) != "")'

    right_source = dict(gate.right)
    if gate.case_insensitive and "const" in right_source:
        right_source = {"const": str(right_source["const"]).lower()}
        right = value_expression(right_source, where, step_ids, row_prefix, index_records)
    else:
        right = value_expression(right_source, where, step_ids, row_prefix, index_records)
        if gate.case_insensitive:
            right = f"$lowercase({right})"

    if gate.op == "eq":
        return f"{left} = {right}"
    if gate.op == "ne":
        return f"{left} != {right}"
    if gate.op == "contains":
        return f"$contains({left}, {right})"
    if gate.op == "gt":
        return f"$number({left}) > $number({right})"
    if gate.op == "ge":
        return f"$number({left}) >= $number({right})"
    if gate.op == "lt":
        return f"$number({left}) < $number({right})"
    if gate.op == "le":
        return f"$number({left}) <= $number({right})"
    if gate.op == "in":
        return f"{left} in {right}"
    if gate.op == "notIn":
        return f"$not({left} in {right})"
    raise M3RestError(f"{where}: no JSONata rendering for gate operator {gate.op!r}")


def render_bulk_transform(step, step_ids: Optional[dict] = None) -> str:
    """
    Render the Transform half of an M3 bulk pair: JSONata producing
    {"payload": {"program": ..., "transactions": [...]}}.

    Shape copied from the real customer workflows ("Claim register",
    "Package Lost"), including the `$data.{...}[]` mapping that turns N
    input rows into N M3 transactions inside ONE call, and the trailing
    `[]` that forces an array even for a single row (without it JSONata
    yields a bare object, which is not what M3 expects).
    """
    # A gate that reads an earlier call's output has to be evaluated per
    # row WITH the row's index in hand, so it can line up against that
    # call's Nth record. That needs a $filter callback rather than a plain
    # predicate - a predicate has no index to correlate with.
    where = f"m3Bulk {step.program}"
    correlated = any(
        "step" in value
        for gate in step.gates
        for value in (gate.left, gate.right)
        if value
    )

    if step.filter_expression:
        rows_expr = f"{step.rows}[{step.filter_expression}]"
    elif step.gates and correlated:
        clauses = [
            gate_expression(g, where, step_ids, row_prefix="$row.", index_records=True)
            for g in step.gates
        ]
        rows_expr = (
            f"$filter({step.rows}, function($row, $i) {{ "
            + " and ".join(clauses)
            + " })"
        )
    elif step.gates:
        clauses = [gate_expression(g, where, step_ids) for g in step.gates]
        rows_expr = f"{step.rows}[" + " and ".join(clauses) + "]"
    else:
        rows_expr = step.rows

    calls = []
    for call in step.calls:
        record_lines = [
            "            {}: {}".format(
                json.dumps(field),
                value_expression(value, f"{step.program}/{call.transaction} field {field}", step_ids),
            )
            for field, value in call.record.items()
        ]
        body = [
            f'          "transaction": {json.dumps(call.transaction)},',
            '          "record": {',
            ",\n".join(record_lines),
            "          }",
        ]
        if call.selected_columns:
            body[-1] += ","
            body.append(f'          "selectedColumns": {json.dumps(call.selected_columns)}')
        calls.append("\n".join(body))   # inner fields only, braces added below

    extras = ""
    if step.cono is not None:
        extras += f'\n      "cono": {json.dumps(step.cono)},'
    if step.max_returned_records is not None:
        extras += f'\n      "maxReturnedRecords": {json.dumps(step.max_returned_records)},'

    # One transaction per row is the confirmed form: `$data.{...}[]`, the
    # trailing [] forcing an array even when there is a single row.
    # Several transactions per row makes the map yield an array per row,
    # which JSONata flattens for us - hence the [ ... ] wrapper instead.
    if len(calls) == 1:
        transactions_expr = "$data.{\n" + calls[0] + "\n      }[]"
    else:
        wrapped = ["        {\n" + call + "\n        }" for call in calls]
        transactions_expr = "$data.[\n" + ",\n".join(wrapped) + "\n      ]"

    return (
        "(\n"
        f"  $data := {rows_expr};\n"
        "  {\n"
        '    "payload": {'
        f"{extras}"
        f'\n      "program": {json.dumps(step.program)},'
        f'\n      "transactions": {transactions_expr}'
        "\n    }\n"
        "  }\n"
        ")\n"
    )


def render_bulk_rest_config(step, connection_id: str) -> dict:
    """
    The REST API half of an M3 bulk pair - the same shape every real
    customer workflow uses, only the endpoint's tenant base and the
    connection id differing.
    """
    config = {
        "version": M3_BULK_VERSION,
        "connectionId": step.connection_id or connection_id,
        "endpoint": mi_bulk_endpoint(step.base_url),
        "method": M3_BULK_METHOD,
        "headers": dict(step.headers) if step.headers else dict(M3_BULK_HEADERS),
        "bodyPath": M3_BULK_BODY_PATH,
    }
    if not config["connectionId"]:
        raise M3RestError(
            "An M3 bulk call needs a connection id - Live uses it to attach the ION API "
            "connection's auth to the request automatically."
        )
    if step.skip_if_expression:
        # Property name from the ticket that implemented it; syntax
        # unconfirmed. build_pipeline warns whenever this is set.
        config["skipIfExpression"] = step.skip_if_expression
    return config


# --- Code step (the escape hatch) -------------------------------------
# Only for what Transform + REST API genuinely cannot do: per-row
# SEQUENTIAL work where a later call's input depends on an earlier call's
# result for that same row, plus throttling and per-row error handling.
# Anything set-shaped belongs in the Transform + bulk REST pair above.

_RUNTIME_JS = r"""
  // ---- VinceForge generated: sequential per-row M3 bulk caller -------
  // Generated by converter/m3_rest.py. PLAN is data; the code below is a
  // fixed interpreter for it. Regenerate from the spec rather than
  // hand-patching, or the next build drops your edit.
  //
  // Prefer Transform + REST API for anything set-shaped: it is the idiom
  // every real Vince workflow uses, it stays visible in the builder, and
  // it does not spend a Lambda's wall clock on sequential HTTP.
  const authorizedAxios = require('authorizedAxios');
  const getContext = require('getContext');

  const connection = await getContext('globals.connection', PLAN.connectionId);
  const axios = await authorizedAxios(connection);

  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const rows = (function () {
    let candidate = null;
    try { candidate = PLAN_ROWS; } catch (error) { candidate = null; }
    if (Array.isArray(candidate)) return candidate;
    if (candidate && Array.isArray(candidate.data)) return candidate.data;
    return candidate ? [candidate] : [];
  })();

  // Row property lookup: the key as given, then PLAN.columnHeaders'
  // translation of it, then case-insensitive on both. CONFIRMED 2026-09-11
  // against a real VXL-triggered run: a spreadsheet row arrives keyed by
  // COLUMN LETTER ("A", "B", ...), not header name - unlike every
  // widget-triggered example, which is keyed by header name
  // ("OrderNumber", "Order Number"). The map and the fallback order stay,
  // since a widget-driven trigger still needs header-name lookup, and
  // _diagnostics still reports the real first row for the next surprise.
  function rowValue(row, key) {
    if (row === null || row === undefined) return undefined;
    const aliases = [key];
    const mapped = PLAN.columnHeaders && PLAN.columnHeaders[key];
    if (mapped && aliases.indexOf(mapped) === -1) aliases.push(mapped);
    for (const alias of aliases) {
      if (Object.prototype.hasOwnProperty.call(row, alias)) return row[alias];
    }
    const lowered = aliases.map(function (alias) { return String(alias).toLowerCase(); });
    const hit = Object.keys(row).find(function (k) {
      return lowered.indexOf(String(k).toLowerCase()) !== -1;
    });
    return hit === undefined ? undefined : row[hit];
  }

  function triggerValue(name) {
    const bags = [
      trigger && trigger.header,
      trigger && trigger.headers,
      trigger && Array.isArray(trigger.body) ? trigger.body[0] : null,
      trigger && trigger.body && !Array.isArray(trigger.body) ? trigger.body : null
    ];
    for (const bag of bags) {
      if (bag && Object.prototype.hasOwnProperty.call(bag, name)) return bag[name];
    }
    return undefined;
  }

  // Confirmed bulk response shape:
  //   { statusCode, body: { results: [ { transaction, records: [...] } ] } }
  // axios puts that under .data. The `|| data` fallback only exists so a
  // shape change surfaces as empty data rather than as a crash.
  function bulkResults(response) {
    const data = (response && response.data) || {};
    const body = data.body || data;
    return Array.isArray(body.results) ? body.results : [];
  }

  function firstRecord(results, transaction) {
    for (const result of results) {
      if (!transaction || result.transaction === transaction) {
        const records = result.records || [];
        if (records.length) return records[0];
      }
    }
    return {};
  }

  function bulkBody(response) {
    const data = (response && response.data) || {};
    return data.body || data;
  }

  // Did the ONE transaction in this request succeed? The fields read here
  // are the confirmed ones: nrOfSuccessfullTransactions /
  // nrOfFailedTransactions / wasTerminated. This path posts exactly one
  // transaction per request, so that counter is a verdict on THIS call -
  // which is why Classic's BasedOnTransaction is expressible here and not
  // inside a bulk payload carrying N transactions, where the same counter
  // says only "some of them failed".
  //
  // Record count is deliberately NOT used as the test: a Get that finds
  // nothing and a write that succeeds and returns nothing both come back
  // with no records, and treating those alike is how a get-or-create turns
  // into a double-create.
  //
  // Anything else answers 'unknown' rather than guessing. An unknown
  // outcome fails a gate that wants 'ok', so a chain stops rather than
  // writing on a verdict nobody established, and diagnostics.unknownStatus
  // makes that a number instead of a silence.
  function transactionOutcome(body) {
    if (!body || typeof body !== 'object') return 'unknown';
    if (body.wasTerminated === true) return 'failed';
    if (typeof body.nrOfFailedTransactions === 'number') {
      return body.nrOfFailedTransactions > 0 ? 'failed' : 'ok';
    }
    return 'unknown';
  }

  // Classic detects a group break by comparing with the PREVIOUS row only,
  // which is confirmed behaviour and means the rows have to arrive sorted
  // by the group columns. Deliberately not "remember every key seen".
  function groupKey(row) {
    const columns = PLAN.groupColumns || [];
    if (!columns.length) return '';
    return columns.map(function (column) {
      return norm(rowValue(row, column), false);
    }).join('\u0001');
  }

  function describeGates(gates) {
    return gates.map(function (gate) {
      return JSON.stringify(gate.left) + ' ' + gate.op + ' ' + JSON.stringify(gate.right || null);
    }).join('; ');
  }

  function resolveValue(spec, ctx) {
    if (!spec) return undefined;
    if ('const' in spec) return spec['const'];
    if ('row' in spec) return rowValue(ctx.row, spec.row);
    if ('trigger' in spec) return triggerValue(spec.trigger);
    if ('call' in spec) {
      const parts = String(spec.call).split('.');
      const prior = ctx.results[parts[0]];
      if (!prior) return undefined;
      if (parts.length < 2) return prior;
      return firstRecord(prior, null)[parts[1]];
    }
    // 'ok' | 'failed' | 'skipped' (a gate blocked it) | 'notrun' (it never
    // got that far, or it is once-per-group and this row is not a group's
    // first). Classic's BasedOnTransaction compares against the first two.
    if ('callStatus' in spec) {
      return ctx.status[String(spec.callStatus).split('.')[0]] || 'notrun';
    }
    return undefined;
  }

  function norm(value, caseInsensitive) {
    if (value === null || value === undefined) return '';
    const text = String(value).trim();
    return caseInsensitive ? text.toLowerCase() : text;
  }

  function gatePasses(gate, ctx) {
    const left = norm(resolveValue(gate.left, ctx), gate.caseInsensitive);
    const right = gate.right ? norm(resolveValue(gate.right, ctx), gate.caseInsensitive) : '';
    const list = function () {
      return right.split(',').map(function (part) { return part.trim(); });
    };
    switch (gate.op) {
      case 'eq': return left === right;
      case 'ne': return left !== right;
      case 'empty': return left === '';
      case 'notEmpty': return left !== '';
      case 'contains': return left.indexOf(right) !== -1;
      case 'gt': return Number(left) > Number(right);
      case 'ge': return Number(left) >= Number(right);
      case 'lt': return Number(left) < Number(right);
      case 'le': return Number(left) <= Number(right);
      case 'in': return list().indexOf(left) !== -1;
      case 'notIn': return list().indexOf(left) === -1;
      default: return false;
    }
  }

  // What a person reads in a sheet cell: M3's own error text and code, e.g.
  // 'failed: Item number does not exist (WIT0103)'. The raw bulk response
  // (about 250 characters per failed call) is only the fallback, for a failure
  // that carries no errorMessage - an 'unknown' outcome, say.
  function failureText(outcome, body) {
    const found = [];
    ((body && body.results) || []).forEach(function (result) {
      if (result && result.errorMessage) {
        const text = String(result.errorMessage).replace(/\s+/g, ' ').trim();
        found.push(result.errorCode ? text + ' (' + result.errorCode + ')' : text);
      }
    });
    if (found.length) return outcome + ': ' + found.join('; ');
    return 'M3 outcome ' + outcome + ': ' +
      JSON.stringify((body && body.results) || body || {}).slice(0, 500);
  }

  // Classic's per-call MessageColumn: this call's own failure text, in its own
  // column. Calls that share a column (CustomerA reuses BC/BD for dozens) are
  // joined in call order, as the block-level column is.
  function noteForColumn(ctx, call, text) {
    if (!call.messageColumn) return;
    (ctx.columnMessages[call.messageColumn] = ctx.columnMessages[call.messageColumn] || []).push(text);
  }
  const callMessageColumns = [];
  PLAN.calls.forEach(function (call) {
    if (call.messageColumn && callMessageColumns.indexOf(call.messageColumn) === -1) {
      callMessageColumns.push(call.messageColumn);
    }
  });

  const out = [];
  const diagnostics = {
    baseURL: (function () {
      try { return axios.defaults.baseURL || null; } catch (error) { return null; }
    })(),
    triggerKeys: trigger ? Object.keys(trigger) : [],
    // The real row shape, which is the one thing a first run has to settle:
    // are rows keyed by column letter or by header name? Read rowKeys.
    firstRow: rows[0] === undefined ? null : rows[0],
    rowKeys: rows[0] && typeof rows[0] === 'object' ? Object.keys(rows[0]) : [],
    rowCount: rows.length,
    firstPayload: null,
    skipped: 0,
    failed: 0,
    rowsSkipped: 0,
    groupsSeen: 0,
    groupRepeatsSkipped: 0,
    unknownStatus: 0
  };

  let aborted = null;
  let previousGroupKey = null;
  // Each row's ctx, kept for output "sheet", which writes a row's call
  // results back into that same row once the loop is done.
  const rowContexts = [];
  // A once-per-group call's result and outcome, carried to the rest of its
  // group when PLAN.groupResultScope is 'group' (the default) and dropped
  // at the group's first row when it is 'firstRow'.
  let carriedResults = {};
  let carriedStatus = {};

  // VXL sends the sheet's used range from ROW 1 (observed 2026-10-01), so
  // title/header/label rows arrive as data. Rows above Classic's
  // InputStartRow are never called - they would reach M3 as input (a
  // CpyItmWhs/UpdItmWhs would write a header's text).
  const dataFrom = PLAN.inputStartRow ? PLAN.inputStartRow - 1 : 0;
  diagnostics.dataFrom = dataFrom;

  // The add-in sends a workflow without an Excel step the sheet's USED RANGE
  // (read from its code 2026-10-13, docs/vince-excel-addin-internals.md §4):
  // empty LEADING ROWS are dropped, so rows[i] is sheet row i+1 only when
  // row 1 has content. Shifted, the "header" row below is really a data row,
  // and writing back would replace the table body with rows moved up - so a
  // header row with content but no header over a column this step WRITES
  // (Excel never leaves a table header blank) stops the run before any M3
  // call. Thrown, not returned: a failed run writes nothing back.
  if (PLAN.inputStartRow && PLAN.inputStartRow > 1) {
    const expectedHeader = PLAN.headerRow ? PLAN.headerRow - 1 : PLAN.inputStartRow - 2;
    const headerCells = rows[expectedHeader] && typeof rows[expectedHeader] === 'object' ? rows[expectedHeader] : {};
    const filled = function (column) {
      const cell = headerCells[column];
      return cell !== null && cell !== undefined && String(cell).trim() !== '';
    };
    if (Object.keys(headerCells).some(filled)) {
      const written = [];
      (PLAN.sheetCells || []).forEach(function (cell) { written.push(cell.column); });
      if (PLAN.messageColumn) written.push(PLAN.messageColumn);
      callMessageColumns.forEach(function (column) { written.push(column); });
      const unheaded = written.filter(function (column, i) {
        return written.indexOf(column) === i && !filled(column);
      });
      if (unheaded.length) {
        throw new Error('Sheet layout check failed: expected the table header on sheet row ' +
          (expectedHeader + 1) + ', but it has no header over column(s) ' + unheaded.join(', ') +
          ' that this workflow writes. Nothing was sent to M3. Most likely row 1 of the sheet ' +
          'is empty (Vince skips empty leading rows, shifting every row up) - put a title in ' +
          'row 1 - or the table does not cover those columns.');
      }
    }
  }

  // A row with no value in any cell is not data: Classic never processes one,
  // and a table sized past its data (203 blank body rows seen 2026-10-01)
  // would otherwise call M3 once per blank row with every field empty. Not
  // called, and returned as it came so the table stays aligned.
  function isBlankRow(row) {
    if (row === null || row === undefined || typeof row !== 'object') return true;
    return Object.keys(row).every(function (key) {
      const cell = row[key];
      return cell === null || cell === undefined || String(cell).trim() === '';
    });
  }
  const blankRows = {};
  diagnostics.blankSkipped = 0;

  for (let index = 0; index < rows.length && !aborted; index++) {
    if (index < dataFrom) continue;
    if (isBlankRow(rows[index])) {
      blankRows[index] = true;
      diagnostics.blankSkipped++;
      continue;
    }
    const thisGroupKey = groupKey(rows[index]);
    const isNewGroup = index === dataFrom || thisGroupKey !== previousGroupKey;
    previousGroupKey = thisGroupKey;
    if (isNewGroup) {
      diagnostics.groupsSeen++;
      carriedResults = {};
      carriedStatus = {};
    }

    const ctx = {
      row: rows[index], results: {}, status: {}, isNewGroup: isNewGroup,
      ran: {},        // calls that actually RAN on this row (not carried from the group's first)
      messages: [],   // this row's non-ok outcomes, for PLAN.messageColumn
      columnMessages: {}   // call.messageColumn -> this row's failure texts from the calls that write there
    };
    rowContexts[index] = ctx;
    if (PLAN.groupResultScope !== 'firstRow') {
      Object.keys(carriedResults).forEach(function (ref) { ctx.results[ref] = carriedResults[ref]; });
      Object.keys(carriedStatus).forEach(function (ref) { ctx.status[ref] = carriedStatus[ref]; });
    }

    // Classic's <Import>-level filter gates the ROW, not one call, so it is
    // checked once here instead of being repeated onto every call.
    const blockingRow = (PLAN.rowGates || []).filter(function (gate) { return !gatePasses(gate, ctx); });
    if (blockingRow.length) {
      diagnostics.rowsSkipped++;
      out.push({
        _row: index + 1,
        _call: '',
        _status: 'skipped',
        _message: 'row gate not met: ' + describeGates(blockingRow)
      });
      continue;
    }

    for (const call of PLAN.calls) {
      if (call.oncePerGroup && !isNewGroup) {
        // Already fired, on this group's first row. Not an event worth a
        // row in the output - just counted.
        diagnostics.groupRepeatsSkipped++;
        continue;
      }

      // A call's gates are an index into PLAN.gateSets, whose entries are
      // indexes into PLAN.gates - shared to keep the workflow under Live's
      // size limit. Absent means none.
      const callGates = typeof call.gates === 'number'
        ? PLAN.gateSets[call.gates].map(function (i) { return PLAN.gates[i]; })
        : (call.gates || []);
      const blocking = callGates.filter(function (gate) { return !gatePasses(gate, ctx); });
      if (blocking.length) {
        diagnostics.skipped++;
        ctx.status[call.ref] = 'skipped';
        if (call.oncePerGroup) carriedStatus[call.ref] = 'skipped';
        out.push({
          _row: index + 1,
          _call: call.ref,
          _status: 'skipped',
          _message: 'gate not met: ' + describeGates(blocking)
        });
        continue;
      }

      const record = {};
      Object.keys(call.record).forEach(function (name) {
        record[name] = resolveValue(call.record[name], ctx);
      });
      const payload = {
        program: call.program,
        transactions: [{ transaction: call.transaction, record: record }]
      };
      if (call.cono !== null && call.cono !== undefined) payload.cono = call.cono;
      if (call.selectedColumns && call.selectedColumns.length) {
        payload.transactions[0].selectedColumns = call.selectedColumns;
      }
      if (!diagnostics.firstPayload) diagnostics.firstPayload = payload;

      try {
        const response = await axios.post(PLAN.endpoint, payload, {
          headers: { 'content-type': 'application/json' }
        });
        const body = bulkBody(response);
        const results = bulkResults(response);
        // HTTP 200 does NOT mean the M3 transaction succeeded - the bulk
        // endpoint reports that in the body. A later call gated on this one
        // reads the outcome, not the status code.
        const outcome = transactionOutcome(body);
        if (outcome === 'unknown') diagnostics.unknownStatus++;
        if (outcome !== 'ok') diagnostics.failed++;
        ctx.results[call.ref] = results;
        ctx.status[call.ref] = outcome;
        if (call.oncePerGroup) {
          carriedResults[call.ref] = results;
          carriedStatus[call.ref] = outcome;
        }

        const message = outcome === 'ok' ? '' : failureText(outcome, body);
        ctx.ran[call.ref] = true;
        if (message) {
          ctx.messages.push(call.ref + ': ' + message);
          noteForColumn(ctx, call, message);
        }
        const records = (results[0] && results[0].records && results[0].records.length)
          ? results[0].records
          : [{}];
        records.forEach(function (item) {
          const entry = {
            _row: index + 1,
            _call: call.ref,
            _status: outcome,
            _message: message,
            _httpStatus: response.status
          };
          Object.keys(record).forEach(function (name) { entry['I_' + name] = record[name]; });
          Object.keys(item).forEach(function (name) { entry[name] = item[name]; });
          if (call.keepRaw) entry._raw = JSON.stringify(response.data);
          out.push(entry);
        });

        if (outcome !== 'ok') {
          if (PLAN.stopOnError) { aborted = message; break; }
          if (!call.continueOnError) break;   // drop this row's later calls
        }
      } catch (error) {
        diagnostics.failed++;
        ctx.results[call.ref] = [];
        ctx.status[call.ref] = 'failed';
        if (call.oncePerGroup) {
          carriedResults[call.ref] = [];
          carriedStatus[call.ref] = 'failed';
        }
        const entry = {
          _row: index + 1,
          _call: call.ref,
          _status: 'error',
          _message: (error && error.message) || 'request failed',
          _httpStatus: (error && error.response && error.response.status) || null,
          _error: error && error.response && error.response.data
            ? JSON.stringify(error.response.data)
            : null
        };
        Object.keys(record).forEach(function (name) { entry['I_' + name] = record[name]; });
        out.push(entry);
        ctx.ran[call.ref] = true;
        ctx.messages.push(call.ref + ': ' + entry._message);
        noteForColumn(ctx, call, entry._message);

        if (PLAN.stopOnError) { aborted = entry._message; break; }
        if (!call.continueOnError) break;   // drop this row's later calls, continue with the next row
      }

      if (call.throttleMs) await delay(call.throttleMs);
    }

    if (PLAN.rowThrottleMs && index < rows.length - 1) await delay(PLAN.rowThrottleMs);
  }

  // VXL/Excel export needs an array of objects that ALL carry the same
  // property keys, with "" rather than a missing key (confirmed: Product
  // Documentation, "Export data from REST API to VXL Live"). Rows here
  // legitimately differ - a skipped row has no output fields, an errored
  // one has _error - so square them up rather than shipping ragged rows.
  const allKeys = [];
  out.forEach(function (entry) {
    Object.keys(entry).forEach(function (key) {
      if (allKeys.indexOf(key) === -1) allKeys.push(key);
    });
  });
  const records = out.map(function (entry) {
    const squared = {};
    allKeys.forEach(function (key) {
      squared[key] = Object.prototype.hasOwnProperty.call(entry, key) && entry[key] !== null
        ? entry[key]
        : '';
    });
    return squared;
  });
"""

_RETURN_RECORDS = r"""
  // CONFIRMED HAZARD, 2026-09-11: PLAN.debug is read HERE, at runtime - not
  // baked into a fixed return shape at generation time (an earlier version
  // of this template chose between two static return statements in Python,
  // which meant editing "debug" in a PASTED Code step's PLAN literal did
  // nothing, because the JS text itself never referenced it). With debug
  // true this returns the export-ready array plus what the run learned
  // about the unconfirmed bits (real row shape, the connection's base URL,
  // the exact payload sent) - but a Manual-trigger run bound to a
  // spreadsheet range writes that whole object back into a single cell of
  // the SAME source sheet, which then gets read back in as row 1's data on
  // the next run. Set debug: false (right here in the pasted PLAN, no
  // regeneration needed) before any run that reuses this sheet, and the
  // bare array a VXL export wants is what comes back instead.
  return PLAN.debug ? { records: records, _diagnostics: diagnostics, _aborted: aborted } : records;
"""

_RETURN_SHEET = r"""
  // output "sheet": hand back the INPUT rows - same order, the table's header
  // names as keys, only the table body (see below) - with each call's <Cell> fields and the row's messages written
  // in. That is the shape the rows arrived in from VXL (a bare array of
  // flat objects, keyed by column letter - CONFIRMED 2026-09-11), and the
  // shape a write-back into the source sheet needs. The per-call log in
  // `records` stays out of the sheet; debug puts it under _calls.
  //
  // OBSERVED 2026-10-01 (a real run on a TABLE, 210 rows): VXL writes the
  // returned rows into the table by HEADER NAME, from the table's first body
  // row. Keys it does not know become NEW columns - returning letter keys
  // appended 122 columns named A..DR and left the real ones empty. So the
  // output is keyed by the table's header names, read here from the header
  // row of the input itself (Excel writes the final unique names into those
  // cells), and the rows returned start just BELOW that header so they land
  // on the table's body. Rows between the header and InputStartRow (a
  // sample-record row, say) pass through unchanged, uncalled.
  // Assumes rows[i] is sheet row i+1. The add-in DOES drop empty leading rows
  // (and columns - see classic_to_pipeline.row_key); the layout check before
  // the row loop refuses to run when that has shifted the header.
  const headerIdx = PLAN.headerRow ? PLAN.headerRow - 1 : dataFrom - 1;
  const headerNames = {};
  if (headerIdx >= 0 && rows[headerIdx] && typeof rows[headerIdx] === 'object') {
    Object.keys(rows[headerIdx]).forEach(function (letter) {
      const cell = rows[headerIdx][letter];
      if (cell !== null && cell !== undefined && String(cell) !== '') headerNames[letter] = String(cell);
    });
  }
  const hasHeader = Object.keys(headerNames).length > 0;
  // A letter's output key: the table header, else a supplied map, else the letter.
  function outKey(letter) {
    if (hasHeader && Object.prototype.hasOwnProperty.call(headerNames, letter)) return headerNames[letter];
    const mapped = PLAN.columnHeaders && PLAN.columnHeaders[letter];
    return mapped || letter;
  }
  function renamed(row) {
    const copy = {};
    Object.keys(row && typeof row === 'object' ? row : {}).forEach(function (key) {
      copy[outKey(key)] = row[key];
    });
    return copy;
  }
  diagnostics.headerRow = hasHeader ? headerIdx + 1 : null;
  diagnostics.headerCount = Object.keys(headerNames).length;

  // Without a header row (no InputStartRow, or an empty row above it) there
  // is no table to match: every row comes back, keyed as before.
  const firstReturned = hasHeader ? headerIdx + 1 : 0;
  diagnostics.returnedFrom = firstReturned + 1;

  // Blank rows AFTER the last data row are left out of the return: nothing
  // below the data needs writing back, and a table sized past its data (203
  // empty body rows seen 2026-10-01) would otherwise be handed back whole and
  // counted as that many processed records. Blank rows BETWEEN data rows stay,
  // or everything below them would shift up.
  let lastReturned = rows.length - 1;
  while (lastReturned >= firstReturned && lastReturned >= dataFrom && blankRows[lastReturned]) lastReturned--;
  diagnostics.trailingBlankDropped = rows.length - 1 - lastReturned;

  const sheet = [];
  rows.forEach(function (row, index) {
    if (index < firstReturned || index > lastReturned) return;
    const updated = renamed(row);
    if (index < dataFrom || blankRows[index]) { sheet.push(updated); return; }   // above InputStartRow, or blank: untouched
    const ctx = rowContexts[index];
    if (!ctx) {
      // Never reached: the run was aborted before this row.
      if (PLAN.messageColumn) updated[outKey(PLAN.messageColumn)] = 'not run: ' + aborted;
      sheet.push(updated);
      return;
    }
    (PLAN.sheetCells || []).forEach(function (cell) {
      // Only a call that ran on THIS row writes: a once-per-group call's
      // cell lands on the group's first row alone, no fill-down, as in
      // Classic. A skipped call leaves the sheet's own value alone.
      if (!ctx.ran[cell.call]) return;
      updated[outKey(cell.column)] = firstRecord(ctx.results[cell.call] || [], null)[cell.field];
    });
    if (PLAN.messageColumn) updated[outKey(PLAN.messageColumn)] = ctx.messages.join(' | ');
    // A processed row's per-call columns are always written (blank when nothing
    // failed) so a re-run does not leave last run's message behind.
    callMessageColumns.forEach(function (column) {
      updated[outKey(column)] = (ctx.columnMessages[column] || []).join(' | ');
    });
    sheet.push(updated);
  });

  const sheetKeys = [];
  sheet.forEach(function (entry) {
    Object.keys(entry).forEach(function (key) {
      if (sheetKeys.indexOf(key) === -1) sheetKeys.push(key);
    });
  });
  const sheetRows = sheet.map(function (entry) {
    const squared = {};
    sheetKeys.forEach(function (key) {
      squared[key] = entry[key] === null || entry[key] === undefined ? '' : entry[key];
    });
    return squared;
  });

  // debug: see _RETURN_RECORDS - the same self-poisoning hazard applies.
  return PLAN.debug
    ? { records: sheetRows, _calls: records, _diagnostics: diagnostics, _aborted: aborted }
    : sheetRows;
"""

_RETURN_ENVELOPE = r"""
  return {
    status: aborted ? 500 : 200,
    totalRows: rows.length,
    totalResults: records.length,
    aborted: aborted,
    results: records,
    _diagnostics: PLAN.debug ? diagnostics : undefined
  };
"""


def _plan_gate(gate) -> dict:
    """One Gate as the generated JS's interpreter reads it."""
    return {
        "left": gate.left,
        "op": gate.op,
        "right": gate.right,
        "caseInsensitive": gate.case_insensitive,
    }


def _plan_calls(calls) -> tuple:
    """
    (gates, gateSets, calls) for PLAN - kept small, because Live refuses a
    workflow over a size limit (see _compact_plan_literal). Two savings:

    * gates are shared, two levels deep: `gates` holds each distinct gate
      once, a `gateSets` entry lists indexes into it, and a call carries the
      index of its set. A real CustomerA block ("Open All Locations") has 168
      calls repeating 7-8 gates each (~700 bytes a time), yet only 33
      distinct sets built from 24 distinct gates;
    * a field at its default (null cono, no selected columns, no gates,
      false flags, 0 throttle) is left out - the runtime reads each of them
      as falsy/absent already.
    """
    pool, pool_index = [], {}
    gate_sets, set_index = [], {}
    rendered = []
    for call in calls:
        entry = {"ref": call.ref, "program": call.program,
                 "transaction": call.transaction, "record": call.record}
        if call.cono is not None:
            entry["cono"] = call.cono
        if call.selected_columns:
            entry["selectedColumns"] = call.selected_columns
        if call.gates:
            members = []
            for gate in call.gates:
                plain = _plan_gate(gate)
                key = json.dumps(plain, sort_keys=True)
                if key not in pool_index:
                    pool_index[key] = len(pool)
                    pool.append(plain)
                members.append(pool_index[key])
            key = tuple(members)
            if key not in set_index:
                set_index[key] = len(gate_sets)
                gate_sets.append(members)
            entry["gates"] = set_index[key]
        for name, value in (("continueOnError", call.continue_on_error),
                            ("throttleMs", call.throttle_ms),
                            ("keepRaw", call.keep_raw),
                            ("oncePerGroup", call.once_per_group),
                            ("messageColumn", call.message_column)):
            if value:
                entry[name] = value
        rendered.append(entry)
    return pool, gate_sets, rendered


def _compact_plan_literal(plan: dict) -> str:
    """
    PLAN as a JS literal: one top-level key per line, each call compact on
    a line of its own. Live refuses a workflow over a size limit
    (`HTTP 400: Item size to update has exceeded the maximum allowed
    size`, observed 2026-09-29 on real CustomerA imports), and `indent=2`
    alone roughly doubled a large PLAN - enough to push 116-128 KB bodies
    over it while an 82 KB one was accepted. Still one line per key/call,
    so a pasted step stays editable by hand (e.g. flipping "debug").
    """
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    lines = []
    for key, value in plan.items():
        if key in ("calls", "gates", "gateSets") and value:
            items = ",\n".join("    " + compact(item) for item in value)
            lines.append("    " + json.dumps(key) + ": [\n" + items + "\n    ]")
        else:
            lines.append("    " + json.dumps(key) + ": " + compact(value))
    return "{\n" + ",\n".join(lines) + "\n  }"


def render_row_pipeline_js(step, connection_id: str) -> str:
    """
    Render an M3RowPipelineStep into a Code step's `content`.

    The `module.exports = async function (prevStep, allSteps, trigger)`
    wrapper and the authorizedAxios/getContext pair are the confirmed real
    shape, from a real customer Code step.
    """
    resolved_connection = step.connection_id or connection_id
    if not resolved_connection:
        raise M3RestError(
            "A row pipeline needs a connection id - the stored ION API connection it calls through. "
            'Set "connectionId" on the step, or pass a target connection id to the build.'
        )

    plan = {
        "connectionId": resolved_connection,
        "endpoint": mi_bulk_endpoint(step.base_url),
        "stopOnError": step.stop_on_error,
        "rowThrottleMs": step.row_throttle_ms,
        "debug": step.debug,
        "rowGates": [_plan_gate(gate) for gate in step.row_gates],
        "groupColumns": list(step.group_columns),
        "groupResultScope": step.group_result_scope,
        "columnHeaders": dict(step.column_headers),
        "output": step.output,
        "sheetCells": [dict(cell) for cell in step.sheet_cells],
        "messageColumn": step.message_column,
        "inputStartRow": step.input_start_row,
        "headerRow": step.header_row,
    }
    plan["gates"], plan["gateSets"], plan["calls"] = _plan_calls(step.calls)

    # `debug` is NOT branched on here in Python - see _RETURN_RECORDS. It is
    # read by the generated JS itself, so a copy already pasted into a Live
    # Code step can have `debug` flipped there directly, no regeneration
    # needed.
    tail = {"envelope": _RETURN_ENVELOPE, "sheet": _RETURN_SHEET}.get(step.output, _RETURN_RECORDS)

    runtime = _RUNTIME_JS.replace("PLAN_ROWS", step.rows_js)
    plan_literal = _compact_plan_literal(plan)

    return (
        "module.exports = async function (prevStep, allSteps, trigger) {\n"
        f"  const PLAN = {plan_literal};\n"
        f"{runtime}"
        f"{tail}"
        "};\n"
    )
