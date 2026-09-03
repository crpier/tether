from collections.abc import Sequence
from typing import Any, Self

from google.auth.external_account_authorized_user import (
    Credentials as ExternalAccountCredentials,
)
from google.oauth2.credentials import Credentials

class Flow:
    credentials: ExternalAccountCredentials | Credentials
    redirect_uri: str

    @classmethod
    def from_client_secrets_file(
        cls,
        client_secrets_file: str,
        scopes: Sequence[str],
        **kwargs: Any,
    ) -> Self: ...
    def authorization_url(self, **kwargs: Any) -> tuple[str, str]: ...
    def fetch_token(self, **kwargs: Any) -> object: ...

class InstalledAppFlow(Flow):
    def run_local_server(
        self,
        *,
        port: int = ...,
        open_browser: bool = ...,
        **kwargs: Any,
    ) -> ExternalAccountCredentials | Credentials: ...
