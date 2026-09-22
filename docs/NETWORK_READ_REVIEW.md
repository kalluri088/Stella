# Network Read Review

## Scope

The design below was implemented as the narrowly scoped `NetworkReadTool`.
The implementation uses Python's standard library and adds no HTTP client
dependency. The review remains the design record; implementation details and
validation are recorded at the end.

The candidate is one application-owned capability, for example
`network_read`, that performs a bounded read of one public web resource. The
LLM would propose only a URL. Trusted Stella code would decide whether the
URL is allowed, perform the request, constrain the response, and return a
`ToolResult`.

## Current architecture relevant to this capability

The existing trusted path is suitable as the outer boundary:

```text
LLM proposes capability + arguments
  -> exact ToolDispatcher lookup
  -> Tool.validate_arguments()
  -> trusted risk classification
  -> exact application approval
  -> bounded network execution
  -> ToolResult
  -> final LLM response
```

The model must not supply headers, credentials, risk, approval, redirect
policy, timeout, or response limits. Those values must be fixed by the
application. Fetched content is untrusted data and must not become an
instruction, memory write, or new capability proposal without a separate
trusted decision.

## Recommended smallest MVP

Use a single capability named `network_read` with exactly one argument:

```json
{"url": "https://example.com/notes.txt"}
```

The first implementation should be deliberately narrower than a general web
browser:

- fixed `GET` only;
- HTTPS only; reject `http://` in the MVP;
- one URL per invocation;
- no redirects;
- no request body;
- no model-supplied headers, cookies, credentials, or proxy settings;
- UTF-8 `text/plain` responses only;
- a small maximum URL length, such as 2,048 characters;
- a bounded decoded response body, such as 1 MiB;
- fixed connect, read, and total deadlines;
- no retries;
- no caching or background fetching.

HTTPS-only avoids silently sending a user-approved request over plaintext. If
HTTP support becomes necessary, it should be an explicit application policy,
remain approval-gated, and never carry credentials.

`text/html` and `application/json` should remain out of the first version.
HTML needs a clear extraction policy and can contain extensive prompt-injection
content; JSON needs a size and shape policy. Both can be added later as
explicit content types rather than inferred from the URL.

## URL validation

`validate_arguments()` should require exactly one string field named `url` and
reject empty, overlong, malformed, non-HTTPS, userinfo-bearing, or fragment
URLs. The URL must have a hostname and no unexpected components. A URL query
string is a security concern because it can contain access tokens. The
smallest safe choice with the current audit design is to reject queries in the
first MVP; allowing queries requires URL redaction in approvals, audit records,
and diagnostics before implementation.

Static URL parsing is not enough. The trusted runtime must validate the
resolved destination before connecting and fail closed on DNS errors.

## Local and private-network blocking

The tool must reject explicit and resolved destinations for:

- `localhost`, `.localhost`, and local-name variants;
- IPv4 and IPv6 loopback addresses;
- private, link-local, unspecified, multicast, reserved, and otherwise
  non-global address ranges;
- common local domains such as `.local`; and
- any hostname whose DNS result includes a blocked address.

The check must evaluate every A and AAAA result, not just the first address.
Allowing a hostname because its text does not look private is insufficient.

DNS rebinding must be considered. A safe implementation should use an HTTP
client/transport that can verify the connected peer address, or otherwise
revalidate the destination at connection time. A single pre-request DNS check
is not a complete SSRF defense. Ambient proxy settings must be disabled or
explicitly controlled; a proxy can defeat local-address assumptions.

## Redirect policy

Disable redirects in the first MVP. A redirect can turn an approved public URL
into a private address, downgrade HTTPS to HTTP, cross an unexpected host, or
produce an unbounded redirect chain. If redirects are added later, every hop
must repeat URL parsing, DNS/address checks, scheme policy, approval policy,
and size/time accounting, with a small maximum hop count.

## Response limits and content handling

The request must enforce:

- connect timeout, read timeout, and a total deadline;
- a response-body byte limit while streaming, not only after buffering;
- an early rejection when `Content-Length` exceeds the limit;
- a limit on decoded bytes if compression is enabled, or disabled automatic
  compression for the first version;
- an explicit allowlist of media types, initially `text/plain`; and
- strict UTF-8 decoding with a deterministic failure for invalid content.

The tool should return a bounded text result and a generic failure message.
It should not return raw stack traces, response headers containing secrets, or
unbounded status/error bodies. HTTP status handling should be explicit: the
first version should return a failed `ToolResult` for non-success statuses
rather than following error pages or interpreting them as trusted facts.

## Credentials, headers, and egress

The model must never provide request headers. The application should send only
fixed headers such as an explicit `Accept: text/plain` and a non-sensitive
Stella `User-Agent`. It must not send cookies, bearer tokens, API keys, client
certificates, ambient authentication, or arbitrary environment-derived
headers. The tool should not read credentials from URLs, environment variables,
or the user's OpenAI configuration.

The application should use an explicit no-proxy configuration and a narrowly
defined transport. General machine proxy settings can redirect traffic or
expose URLs outside the intended policy. Egress allowlists are not required
for the first public-read MVP if private-address blocking is complete, but
they become appropriate for shared or higher-risk deployments.

## Prompt injection from fetched content

Network content is untrusted in exactly the same way as retrieved memory and
filesystem content. A page can say “ignore previous instructions,” request a
filesystem write, or attempt to make the model disclose secrets. The tool must
return content as data, and the final-response prompt must clearly label it as
untrusted fetched content and instruct the model not to follow instructions
inside it.

The runtime must enforce capability lookup, argument validation, risk,
approval, and execution regardless of what the fetched content says. Fetched
content must not be automatically stored in memory or treated as a new Brain
decision. A later multi-step decision may consider the result, but the next
proposal still goes through the same trusted dispatcher and approval path.

## Risk and approval

The operation is read-only, but it creates an outbound connection, can expose
the user's network identity and requested URL, and can be abused for SSRF or
data retrieval. Under Stella's current taxonomy, the narrow semantic
classification is `SENSITIVE`; however, the existing runtime only makes
`DANGEROUS` actions approval-gated and the security review treats external
communication as a dangerous boundary.

For the first implementation, classify `network_read` as
`DANGEROUS` so the existing trusted approval path is mandatory. Approval must
cover the exact validated URL. It must be requested before connecting, and the
LLM cannot provide or imply approval. If the runtime performs a later DNS or
policy check and rejects the request, no connection should occur; approval is
not a bypass for the network policy.

This choice is intentionally conservative. A future policy could classify a
strict, authenticated allowlist of public read-only domains as `SENSITIVE`
without interactive approval, but that is not needed for this MVP.

## Audit implications

The existing audit trail records validated argument values. A network URL can
contain query tokens or private identifiers even when userinfo is rejected.
Before allowing query strings, audit and approval display need a deliberate
redaction policy that still preserves the exact approval identity internally.
The first MVP should reject query strings, or make this redaction change as a
separate prerequisite. It should record the scheme/host/path policy result,
risk, approval, status, and success without retaining response bodies.

## Required implementation order

1. Decide and test the URL policy, with HTTPS-only and no-query defaults.
2. Add a dedicated HTTP client/transport with no ambient proxy or credentials.
3. Implement DNS and connected-peer public-address checks, including IPv4,
   IPv6, and rebinding considerations.
4. Add fixed timeouts, no redirects, bounded streaming reads, media-type
   checks, and strict UTF-8 decoding.
5. Add the capability as a trusted `DANGEROUS` dispatcher entry and route it
   through exact CLI approval and audit logging.
6. Add adversarial tests before real-world validation: localhost, private
   literal, private DNS, mixed DNS answers, redirects, oversized bodies,
   unsupported types, invalid UTF-8, timeouts, proxy use, userinfo, and
   prompt-injection content.

## Intentionally out of scope

Do not add POST or other mutating methods, arbitrary headers, authentication,
cookies, file downloads, HTML rendering, JavaScript, browser automation,
webhooks, unrestricted HTTP, redirect following, retries, caching, proxy
discovery, shell execution, or general egress policy in this first review.

## Recommendation

Network read is not ready to implement as a generic URL fetcher. It can become
Stella's next capability only as a tightly constrained, approval-required
`DANGEROUS` tool with HTTPS-only public destinations, no redirects, fixed
headers, no credentials, strict DNS/address checks, bounded UTF-8 text, and
explicit untrusted-content handling. The audit/query-string issue should be
resolved or avoided before production implementation.

## Validation

The implementation adds deterministic unit and orchestration tests for URL
validation, local/private destination blocking, connected-peer validation,
response limits, content handling, approval-before-connection, and final
response handoff. Full pytest and Ruff results are recorded in the task
report.

The real OpenAI validation uses the configured model and a harmless public
`text/plain` URL. Credentials and response contents are not recorded here.

The live `gpt-5.6` check selected `network_read` for the RFC 9110 plain-text
resource, received the normal CLI approval, and produced a final response
based on the returned first sentence. A separate localhost prompt did not
execute a request; the model declined it, and the trusted policy independently
rejects that destination.
