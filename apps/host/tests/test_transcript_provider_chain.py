"""Behavior tests for fixed transcript-source composition."""

from collections.abc import Sequence

from snekok.result import Err, Ok
from snektest import assert_eq, assert_isinstance, assert_true, test

from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptBlockedFailure,
    TranscriptDeferredFailure,
    TranscriptFetchPolicy,
    TranscriptFetchResult,
    TranscriptProviderChain,
    TranscriptUnavailableFailure,
)


class ScriptedSource:
    """Transcript source returning configured outcomes in call order."""

    def __init__(
        self,
        source: str,
        outcomes: Sequence[TranscriptFetchResult],
    ) -> None:
        self._outcomes: list[TranscriptFetchResult] = list(outcomes)
        self.calls: int = 0
        self.source: str = source

    async def fetch(self, video_id: str) -> TranscriptFetchResult:
        _ = video_id
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return outcome


class ClosableSource(ScriptedSource):
    """Source recording resource closure."""

    def __init__(self, source: str) -> None:
        super().__init__(
            source,
            [Err(TranscriptUnavailableFailure(locator="video"))],
        )
        self.closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


@test()
async def unavailable_primary_falls_through_in_fixed_order() -> None:
    """The next source runs only after permanent unavailability."""
    primary = ScriptedSource(
        "supadata",
        [Err(TranscriptUnavailableFailure(locator="video"))],
    )
    library = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    chain = TranscriptProviderChain([primary, library])

    transcript = (await chain.fetch("video")).unwrap()

    assert_eq(transcript.text, "hello")
    assert_eq(primary.calls, 1)
    assert_eq(library.calls, 1)


@test()
async def upstream_block_does_not_fall_through() -> None:
    """A real source block surfaces so provider health can pause that source."""
    primary = ScriptedSource(
        "supadata",
        [
            Err(
                TranscriptBlockedFailure(
                    message="blocked",
                    source="supadata",
                )
            )
        ],
    )
    library = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    chain = TranscriptProviderChain([primary, library])

    outcome = await chain.fetch("video")

    failure = assert_isinstance(outcome, Err).error
    _ = assert_isinstance(failure, TranscriptBlockedFailure)
    assert_eq(library.calls, 0)


@test()
async def paused_source_is_deferred_after_reachable_sources_fail() -> None:
    """An untried paused source keeps absence unsettled for a later pass."""
    primary = ScriptedSource(
        "supadata",
        [Err(TranscriptUnavailableFailure(locator="video"))],
    )
    library = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="unused"))],
    )
    chain = TranscriptProviderChain([primary, library])

    outcome = await chain.fetch(
        "video",
        policy=TranscriptFetchPolicy(
            deferred_sources=frozenset({"youtube_transcript_api"})
        ),
    )

    failure = assert_isinstance(outcome, Err).error
    _ = assert_isinstance(failure, TranscriptDeferredFailure)
    assert_eq(library.calls, 0)


@test()
async def local_request_limit_defers_without_provider_block() -> None:
    """A pass safeguard remains distinct from upstream provider health."""
    library = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    chain = TranscriptProviderChain([library])
    policy = TranscriptFetchPolicy(request_limits={"youtube_transcript_api": 1})
    _ = await chain.fetch("first", policy=policy)

    outcome = await chain.fetch("second", policy=policy)

    failure = assert_isinstance(outcome, Err).error
    _ = assert_isinstance(failure, TranscriptDeferredFailure)
    assert_eq(library.calls, 1)


@test()
async def fresh_pass_gets_a_fresh_local_request_limit() -> None:
    """Pass-local accounting never leaks into later or on-demand acquisition."""
    library = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    chain = TranscriptProviderChain([library])
    first_pass = TranscriptFetchPolicy(request_limits={"youtube_transcript_api": 1})
    _ = await chain.fetch("first", policy=first_pass)

    transcript = (
        await chain.fetch(
            "second",
            policy=TranscriptFetchPolicy(request_limits={"youtube_transcript_api": 1}),
        )
    ).unwrap()

    assert_eq(transcript.text, "hello")
    assert_eq(library.calls, 2)


@test()
async def provider_chain_closes_owned_source_resources() -> None:
    """App shutdown can release a Supadata-style async transport through the chain."""
    source = ClosableSource("supadata")
    chain = TranscriptProviderChain([source])

    await chain.aclose()

    assert_true(source.closed)
