"""Environment and in-process configuration for the Tether host."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tether.action_registry import ActionSpec
from tether.gmail_auth_service import GmailAuthBackend
from tether.gmail_client import GmailTransport
from tether.model_selection import AgentModelConfig
from tether.provider_auth import ProviderAuthBackend
from tether.readwise_http import ReaderTransport, ReadwiseTransport
from tether.recall_generation import StudyItemGenerator
from tether.recall_grading import AnswerGrader
from tether.stt import SttClient
from tether.telemetry_model import TelemetryExporter, TelemetrySettings
from tether.transcripts.acquisition import TranscriptAcquisitionConfig
from tether.transcripts.contracts import TranscriptProviderChain, TranscriptSource
from tether.transcripts.worker import TranscriptSyncConfig
from tether.web_search import SearchProvider
from tether.youtube_auth_service import YouTubeAuthBackend
from tether.youtube_oauth import OAuthConfig
from tether.youtube_quota import YouTubeApi


@dataclass(frozen=True, slots=True)
class AppConfig:
    """In-process configuration for one FastAPI app instance.

    ```python
    config = AppConfig(app_password="pw", session_secret="secret")
    assert config.secure_cookies is False
    ```
    """

    app_password: str
    session_secret: str
    api_token: str = ""
    database_path: str | Path = Path(".tether/tether.sqlite3")
    telemetry_database_path: str | Path | None = None
    default_model: str | None = None
    default_model_id: str | None = None
    default_model_provider: str | None = None
    ebook_statistics_db_path: str = ""
    ebook_statistics_sync_interval_seconds: float = 5 * 60
    extra_extension_paths: Sequence[Path] = field(default_factory=tuple)
    kb_root: str | Path = Path(".tether")
    kosync_enabled: bool = False
    kosync_username: str = ""
    kosync_userkey: str = ""
    logging_level: str = "INFO"
    log_file: str | Path | None = None
    model_allowlist: Sequence[AgentModelConfig] = field(default_factory=tuple)
    pi_binary: Path | None = None
    provider_auth_backend: ProviderAuthBackend | None = None
    youtube_api: YouTubeApi | None = None
    youtube_auth_backend: YouTubeAuthBackend | None = None
    youtube_daily_quota_limit: int = 10_000
    youtube_sync_enabled: bool = True
    youtube_sync_interval_seconds: float = 5 * 60
    youtube_sync_hot_pages: int = 2
    youtube_sync_backfill_pages: int = 1
    youtube_sync_page_size: int = 50
    youtube_likes_cutoff_date: date | None = None
    youtube_likes_rewalk_interval_days: float = 30.0
    youtube_likes_drift_alarm_margin: int = 5
    youtube_api_gate_pause_base_seconds: float = 15 * 60
    youtube_api_gate_pause_cap_seconds: float = 6 * 60 * 60
    transcript_acquisition_config: TranscriptAcquisitionConfig = field(
        default_factory=TranscriptAcquisitionConfig
    )
    transcript_provider: TranscriptProviderChain | TranscriptSource | None = None
    transcript_sync_config: TranscriptSyncConfig = field(
        default_factory=TranscriptSyncConfig
    )
    transcript_sync_enabled: bool = True
    transcript_sync_interval_seconds: float = 5 * 60
    readwise_api_key: str = ""
    readwise_sync_enabled: bool = False
    readwise_sync_interval_seconds: float = 60 * 60
    readwise_transport: ReadwiseTransport | None = None
    readwise_reader_sync_enabled: bool = False
    readwise_reader_sync_interval_seconds: float = 60 * 60
    reader_transport: ReaderTransport | None = None
    gmail_transport: GmailTransport | None = None
    gmail_auth_backend: GmailAuthBackend | None = None
    gmail_oauth_config: OAuthConfig | None = None
    gmail_sync_enabled: bool = False
    gmail_sync_interval_seconds: float = 15 * 60
    gmail_triage_batch_size: int = 10
    gmail_purge_enabled: bool = False
    gmail_purge_interval_seconds: float = 60 * 60
    gmail_purge_chunk_size: int = 10
    dreaming_enabled: bool = False
    pi_idle_seconds: float = 30 * 60
    pi_session_root: str | Path | None = None
    proposal_action_specs: Sequence[ActionSpec] | None = None
    public_origin: str = ""
    scheduler_concurrency: int = 4
    scheduler_tick_seconds: float = 30.0
    search_max_uses: int = 1_000
    search_provider: SearchProvider | None = None
    search_reconcile_seconds: float = 5 * 60
    secure_cookies: bool = False
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = ""
    stt_client: SttClient | None = None
    stt_api_key: str = "unconfigured-test-stt-key"
    """Placeholder only: production wiring always goes through `HostSettings`,
    which fails fast at boot if unset (ADR 0018). This default exists purely so
    tests that construct `AppConfig` directly and don't exercise voice/STT
    routes don't need to supply a real key; tests that do exercise transcription
    inject a fake `stt_client` instead."""
    stt_base_url: str = "https://api.openai.com/v1"
    stt_model: str = "whisper-1"
    study_item_generator: StudyItemGenerator | None = None
    answer_grader: AnswerGrader | None = None
    tool_base_url: str = "http://127.0.0.1:8000"
    web_dist: Path | None = None


class HostSettings(BaseSettings):
    """Environment-backed configuration for the host server process.

    ```python
    settings = HostSettings()  # reads `TETHER_` environment variables
    ```
    """

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    app_password: str = Field(default="", min_length=1)
    session_secret: str = Field(default="", min_length=1)
    api_token: str = ""
    """Static bearer token for non-browser clients (the mobile capture app).
    Empty (the default) keeps token auth off — only the browser session cookie
    authenticates. When set, a request carrying `Authorization: Bearer <token>`
    passes the app-session gate exactly as a valid cookie would; revocation is
    rotating this value."""
    database_path: Path = Path(".tether/tether.sqlite3")
    dependency_profile: Literal["local", "production"] = "production"
    """Composition profile for real integrations or deterministic local adapters."""
    local_data_root: Path = Path(".tether/local")
    """Disposable state root used only by the local dependency profile."""
    telemetry_database_path: Path | None = None

    @property
    def resolved_telemetry_database_path(self) -> Path:
        """Resolve the independent telemetry store beside the main database."""
        if self.telemetry_database_path is not None:
            return self.telemetry_database_path
        return self.database_path.parent / "telemetry.sqlite3"

    host: str = "127.0.0.1"
    kb_root: Path = Path(".tether")
    kosync_enabled: bool = False
    """Whether the host serves the KOReader kosync protocol under `/kosync`. Off
    by default and a no-op unless `kosync_username` and `kosync_userkey` are both
    set, so a default install leaves the whole prefix unmounted (404). Devices
    must set KOReader's document-matching method to *filename* (hash =
    `md5(basename)`); the binary default cannot be mapped back to a title."""
    kosync_username: str = ""
    """The single pre-provisioned kosync username a device authenticates as
    (`x-auth-user`). Empty keeps the gate off."""
    kosync_userkey: str = ""
    """The `md5(password)` string the device sends as `x-auth-key`, compared
    verbatim. KOReader hashes the password itself; Tether never sees the
    plaintext. Empty keeps the gate off."""
    logging_level: str = "INFO"
    log_file: Path | None = None
    """Optional path to also write logs to, as one JSON object per line, on top
    of the console. Unset in production/docker (the container's stdout is the log
    sink); `just dev` points it at `.tether/logs/host.log` so an agent can read
    back what the app did when a bug is reported (see `docs/development.md`)."""
    model_allowlist: tuple[AgentModelConfig, ...] = ()
    default_model: str | None = None
    port: int = 8000
    public_origin: str = ""
    """Canonical browser origin for externally visible OAuth callbacks.
    Reverse proxies may expose HTTPS while forwarding plain HTTP to Tether, so
    request-derived URLs are not authoritative in production."""
    reload: bool = False
    scheduler_tick_seconds: float = 30.0
    secure_cookies: bool = False
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = ""
    web_dist: Path | None = None
    youtube_token_path: Path = Path(".tether/youtube-oauth-token.json")
    youtube_client_secret_path: Path = Path(".tether/youtube-client-secret.json")
    youtube_oauth_no_browser: bool = False
    youtube_likes_rewalk_interval_days: float = 30.0
    """How long a completed likes backfill stays settled before the walk restarts.
    Once history has been mirrored the sync only refreshes the hot (newest) pages;
    it re-walks history from the tail once the last completion is older than this,
    catching likes that predate the corpus. Set high to walk history rarely."""
    youtube_likes_drift_alarm_margin: int = 5
    """How far the upstream liked-playlist total may exceed the local corpus before
    a settled backfill is treated as drifted and restarted immediately. Videos
    skipped locally (deleted, private, members-only) are tracked by id and folded
    into the comparison precisely, so this margin only absorbs transient races."""
    youtube_sync_enabled: bool = True
    """Whether the background liked-videos sync runs. On by default; set
    `TETHER_YOUTUBE_SYNC_ENABLED=false` to keep the upstream client wired for
    on-demand use while skipping the eager boot sync (e.g. the fast dev loop,
    where the startup pass otherwise delays the server binding its port)."""
    transcript_sync_enabled: bool = True
    """Whether the background transcript worker runs. On by default; set
    `TETHER_TRANSCRIPT_SYNC_ENABLED=false` to skip the eager boot drain (which
    otherwise fetches per-video transcripts synchronously and delays startup)."""
    transcript_library_enabled: bool = True
    """Whether the `youtube-transcript-api` library source is available to compose.
    Enabled by default; set `TETHER_TRANSCRIPT_LIBRARY_ENABLED=false` to drop it
    from the chain entirely (e.g. if the host IP keeps getting blocked)."""
    transcript_library_max_requests_per_pass: int = 5
    """Hard cap on real `youtube-transcript-api` network calls within a single
    transcript sync pass. Deliberately small and strict: the library gets the host
    IP-blocked in bursts of rapid requests, so one pass must never fire dozens
    at it. Once the cap is spent the provider
    self-throttles for the rest of that pass (remaining candidates stay pending,
    picked up next pass) rather than making further real calls; a fresh pass gets
    a fresh budget. Applies to `youtube_transcript_api` only; Supadata's request
    pacing is unaffected."""
    transcript_library_min_request_interval_seconds: float = 5.0
    """Minimum spacing between consecutive real `youtube-transcript-api` calls.
    Mirrors `supadata_min_request_interval_seconds`: back-to-back requests read as
    bot traffic to YouTube, so pacing even the small per-pass budget's calls keeps
    the host looking less like a scraper. 0 disables pacing."""
    transcript_block_pause_base_seconds: float = 2 * 60 * 60
    """Initial cooldown once a blockable transcript source (the free library, or
    Supadata) reports an IP block / rate limit, before its escalating per-source
    pause is retried. Raised from a historical 30 minutes: youtube-transcript-api's
    IP blocks routinely outlast a half hour, so a short initial cooldown just
    re-triggers the same block on the very next pass. Doubles on each further
    consecutive block, clamped to `transcript_block_pause_cap_seconds`."""
    transcript_block_pause_cap_seconds: float = 24 * 60 * 60
    """Ceiling on the escalating per-source transcript-provider pause. Raised from
    6 hours to a full day: a source still getting blocked after several
    escalations is likely under a longer-lived IP ban, so backing off for up to a
    day is worth the slower synchronization."""
    transcript_languages: str = "en,ro"
    """Comma-separated preferred transcript languages, most preferred first (ISO
    codes). Passed to the `youtube-transcript-api` library (which tries them in
    order) and to Supadata (which requests the most preferred track), replacing the
    old hardcoded English-only preference. The default is English primary, Romanian
    secondary."""
    supadata_enabled: bool = False
    """Whether to compose the paid Supadata provider. When enabled (and keyed) it
    becomes the *primary* transcript source, with the free transcript library as
    its fallback. Off by default and a no-op
    unless `supadata_api_key` is also set, so enabling paid transcription is a
    deliberate, credentialed choice."""
    supadata_api_key: str = ""
    """Supadata API key. Empty (the default) keeps Supadata out of the chain
    entirely, so the default install never spends and stays offline-friendly."""
    supadata_base_url: str = "https://api.supadata.ai/v1"
    """Supadata API root the provider's HTTP transport issues requests against."""
    supadata_timeout_seconds: float = 30.0
    """Per-request HTTP timeout for Supadata submit and poll calls."""
    supadata_poll_interval_seconds: float = 2.0
    """Delay between polls of an in-flight async Supadata transcript job."""
    supadata_max_poll_attempts: int = 10
    """Poll budget for a Supadata async job before the attempt is treated as transient."""
    supadata_min_request_interval_seconds: float = 1.0
    """Minimum spacing between billed Supadata submits. The transcript sweep fetches
    videos back-to-back, so a low-rate plan returns `429 limit-exceeded` on the burst
    and pauses the source; spacing submits keeps them under that per-request rate. The
    1.0s default suits a modest plan; set 0 to disable pacing on a generous one."""
    search_enabled: bool = True
    """Whether the agent may call Tavily when an API key is configured."""
    search_api_key: str = ""
    """Tavily API key. Empty keeps web search disabled even when flagged on."""
    search_max_uses: int = 1_000
    """Hard persisted monthly cap on Tavily credits."""
    search_min_request_interval_seconds: float = 1.0
    """Minimum spacing between Tavily requests; zero disables pacing."""
    readwise_api_key: str = ""
    """Readwise API token. Empty (the default) keeps the ingestion gate off, so
    the default install never calls Readwise. Paired with
    `readwise_sync_enabled`; both are required for the worker to run."""
    readwise_sync_enabled: bool = False
    """Whether the background Readwise ingestion gate runs. Off by default and a
    no-op unless `readwise_api_key` is also set, so mirroring highlights into the
    Commons is a deliberate, credentialed choice."""
    readwise_sync_interval_seconds: float = 60 * 60
    """Seconds between Readwise export passes. The Export API is generous (240
    req/min) but highlights change slowly, so an hourly cadence is ample."""
    stt_api_key: str = Field(default="", min_length=1)
    """API key for the OpenAI-compatible transcription endpoint. Required (ADR
    0018): STT is an always-on host dependency, so the host fails fast at boot
    if this is missing/empty rather than degrading to a disabled voice UI."""
    stt_base_url: str = "https://api.openai.com/v1"
    """Root of the OpenAI-compatible transcription API. Point it at OpenAI, Groq,
    or any compatible endpoint — the only per-provider knob is this URL."""
    stt_model: str = "whisper-1"
    """Transcription model requested with each upload (e.g. `whisper-1`,
    `whisper-large-v3`), passed through verbatim to the configured endpoint."""
    readwise_reader_sync_enabled: bool = False
    """Whether the background Readwise Reader v3 progress rider runs. Off by
    default and a no-op unless `readwise_api_key` is also set, so polling reading
    progress into the ebook Telemetry tables is a deliberate, credentialed
    choice."""
    readwise_reader_sync_interval_seconds: float = 60 * 60
    """Seconds between Reader v3 list passes. The list API is rate-limited (20
    req/min) and reading progress changes slowly, so an hourly cadence is ample."""
    gmail_token_path: Path = Path(".tether/gmail-oauth-token.json")
    """Cached Gmail OAuth token path. Absent (the default) keeps the ingestion
    gate off, so a fresh checkout never touches mail. Minted by `just
    gmail-auth`."""
    gmail_client_secret_path: Path = Path(".tether/youtube-client-secret.json")
    """OAuth client secret path. Defaults to the shared YouTube client secret —
    the Gmail gate reuses the same already-completed Google Cloud Console
    setup, so no new GCP project or client is needed."""
    gmail_oauth_no_browser: bool = False
    """Print the Gmail consent URL instead of opening a browser (headless box)."""
    gmail_sync_enabled: bool = False
    """Whether the background Gmail ingestion gate runs. Off by default and a
    no-op unless a cached token also exists at `gmail_token_path`, so reading
    mail is a deliberate, credentialed choice — never on for a fresh checkout."""
    gmail_sync_interval_seconds: float = 15 * 60
    """Seconds between Gmail sync passes. Steady-state volume is a handful of
    eligible messages a day, so a 15-minute cadence is ample without being
    chatty."""
    gmail_triage_batch_size: int = 10
    """How many messages are triaged per ephemeral agent prompt run. Bounds
    both the prompt size and the blast radius of one malformed model reply."""
    gmail_purge_enabled: bool = False
    """Whether the background Gmail backlog-purge sweep runs. Off by default and
    a no-op unless a Gmail transport is also configured. Opt-in on top of the
    read-only ingestion gate because the sweep proposes consequential mailbox
    writes (archive/label/trash); those still require human approval or a
    standing autonomy grant before any write happens."""
    gmail_purge_interval_seconds: float = 60 * 60
    """Seconds between backlog-purge sweeps. Backlog changes slowly, so an
    hourly cadence is ample."""
    gmail_purge_chunk_size: int = 10
    """How many backlog messages one sweep chunk triages and one proposal
    bundles. Bounds both the prompt size and how large a single proposal gets."""
    dreaming_enabled: bool = False
    """Whether Dreaming orchestration is allowed to queue and complete runs."""
    ebook_statistics_db_path: str = ""
    """Host-visible path to a Syncthing-mirrored copy of KOReader's
    `statistics.sqlite`. Empty (the default) keeps the ingestion worker off, so
    the default install never touches a stats file. The live path is never
    opened directly — only a private snapshot copy is, since Syncthing may have
    it mid-write."""
    ebook_statistics_sync_interval_seconds: float = 5 * 60
    """Seconds between statistics-file stat checks. A local file stat is cheap
    and KOReader flushes stats on every page turn, so a five-minute cadence
    keeps the Telemetry reasonably fresh without busy-polling."""
    telemetry_environment: str = "development"
    telemetry_exporter: TelemetryExporter = TelemetryExporter.NONE
    telemetry_service_name: str = "tether-host"
    tool_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def telemetry(self) -> TelemetrySettings:
        """OpenTelemetry settings derived from `TETHER_TELEMETRY_` variables."""
        return TelemetrySettings(
            environment=self.telemetry_environment,
            exporter=self.telemetry_exporter,
            service_name=self.telemetry_service_name,
            service_version="0.1.0",
        )
