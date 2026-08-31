"""REST behavior tests for durable chat attachments."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfWriter
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_raises, test
from starlette.testclient import TestClient

from tether.attachments import AttachmentNotFoundError, AttachmentService
from tether.conversations import ConversationService
from tether.host_schema import create_host_schema
from tether.model_selection import AgentModelCatalog
from tether.search_projection.embeddings import FakeEmbedder
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
)


def _client(root: Path) -> TestClient:
    """Create an isolated authenticated-app test client."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / "kb",
                session_secret=SESSION_SECRET,
            ),
            embedder=FakeEmbedder(),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def _login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def _main_conversation_id(client: TestClient) -> str:
    """Return the permanent Main Conversation identity."""
    response = client.get("/api/conversations")
    assert_eq(response.status_code, 200)
    return str(response.json()[0]["id"])


def _pdf_bytes() -> bytes:
    """Build one valid PDF without relying on a checked-in binary fixture."""
    output = BytesIO()
    writer = PdfWriter()
    _ = writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


@test()
async def abandoned_upload_is_removed_after_one_day() -> None:
    """A staged file that never joined a turn does not persist indefinitely."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_host_schema(database)
    conversation_service = ConversationService(
        database,
        model_catalog=AgentModelCatalog(default_model=None, models=()),
    )
    conversation = await conversation_service.fetch_main_conversation()
    with TemporaryDirectory() as directory:
        attachments = AttachmentService(database, Path(directory))
        attachment = await attachments.create(
            conversation.id,
            content=b"temporary note",
            declared_mime_type="text/plain",
            filename="note.txt",
        )

        removed = await attachments.prune_abandoned(
            datetime.now(UTC) + timedelta(days=2)
        )

        with assert_raises(AttachmentNotFoundError):
            _ = await attachments.fetch(attachment.id)
    await database.close()

    assert_eq(removed, 1)


@test()
def pdf_upload_returns_document_attachment_metadata() -> None:
    """A valid PDF is accepted as an agent-readable document."""
    with TemporaryDirectory() as directory, _client(Path(directory)) as client:
        _login(client)
        conversation_id = _main_conversation_id(client)
        pdf = _pdf_bytes()

        response = client.post(
            f"/api/conversations/{conversation_id}/attachments",
            files={"file": ("brief.pdf", pdf, "application/pdf")},
        )

    assert_eq(response.status_code, 201)
    assert_eq(response.json()["filename"], "brief.pdf")
    assert_eq(response.json()["kind"], "document")
    assert_eq(response.json()["mime_type"], "application/pdf")


@test()
def text_upload_returns_document_attachment_metadata() -> None:
    """A UTF-8 text file is accepted as an agent-readable document."""
    with TemporaryDirectory() as directory, _client(Path(directory)) as client:
        _login(client)
        conversation_id = _main_conversation_id(client)

        response = client.post(
            f"/api/conversations/{conversation_id}/attachments",
            files={
                "file": (
                    "notes.md",
                    b"# Packing list\n\n- passport\n",
                    "text/markdown",
                )
            },
        )

    assert_eq(response.status_code, 201)
    assert_eq(response.json()["filename"], "notes.md")
    assert_eq(response.json()["kind"], "document")
    assert_eq(response.json()["mime_type"], "text/markdown")


@test()
def uploaded_attachment_can_be_downloaded_with_its_original_filename() -> None:
    """A stored file remains retrievable through the authenticated route."""
    content = b"# Packing list\n"
    with TemporaryDirectory() as directory, _client(Path(directory)) as client:
        _login(client)
        conversation_id = _main_conversation_id(client)
        upload = client.post(
            f"/api/conversations/{conversation_id}/attachments",
            files={"file": ("packing.md", content, "text/markdown")},
        )

        response = client.get(f"/api/attachments/{upload.json()['id']}")

    assert_eq(response.status_code, 200)
    assert_eq(response.content, content)
    assert_eq(
        response.headers["content-disposition"],
        'attachment; filename="packing.md"',
    )


@test()
def oversized_attachment_returns_payload_too_large() -> None:
    """A file over 10 MB is rejected with a stable size failure."""
    with TemporaryDirectory() as directory, _client(Path(directory)) as client:
        _login(client)
        conversation_id = _main_conversation_id(client)

        response = client.post(
            f"/api/conversations/{conversation_id}/attachments",
            files={
                "file": (
                    "large.txt",
                    b"x" * (10 * 1024 * 1024 + 1),
                    "text/plain",
                )
            },
        )

    assert_eq(response.status_code, 413)


@test()
def image_upload_returns_durable_attachment_metadata() -> None:
    """Uploading a supported image returns its immutable browser representation."""
    with TemporaryDirectory() as directory, _client(Path(directory)) as client:
        _login(client)
        conversation_id = _main_conversation_id(client)

        response = client.post(
            f"/api/conversations/{conversation_id}/attachments",
            files={"file": ("pixel.png", PNG_BYTES, "image/png")},
        )

    assert_eq(response.status_code, 201)
    assert_eq(response.json()["filename"], "pixel.png")
    assert_eq(response.json()["kind"], "image")
    assert_eq(response.json()["mime_type"], "image/png")
    assert_eq(response.json()["size_bytes"], len(PNG_BYTES))
