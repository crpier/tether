"""Build the fixed Supadata then transcript-library source chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from snekok import NonEmptySecretStr, validate_python

from tether.transcripts.contracts import TranscriptProviderChain, TranscriptSource
from tether.transcripts.library import (
    TranscriptLibraryConfig,
    YouTubeTranscriptApiSource,
)
from tether.transcripts.supadata import (
    HttpSupadataTransport,
    SupadataConfig,
    SupadataTranscriptSource,
)


def _default_languages() -> tuple[str, ...]:
    return ("en",)


@dataclass(frozen=True, slots=True)
class TranscriptLibrarySourceConfig:
    """Configuration specific to the local transcript-library source."""

    enabled: bool = True
    min_request_interval: timedelta = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class SupadataSourceConfig:
    """Configuration specific to the optional paid Supadata source."""

    api_key: str = ""
    base_url: str = "https://api.supadata.ai/v1"
    enabled: bool = False
    max_poll_attempts: int = 10
    min_request_interval: timedelta = timedelta(seconds=1)
    poll_interval: timedelta = timedelta(seconds=2)
    timeout: timedelta = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class TranscriptProviderConfig:
    """Complete startup value for fixed transcript-source composition."""

    languages: tuple[str, ...] = field(default_factory=_default_languages)
    library: TranscriptLibrarySourceConfig = field(
        default_factory=TranscriptLibrarySourceConfig
    )
    supadata: SupadataSourceConfig = field(default_factory=SupadataSourceConfig)


def build_configured_transcript_provider(
    config: TranscriptProviderConfig,
) -> TranscriptProviderChain | None:
    """Build Supadata first and the transcript library second when enabled."""
    providers: list[TranscriptSource] = []
    if config.supadata.enabled and config.supadata.api_key:
        supadata_config = SupadataConfig(
            base_url=config.supadata.base_url,
            languages=config.languages,
            max_poll_attempts=config.supadata.max_poll_attempts,
            min_request_interval=config.supadata.min_request_interval,
            poll_interval=config.supadata.poll_interval,
            timeout=config.supadata.timeout,
        )
        providers.append(
            SupadataTranscriptSource(
                HttpSupadataTransport(
                    validate_python(
                        NonEmptySecretStr, config.supadata.api_key
                    ).unwrap(),
                    config=supadata_config,
                ),
                config=supadata_config,
            )
        )
    if config.library.enabled:
        providers.append(
            YouTubeTranscriptApiSource(
                languages=config.languages,
                config=TranscriptLibraryConfig(
                    min_request_interval=config.library.min_request_interval
                ),
            )
        )
    return TranscriptProviderChain(providers) if providers else None
