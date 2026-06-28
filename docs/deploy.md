# Deploying Tether

Tether runs as a **single `docker compose` service** (`host`). The image carries
everything the host needs at runtime: Python (uv) for the host process, Node for
the `pi` agent subprocess, the agent's installed deps, and the built SPA. The
host serves the SPA, the REST `/api`, and the `/ws` WebSocket on one port.

The same `compose.yaml` runs **locally over HTTP** and **on a VM behind Tailscale
HTTPS** — only the environment differs.

## What's in the image

- Built from the repo root `Dockerfile` (three stages: build the SPA, install the
  agent's Node deps, assemble the runtime).
- The repo layout (`apps/host`, `apps/agent`, `apps/web`) is preserved at `/app`
  because the host resolves the agent binary and SPA by walking up from its own
  installed package directory.
- `snekql` comes from PyPI; no editable/sibling source is needed to build.

## Local run (verify the whole stack on your machine)

1. Copy the env template and fill it in:
   ```sh
   cp .env.example .env
   # set TETHER_APP_PASSWORD, TETHER_SESSION_SECRET, and a provider API key
   ```
   Generate strong secrets:
   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Leave `TETHER_SECURE_COOKIES=false` locally (no HTTPS).
2. Build and start:
   ```sh
   docker compose up -d --build
   ```
3. Open <http://localhost:8000>, log in with `TETHER_APP_PASSWORD`, and you're in
   the chat view.

The published port binds to `127.0.0.1` only. If `8000` is taken on your machine
(e.g. a `just host` dev process), override the host-side port:
```sh
TETHER_HOST_PORT=8001 docker compose up -d --build
```

State lives on two named docker volumes, so `docker compose up` / redeploys never
touch your data:

- `data` → `/data`: the SQLite source of truth (`tether.sqlite3`) and the derived
  markdown KB (`/data/kb`).
- `model-cache` → `/cache`: the fastembed ONNX model download.

`docker compose down` keeps the volumes; `down -v` deletes them.

## Deploy to the VM

The VM steps below are manual (one-time). The image is built where the repo
lives (locally or on the box) and run with the same compose file.

### 1. Provision the box (HITL)

- Rent a Hetzner **CX22** (2 vCPU / 4 GB — the RAM floor is the host container
  running fastembed in-process).
- Install Docker and enable it at boot (`systemctl enable --now docker`).
- Install Tailscale **natively on the VM** (systemd, outside compose):
  ```sh
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up
  ```
- In the tailnet admin console, enable **MagicDNS** and **HTTPS Certificates**.
- Terminate HTTPS at the machine's `*.ts.net` name and reverse-proxy to the host:
  ```sh
  sudo tailscale serve --bg 8000
  ```
  This gives a real, browser-trusted cert with no domain to own and no certbot.
  `serve` (not `funnel`) keeps the app tailnet-private — only your own devices
  reach it.

### 2. Secrets on the box

Create `.env` next to `compose.yaml` (gitignored; never committed):

- `TETHER_APP_PASSWORD`, `TETHER_SESSION_SECRET` — real values.
- `TETHER_SECURE_COOKIES=true` — the app is served over HTTPS on the VM.
- `TETHER_DEFAULT_MODEL` / `TETHER_MODEL_ALLOWLIST` and the provider API key.

### 3. Build, run, and verify

```sh
docker compose up -d --build
```

- `restart: unless-stopped` plus Docker-enabled-at-boot keeps the host running
  across reboots and crashes.
- Open `https://<machine>.ts.net` from a tailnet device: the SPA loads and
  login → chat works over HTTPS.

## Deploy + rollback (manual)

This repo builds the image locally and tags it; there is no registry or CI.

**Deploy a new version:**
```sh
docker compose build
docker tag tether-host:latest tether-host:$(git rev-parse --short HEAD)
docker compose up -d
```
(Build on the VM, or `docker save | ssh … docker load` to ship the image.)

**Roll back** to a previous build:
```sh
docker tag tether-host:<previous-short-sha> tether-host:latest
docker compose up -d
```
The `data` volume is untouched by either, so the source of truth survives both.

## Notes

- **Closed-tab web push (VAPID)** is out of scope here (issue #77). On this
  deploy a fired Scheduled trigger is delivered over the **open** WebSocket — it
  reaches you when a tab is open. The `*.ts.net` HTTPS origin is the secure origin
  that future push work needs.
- **Encrypted off-box backups** of the SQLite source of truth are separate
  (issue #61), blocked on this deploy.
