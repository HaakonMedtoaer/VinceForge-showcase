# Vince Platform API — reference notes for Claude Code

This pairs with `vince-platform-openapi.yaml`. Point Claude Code at that file
instead of re-fetching vincesoftware.com docs each session — it's far cheaper
on tokens once filled in.

## Confidence key (matches comments in the YAML)

- **CONFIRMED** — verified directly against live vincesoftware.com documentation
  (Access Management page, fetched 2026-08-24). Safe to trust as-is.
- **PLACEHOLDER** — the *object type* is confirmed to exist (it's listed in the
  docs site's "Vince Platform API's" navigation), but the exact path, request/
  response shape was not retrievable via web search. Treat these as scaffolding,
  not fact.

## Object types Vince Platform's API works with

Per the docs nav (Administration → Vince Platform API's):

| Object | Status in this spec |
|---|---|
| Users | CONFIRMED (`/users` referenced directly in docs) |
| Roles | Object confirmed; path is a best-guess pattern, unverified |
| API Clients | CONFIRMED (creation flow, OAuth2 fields) |
| Workflows | Object confirmed; path/schema placeholder |
| Custom Tables | Object confirmed; path/schema placeholder |
| Tags | Object confirmed (used for dynamic workflow inputs); path/schema placeholder |
| Tenant | Object confirmed to exist as a category; nothing else known |
| Utils | Object confirmed to exist as a category; nothing else known |
| Variables | Object confirmed; path/schema placeholder |
| Schedules | Object confirmed; path/schema placeholder |
| Auto-increment | Object confirmed to exist as a category; nothing else known |
| Common features | Likely shared conventions (pagination, filtering, error format) — not a real object type |

## How to fill in the placeholders (fastest to slowest)

1. **Browser devtools (fastest):** Open Vince Platform, open Network tab, click
   around Tables / Workflows / Tags / Schedules / Variables screens. The XHR/fetch
   calls will show you the real paths, methods, and response JSON — copy those
   straight into the YAML.
2. **Postman/curl against your tenant:** Use an API Client you already have
   (Admin → Access Management → API Clients) to get a token, then hit guessed
   paths (`/workflows`, `/tables`, etc.) and see what comes back.
3. **Ask Vince support/your account contact** for an official Postman collection
   or OpenAPI file — worth a two-line email given your CaaS role; some vendors
   have this but don't publish it on the public docs site.

## Using this in Claude Code

Drop both files into your project (e.g. `docs/vince-api/`) and reference the
YAML in your `CLAUDE.md` or project instructions, e.g.:

```
When working with the Vince Platform API, consult docs/vince-api/vince-platform-openapi.yaml
instead of searching the web. Treat entries marked PLACEHOLDER as unverified —
confirm the real path before writing code against it.
```

This keeps Claude Code from re-deriving Vince's API surface from scratch (or
worse, hallucinating endpoints) every session, while being honest about what's
actually confirmed vs. still needs your verification.
