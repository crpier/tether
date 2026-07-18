"""Compose the transcript-provider chain from settings and bind its late budgets.

Provider *selection* (which sources compose the chain, in what order, validated
against typos and unconfigured-everything) and the on-demand wiring path's late
budget binds belong beside the adapters they assemble, not in the ASGI
composition root (`tether.server`) — every new provider or knob otherwise forces
a `server.py` edit (issue #202). `server.py` only calls `build_configured_
transcript_provider` (settings -> provider, or `None` with no OAuth token) and
`resolve_transcript_provider` (the on-demand wiring path: pick a provider and
late-bind its persisted budgets/caps once the database/client exist) and passes
the result to `YouTubeService`/`TranscriptSyncService`.

```python
provider = build_configured_transcript_provider(settings)  # None with no token
```
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from snekql.sqlite import Database

from tether.transcript_library import LibraryPassBudget, YouTubeTranscriptApiProvider
from tether.transcript_supadata import (
    HttpSupadataTransport,
    SupadataConfig,
    SupadataMode,
    SupadataTranscriptProvider,
    bind_supadata_spend_guard,
)
from tether.youtube import (
    FallbackTranscriptProvider,
    InMemoryYouTubeApi,
    NullTranscriptProvider,
    TranscriptProvider,
    YouTubeApi,
    YouTubeApiClient,
)
from tether.youtube_oauth import (
    CaptionsTranscriptProvider,
    OAuthConfig,
    bind_captions_daily_quota,
)

_KNOWN_TRANSCRIPT_SOURCES: frozenset[str] = frozenset(
    {"supadata", "captions", "library"}
)
"""The transcript source names accepted in `TETHER_TRANSCRIPT_PROVIDER_ORDER`."""


class TranscriptProviderConfigError(Exception):
    """The configured transcript provider order is unusable.

    Raised at wire time when `TETHER_TRANSCRIPT_PROVIDER_ORDER` names an unknown
    source, or when every named source is unconfigured so no provider remains to
    compose.

    ```python
    _compose_transcript_provider(["typo"], {})  # raises
    ```
    """


class TranscriptProviderSettings(Protocol):
    """The subset of `HostSettings` provider composition reads.

    A structural `Protocol` (rather than importing `HostSettings` itself) keeps
    this module free of a dependency on `tether.server` — it is `server.py` that
    depends on this module for provider composition, not the reverse.
    """

    youtube_token_path: Path
    youtube_client_secret_path: Path
    youtube_oauth_no_browser: bool
    transcript_languages: str
    transcript_provider_order: str
    transcript_library_enabled: bool
    transcript_library_max_requests_per_pass: int
    transcript_library_min_request_interval_seconds: float
    supadata_enabled: bool
    supadata_api_key: str
    supadata_base_url: str
    supadata_timeout_seconds: float
    supadata_poll_interval_seconds: float
    supadata_max_poll_attempts: int
    supadata_min_request_interval_seconds: float
    supadata_mode: SupadataMode


def _parse_transcript_provider_order(raw: str) -> list[str]:
    """Split the comma-separated order flag into normalized source names."""
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def _parse_transcript_languages(raw: str) -> tuple[str, ...]:
    """Split the comma-separated language flag into normalized ISO codes."""
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def _youtube_oauth_config(settings: TranscriptProviderSettings) -> OAuthConfig:
    """Build the shared OAuth config the captions adapter needs."""
    return OAuthConfig(
        token_path=settings.youtube_token_path,
        client_secret_path=settings.youtube_client_secret_path,
        no_browser=settings.youtube_oauth_no_browser,
    )


def build_configured_transcript_provider(
    settings: TranscriptProviderSettings,
) -> TranscriptProvider | None:
    """Build the transcript provider chain from the configured source order.

    The chain is composed from `TETHER_TRANSCRIPT_PROVIDER_ORDER` (primary first).
    Each named source is included only when it is actually configured: `captions`
    is always available once a token exists, `library` unless disabled, and
    `supadata` only when keyed + enabled. The default order leads with Supadata (the
    only source that reliably transcribes third-party liked videos) and trails with
    the free `youtube-transcript-api` library; the owner-only captions API is
    available by name but dropped from the default because it transcribes almost
    none of the corpus. With no token, returns `None` so the on-demand path falls
    back to a null provider and the background transcript worker stays off.

    The per-call Supadata use cap is late-bound at wire time (it needs the
    database), so it is not applied here.
    """
    if not settings.youtube_token_path.exists():
        return None
    # An empty flag falls back to English so the library/Supadata always get a hint.
    languages = _parse_transcript_languages(settings.transcript_languages) or ("en",)
    available: dict[str, TranscriptProvider] = {
        "captions": CaptionsTranscriptProvider.from_config(
            _youtube_oauth_config(settings)
        )
    }
    library = _build_library_provider(settings, languages=languages)
    if library is not None:
        available["library"] = library
    supadata = _build_supadata_provider(settings, languages=languages)
    if supadata is not None:
        available["supadata"] = supadata
    return _compose_transcript_provider(
        _parse_transcript_provider_order(settings.transcript_provider_order), available
    )


def _build_library_provider(
    settings: TranscriptProviderSettings, *, languages: tuple[str, ...] = ()
) -> YouTubeTranscriptApiProvider | None:
    """Build the free `youtube-transcript-api` provider, unless disabled.

    Threads the strict per-pass request budget and mandatory request spacing from
    settings so they reach the real provider — the same way `_build_supadata_
    provider` threads Supadata's own pacing. Both default to deliberately
    conservative values (issue #179): the library gets the host IP-blocked in
    bursts of roughly 10+ rapid requests, so a single sync pass must never be
    allowed to fire dozens at it.
    """
    if not settings.transcript_library_enabled:
        return None
    return YouTubeTranscriptApiProvider(
        languages=languages,
        budget=LibraryPassBudget(
            max_requests_per_pass=settings.transcript_library_max_requests_per_pass,
            min_request_interval=timedelta(
                seconds=settings.transcript_library_min_request_interval_seconds
            ),
        ),
    )


def _compose_transcript_provider(
    order: Sequence[str], available: Mapping[str, TranscriptProvider]
) -> TranscriptProvider:
    """Compose the configured sources into one chain, primary first.

    Walks `order`, keeping each named source that is actually available (skipping
    the unconfigured ones), and composes the survivors as
    `FallbackTranscriptProvider(primary, fallbacks=rest)`. A single survivor is
    returned uncomposed. An unknown name, or an order that leaves no available
    source, raises `TranscriptProviderConfigError`.
    """
    selected: list[TranscriptProvider] = []
    for name in order:
        if name not in _KNOWN_TRANSCRIPT_SOURCES:
            message = (
                f"unknown transcript source {name!r} in "
                f"TETHER_TRANSCRIPT_PROVIDER_ORDER; known sources are "
                f"{sorted(_KNOWN_TRANSCRIPT_SOURCES)}"
            )
            raise TranscriptProviderConfigError(message)
        provider = available.get(name)
        if provider is not None:
            selected.append(provider)
    if not selected:
        message = (
            f"no transcript source in {list(order)} is configured; check "
            f"TETHER_TRANSCRIPT_PROVIDER_ORDER and each source's credentials"
        )
        raise TranscriptProviderConfigError(message)
    if len(selected) == 1:
        return selected[0]
    return FallbackTranscriptProvider(selected[0], fallbacks=selected[1:])


def _build_supadata_provider(
    settings: TranscriptProviderSettings, *, languages: tuple[str, ...] = ()
) -> SupadataTranscriptProvider | None:
    """Build the paid Supadata provider only when its key *and* flag are both set.

    Either missing makes Supadata a true no-op (omitted from the chain), so the
    free providers behave exactly as before and no cost can be incurred by
    accident. `languages` sets the preferred caption language sent on each submit.
    """
    if not (settings.supadata_enabled and settings.supadata_api_key):
        return None
    config = SupadataConfig(
        base_url=settings.supadata_base_url,
        languages=languages,
        timeout=timedelta(seconds=settings.supadata_timeout_seconds),
        poll_interval=timedelta(seconds=settings.supadata_poll_interval_seconds),
        max_poll_attempts=settings.supadata_max_poll_attempts,
        mode=settings.supadata_mode,
        min_request_interval=timedelta(
            seconds=settings.supadata_min_request_interval_seconds
        ),
    )
    transport = HttpSupadataTransport(settings.supadata_api_key, config=config)
    return SupadataTranscriptProvider(transport, config=config)


def resolve_transcript_provider(
    *,
    configured_provider: TranscriptProvider | None,
    api: YouTubeApi,
    database: Database,
    client: YouTubeApiClient,
    supadata_max_uses: int,
) -> TranscriptProvider:
    """Pick the transcript provider and bind its persisted budgets/caps.

    The on-demand fetch path always needs a provider: prefer the explicitly
    configured one (the composed captions/Supadata chain in production), else reuse
    the upstream fake when it doubles as a `TranscriptProvider` (the in-memory test
    double), else a null provider that reports every video unavailable. Two things
    are late-bound here — the provider tree is built from settings before the
    database/client exist:

    * The Supadata monthly use cap (a no-op when the chain has no Supadata).
    * The YouTube Data API daily-quota charge, onto any captions provider in the
      chain (a no-op when the chain has no captions — the default order). Only
      captions calls consume the Data API's daily budget; the free library and
      Supadata never do, so they are never bound to it.
    """
    provider = configured_provider or (
        api if isinstance(api, InMemoryYouTubeApi) else NullTranscriptProvider()
    )
    bind_supadata_spend_guard(provider, database, max_uses=supadata_max_uses)
    bind_captions_daily_quota(provider, client.charge_transcript)
    return provider
