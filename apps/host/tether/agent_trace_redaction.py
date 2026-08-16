"""Recursive retention-safety policy for agent-run traces."""

from typing import Any, Final, cast

_MAX_STRING: Final = 500
_TRUNCATION_SUFFIX: Final = "…(truncated)"
_SENSITIVE_KEY_MARKERS: Final = ("secret", "token", "password", "authorization")
_REDACTED: Final = "[redacted]"


def truncate_trace_text(value: str) -> str:
    """Cap retained trace text and mark values that were shortened."""
    if len(value) <= _MAX_STRING:
        return value
    return value[:_MAX_STRING] + _TRUNCATION_SUFFIX


def _is_sensitive_key(key: str) -> bool:
    """Report whether an argument key names credential material."""
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _redact_value(value: Any) -> Any:
    """Walk JSON-like arguments so nested credentials cannot bypass redaction."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested_value in cast("dict[Any, Any]", value).items():
            rendered_key = str(key)
            redacted[rendered_key] = (
                _REDACTED
                if _is_sensitive_key(rendered_key)
                else _redact_value(nested_value)
            )
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in cast("list[Any]", value)]
    if isinstance(value, str):
        return truncate_trace_text(value)
    return value


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask credentials and truncate retained string arguments.

    ```python
    assert redact_args({"auth": {"token": "abc"}}) == {
        "auth": {"token": "[redacted]"}
    }
    ```
    """
    return cast("dict[str, Any]", _redact_value(args))


def summarize_result(result: object) -> object:
    """Reduce bulk tool output while retaining small diagnostic structure."""
    if isinstance(result, list):
        return {"kind": "collection", "count": len(cast("list[Any]", result))}
    if isinstance(result, dict):
        summary: dict[str, Any] = {}
        for key, value in cast("dict[Any, Any]", result).items():
            summary[str(key)] = summarize_result(value)
        return summary
    if isinstance(result, str):
        return truncate_trace_text(result)
    return result
