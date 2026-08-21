"""`python -m tether.gmail.auth`: the one-time Gmail authorization bootstrap.

Wired to the `just gmail-auth` recipe. Mirrors `tether.youtube.auth`: reads the
token/client-secret paths and the no-browser toggle from the `TETHER_`
environment, runs the installed-app OAuth flow (reusing the YouTube client
secret by default — no new GCP setup is needed), caches the token, and lists a
few recent eligible subjects so the user can confirm authorization worked end
to end. The background ingestion gate then activates on the next host start
because a real upstream transport is configured.

It reads its own small settings (the `TETHER_GMAIL_*` variables) rather than
the full host settings, so authorizing does not require the app password or
session secret to be set, and importing it stays cheap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict
from snekok import Err, Ok, Result

from tether.gmail.client import GmailClient, GmailFailure
from tether.gmail.oauth import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    HttpGmailTransport,
)
from tether.google_oauth import GoogleAuthError, OAuthConfig, run_auth_flow
from tether.structured_logging import Logger

_VERIFY_COUNT = 5
"""How many recent eligible subjects the bootstrap lists as a sanity check."""


class GmailAuthSettings(BaseSettings):
    """The `TETHER_GMAIL_*` subset the auth bootstrap needs."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", validate_default=True)

    gmail_token_path: Path = Path(".tether/gmail-oauth-token.json")
    gmail_client_secret_path: Path = Path(".tether/youtube-client-secret.json")
    gmail_oauth_no_browser: bool = False


async def _recent_subjects(
    config: OAuthConfig, count: int, *, logger: Logger
) -> Result[list[str], GmailFailure]:
    """List a handful of recent eligible subjects, as an end-to-end sanity check."""
    async with HttpGmailTransport(config) as transport:
        client = GmailClient(transport=transport)
        listing = await client.list_message_ids(
            query="-in:spam -in:trash -in:sent", logger=logger
        )
        if isinstance(listing, Err):
            return Err(listing.error)
        subjects: list[str] = []
        for message_id in listing.value[:count]:
            message = await client.get_message(message_id)
            if isinstance(message, Err):
                return Err(message.error)
            subjects.append(message.value.subject or "(no subject)")
        return Ok(subjects)


def main() -> None:
    """Run the OAuth bootstrap from environment-backed settings."""
    settings = GmailAuthSettings()
    config = OAuthConfig(
        token_path=settings.gmail_token_path,
        client_secret_path=settings.gmail_client_secret_path,
        # Both scopes are requested: `gmail.readonly` for the ingestion gate's
        # listing/reads and `gmail.modify` for the backlog-purge write path
        # (archive/label/trash). A token minted before `gmail.modify` was added
        # must be re-authorized by re-running this bootstrap and re-consenting.
        scopes=(GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE),
        no_browser=settings.gmail_oauth_no_browser,
    )
    try:
        # `run_auth_flow` and `load_credentials` raise the shared OAuth-flow
        # error type; it is not Gmail-specific despite the name, since both
        # integrations drive the same installed-app flow code.
        _ = run_auth_flow(config)
        subject_result = asyncio.run(
            _recent_subjects(
                config,
                _VERIFY_COUNT,
                logger=structlog.stdlib.get_logger("gmail-auth"),
            )
        )
    except GoogleAuthError as error:
        print(f"Gmail authorization failed: {error}")
        raise SystemExit(1) from error
    if isinstance(subject_result, Err):
        print(
            f"Gmail authorization verification failed: {type(subject_result.error).__name__}"
        )
        raise SystemExit(1)
    subjects = subject_result.value
    print(f"Authorized. Token cached at {config.token_path}.")
    if subjects:
        print("Most-recent eligible subjects:")
        for subject in subjects:
            print(f"  - {subject}")
    else:
        print("No eligible messages found yet.")


if __name__ == "__main__":
    main()
