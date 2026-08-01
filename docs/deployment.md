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

- `data` → `/data`: the independent SQLite sources of truth (`tether.sqlite3`
  and `telemetry.sqlite3`) and the derived markdown KB (`/data/kb`). Compose
  sets `TETHER_TELEMETRY_DATABASE_PATH=/data/telemetry.sqlite3` explicitly.
- `model-cache` → `/cache`: the fastembed ONNX model download.

`docker compose down` keeps the volumes; `down -v` deletes them.

## Deploy to the VM

This repo builds the image **locally** (x86 → x86, same arch as the VM), pushes
it to GHCR, then `ssh`es to the VM and pulls it — wrapped in `just deploy`. There
is no CI; `main` staying green (the validation gate) is what stands in for it.

### Live production and routine releases

The live VM's tailnet host alias is `tether`; SSH as `tether@tether`. After a PR
is merged, deploy a clean, validated `main` from the laptop:

```sh
git switch main
git pull --ff-only
# Run the validation gate from AGENTS.md before publishing.
TETHER_DEPLOY_HOST=tether@tether just deploy
```

`just deploy` updates the image only. If the release changes `compose.yaml` or
anything under `deploy/`, first update the VM checkout:

```sh
ssh tether@tether 'cd /srv/tether && git pull --ff-only'
```

A rollback leaves `TETHER_IMAGE_TAG` pinned in the VM's `.env`. Remove that line
before a forward release; ordinary `just deploy` does not remove it. GHCR login
and package-public bootstrap are covered under [First deploy](#3-first-deploy).

### 1. Provision the box (HITL)

- Prefer a Hetzner **CX43** (8 shared vCPU / 16 GB / 160 GB) for generous
  headroom. If cost-optimized capacity is unavailable, **CPX22** (2 vCPU / 4 GB /
  80 GB, x86) is the field-tested minimum for the current single-user workload;
  monitor memory/swap and resize if needed. Use Falkenstein and Debian 13. Paste
  `deploy/cloud-init.yaml` into Hetzner's "Cloud config" field at creation,
  after filling in its two placeholders:
  - `CHANGEME_SSH_PUBLIC_KEY` — your SSH public key (password auth is disabled;
    key-only from first boot).
  - `CHANGEME_TAILSCALE_AUTHKEY` — a one-shot Tailscale pre-auth key
    ([admin console → Keys](https://login.tailscale.com/admin/settings/keys)):
    reusable=false, ephemeral=false (the node must persist across reboots).
  cloud-init installs Docker (official repo) + the compose plugin, Tailscale,
  a 2 GB swapfile, unattended-upgrades (security-only, no auto-reboot), and
  creates `/srv/tether` (owned by the `tether` user) + `/srv/tether/pi-agent`.
  The file doubles as the disaster-recovery runbook — see
  [Total-loss recovery](#total-loss-recovery). Attach a Hetzner firewall with
  inbound TCP 22 restricted to the provisioning laptop's current `/32`; leave
  outbound unrestricted. Once tailnet SSH survives a reboot, remove that rule
  so the firewall has no public inbound rules.
- In the Tailscale Machines page, name the node `tether`; this becomes the
  stable SSH/MagicDNS alias. In the
  [tailnet admin console](https://login.tailscale.com/admin/dns), enable
  **MagicDNS** and **HTTPS Certificates**.
- SSH in as `tether@<box-ip>` (or the tailnet name once Tailscale is up) and
  terminate HTTPS at the machine's `*.ts.net` name, proxying to the host:
  ```sh
  sudo tailscale serve --bg 8000
  ```
  This gives a real, browser-trusted cert with no domain to own and no certbot.
  `serve` (not `funnel`) keeps the app tailnet-private — only your own tailnet
  devices reach it. Revoke the one-shot auth key and delete any local rendered
  cloud-init copy after provisioning succeeds.

### 2. Assemble secrets on the box

1Password is the source of truth for every secret below — write them there
first, then copy onto the VM (never the other way around).

Cloud-init pre-creates `/srv/tether/pi-agent`, so `git clone ... .` would reject
the nonempty destination. Initialize the checkout around that directory:

```sh
ssh tether@<box>
cd /srv/tether
git init -b main
git remote add origin https://github.com/crpier/tether.git
git fetch origin main
git checkout -B main origin/main
git branch --set-upstream-to=origin/main main

cp deploy/.env.example .env
cp deploy/restic.env.example restic.env
chmod 600 .env restic.env
$EDITOR .env restic.env
```

Fill in `.env` (see the template's comments for detail on each var):
`TETHER_APP_PASSWORD`, `TETHER_SESSION_SECRET`, `TETHER_API_TOKEN`,
`TETHER_STT_API_KEY`, `TETHER_DEFAULT_MODEL` / `TETHER_MODEL_ALLOWLIST`.
Generate `TETHER_API_TOKEN` independently and enter the same value in the
phone/watch capture settings; it is the static bearer credential for those
non-browser clients. Leave
`TETHER_SECURE_COOKIES=true` (the template's default — the VM is only ever
reached over Tailscale HTTPS).

The VM has no native Node/pnpm install. Authorize the provider locally with pi,
then copy its credential and lock down permissions:

```sh
scp ~/.pi/agent/auth.json tether@<box>:/srv/tether/pi-agent/auth.json
ssh tether@<box> 'chmod 700 /srv/tether/pi-agent && chmod 600 /srv/tether/pi-agent/auth.json'
```

The container silently refreshes it afterward. `pi-agent/auth.json` is durable
across deploys but is not currently in the backup set; provider reauthorization
or a separately secured copy is the recovery path.

Fill in `restic.env` — see [Backups](#backups) below.

### 3. First deploy

From your laptop (not the VM):

```sh
gh auth refresh -h github.com -s write:packages
gh auth token | docker login ghcr.io -u <github-user> --password-stdin
TETHER_DEPLOY_HOST=tether@<box> just deploy
```

`just deploy` builds the image, tags it `:<git-sha>` and `:latest`, pushes both
to `ghcr.io/crpier/tether`, then `ssh`es in and runs `docker compose pull &&
docker compose up -d`. A newly created GHCR package defaults private: after the
first successful push, change the `tether` package visibility to public before
the VM's anonymous pull. Images contain code, not runtime secrets.

- `restart: unless-stopped` plus Docker-enabled-at-boot (cloud-init) keeps the
  host running across reboots and crashes.
- Open `https://<box>.<tailnet>.ts.net` from a tailnet device (laptop and
  phone): the SPA loads and login → chat works over HTTPS.

If this is a fresh box (not yet holding real data), see
[Migrating from local](#migrating-from-local) next.

## Update flow

Use [Live production and routine releases](#live-production-and-routine-releases).
The deploy rebuilds and pushes `:<git-sha>` + `:latest`, then runs `pull && up
-d` on the VM. The `data`/`model-cache` volumes are untouched. Verify HTTPS
login/chat after every release.

## Rollback

```sh
TETHER_DEPLOY_HOST=tether@<box> just deploy-rollback <previous-short-sha>
```
Pins `TETHER_IMAGE_TAG=<sha>` in the VM's `.env` (that tag must already exist on
GHCR — `just deploy` always leaves the prior sha there) and re-runs `pull && up
-d`. The `data` volume is untouched, so the source of truth survives. To resume
tracking `latest`, remove the `TETHER_IMAGE_TAG` line from the VM's `.env` and
redeploy. Do this explicitly: ordinary `just deploy` currently does not clear
the rollback pin.

## Migrating from local

Move local dev data onto the VM's durable volumes once, before treating the VM
as the live instance. **Never run two live instances at once** — the YouTube/
Gmail sync workers and scheduled triggers both write, and two writers racing
against the same upstream state (or double-firing a trigger) is a correctness
bug, not just wasted API quota. Stop `just dev` for good once the VM is live.

Local `kb_root` is `.tether/`, not `.tether/kb`; its top-level UUID-named
Markdown files are the KB. Do not copy the whole directory: it also contains
the live DB, OAuth files, logs, sessions, and disposable indexes.

On the laptop, with local dev stopped:

```sh
rm -rf /tmp/tether-kb-migrate
rm -f /tmp/tether-migrate.sqlite3
sqlite3 .tether/tether.sqlite3 \
  "VACUUM INTO '/tmp/tether-migrate.sqlite3'"
mkdir -m 700 /tmp/tether-kb-migrate
sqlite3 .tether/tether.sqlite3 \
  "select id from memory where tethered_at is not null and deleted_at is null" |
while read -r id; do
  cp ".tether/${id}.md" /tmp/tether-kb-migrate/
done
sqlite3 /tmp/tether-migrate.sqlite3 "pragma integrity_check;"  # expect: ok

scp /tmp/tether-migrate.sqlite3 tether@<box>:/tmp/
scp -r /tmp/tether-kb-migrate tether@<box>:/tmp/
```

Stop the app and use a helper container to validate and atomically swap the
volume contents. Keeping the old files until browser verification makes the
cutover reversible:

```sh
ssh tether@<box>
cd /srv/tether
docker compose stop host
docker compose run --rm --no-deps \
  --entrypoint sh \
  -v /tmp/tether-migrate.sqlite3:/import/tether.sqlite3:ro \
  -v /tmp/tether-kb-migrate:/import/kb:ro \
  host -c '
    set -eu
    cp /import/tether.sqlite3 /data/tether.sqlite3.migrate
    python3 -c "import sqlite3; assert sqlite3.connect(\"/data/tether.sqlite3.migrate\").execute(\"pragma integrity_check\").fetchone() == (\"ok\",)"
    rm -rf /data/kb.migrate
    mkdir /data/kb.migrate
    cp -a /import/kb/. /data/kb.migrate/
    rm -f /data/tether.sqlite3.pre-migration
    mv /data/tether.sqlite3 /data/tether.sqlite3.pre-migration
    mv /data/tether.sqlite3.migrate /data/tether.sqlite3
    rm -rf /data/kb.pre-migration
    mv /data/kb /data/kb.pre-migration
    mv /data/kb.migrate /data/kb
  '
docker compose start host
```

After counts and browser-visible data match, remove the `*.pre-migration`
volume files and local/remote `/tmp` staging files.

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
lifecycle). The script uses Python's SQLite driver inside the container to make
independent, consistent `VACUUM INTO` snapshots of `/data/tether.sqlite3` and
`/data/telemetry.sqlite3`, then backs up both snapshots, all of `/data/kb`, and
`.env` in one restic run. Failure to snapshot or copy either database fails the
whole run; a partial source-of-truth backup is never reported as successful.
Retention: `--keep-daily 7 --keep-weekly 4 --prune`. Every run pings
healthchecks.io — success, and `/fail` on any error via a shell trap — so a
run that fails *or silently stops happening* (VM down, timer disabled) alerts.

Current coverage is intentionally explicit:

- Included: both SQLite sources of truth (`tether.sqlite3` and
  `telemetry.sqlite3`), `/data/kb`, and production `.env`.
- `/data/kb` currently contains Markdown plus derived Lance indexes and pi
  sessions. Backing those derived files is safe but made the first restore about
  544 MiB; recovery does not depend on the index copy.
- Excluded: `/srv/tether/pi-agent`, `/data/youtube`, and other OAuth/ingestion
  state outside `/data/kb`.
  Reauthorize those providers or protect them separately until coverage grows.
- `restic.env` is restored from 1Password, not from the restic repository.

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

Because `backup.sh` passes its absolute temporary work directory to restic, a
restore retains a random `tmp/tmp.*` prefix. Locate the DB rather than assuming
a flat target path. Debian has Python 3 but the cloud-init package set does not
install the `sqlite3` CLI:

```sh
set -a; source /srv/tether/restic.env; set +a
restic snapshots
rm -rf /tmp/tether-restore
restic restore latest --target /tmp/tether-restore
tether_db=$(find /tmp/tether-restore -type f -name tether.sqlite3 -print -quit)
telemetry_db=$(find /tmp/tether-restore -type f -name telemetry.sqlite3 -print -quit)
test -n "$tether_db" && test -n "$telemetry_db"
for db in "$tether_db" "$telemetry_db"; do
  python3 - "$db" <<'PY'
import sqlite3
import sys

result = sqlite3.connect(sys.argv[1]).execute("pragma integrity_check").fetchone()
print(f"{sys.argv[1]}: {result[0]}")
assert result == ("ok",)
PY
done
root=$(dirname "$tether_db")
test "$(dirname "$telemetry_db")" = "$root"
test -f "$root/env"
find "$root/kb" -maxdepth 1 -type f -name '*.md' -print
```

For a scratch restore into the live volume, stop the host, stage and recheck
both files, then swap both sources of truth together. Keep the old copies until
HTTPS login/chat and Health Connect current-row counts are verified:

```sh
cd /srv/tether
docker compose stop host
docker compose run --rm --no-deps --entrypoint sh \
  -v "$root:/restore:ro" host -c '
    set -eu
    for name in tether telemetry; do
      cp "/restore/${name}.sqlite3" "/data/${name}.sqlite3.restore"
      python3 -c "import sqlite3; assert sqlite3.connect(\"/data/${name}.sqlite3.restore\").execute(\"pragma integrity_check\").fetchone() == (\"ok\",)"
      rm -f "/data/${name}.sqlite3.pre-restore"
      mv "/data/${name}.sqlite3" "/data/${name}.sqlite3.pre-restore"
      mv "/data/${name}.sqlite3.restore" "/data/${name}.sqlite3"
    done
  '
docker compose start host
# Verify HTTPS login/chat and Health Connect current-row counts before deleting
# /data/*.pre-restore through a helper container.
```

Also verify the dead-man's-switch: temporarily shorten the healthchecks.io
period/grace, reset it with a success ping, disable the timer, and confirm a
missed-ping alert. Restore the 1-day period/2-hour grace, send a success ping,
and re-enable the timer. If restic reports a stale lock after an interrupted
drill, first verify no restic process is active, then run `restic unlock`.

### Total-loss recovery

If the VM is gone entirely: 1Password (secrets) + this repo (cloud-init +
compose + Dockerfile) + the B2 bucket (data) is everything needed to rebuild.

1. Rent a fresh x86 CX43 or CPX22, paste `deploy/cloud-init.yaml` (filled in) —
   see [Provision the box](#1-provision-the-box-hitl).
2. Assemble `.env` and `restic.env` from 1Password — see
   [Assemble secrets on the box](#2-assemble-secrets-on-the-box) and the
   restore-drill commands above.
3. Restore latest to a scratch directory, locate both databases despite the
   random restored root, and integrity-check each as shown in the drill. Copy
   that root's `tether.sqlite3`, `telemetry.sqlite3`, `kb`, and `env` into the
   named volume/checkout using the stopped helper-container pattern in
   [Migrating from local](#migrating-from-local). Restore `restic.env` from
   1Password.
4. Reauthorize/copy pi provider credentials and any enabled ingestion OAuth
   state; those are outside the current backup set.
5. Deploy from the laptop to bring the app up, verify data, then enable
   `tether-backup.timer`.

## Resource monitoring

The single container includes Python, Node/pi, and FastEmbed, so its cgroup is
the whole app runtime:

```sh
ssh tether@tether 'docker stats --no-stream tether-host-1; free -h'
ssh tether@tether 'sudo journalctl -k --grep="out of memory\|oom-kill"'
```

On CPX22, resize if Tether sustains roughly 3 GiB, swap use keeps growing, or
OOM events appear. Short CPU spikes around 100% are one vCPU and are expected
while rebuilding derived indexes.

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
