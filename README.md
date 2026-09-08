# Tether

An AI personal assistant.

## Backend

Requires `uv` and Python 3.14. `uv` can install Python if needed.

```sh
cd backend
uv sync --locked
uv run python tether/main.py --reload
```

With direnv enabled, you can also start from the repository root:

```sh
python backend/tether/main.py --reload
```

Omit `--reload` to disable automatic reload. Server startup is configured in
`backend/tether/main.py`.

Open `http://localhost:8000/docs`. No application routes exist yet.

Checks, from `backend/`:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

`snektest` is installed; no tests exist yet.

## Docker

From the repository root:

```sh
docker compose up --build
```

Open `http://localhost:8000/docs`. Backend source changes trigger reload.
Rebuild after dependency changes. The image runs without reload when started
outside Compose.

```sh
docker compose down
```

## Layout

- `backend/tether/`: `FastAPI` application
- `backend/tests/`: reserved for `snektest` tests
- `agent/`: reserved for pi integration
- `frontend/`: reserved for the `SolidJS` SPA
- `android/`: reserved for the Android app
