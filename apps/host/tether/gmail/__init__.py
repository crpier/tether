"""Gmail Integration interface (ADR-0025).

The only import surface for Tether code outside this package. Everything else
in ``tether.gmail`` is an internal seam owned by this Integration and its
tests.
"""

from tether.gmail.auth_routes import router as auth_routes_router
from tether.gmail.auth_service import (
    GmailAuthBackend,
    GoogleGmailAuthBackend,
    GoogleGmailAuthService,
    ReauthorizableGmailClient,
)
from tether.gmail.client import (
    GmailAuthenticationFailure,
    GmailClient,
    GmailFailure,
    GmailMessage,
    GmailTransport,
)
from tether.gmail.oauth import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    HttpGmailTransport,
)
from tether.gmail.store import (
    GMAIL_PURGE_WATERMARK_KEY,
    create_gmail_schema,
    read_sync_watermark,
    write_sync_watermark,
)
from tether.gmail.sync import GmailSyncService
from tether.gmail.tools import GMAIL_TOOL_SPECS, internal_gmail_tool_routes
from tether.gmail.triage import GmailTriageRunner

__all__ = [
    "GMAIL_MODIFY_SCOPE",
    "GMAIL_PURGE_WATERMARK_KEY",
    "GMAIL_READONLY_SCOPE",
    "GMAIL_TOOL_SPECS",
    "GmailAuthBackend",
    "GmailAuthenticationFailure",
    "GmailClient",
    "GmailFailure",
    "GmailMessage",
    "GmailSyncService",
    "GmailTransport",
    "GmailTriageRunner",
    "GoogleGmailAuthBackend",
    "GoogleGmailAuthService",
    "HttpGmailTransport",
    "ReauthorizableGmailClient",
    "auth_routes_router",
    "create_gmail_schema",
    "internal_gmail_tool_routes",
    "read_sync_watermark",
    "write_sync_watermark",
]
