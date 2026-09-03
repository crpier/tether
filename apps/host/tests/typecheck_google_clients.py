"""Static typing contracts for the Google client boundaries."""

from __future__ import annotations

from typing import assert_type

from google.auth.external_account_authorized_user import (
    Credentials as ExternalAccountCredentials,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient._apis.youtube.v3 import YouTubeResource
from googleapiclient.discovery import build


def google_api_discovery_uses_installed_stubs() -> None:
    """The stub-only package resolves the concrete YouTube API resource."""
    assert_type(build("youtube", "v3"), YouTubeResource)


def google_credentials_constructor_is_typed() -> None:
    """The unannotated Google Auth constructor retains its concrete type."""
    assert_type(
        Credentials.from_authorized_user_info({"client_id": "test"}), Credentials
    )


def installed_app_flow_is_typed() -> None:
    """The unannotated OAuth flow retains its concrete types."""
    flow = InstalledAppFlow.from_client_secrets_file("test", ["test"])
    assert_type(flow, InstalledAppFlow)
    assert_type(
        flow.run_local_server(port=0, open_browser=False),
        ExternalAccountCredentials | Credentials,
    )
