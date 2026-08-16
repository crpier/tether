"""Behavior tests for the Tavily-backed web search provider."""

from __future__ import annotations

from snekok import Err, Ok
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_is_none, assert_true, fail, test

from tether.search_spend import PersistentSearchSpendGuard
from tether.server import HostSettings, _build_search_provider
from tether.tavily_search import (
    TavilyResponse,
    TavilySearchProvider,
    TavilySearchRequest,
)
from tether.web_search import (
    SearchBudgetExhaustedFailure,
    SearchDepth,
    SearchUpstreamFailure,
)
from tether.youtube_store import create_youtube_schema


class FakeMonotonicClock:
    """A pacing clock whose sleep advances monotonic time."""

    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTavilyTransport:
    """A Tavily transport returning one scripted provider response."""

    def __init__(self, response: TavilyResponse) -> None:
        self._response: TavilyResponse = response
        self.requests: list[TavilySearchRequest] = []

    async def search(self, request: TavilySearchRequest) -> TavilyResponse:
        self.requests.append(request)
        return self._response


@test()
def search_provider_is_omitted_when_the_feature_flag_is_off() -> None:
    """A configured API key cannot spend while web search remains disabled."""
    settings = HostSettings(
        app_password="test-app-password",
        search_api_key="tvly-secret",
        search_enabled=False,
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
    )

    provider = _build_search_provider(settings)

    assert_is_none(provider)


@test()
def search_provider_is_built_when_the_flag_and_key_are_set() -> None:
    """Enabling with credentials constructs the Tavily adapter."""
    settings = HostSettings(
        app_password="test-app-password",
        search_api_key="tvly-secret",
        search_enabled=True,
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
    )

    provider = _build_search_provider(settings)

    assert_true(isinstance(provider, TavilySearchProvider))


@test()
async def tavily_results_are_normalized_for_the_agent() -> None:
    """Provider-specific result fields become Tether's stable search result shape."""
    transport = FakeTavilyTransport(
        TavilyResponse(
            status_code=200,
            payload={
                "answer": "ignored synthesis",
                "results": [
                    {
                        "title": "Tavily docs",
                        "url": "https://docs.tavily.com/",
                        "content": "Search API documentation.",
                        "raw_content": "# Search API\nFull extracted page.",
                    },
                    {
                        "title": "Example",
                        "url": "https://example.com/",
                        "content": "A snippet only.",
                        "raw_content": None,
                    },
                ],
            },
        )
    )

    outcome = await TavilySearchProvider(transport).search(
        "tavily API", max_results=2, search_depth=SearchDepth.BASIC
    )

    match outcome:
        case Ok(response):
            assert_eq(response.results[0].title, "Tavily docs")
            assert_eq(response.results[0].url, "https://docs.tavily.com/")
            assert_eq(response.results[0].snippet, "Search API documentation.")
            assert_eq(
                response.results[0].extracted_content,
                "# Search API\nFull extracted page.",
            )
            assert_is_none(response.results[1].extracted_content)
        case Err(error):
            fail(f"unexpected failure: {error}")


@test()
async def tavily_error_response_is_a_typed_upstream_failure() -> None:
    """Provider HTTP failures do not masquerade as empty successful searches."""
    transport = FakeTavilyTransport(
        TavilyResponse(status_code=429, payload={"detail": "rate limited"})
    )

    outcome = await TavilySearchProvider(transport).search(
        "latest news", max_results=5, search_depth=SearchDepth.BASIC
    )

    match outcome:
        case Err(SearchUpstreamFailure(status_code=status_code)):
            assert_eq(status_code, 429)
        case Ok(response):
            fail(f"unexpected success: {response}")
        case Err(error):
            fail(f"unexpected failure: {error}")


@test()
async def tavily_request_asks_for_raw_content_without_a_synthesized_answer() -> None:
    """The adapter requests extractable pages but never Tavily's generated answer."""
    transport = FakeTavilyTransport(TavilyResponse(status_code=200, payload={}))

    _ = await TavilySearchProvider(transport).search(
        "async Python", max_results=7, search_depth=SearchDepth.ADVANCED
    )

    assert_eq(len(transport.requests), 1)
    assert_eq(transport.requests[0].query, "async Python")
    assert_eq(transport.requests[0].max_results, 7)
    assert_eq(transport.requests[0].search_depth, SearchDepth.ADVANCED)
    assert_eq(transport.requests[0].include_raw_content, True)
    assert_eq(transport.requests[0].include_answer, False)


@test()
async def consecutive_searches_are_paced_before_the_second_request() -> None:
    """Configured minimum request spacing delays a back-to-back Tavily call."""
    clock = FakeMonotonicClock()
    transport = FakeTavilyTransport(TavilyResponse(status_code=200, payload={}))
    provider = TavilySearchProvider(
        transport,
        min_request_interval_seconds=1.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    _ = await provider.search("first", max_results=1, search_depth=SearchDepth.BASIC)
    _ = await provider.search("second", max_results=1, search_depth=SearchDepth.BASIC)

    assert_eq(clock.sleeps, [1.5])


@test()
async def persisted_credit_cap_blocks_before_an_upstream_call() -> None:
    """An advanced search spends two credits; another cannot cross the monthly cap."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(database)
    transport = FakeTavilyTransport(TavilyResponse(status_code=200, payload={}))
    provider = TavilySearchProvider(
        transport,
        spend_guard=PersistentSearchSpendGuard(database, max_uses=2),
    )
    _ = await provider.search("first", max_results=1, search_depth=SearchDepth.ADVANCED)

    outcome = await provider.search(
        "blocked", max_results=1, search_depth=SearchDepth.BASIC
    )

    match outcome:
        case Err(SearchBudgetExhaustedFailure(used=used, limit=limit)):
            assert_eq(used, 2)
            assert_eq(limit, 2)
        case Ok(response):
            fail(f"unexpected success: {response}")
        case Err(error):
            fail(f"unexpected failure: {error}")
    assert_eq(len(transport.requests), 1)
    await database.close()
