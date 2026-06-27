"""Behavior tests for exporting the committed OpenAPI document."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from snektest import assert_eq, assert_in, assert_true, test

from tether.openapi_export import write_openapi_document


@test()
def write_openapi_document_emits_the_public_api_surface() -> None:
    """The committed contract source contains the browser REST paths."""
    with TemporaryDirectory() as directory:
        output_path = Path(directory) / "openapi.json"

        write_openapi_document(output_path)

        document = json.loads(output_path.read_text())
    assert_eq(document["openapi"], "3.1.0")
    assert_in("/api/auth/session", document["paths"])
    assert_in("/api/memories", document["paths"])
    assert_in("/api/bucket-items", document["paths"])
    assert_eq(document["components"]["schemas"]["JsonValue"], {})
    assert_true(all(path.startswith("/api/") for path in document["paths"]))
