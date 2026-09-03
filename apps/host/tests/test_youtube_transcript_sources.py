"""Tests for fixed transcript-provider composition policy."""

from snektest import assert_eq, assert_is_none, test

from tether.youtube.transcript_sources import (
    SupadataSourceConfig,
    TranscriptLibrarySourceConfig,
    TranscriptProviderConfig,
    build_configured_transcript_provider,
)


@test()
def disabled_sources_produce_no_transcript_provider() -> None:
    """Disabling both fixed sources leaves transcript acquisition unwired."""
    provider = build_configured_transcript_provider(
        TranscriptProviderConfig(
            library=TranscriptLibrarySourceConfig(enabled=False),
            supadata=SupadataSourceConfig(enabled=False),
        )
    )

    assert_is_none(provider)


@test()
def library_source_needs_no_youtube_oauth_configuration() -> None:
    """Provider configuration contains no YouTube Data API credential coupling."""
    provider = build_configured_transcript_provider(TranscriptProviderConfig())
    assert provider is not None

    assert_eq(
        [source.source for source in provider.sources],
        ["youtube_transcript_api"],
    )


@test()
def supadata_requires_explicit_enablement() -> None:
    """An API key alone does not activate paid transcript acquisition."""
    provider = build_configured_transcript_provider(
        TranscriptProviderConfig(
            supadata=SupadataSourceConfig(api_key="sk-secret", enabled=False)
        )
    )
    assert provider is not None

    assert_eq(
        [source.source for source in provider.sources],
        ["youtube_transcript_api"],
    )


@test()
def supadata_requires_an_api_key() -> None:
    """Enabling Supadata without credentials keeps the free source only."""
    provider = build_configured_transcript_provider(
        TranscriptProviderConfig(
            supadata=SupadataSourceConfig(api_key="", enabled=True)
        )
    )
    assert provider is not None

    assert_eq(
        [source.source for source in provider.sources],
        ["youtube_transcript_api"],
    )


@test()
async def supadata_precedes_the_transcript_library() -> None:
    """The paid source is tried before the fixed library fallback."""
    provider = build_configured_transcript_provider(
        TranscriptProviderConfig(
            supadata=SupadataSourceConfig(api_key="sk-secret", enabled=True)
        )
    )
    assert provider is not None

    assert_eq(
        [source.source for source in provider.sources],
        ["supadata", "youtube_transcript_api"],
    )
    await provider.aclose()
