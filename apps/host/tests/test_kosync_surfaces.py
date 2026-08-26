"""HTTP-surface tests for the kosync gate.

Two surfaces, one app. The device-facing `/kosync/*` protocol is driven through
the test client with `x-auth-user`/`x-auth-key` headers (no browser session,
since it lives outside the `/api/*` gate); its request parsing, status codes,
and error bodies are asserted against the kosync protocol exactly. The owner-
facing ebook-labeling capability is driven through both its REST route and its
`/internal/tools/*` envelope, both deriving from `tether.kosync_capabilities`.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, test

from tests.surfaces import call_tool, login, surface_client

KOSYNC_USER = "reader"
KOSYNC_KEY = "5f4dcc3b5aa765d61d8327deb882cf99"


def kosync_client(root: Path) -> Any:
    """A surface client with the kosync gate configured and mounted."""
    return surface_client(
        root,
        kosync_enabled=True,
        kosync_username=KOSYNC_USER,
        kosync_userkey=KOSYNC_KEY,
    )


def auth_headers(user: str = KOSYNC_USER, key: str = KOSYNC_KEY) -> dict[str, str]:
    """The device auth headers a KOReader push carries."""
    return {"x-auth-user": user, "x-auth-key": key}


def progress_body(**overrides: Any) -> dict[str, Any]:
    """A valid `PUT /kosync/syncs/progress` body."""
    body: dict[str, Any] = {
        "document": "hash-abc",
        "percentage": 0.42,
        "progress": "/body/DocFragment[3]",
        "device": "Phone",
    }
    body.update(overrides)
    return body


@test()
def healthcheck_reports_ok_without_auth() -> None:
    """The liveness probe answers OK and needs no credentials."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.get("/kosync/healthcheck")

        assert_eq(response.status_code, 200)
        assert_eq(response.json(), {"state": "OK"})


@test()
def user_registration_is_always_refused() -> None:
    """Single-tenant: create always returns the disabled error (2005 / 402)."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.post(
            "/kosync/users/create",
            json={"username": "x", "password": "y"},
        )

        assert_eq(response.status_code, 402)
        assert_eq(response.json()["code"], 2005)


@test()
def auth_check_accepts_the_configured_credentials() -> None:
    """Valid `x-auth-user`/`x-auth-key` headers authorise the device."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.get("/kosync/users/auth", headers=auth_headers())

        assert_eq(response.status_code, 200)
        assert_eq(response.json(), {"authorized": "OK"})


@test()
def auth_check_rejects_a_bad_key() -> None:
    """A wrong `x-auth-key` is unauthorised (2001 / 401)."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.get("/kosync/users/auth", headers=auth_headers(key="wrong"))

        assert_eq(response.status_code, 401)
        assert_eq(response.json()["code"], 2001)


@test()
def put_progress_stores_and_echoes_the_timestamp() -> None:
    """A valid push is accepted and the server timestamp is echoed back."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.put(
            "/kosync/syncs/progress",
            json=progress_body(),
            headers=auth_headers(),
        )

        assert_eq(response.status_code, 200)
        assert_eq(response.json()["document"], "hash-abc")
        assert_eq(isinstance(response.json()["timestamp"], int), True)


@test()
def put_progress_requires_auth() -> None:
    """An unauthenticated push is rejected before any storage (2001 / 401)."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.put("/kosync/syncs/progress", json=progress_body())

        assert_eq(response.status_code, 401)
        assert_eq(response.json()["code"], 2001)


@test()
def put_progress_rejects_a_missing_document() -> None:
    """An empty `document` is its own error (2004 / 403)."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.put(
            "/kosync/syncs/progress",
            json=progress_body(document=""),
            headers=auth_headers(),
        )

        assert_eq(response.status_code, 403)
        assert_eq(response.json()["code"], 2004)


@test()
def put_progress_rejects_a_missing_field() -> None:
    """A missing required field other than document is invalid (2003 / 403)."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        body = progress_body()
        del body["percentage"]

        response = client.put(
            "/kosync/syncs/progress", json=body, headers=auth_headers()
        )

        assert_eq(response.status_code, 403)
        assert_eq(response.json()["code"], 2003)


@test()
def get_progress_returns_the_stored_fields() -> None:
    """A GET after a push returns the furthest-progress fields."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        _ = client.put(
            "/kosync/syncs/progress",
            json=progress_body(),
            headers=auth_headers(),
        )

        response = client.get("/kosync/syncs/progress/hash-abc", headers=auth_headers())

        assert_eq(response.status_code, 200)
        assert_eq(response.json()["percentage"], 0.42)
        assert_eq(response.json()["progress"], "/body/DocFragment[3]")


@test()
def get_progress_is_empty_when_nothing_is_stored() -> None:
    """A document with no push comes back as an empty object, no `document` key."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        response = client.get("/kosync/syncs/progress/never", headers=auth_headers())

        assert_eq(response.status_code, 200)
        assert_eq(response.json(), {})


@test()
def the_protocol_is_not_mounted_when_disabled() -> None:
    """A default (disabled) install leaves the whole `/kosync` prefix at 404."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        response = client.get("/kosync/healthcheck")

        assert_eq(response.status_code, 404)


@test()
def rest_labels_an_ebook() -> None:
    """The REST surface attaches a title to a document hash."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        login(client)

        response = client.post(
            "/api/ebooks/label",
            json={"document_hash": "hash-1", "title": "Dune"},
        )

        assert_eq(response.status_code, 200)
        assert_eq(response.json()["title"], "Dune")


@test()
def rest_lists_unlabeled_ebooks() -> None:
    """A pushed-but-unlabeled document appears in the unlabeled listing."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        login(client)
        _ = client.put(
            "/kosync/syncs/progress",
            json=progress_body(document="unl"),
            headers=auth_headers(),
        )

        response = client.get("/api/ebooks/unlabeled")

        assert_eq(response.status_code, 200)
        assert_eq([entry["document_hash"] for entry in response.json()], ["unl"])


@test()
def the_tool_surface_labels_an_ebook() -> None:
    """The internal tool drives the same labeling capability via an envelope."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        login(client)

        envelope = call_tool(
            client, "label_ebook", document_hash="hash-2", title="Neuromancer"
        )

        assert_eq(envelope["success"], True)
        assert_eq(envelope["result"]["title"], "Neuromancer")


@test()
def the_tool_surface_rejects_an_empty_title() -> None:
    """A blank title is a well-formed invalid_input envelope, no row written."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        login(client)

        envelope = call_tool(client, "label_ebook", document_hash="h", title="")

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def the_tool_surface_matches_a_filename() -> None:
    """`match_ebook_filename` labels the computed hash with the filename stem."""
    with TemporaryDirectory() as root, kosync_client(Path(root)) as client:
        login(client)

        envelope = call_tool(
            client, "match_ebook_filename", filename="/mnt/Snow Crash.epub"
        )

        assert_eq(envelope["success"], True)
        assert_eq(envelope["result"]["title"], "Snow Crash")
        assert_eq(envelope["result"]["finished"], False)
