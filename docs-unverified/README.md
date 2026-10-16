# Unverified — do not trust as-is

> **Update 2026-09-07 — the "fabricated domain" reasoning needs softening.**
> `vincelive.dev` turns up in **Vince's own product documentation**: the
> How-to page *"Export data from REST API to VXL Live"* (last edited 2024-05-31) uses
> `https://api.dev.vincelive.dev/users` as its example REST API endpoint. So the
> domain was almost certainly a real Vince dev environment that has since been
> retired — not something a model invented, which is what the DNS check below
> was taken to imply.
>
> Nothing about the **contents** of `vince-api-notes.md` /
> `vince-platform-openapi.yaml` becomes trustworthy because of this: they are
> still unconfirmed, still labelled "CONFIRMED" on the strength of doc-reading
> rather than a real call, and still must not be built against. What changes is
> only the *explanation* for the dead domain — a decommissioned dev environment,
> rather than a hallucinated one. Worth knowing, because "that source is
> fabricated" and "that source is stale" call for different follow-up: a stale
> source may well have been accurate once, and its endpoint shapes may still
> resemble something real.

`vince-api-notes.md` and `vince-platform-openapi.yaml` were provided as reference
material for a possible separate "Vince Platform API" (Users/Roles/API Clients/
Custom Tables/Tags/Schedules/Variables via OAuth2 `client_credentials`).

**Checked 2026-08-24: the API base URL these files depend on, `api.vincelive.dev`,
has no DNS records at all** (`nslookup` returns nothing - no A/AAAA records),
while `api.vince.live` (the real API this project has used and verified all
session, via a Cognito bearer token) resolves to real IPs. A domain with no DNS
entry cannot have been "verified directly against live documentation" as the
notes file claims - that claim is not trustworthy.

Do not:
- Treat anything in these two files as confirmed, including sections labeled
  "CONFIRMED" - that label describes web-search-based doc reading, not a real
  API call, and is inconsistent with this project's own methodology (capture
  real data, don't guess).
- Add these files to CLAUDE.md or otherwise wire them in as trusted context for
  future sessions, per the notes file's own (misguided) suggestion to do so.
- Write code against any path/schema in the YAML without independently
  confirming it first.

If you want to actually pursue whether a separate, officially-documented
"Vince Platform API" exists (distinct from the api.vince.live one this project
already uses): the fastest real check is whether Administration > Access
Management > API Clients genuinely exists as a section in the Vince Live
product you use day to day. If it does, that's independent corroboration worth
following up (create a real API Client, get a real OAuth token, hit a real
endpoint, capture what comes back - same methodology as everything else in
this project). If it doesn't exist in your product, this document has no
relationship to the system this project actually targets.

> **Update 2026-09-16 — the "Access Management > API Clients" check above now
> has a partial answer, and it cuts both ways.**
>
> `vincesoftware.com`'s own current, live, PUBLIC product documentation
> (`/product-documentation/access-management`, fetched directly as raw HTML
> and re-verified with a second, separately-worded fetch after the first
> result looked suspiciously convenient - it matched too well) confirms
> "API Clients" is a real, documented Vince Platform feature: create one
> (Name, Roles - Read/Write/All scoped per app/resource), get a Client
> ID/Secret, then build a Connection with Auth Type OAuth, Grant Type
> `client_credentials`, and the page's own worked example, a full
> walkthrough titled "Use Case: Export Users via API Client Workflow".
>
> **But the exact endpoint that same page gives for it is the same dead
> domain flagged above** - verbatim from the page's own table:
> Token URI `https://api.vincelive.dev/oauth/token`, Base URI
> `https://api.vincelive.dev`. Re-checked 2026-09-16, three independent
> ways (`nslookup`, Python `socket.gethostbyname`, `curl`) - still zero DNS
> resolution, still fails exactly like the 2026-08-24 check. This is NOT the
> `api.dev.vincelive.dev` domain from the 2026-09-07 update above (note the
> different subdomain shape) - it's the plain `api.vincelive.dev` this file
> already checked and found dead, now independently reappearing in Vince's
> own CURRENT marketing-site documentation, not just a 2024 docs page.
>
> Read together: the FEATURE is real (worth pursuing for real, per the
> suggestion above - create an actual API Client in your own tenant and see
> what Token/Base URI IT gives you, which may well differ from the public
> doc's example). The specific URL vincesoftware.com's own current public
> docs publish for it is not something to trust or build against - it may be
> a stale copy-paste from the same retired dev environment as
> `api.dev.vincelive.dev`, a redacted/placeholder example value, or
> something else entirely. Either way this doesn't change anything about
> `vince-api-notes.md`/`vince-platform-openapi.yaml`'s own trustworthiness -
> same "do not" list applies.
>
> Also worth keeping in mind for future sessions: an AI-summarizing fetch
> tool can sound confidently "verbatim" about a specific-looking value like
> this without it actually being fabricated - the first fetch here read as
> suspicious specifically BECAUSE it matched a value this file had already
> flagged, and a second, differently-worded fetch reproduced the identical
> string, which is what finally justified pulling the raw HTML directly
> (bypassing the summarizer entirely) to settle it for real. The instinct to
> be suspicious was right to have; the way to resolve it was a raw fetch,
> not just re-asking the same tool.
