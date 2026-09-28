"""Web tools: fake-backend tests, no network, no TinyFish key needed."""

import datetime as dt

from stella.app import StellaSettings
from stella.tools import ApprovalRequest, RiskLevel, action_summary
from stella.web_tools import (
    CONTENT_CLOSE,
    CONTENT_OPEN,
    FETCH_ENDPOINT,
    MAX_PAGE_CHARS,
    SEARCH_ENDPOINT,
    WebBudget,
    WebClient,
    WebFetchTool,
    WebSearchTool,
    build_web_tools,
    web_tool_summaries,
)

SEARCH_RESULTS = {
    "results": [
        {
            "title": "First hit",
            "url": "https://a.example/page",
            "snippet": "about the query",
            "published_date": "2026-09-01",
        },
        {
            "title": "Second hit",
            "link": "https://b.example/other",
            "description": "alternate field shape",
        },
    ]
}


class FakeTransport:
    """Queued (status, payload) answers; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.responses.pop(0)


def tinyfish_client(*responses, budget=None, **kwargs):
    return WebClient(
        api_key="test-key",
        transport=FakeTransport(*responses),
        budget=budget or WebBudget(),
        **kwargs,
    )


def keyless_client(ddg=None, fetch_page=None, budget=None):
    return WebClient(
        api_key=None,
        transport=FakeTransport(),  # must never be touched keyless
        ddg_search=ddg,
        fetch_page=fetch_page,
        budget=budget or WebBudget(),
    )


class SteadyClock:
    """Mutable now() so bucket rollover is testable without sleeping."""

    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


# --------------------------------------------------------------------------
# budget (rule 3: runtime owns the quota, never the model)
# --------------------------------------------------------------------------


def test_search_budget_refuses_after_the_hour_cap():
    clock = SteadyClock(dt.datetime(2026, 9, 28, 10, 0, 0, tzinfo=dt.UTC))
    budget = WebBudget(searches_per_hour=2, clock=clock)
    assert budget.allow_search() and budget.allow_search()
    assert not budget.allow_search()
    assert budget.searches_remaining == 0
    clock.moment += dt.timedelta(hours=1)
    assert budget.allow_search()  # the bucket rolled over


def test_fetch_budget_counts_urls_and_rolls_daily():
    clock = SteadyClock(dt.datetime(2026, 9, 28, 23, 59, 0, tzinfo=dt.UTC))
    budget = WebBudget(fetch_urls_per_day=2, clock=clock)
    assert budget.charge_fetch(1) and budget.charge_fetch(1)
    assert not budget.charge_fetch(1)
    clock.moment += dt.timedelta(minutes=2)
    assert budget.charge_fetch(2)  # new day bucket


def test_search_denied_once_budget_is_spent_with_a_clear_message():
    budget = WebBudget(searches_per_hour=1)
    tool = WebSearchTool(tinyfish_client((200, SEARCH_RESULTS), budget=budget))
    assert tool.execute({"query": "first"}).success
    denied = tool.execute({"query": "second"})
    assert not denied.success
    assert "hourly search budget" in denied.output
    # the refusal never reached the provider
    assert len(tool._client.transport.calls) == 1


def test_fetch_denied_once_budget_is_spent():
    budget = WebBudget(fetch_urls_per_day=1)
    tool = WebFetchTool(
        tinyfish_client((200, {"results": [{"text": "hi"}]}), budget=budget)
    )
    assert tool.execute({"url": "https://a.example/"}).success
    denied = tool.execute({"url": "https://b.example/"})
    assert not denied.success
    assert "daily fetch budget" in denied.output


# --------------------------------------------------------------------------
# tinyfish search: request building and response parsing
# --------------------------------------------------------------------------


def test_tinyfish_search_builds_the_verified_request_shape():
    transport = FakeTransport((200, SEARCH_RESULTS))
    client = WebClient(api_key="k", transport=transport)
    tool = WebSearchTool(client)
    result = tool.execute(
        {
            "query": "rust async",
            "recency_minutes": 60,
            "domain_type": "news",
            "max": 2,
        }
    )
    method, url, headers, body = transport.calls[0]
    assert (method, body) == ("GET", None)
    assert url.startswith(SEARCH_ENDPOINT + "?")
    assert "query=rust+async" in url
    assert "recency_minutes=60" in url
    assert "domain_type=news" in url
    assert "limit" not in url  # only live-verified params are sent
    assert headers["X-API-Key"] == "k"
    assert result.success
    assert "First hit — https://a.example/page [2026-09-01]" in result.output
    # the alternate field shapes (link/description) are mapped too
    assert "Second hit — https://b.example/other" in result.output
    assert "alternate field shape" in result.output


def test_tinyfish_search_defaults_domain_web_and_keeps_filters_out():
    transport = FakeTransport((200, {"results": []}))
    tool = WebSearchTool(WebClient(api_key="k", transport=transport))
    result = tool.execute({"query": "x", "domain_type": "web"})
    assert "domain_type" not in transport.calls[0][1]
    assert result.success and "no results" in result.output


def test_tinyfish_search_surfaces_provider_error_codes():
    tool = WebSearchTool(tinyfish_client((429, {"error": {"code": "RATE"}})))
    result = tool.execute({"query": "x"})
    assert not result.success
    assert "RATE" in result.output


# --------------------------------------------------------------------------
# keyless search: ddg fallback, ignored filters, NO_BACKEND
# --------------------------------------------------------------------------


def test_keyless_search_uses_ddg_and_reports_ignored_filters():
    rows = [
        {"title": "D", "url": "https://d.example", "snippet": "s", "date": ""}
    ]
    tool = WebSearchTool(keyless_client(ddg=lambda q, m: rows))
    result = tool.execute({"query": "x", "recency_minutes": 30})
    assert result.success
    assert "via ddg" in result.output
    assert "filters not supported here: recency_minutes" in result.output


def test_keyless_search_without_any_backend_says_web_is_off():
    tool = WebSearchTool(keyless_client(ddg=lambda q, m: None))
    result = tool.execute({"query": "x"})
    assert not result.success
    assert "web is off" in result.output


# --------------------------------------------------------------------------
# tinyfish fetch: POST shape, text/content, error entries
# --------------------------------------------------------------------------


def test_tinyfish_fetch_posts_the_url_list_and_reads_text():
    transport = FakeTransport(
        (200, {"results": [{"title": "T", "text": "clean markdown"}]})
    )
    tool = WebFetchTool(WebClient(api_key="k", transport=transport))
    result = tool.execute({"url": "https://a.example/article?x=1"})
    method, url, headers, body = transport.calls[0]
    assert (method, url) == ("POST", FETCH_ENDPOINT)
    assert body == {"urls": ["https://a.example/article?x=1"]}
    assert headers["X-API-Key"] == "k"
    assert result.success
    assert f"{CONTENT_OPEN}\nclean markdown\n{CONTENT_CLOSE}" in result.output
    assert "(T)" in result.output


def test_tinyfish_fetch_falls_back_to_content_and_reports_page_errors():
    transport = FakeTransport(
        (200, {"results": [{"content": "alt key"}]}),
        (200, {"errors": ["invalid_url"]}),
    )
    tool = WebFetchTool(WebClient(api_key="k", transport=transport))
    assert "alt key" in tool.execute({"url": "https://a.example/"}).output
    failed = tool.execute({"url": "https://dead.example/"})
    assert not failed.success and "invalid_url" in failed.output


# --------------------------------------------------------------------------
# keyless fetch: injected seam, markers, cap, http status
# --------------------------------------------------------------------------


def test_keyless_fetch_wraps_bounded_text_in_untrusted_markers():
    page = "P" * (MAX_PAGE_CHARS + 500)
    tool = WebFetchTool(
        keyless_client(fetch_page=lambda u: (200, "Title", page))
    )
    result = tool.execute({"url": "https://a.example/"})
    assert result.success
    assert CONTENT_OPEN in result.output and CONTENT_CLOSE in result.output
    assert result.output.count("P") == MAX_PAGE_CHARS
    assert "via stdlib" in result.output


def test_keyless_fetch_reports_non_2xx_as_failure():
    tool = WebFetchTool(
        keyless_client(fetch_page=lambda u: (404, "", "missing"))
    )
    result = tool.execute({"url": "https://a.example/nope"})
    assert not result.success
    assert "HTTP_404" in result.output


def test_keyless_fetch_refuses_when_the_fetcher_returns_none():
    tool = WebFetchTool(keyless_client(fetch_page=lambda u: None))
    result = tool.execute({"url": "https://a.example/"})
    assert not result.success
    assert "UNSAFE_URL" in result.output


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------


def test_search_validator_bounds():
    tool = WebSearchTool(tinyfish_client((200, {"results": []})))
    assert tool.validate_arguments({"query": "ok"})
    assert tool.validate_arguments({"query": "x", "max": 10})
    assert not tool.validate_arguments({})  # query missing
    assert not tool.validate_arguments({"query": "   "})
    assert not tool.validate_arguments({"query": "x", "max": 0})
    assert not tool.validate_arguments({"query": "x", "max": 11})
    assert not tool.validate_arguments({"query": "x", "max": True})
    assert not tool.validate_arguments({"query": "x", "max": "5"})
    assert not tool.validate_arguments({"query": "x", "extra": 1})
    assert not tool.validate_arguments({"query": "x", "domain_type": "spam"})
    assert not tool.validate_arguments({"query": "x", "recency_minutes": 0})
    assert not tool.validate_arguments({"query": "x" * 500})


def test_fetch_validator_requires_clean_public_https():
    tool = WebFetchTool(keyless_client(fetch_page=lambda u: (200, "", "x")))
    assert tool.validate_arguments({"url": "https://example.com/a?b=1#f"})
    assert tool.validate_arguments({"url": "https://example.com:443/"})
    assert not tool.validate_arguments({"url": "http://example.com/"})
    assert not tool.validate_arguments({"url": "https://user:pw@example.com/"})
    assert not tool.validate_arguments({"url": "https://example.com:8443/"})
    assert not tool.validate_arguments({"url": "https://example.com/ünïcode"})
    assert not tool.validate_arguments({"url": "https://example.com/a b"})
    assert not tool.validate_arguments({"url": " https://example.com/"})
    assert not tool.validate_arguments({"url": ""})
    assert not tool.validate_arguments({"url": "https://example.com/" + "a" * 2100})
    # localhost-style names are refused outright (the Outline app lives there)
    assert not tool.validate_arguments({"url": "https://localhost/"})
    assert not tool.validate_arguments({"url": "https://x.local/"})
    assert not tool.validate_arguments({"url": "https://a.b.localhost/"})
    # dotted-quad literals are screened now; hostnames resolve-time checked
    assert not tool.validate_arguments({"url": "https://127.0.0.1/"})
    assert not tool.validate_arguments({"url": "https://10.1.2.3/"})
    assert not tool.validate_arguments({"url": "https://192.168.0.1/"})
    assert not tool.validate_arguments({"url": "https://169.254.1.1/"})
    assert tool.validate_arguments({"url": "https://93.184.216.34/"})


def test_malformed_arguments_never_reach_the_backend():
    tool = WebSearchTool(tinyfish_client((200, {"results": []})))
    result = tool.execute({"query": ""})
    assert not result.success and result.output == "Invalid tool arguments."
    assert tool._client.transport.calls == []


def test_stdlib_fetcher_stops_bad_urls_before_any_dns():
    from stella.web_tools import _stdlib_fetch_page

    assert _stdlib_fetch_page("http://example.com/") is None
    assert _stdlib_fetch_page("https://user:pw@example.com/") is None


# --------------------------------------------------------------------------
# risk, previews, summaries
# --------------------------------------------------------------------------


def test_both_web_tools_are_dangerous():
    client = tinyfish_client((200, {"results": []}))
    assert WebSearchTool(client).risk_level is RiskLevel.DANGEROUS
    assert WebFetchTool(client).risk_level is RiskLevel.DANGEROUS


def test_previews_name_the_backend_and_budget():
    with_key = WebSearchTool(tinyfish_client((200, {"results": []})))
    lines = "\n".join(
        with_key.preview(ApprovalRequest("web_search", {"query": "x"})).detail_lines
    )
    assert "TinyFish" in lines and "commercial decision" in lines
    assert "budget left this hour" in lines

    keyless = WebFetchTool(keyless_client(fetch_page=lambda u: (200, "", "")))
    lines = "\n".join(
        keyless.preview(
            ApprovalRequest("web_fetch", {"url": "https://a.example/"})
        ).detail_lines
    )
    assert "this machine directly" in lines and "pinned-DNS" in lines
    assert "budget left today" in lines


def test_summaries_name_the_receiving_third_party():
    assert web_tool_summaries(
        "web_search", {"query": "weather"}, key_present=True
    ) == 'search the web for "weather" via TinyFish (api.tinyfish.ai)'
    assert "DuckDuckGo" in web_tool_summaries(
        "web_search", {"query": "weather"}, key_present=False
    )
    assert "this machine" in web_tool_summaries(
        "web_fetch", {"url": "https://a.example/"}, key_present=False
    )
    assert "TinyFish" in web_tool_summaries(
        "web_fetch", {"url": "https://a.example/"}, key_present=True
    )
    assert web_tool_summaries("datetime", {}, key_present=False) is None
    assert web_tool_summaries("web_search", {}, key_present=False) is None


def test_action_summary_routes_web_through_the_chain(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    summary = action_summary(
        ApprovalRequest("web_search", {"query": "rust async"})
    )
    assert summary == (
        'search the web for "rust async" via DuckDuckGo (keyless fallback)'
    )


# --------------------------------------------------------------------------
# build + settings wiring
# --------------------------------------------------------------------------


def test_build_web_tools_returns_both_tools_keyed_off_the_env():
    with_key = build_web_tools({"TINYFISH_API_KEY": "k"})
    assert [t.name for t in with_key] == ["web_search", "web_fetch"]
    assert with_key[0]._client.tinyfish()
    empty = build_web_tools({})
    assert not empty[0]._client.tinyfish()
    # an empty-string key is not a key
    blank = build_web_tools({"TINYFISH_API_KEY": ""})
    assert not blank[0]._client.tinyfish()


def test_default_environment_leaves_web_tools_off():
    settings = StellaSettings(provider="ollama", model="x")
    assert settings.web_tools_enabled is False


def test_saved_web_flag_respects_env_override(monkeypatch):
    monkeypatch.setenv("STELLA_WEB", "1")
    settings = StellaSettings.from_saved(
        provider="ollama", model="x", web_tools_enabled=False
    )
    assert settings.web_tools_enabled is True
    monkeypatch.setenv("STELLA_WEB", "0")
    settings = StellaSettings.from_saved(
        provider="ollama", model="x", web_tools_enabled=True
    )
    assert settings.web_tools_enabled is False
