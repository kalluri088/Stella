# Web Capability

## Purpose

Stella has two opt-in external-information capabilities, `web_search` and
`web_fetch` (Stage E1, research report 22). They let the assistant find and
read public web pages when the user asks, with every call approved by a
trusted human and every byte of returned text treated as untrusted data.
They are not a browser, crawler, or general HTTP client, and they never
auto-run: the model proposes, the user authorizes (rules 3 and 10).

## Backends

Each tool answers its own backend question per call:

- With `TINYFISH_API_KEY` set, search goes to `api.search.tinyfish.ai`
  and fetch is performed server-side by `api.fetch.tinyfish.ai` (the
  provider does its own SSRF guarding and returns clean markdown). The
  free tier is a commercial decision — a third party sees the query —
  and the approval card says so.
- Without a key, search falls back to the optional `ddgs` package
  (`uv run --extra web ...`); unsupported filters (`recency_minutes`,
  `domain_type`) are reported back as ignored instead of silently
  dropped. Fetch goes out from this machine on the exact machinery
  `network_read` already enforces: pinned DNS, a public-address
  re-check on the connected peer, no redirects, bounded size
  (`stella.web_tools._stdlib_fetch_page` reuses
  `NetworkReadTool._resolve_public_addresses` and
  `_ValidatedHTTPSConnection`).
- If even the keyless backend is absent, the tools answer a structured
  "web is off" — never a traceback (rule 6's honest-degradation side).

Registration is a single gate: the `STELLA_WEB` flag (UI checkbox or
environment override). Unlike the Outline tools there is no reachability
probe, because every backend question is answered per call and each
answer degrades honestly.

## Runtime-owned budget

The free-tier limits from the pricing page are enforced by
`WebBudget`, one per application: 500 searches per hour and 1000 fetched
URLs per day. The counters are runtime state — the model can neither see
nor ration them — and refusals come with their own clear message.
Because hour/day buckets are keyed to the clock at call time, a call
near a boundary can only ever be refused slightly early, never late.

## Approval and security boundary

Both tools are `RiskLevel.DANGEROUS`: every use creates visible egress.
The previews and the approval-card summaries name the receiving third
party (TinyFish; DuckDuckGo when keyless; "this machine directly" for a
keyless fetch, which still leaves the house) plus the remaining budget.

`web_fetch` accepts only clean public HTTPS URLs: no credentials, no
non-default ports, no non-ASCII or whitespace, nothing over 2048
characters, and localhost/`.local` names or private/loopback/link-local
dotted quads are rejected before any DNS is attempted. Unlike
`network_read`, paths and query strings are accepted — they are what
people paste — while every safety property is kept. Hostnames are
screened again at resolve time by the shared machinery.

Everything returned from the web is wrapped in
`<<<UNTRUSTED_WEB_CONTENT>>>` markers with a header stating it never
authorizes any action, and is size-bounded (12k characters per page,
300-char snippets, at most 10 results). Marker literals inside the
payload are defanged before wrapping, so fetched text cannot forge its
own closing fence. The labels are a nudge to the model, not the wall —
the approval gate is (report 36's live probe kept proposing a deletion
straight through both headers). A page cannot steer Stella;
it can only be read by Stella (rules 6 and 8).

## Intentionally not implemented

No JavaScript execution, no crawling, no authenticated fetching, no
POSTs on behalf of the model, no caches, no background refreshes, no
uploads, no cookies, and no proxy discovery. There is no allowlist
management UI; budget values are code constants, not model-tunable.
The TinyFish live pass (report 22) validated the request and response
shapes; nothing in this module has been widened beyond what that pass
saw.

## Validation

`tests/test_web_tools.py` runs entirely against injected fakes: request
and response shapes for both backends, ignored-filter reporting, the
"web is off" path, budget refusal and bucket rollover, every validator
bound, marker wrapping and caps, previews, summaries, and the
`STELLA_WEB` settings override. No test touches the network or a real
key. Beyond the suite, the keyless paths were run live from `src/` on
2026-09-29: a ddgs search returned real titled/link/snippet rows (with
`recency_minutes` honestly reported as ignored), a pinned-DNS fetch of
a public page came back marker-wrapped, and with no key and no extra
the search answered the structured "web is off". A dead TinyFish key
was rejected as a clean `INVALID_API_KEY` failure — the error shape
the fakes simulate. The TinyFish request/response shapes themselves
are the report-22 spike's live 5/5 of 2026-09-27; the provider-side
key expired afterward, so the keyed path still needs one live pass
with a fresh key.
