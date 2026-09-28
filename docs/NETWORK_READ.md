# Network Read Tool

## Purpose

Stella now has one deliberately narrow external-data capability,
`network_read`. It reads a single public text resource when the user asks for
information available at a specific URL. It is not a browser, search engine,
downloader, or general HTTP client.

The capability was chosen because it proves the network security boundary with
one useful read-only operation while keeping the method, content type,
destination policy, and resource limits fixed.

## Runtime flow

```text
LLM proposes network_read + {"url": "..."}
  -> exact trusted dispatcher lookup
  -> exact argument validation
  -> trusted DANGEROUS risk classification
  -> exact CLI/application approval
  -> HTTPS policy and public-address checks
  -> bounded GET
  -> ToolResult containing untrusted text
  -> final LLM response
```

The model cannot supply approval, risk, headers, cookies, credentials, proxy
settings, redirects, timeouts, or response limits. Every fetch attempt ends
with a `fetch` receipt — `verified` with the fetched byte count on success,
`failed` (transport, status, content, or size) or `invalid` (blocked
destination) on failure — recorded with the validated URL in the durable
action history and shown in the desktop History tab.

## URL and network policy

The tool accepts exactly one `url` string. It requires HTTPS, a hostname, and
the default port. It rejects credentials/userinfo, query strings, fragments,
empty or overlong URLs, and malformed arguments.

Before connecting, trusted code rejects localhost names and every resolved
address that is loopback, private, link-local, unspecified, multicast,
reserved, or otherwise non-global. All DNS results must be public; a mixed
public/private answer fails closed. The connection is then made directly to a
validated numeric address and the actual connected peer is checked again.
This prevents the model, URL, DNS result, or ambient proxy configuration from
silently widening the destination.

The request is a fixed HTTPS GET with no redirects, no request body, and fixed
`Accept`, `Accept-Encoding`, and non-secret `User-Agent` headers. There are no
retries or background requests. Connect, read, and total deadlines are fixed
in the tool.

## Response policy

Only successful responses with `text/plain` content are accepted. UTF-8 is
decoded strictly. The body is bounded to 1 MiB while reading, and an advertised
larger `Content-Length` is rejected before the body is consumed. HTML, JSON,
binary data, invalid UTF-8, redirects, oversized responses, and transport
failures produce deterministic failed `ToolResult` values.

Fetched content is untrusted data. It can contain misleading text or prompt
injection, so the final response path must treat it as an observation, not an
instruction or authorization. It is not written to memory automatically.

## Approval and security boundary

`network_read` is classified as `RiskLevel.DANGEROUS`. This is intentionally
conservative: even a read-only request creates an outbound connection, can
expose the user's network identity, and can be abused as an SSRF primitive.
The existing application-owned approval callback must approve the exact URL
after argument validation and before the connection is attempted.

This does not provide an egress allowlist, authentication, identity, a proxy
policy, a sandbox, secret scanning, or a general permission system. A user
authorized public network resource is still trusted input only within the
narrow limits described here.

## Validation

Unit and integration tests cover URL shape, exact arguments, local/private
destinations, bounded text responses, unsupported content, invalid UTF-8,
redirects, approval-before-connection, connected-peer validation, and final
response handoff. Real OpenAI validation is performed separately with a
configured model and a harmless public `text/plain` URL; no API credentials are
recorded in this document.

The initial live check used `gpt-5.6` with the public RFC 9110 plain-text
resource. The model selected `network_read`, the CLI requested explicit
approval, and the final response reproduced the first sentence returned by
the tool. A second live prompt for a localhost URL did not execute a network
request; the model declined the private destination, and the trusted runtime
also rejects localhost regardless of model output.

## Intentionally not implemented

Stella does not support HTTP, redirects, arbitrary headers, cookies, URL
queries, credentials, POST or other methods, crawling, file downloads,
retries, caching, proxy discovery, or unrestricted egress. Search and
HTML-page reading exist separately in the opt-in web capability
(`docs/WEB.md`); `network_read` itself stays exactly this narrow.
