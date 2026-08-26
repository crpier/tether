"""Environment and in-process configuration for the headless host."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tether.telemetry_model import TelemetryExporter, TelemetrySettings


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Configuration needed by one deterministic capability host."""

    api_token: str
    open_webui_token: str
    database_path: str | Path = Path(".tether/tether.sqlite3")
    health_episode_sweep_seconds: float = 60.0
    log_file: str | Path | None = None
    logging_level: str = "INFO"
    telemetry_database_path: str | Path | None = None


class HostSettings(BaseSettings):
    """Environment-backed process configuration.

    Only the Android and Open WebUI bearer credentials are application secrets.
    """

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    api_token: str = Field(default="", min_length=1)
    database_path: Path = Path(".tether/tether.sqlite3")
    health_episode_sweep_seconds: float = 60.0
    host: str = "127.0.0.1"
    log_file: Path | None = None
    logging_level: str = "INFO"
    open_webui_token: str = Field(default="", min_length=1)
    port: int = 8000
    reload: bool = False
    telemetry_database_path: Path | None = None
    telemetry_environment: str = "development"
    telemetry_exporter: TelemetryExporter = TelemetryExporter.NONE
    telemetry_service_name: str = "tether-host"

    @property
    def resolved_telemetry_database_path(self) -> Path:
        """Place telemetry beside the main database unless explicitly configured."""
        if self.telemetry_database_path is not None:
            return self.telemetry_database_path
        return self.database_path.parent / "telemetry.sqlite3"

    @property
    def telemetry(self) -> TelemetrySettings:
        """Build OpenTelemetry settings for the process."""
        return TelemetrySettings(
            environment=self.telemetry_environment,
            exporter=self.telemetry_exporter,
            service_name=self.telemetry_service_name,
            service_version="0.1.0",
        )


__all__ = ["AppConfig", "HostSettings"]
