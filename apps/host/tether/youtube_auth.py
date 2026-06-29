"""`python -m tether.youtube_auth`: the one-time YouTube authorization bootstrap.

Wired to the `just youtube-auth` recipe. Reads the token/client-secret paths and
the no-browser toggle from the `TETHER_` environment, runs the installed-app
OAuth flow, caches the token, and prints the most-recent liked titles so the user
can confirm authorization worked end to end. The background ingestion sync then
activates on the next host start because a real upstream client is configured.

It reads its own small settings (the three `TETHER_YOUTUBE_*` variables) rather
than the full host settings, so authorizing does not require the app password or
session secret to be set, and importing it stays cheap.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from tether.youtube_oauth import OAuthConfig, YouTubeAuthError, bootstrap


class YouTubeAuthSettings(BaseSettings):
    """The `TETHER_YOUTUBE_*` subset the auth bootstrap needs."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    youtube_token_path: Path = Path(".tether/youtube-oauth-token.json")
    youtube_client_secret_path: Path = Path(".tether/youtube-client-secret.json")
    youtube_oauth_no_browser: bool = False


def main() -> None:
    """Run the OAuth bootstrap from environment-backed settings."""
    settings = YouTubeAuthSettings()
    config = OAuthConfig(
        token_path=settings.youtube_token_path,
        client_secret_path=settings.youtube_client_secret_path,
        no_browser=settings.youtube_oauth_no_browser,
    )
    try:
        result = bootstrap(config)
    except YouTubeAuthError as error:
        print(f"YouTube authorization failed: {error}")
        raise SystemExit(1) from error
    print(f"Authorized. Token cached at {result.token_path}.")
    if result.recent_titles:
        print("Most-recent liked videos:")
        for title in result.recent_titles:
            print(f"  - {title}")
    else:
        print("No liked videos found yet.")


if __name__ == "__main__":
    main()
