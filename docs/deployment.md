# Deploying Tether

Tether runs as a **single `docker compose` service** (`host`). The image carries
everything the host needs at runtime: Python (uv) for the host process, Node for
the `pi` agent subprocess, the agent's installed deps, and the built SPA. The
host serves the SPA, the REST `/api`, and the `/ws` WebSocket on one port.

The same `compose.yaml` runs **locally over HTTP** and **on a VM behind Tailscale
HTTPS** — only the environment differs.

> **Developing, not deploying?** Don't iterate through `docker compose
> up --build` — it rebuilds the whole image on every change. Use the native
> host + web loop (`just dev`) instead; see [development.md](./development.md).
> The local-run section below is for verifying the *production image* end to end.

## What's in the image

- Built from the repo root `Dockerfile` (three stages: build the SPA, install the
  agent's Node deps, assemble the runtime).
- The repo layout (`apps/host`, `apps/agent`, `apps/web`) is preserved at `/app`
  because the host resolves the agent binary and SPA by walking up from its own
  installed package directory.
- `snekql` comes from PyPI; no editable/sibling source is needed to build.

## Local run (verify the whole stack on your machine)

This builds and boots the production image — use it to confirm a deploy works,
not to iterate. For the fast dev loop use `just dev` ([development.md](./development.md)).

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

This repo builds the image **locally** (x86 → x86, same arch as the VM), pushes
it to GHCR, then `ssh`es to the VM and pulls it — wrapped in `just deploy`. There
is no CI; `main` staying green (the validation gate) is what stands in for it.

### 1. Provision the box (HITL)

- Rent a Hetzner **CX43** (8 shared vCPU / 16 GB / 160 GB, ~€19.6/mo incl. VAT,
  Falkenstein), Debian 13. Paste `deploy/cloud-init.yaml` into Hetzner's "Cloud
  config" field at creation, after filling in its two placeholders:
  - `CHANGEME_SSH_PUBLIC_KEY` — your SSH public key (password auth is disabled;
    key-only from first boot).
  - `CHANGEME_TAILSCALE_AUTHKEY` — a one-shot Tailscale pre-auth key
    ([admin console → Keys](https://login.tailscale.com/admin/settings/keys)):
    reusable=false, ephemeral=false (the node must persist across reboots).
  cloud-init installs Docker (official repo) + the compose plugin, Tailscale,
  a 2 GB swapfile, unattended-upgrades (security-only, no auto-reboot), and
  creates `/srv/tether` (owned by the `tether` user) + `/srv/tether/pi-agent`.
  It's sized generously for future side projects; Tether is the only tenant
  today. The file doubles as the disaster-recovery runbook — see
  [Total-loss recovery](#total-loss-recovery).
- In the [tailnet admin console](https://login.tailscale.com/admin/dns), enable
  **MagicDNS** and **HTTPS Certificates**.
- SSH in as `tether@<box-ip>` (or the tailnet name once Tailscale is up) and
  terminate HTTPS at the machine's `*.ts.net` name, proxying to the host:
  ```sh
  sudo tailscale serve --bg 8000
  ```
  This gives a real, browser-trusted cert with no domain to own and no certbot.
  `serve` (not `funnel`) keeps the app tailnet-private — only your own tailnet
  devices reach it.

### 2. Assemble secrets on the box

1Password is the source of truth for every secret below — write them there
first, then copy onto the VM (never the other way around).

```sh
ssh tether@<box>
cd /srv/tether
git clone <this-repo-url> .          # or `git pull` on redeploy — see below
cp deploy/.env.example .env
cp deploy/restic.env.example restic.env
chmod 600 .env restic.env
$EDITOR .env restic.env
```

Fill in `.env` (see the template's comments for detail on each var):
`TETHER_APP_PASSWORD`, `TETHER_SESSION_SECRET`, `TETHER_STT_API_KEY`,
`TETHER_DEFAULT_MODEL` / `TETHER_MODEL_ALLOWLIST`. Leave
`TETHER_SECURE_COOKIES=true` (the template's default — the VM is only ever
reached over Tailscale HTTPS). Then authorize the agent's model provider:

```sh
mkdir -p /srv/tether/pi-agent && chmod 700 /srv/tether/pi-agent
PI_CODING_AGENT_DIR=/srv/tether/pi-agent /srv/tether/apps/agent/node_modules/.bin/pi
# /login openai-codex   (or /login opencode-go), then exit
```

(That needs `apps/agent` installed on the VM once — `pnpm -C apps/agent install
--prod`, or just run `just pi-auth`-equivalent from a laptop over `ssh -L` and
scp the resulting `auth.json` in. Either way it only has to happen once; the
container's silent refresh keeps it current afterward.)

Fill in `restic.env` — see [Backups](#backups) below.

### 3. First deploy

From your laptop (not the VM):

```sh
docker login ghcr.io -u <github-user>   # or: gh auth token | docker login ghcr.io -u <user> --password-stdin
TETHER_DEPLOY_HOST=tether@<box> just deploy
```

`just deploy` builds the image, tags it `:<git-sha>` and `:latest`, pushes both
to `ghcr.io/crpier/tether`, then `ssh`es in and runs `docker compose pull &&
docker compose up -d`.

- `restart: unless-stopped` plus Docker-enabled-at-boot (cloud-init) keeps the
  host running across reboots and crashes.
- Open `https://<box>.<tailnet>.ts.net` from a tailnet device (laptop and
  phone): the SPA loads and login → chat works over HTTPS.

If this is a fresh box (not yet holding real data), see
[Migrating from local](#migrating-from-local) next.

## Update flow

```sh
TETHER_DEPLOY_HOST=tether@<box> just deploy
```
Rebuilds, re-pushes `:<git-sha>` + `:latest`, and re-runs `pull && up -d` on the
VM. The `data`/`model-cache` volumes are untouched.

## Rollback

```sh
TETHER_DEPLOY_HOST=tether@<box> just deploy-rollback <previous-short-sha>
```
Pins `TETHER_IMAGE_TAG=<sha>` in the VM's `.env` (that tag must already exist on
GHCR — `just deploy` always leaves the prior sha there) and re-runs `pull && up
-d`. The `data` volume is untouched, so the source of truth survives. To resume
tracking `latest`, remove the `TETHER_IMAGE_TAG` line from the VM's `.env` and
redeploy.

## Migrating from local

Move local dev data onto the VM's durable volumes once, before treating the VM
as the live instance. **Never run two live instances at once** — the YouTube/
Gmail sync workers and scheduled triggers both write, and two writers racing
against the same upstream state (or double-firing a trigger) is a correctness
bug, not just wasted API quota. Stop `just dev` for good once the VM is live.

```sh
# 1. Snapshot the local SQLite DB (VACUUM INTO defragments + gives a consistent copy)
sqlite3 .tether/tether.sqlite3 "VACUUM INTO '/tmp/tether-migrate.sqlite3'"

# 2. Ship the DB snapshot + kb_root to the VM
scp /tmp/tether-migrate.sqlite3 tether@<box>:/tmp/tether.sqlite3
scp -r .tether/kb tether@<box>:/tmp/kb

# 3. On the VM, load them into the running container's data volume
ssh tether@<box>
docker compose cp /tmp/tether.sqlite3 host:/data/tether.sqlite3
docker compose cp /tmp/kb host:/data/kb
docker compose restart host
```

Then follow [YouTube ingestion](#youtube-ingestion) below to move the OAuth
token over, and demote local dev: keep using `just dev` for iteration, but
understand its DB/KB are now a stale fork — don't expect it to reflect what the
VM sees, and never point it at any real ingestion sync while the VM is live.

## YouTube ingestion

Optional. The container can't run the browser OAuth flow, so the token is
**authorized on a laptop and installed into the data volume** — where pi's silent
refresh writes back to a path that survives redeploys (`compose.yaml` sets
`TETHER_YOUTUBE_TOKEN_PATH=/data/youtube/token.json`).

1. On your laptop, install the Google clients and authorize once:
   ```sh
   uv sync --group youtube
   # place a Desktop-app OAuth client JSON at .tether/youtube-client-secret.json
   just youtube-auth          # opens a browser, caches .tether/youtube-oauth-token.json
   ```
2. Install the token into the running container's data volume and restart:
   ```sh
   just youtube-token-install            # docker compose cp into /data/youtube/token.json
   docker compose restart host
   ```
   (If you also want the client-secret in the volume — needed so an *expired*
   token can be re-minted in place — copy it too:
   `docker compose cp .tether/youtube-client-secret.json host:/data/youtube/client-secret.json`.)

Once the token is present the background ingestion sync activates on the next
host start. With no token, ingestion runs the in-memory fake and the sync stays
off — the rest of Tether is unaffected.

## KOReader ebook progress (kosync)

Optional. Tether can *be* the KOReader sync server: KOReader devices push
reading progress straight at the host, and a book crossing ~98% mints a single
"Finished reading …" memory. Off by default; enable it by setting all three:

```sh
TETHER_KOSYNC_ENABLED=true
TETHER_KOSYNC_USERNAME=<any username you pick>
TETHER_KOSYNC_USERKEY=<md5 of the password you'll enter in KOReader>
```

`TETHER_KOSYNC_USERKEY` is the **MD5 of the password**, not the password —
KOReader hashes it client-side and Tether compares the hash verbatim
(`printf %s 'yourpassword' | md5sum`). With any of the three unset the `/kosync`
routes are not mounted (404) and the rest of Tether is unaffected.

On each device, in KOReader: **Tools → Progress sync → Custom sync server** and
point it at your host's base URL with the `/kosync` path (e.g.
`https://tether.example/kosync`), then register/login with the username and
password above. Critically, set **Progress sync → Document matching method →
Filename**: Tether maps a book by `md5(basename)`, and KOReader's default binary
partial-MD5 cannot be mapped back to a title. Use `label_ebook` (or ask the
assistant, which can `list_unlabeled_ebooks`) to attach titles to hashes.

## Backups

Nightly `restic` → Backblaze B2, client-side encrypted, run by a systemd timer
**on the VM host, outside compose** (so it's independent of the app container's
lifecycle). Snapshotted: a `sqlite3 VACUUM INTO` DB copy, `kb_root`, and `.env`.
Retention: `--keep-daily 7 --keep-weekly 4 --prune`. Every run pings
healthchecks.io — success, and `/fail` on any error via a shell trap — so a
run that fails *or silently stops happening* (VM down, timer disabled) alerts.

### One-time setup (on the VM)

```sh
# restic + the deploy/ scripts arrive with the repo checkout at /srv/tether
cp deploy/restic.env.example /srv/tether/restic.env
chmod 600 /srv/tether/restic.env
$EDITOR /srv/tether/restic.env    # RESTIC_REPOSITORY, RESTIC_PASSWORD, B2 keys, healthchecks URL
```

Fill in, from 1Password (create these there first — restic's passphrase must
**never live only on the VM**, or a lost VM makes the B2 backup unrecoverable):

- `RESTIC_REPOSITORY` — `b2:<bucket>:restic`, a B2 bucket dedicated to Tether backups.
- `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` — a B2 application key scoped to that bucket.
- `RESTIC_PASSWORD` — the repo encryption passphrase (generate once, store in 1Password).
- `HEALTHCHECKS_PING_URL` — a [healthchecks.io](https://healthchecks.io) check's ping URL; set its expected period to ~1 day + grace.

Initialize the restic repo once (idempotent to re-run, but only needed the
first time):
```sh
set -a; source /srv/tether/restic.env; set +a
restic init
```

Install and enable the timer:
```sh
sudo cp deploy/tether-backup.service deploy/tether-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tether-backup.timer
sudo systemctl start tether-backup.service   # optional: run once now to verify
journalctl -u tether-backup.service -e       # check the run
```

### Restore drill (do this before you need it)

```sh
set -a; source /srv/tether/restic.env; set +a
restic snapshots                              # find the snapshot id
restic restore latest --target /tmp/tether-restore
sqlite3 /tmp/tether-restore/tether.sqlite3 "pragma integrity_check;"   # expect: ok
ls /tmp/tether-restore/kb                     # kb_root markdown present
```
Also verify the dead-man's-switch: disable the timer, wait past the
healthchecks.io check's grace period, and confirm the alert fires. Re-enable
the timer afterward.

### Total-loss recovery

If the VM is gone entirely: 1Password (secrets) + this repo (cloud-init +
compose + Dockerfile) + the B2 bucket (data) is everything needed to rebuild.

1. Rent a fresh Hetzner CX43, paste `deploy/cloud-init.yaml` (filled in) —
   see [Provision the box](#1-provision-the-box-hitl).
2. Assemble `.env` and `restic.env` from 1Password — see
   [Assemble secrets on the box](#2-assemble-secrets-on-the-box) and the
   restore-drill commands above.
3. `restic restore latest --target /srv/tether/restore`, then load the restored
   SQLite + `kb` + `.env` into place the same way as
   [Migrating from local](#migrating-from-local) (steps 2–3), and restore
   `restic.env` itself from 1Password.
4. `just deploy` (or `docker compose up -d --build` directly on the box) to
   bring the app up, then re-enable `tether-backup.timer`.

## Logs

The container emits structured JSON to stdout (captured by Docker). Render it
readable and optionally follow a single chat turn end to end by its `run_id`:

```sh
just logs              # all host logs, pretty-printed (needs jq)
just logs <run_id>     # only the lines servicing that turn
```

`run_id` is stamped on every host log line driving a chat prompt. Raw access is
`docker compose logs -f host`.

## Notes

- **Closed-tab web push (VAPID)** is out of scope here (issue #77). On this
  deploy a fired Scheduled trigger is delivered over the **open** WebSocket — it
  reaches you when a tab is open. The `*.ts.net` HTTPS origin is the secure origin
  that future push work needs.
- **No CI.** `just deploy` is a manual, laptop-initiated push; the validation
  gate (`AGENTS.md`/`CLAUDE.md`) is what keeps `main` deployable. CI is a
  documented follow-up, not in scope here.
