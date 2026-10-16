#!/usr/bin/env python3
"""
Pulls the full contents of the m3-api-catalog Vince Custom Table.

BACKGROUND / WHY THIS APPROACH:
The search endpoint is backed by Elasticsearch. Two separate pagination bugs
were found empirically (2026-08-31) when using ordinary from/size paging:

  1. "from" has a minimum of 1 and maps straight through to ES's native
     "from" (a 0-indexed skip count). This makes the true first result of
     every query permanently unreachable via from=1 — confirmed by diffing
     a from=1 response against an otherwise-identical request with "from"
     omitted entirely (omitting it recovers exactly the record from=1 drops).

  2. Even after working around #1, multi-page fetches (paging through a
     single query's results via increasing "from" values) showed silent
     record duplication/loss for SOME large multi-page queries but not
     others (e.g. Program:C* consistently lost ~239 records across repeated
     attempts, Program:M* — also large and multi-page — never did). This
     looks like the endpoint's default (unspecified) sort order is not
     guaranteed stable across separate HTTP requests, which breaks deep
     from/size pagination in a way client-side offset math cannot reliably
     fix. An explicit "sort=" param was tried and appeared to be a no-op
     (results were not alphabetically ordered with or without it).

Single-page fetches (no "from" at all, everything in one response) have
NOT shown either problem in any test so far. So this script avoids
multi-page fetches entirely: it queries by `Program` prefix (starting with
single letters A-Z, since every observed Program value starts with a
letter) and, whenever a prefix's live `total` exceeds the page-size cap,
recursively splits it into longer prefixes (adding one more A-Z0-9
character) until every leaf query's total fits in a single page. This is
adaptive rather than hardcoded, since the exact prefixes that end up
oversized (confirmed 2026-08-31: plain "C" needs splitting down to 4
characters for "CMS*" and "CRS*" specifically) will drift as the M3
catalog grows over time.

CAVEAT: the recursive multi-character prefix query syntax (e.g.
q=Program:CMS*) is assumed to work the same way as the single-letter case
(q=Program:C*), which WAS confirmed directly via Postman. The multi-char
form has not been independently spot-checked. If results look wrong,
verify a multi-char prefix query manually before trusting a full run.

Usage:
    python3 fetch_m3_api_catalog.py --token "eyJhbGciOi..."

Or set the token via environment variable to avoid it landing in shell history:
    export VINCE_BEARER_TOKEN="eyJhbGciOi..."
    python3 fetch_m3_api_catalog.py
"""

import argparse
import json
import os
import string
import sys
import time
import urllib.parse

import requests

BASE_URL = "https://api.vince.live/v1/custom-tables/search/m3-api-catalog"
PAGE_SIZE = 2000                # confirmed hard cap per request
SAFE_SINGLE_PAGE_LIMIT = 2000   # a query with total <= this is fetched in one page, no "from"
MAX_PREFIX_LENGTH = 10          # safety valve against runaway recursion; M3 program names are short
TABLE_NAME = "m3-api-catalog"
OUTPUT_FILE = "m3-api-catalog-full.json"
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2

FIRST_LEVEL_PREFIXES = list(string.ascii_uppercase)  # every observed Program value starts with a letter
SPLIT_CHARS = list(string.ascii_uppercase) + list(string.digits)  # candidate next characters when splitting


def fetch_page(token: str, query: str, size: int = PAGE_SIZE) -> dict:
    """Always a single-page fetch: no "from" param, ever. See module docstring for why."""
    params = {
        "q": query,
        "size": size,
        # NOTE: deliberately no "order" and no "from". See module docstring.
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if not resp.ok:
                print(
                    f"    [debug] status={resp.status_code} body={resp.text[:500]!r}",
                    file=sys.stderr,
                )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(
                f"    [warn] attempt {attempt}/{RETRY_ATTEMPTS} failed for query={query!r}: {exc}",
                file=sys.stderr,
            )
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch query={query!r} after {RETRY_ATTEMPTS} attempts") from last_exc


def fetch_prefix(token: str, prefix: str, depth: int = 0) -> list:
    """
    Fetches every record whose Program starts with `prefix`, splitting recursively
    into longer prefixes if a single page isn't enough to cover it.
    """
    indent = "  " * (depth + 1)
    query = f"Program:{prefix}*"
    page = fetch_page(token, query)
    total = page.get("total", 0)

    if total == 0:
        return []

    if total <= SAFE_SINGLE_PAGE_LIMIT:
        items = page.get("items", [])
        if len(items) != total:
            print(
                f"{indent}[warn] prefix={prefix!r}: total={total} but only {len(items)} items "
                "returned in a single page — unexpected, investigate.",
                file=sys.stderr,
            )
        return items

    # Too many results for one page — split into finer prefixes.
    if depth + 1 >= MAX_PREFIX_LENGTH:
        print(
            f"{indent}[error] prefix={prefix!r} still has total={total} at max recursion depth "
            f"({MAX_PREFIX_LENGTH}). Giving up on this branch — results are INCOMPLETE.",
            file=sys.stderr,
        )
        return []

    print(f"{indent}prefix={prefix!r} has total={total} > {SAFE_SINGLE_PAGE_LIMIT}, splitting further...")
    items = []
    for ch in SPLIT_CHARS:
        child_items = fetch_prefix(token, prefix + ch, depth=depth + 1)
        items.extend(child_items)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch full m3-api-catalog table via recursive prefix splitting.")
    parser.add_argument(
        "--token",
        default=os.environ.get("VINCE_BEARER_TOKEN"),
        help="Bearer token. Falls back to VINCE_BEARER_TOKEN env var if not passed.",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_FILE,
        help=f"Output file path (default: {OUTPUT_FILE})",
    )
    args = parser.parse_args()

    if not args.token:
        print("ERROR: no bearer token provided. Pass --token or set VINCE_BEARER_TOKEN.", file=sys.stderr)
        return 1

    all_items = []
    per_letter_counts = {}

    for letter in FIRST_LEVEL_PREFIXES:
        print(f"Fetching prefix '{letter}'...")
        items = fetch_prefix(args.token, letter)
        per_letter_counts[letter] = len(items)
        print(f"  -> {len(items)} items")
        all_items.extend(items)

    total_fetched = len(all_items)
    print(f"\nFetched {total_fetched} items total across {len(FIRST_LEVEL_PREFIXES)} top-level prefixes.")
    for letter, count in per_letter_counts.items():
        if count:
            print(f"  {letter}: {count}")

    # Duplicate-key sanity check (Program + Transaction), same key used as the table's primary key
    seen = {}
    dupes = {}
    for item in all_items:
        key = (item.get("Program"), item.get("Transaction"))
        if key in seen:
            dupes.setdefault(key, [seen[key]]).append(item)
        else:
            seen[key] = item
    if dupes:
        print(f"  [warn] found {len(dupes)} duplicate (Program, Transaction) keys across fetched data.", file=sys.stderr)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {
                "tableName": TABLE_NAME,
                "fetchedCount": total_fetched,
                "duplicateKeyCount": len(dupes),
                "perLetterCounts": per_letter_counts,
                "items": all_items,
            },
            f,
            indent=2,
        )

    print(f"\nWrote combined result to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
