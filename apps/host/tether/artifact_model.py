"""Domain values and payload limits for Artifacts."""

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
"""A recursively typed JSON value accepted from an Artifact event."""

ARTIFACT_HTML_SIZE_CAP_BYTES = 1_000_000
"""Host-side cap on an Artifact's HTML payload, UTF-8 encoded."""
