"""Read-only inspection surface for agent-run traces.

These routes render the in-process `AgentTraceRecorder`: a list of recent runs
and a single run's full trace (its ordered tool calls, envelopes, and timing).
They are mounted under `/api/`, so `AppSessionMiddleware` gates them behind the
app session like every other browser-facing route, but they are deliberately
kept out of the public OpenAPI document and generated client — tracing is an
operational aid, queried ad hoc, not part of the product API surface.

Everything rendered here is already redacted at record time (secrets masked,
bulk corpus content summarised to counts), so a trace is safe to return as-is.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.agent_trace import AgentTraceRecorder

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _recorder(request: Request) -> AgentTraceRecorder:
    """Return the host's agent-trace recorder."""
    return request.app.state.trace_recorder


def _limit(request: Request) -> int:
    """Parse a bounded `limit` query parameter, falling back to the default."""
    raw = request.query_params.get("limit")
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_LIMIT
    return max(1, min(parsed, _MAX_LIMIT))


async def list_runs(request: Request) -> Response:
    """Return the most recently started agent runs, newest first."""
    runs = _recorder(request).recent_runs(limit=_limit(request))
    return JSONResponse({"runs": [run.render() for run in runs]})


async def get_run(request: Request) -> Response:
    """Return one run's full trace, or 404 if it has been evicted/never existed."""
    run = _recorder(request).get_run(request.path_params["run_id"])
    if run is None:
        return JSONResponse({"detail": "run not found"}, status_code=404)
    return JSONResponse(run.render())


def trace_routes() -> list[Route]:
    """Mount the agent-trace inspection routes under `/api/traces`."""
    return [
        Route("/api/traces", list_runs, methods=["GET"]),
        Route("/api/traces/{run_id}", get_run, methods=["GET"]),
    ]
