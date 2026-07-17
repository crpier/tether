"""Tests for settings -> transcript-provider composition (issue #202).

Moved out of `test_server.py`, which previously reached past `tether.server`'s
own module boundary to import five private composition helpers. They now live
beside the module that owns the composition — `build_configured_transcript_
provider` (the public settings -> provider entry point the composition root
calls) plus its private building blocks, exercised directly here since they are
this module's own internals, not `server.py`'s.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from snektest import assert_eq, assert_raises, assert_true, test

from tether.server import HostSettings
from tether.transcript_library import YouTubeTranscriptApiProvider
from tether.transcript_provider_composition import (
    TranscriptProviderConfigError,
    _build_library_provider,
    _build_supadata_provider,
    _compose_transcript_provider,
    _parse_transcript_languages,
    _parse_transcript_provider_order,
    build_configured_transcript_provider,
)
from tether.transcript_supadata import SupadataTranscriptProvider
from tether.youtube import (
    FallbackTranscriptProvider,
    NullTranscriptProvider,
    TranscriptProvider,
)


def _supadata_settings(*, enabled: bool, api_key: str) -> HostSettings:
    """HostSettings with the always-required secrets plus the Supadata gating."""
    return HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        supadata_enabled=enabled,
        supadata_api_key=api_key,
    )


@test()
def supadata_is_omitted_when_the_flag_is_off() -> None:
    """A configured key with the flag off keeps Supadata out of the chain (no spend)."""
    provider = _build_supadata_provider(
        _supadata_settings(enabled=False, api_key="sk-secret")
    )
    assert_true(provider is None)


@test()
def supadata_is_omitted_when_the_key_is_absent() -> None:
    """The flag on but no key is still a no-op — paid transcription needs credentials."""
    provider = _build_supadata_provider(_supadata_settings(enabled=True, api_key=""))
    assert_true(provider is None)


@test()
def supadata_is_built_when_key_and_flag_are_both_set() -> None:
    """Key + flag together build the Supadata provider for the fallback chain."""
    provider = _build_supadata_provider(
        _supadata_settings(enabled=True, api_key="sk-secret")
    )
    assert_true(isinstance(provider, SupadataTranscriptProvider))


def _library_settings(
    *, enabled: bool = True, max_requests_per_pass: int = 5, min_interval: float = 5.0
) -> HostSettings:
    """HostSettings with the always-required secrets plus the library gating."""
    return HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        transcript_library_enabled=enabled,
        transcript_library_max_requests_per_pass=max_requests_per_pass,
        transcript_library_min_request_interval_seconds=min_interval,
    )


@test()
def library_is_omitted_when_disabled() -> None:
    """`TETHER_TRANSCRIPT_LIBRARY_ENABLED=false` drops it from the chain entirely."""
    provider = _build_library_provider(_library_settings(enabled=False))
    assert_true(provider is None)


@test()
def library_threads_the_per_pass_budget_and_pacing_from_settings() -> None:
    """The per-pass request budget and request spacing reach the real provider
    (issue #179) — the same way Supadata's own pacing is threaded."""
    provider = _build_library_provider(
        _library_settings(max_requests_per_pass=3, min_interval=7.5)
    )
    assert provider is not None
    assert isinstance(provider, YouTubeTranscriptApiProvider)
    assert_eq(provider._budget.max_requests_per_pass, 3)
    assert_eq(provider._budget.min_request_interval, timedelta(seconds=7.5))


def _named_providers() -> dict[str, TranscriptProvider]:
    """A distinct fake provider per known source name, for order assertions."""
    return {
        "supadata": NullTranscriptProvider(),
        "captions": NullTranscriptProvider(),
        "library": NullTranscriptProvider(),
    }


@test()
def the_order_flag_composes_sources_primary_first() -> None:
    """The first named source leads; the rest trail as fallbacks, in order."""
    available = _named_providers()

    provider = _compose_transcript_provider(["supadata", "library"], available)

    assert isinstance(provider, FallbackTranscriptProvider)
    assert_true(provider.leaf_providers()[0] is available["supadata"])
    assert_true(provider.leaf_providers()[1] is available["library"])


@test()
def an_unconfigured_named_source_is_skipped_in_the_order() -> None:
    """A named source absent from the available map drops out of the chain."""
    available = {"library": NullTranscriptProvider()}

    provider = _compose_transcript_provider(["supadata", "library"], available)

    # Supadata is unconfigured, so the single survivor is returned uncomposed.
    assert_true(provider is available["library"])


@test()
def an_unknown_source_name_is_rejected() -> None:
    """A typo'd source name in the order flag fails loudly at wire time."""
    with assert_raises(TranscriptProviderConfigError):
        _ = _compose_transcript_provider(["captoins"], _named_providers())


@test()
def an_order_with_no_configured_source_is_rejected() -> None:
    """An order whose every source is unconfigured leaves nothing to compose."""
    with assert_raises(TranscriptProviderConfigError):
        _ = _compose_transcript_provider(["supadata"], {})


@test()
def the_order_flag_is_parsed_into_normalized_names() -> None:
    """Whitespace and case are normalized; blank entries are dropped."""
    assert_eq(
        _parse_transcript_provider_order(" Supadata , library ,"),
        ["supadata", "library"],
    )


@test()
def the_language_flag_is_parsed_in_preference_order() -> None:
    """Languages keep their order (most preferred first); blanks are dropped."""
    assert_eq(_parse_transcript_languages("en, ro ,"), ("en", "ro"))


@test()
def build_configured_transcript_provider_returns_none_with_no_token() -> None:
    """With no cached OAuth token, the whole chain is absent (nothing to build)."""
    settings = HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        youtube_token_path=Path("/nonexistent/token.json"),
    )

    provider = build_configured_transcript_provider(settings)

    assert_true(provider is None)
