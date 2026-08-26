# Deploying Tether

This is the runbook for the target architecture in
[ADR 0030](./adr/0030-open-webui-owns-assistant-runtime.md). The migration is
still local. Production continues to run the locked pre-migration release until
the migration PR is merged and a separate cutover is explicitly approved.

Do not use the cutover commands against production during migration work.

## Deployment shape

Compose runs two services:

- `host` is the headless Python capability host on loopback port `8000`.
- `open-webui` is stock Open WebUI on loopback port `3000`.

Tailscale runs on the VM outside Compose and terminates public HTTPS:

- Existing HTTPS 443 proxies to host port `8000`. Android Health Connect and
  its bearer-authenticated sync keep this origin.
- HTTPS 8443 proxies to Open WebUI port `3000`.

The Open WebUI service uses this exact official image:

```text
ghcr.io/open-webui/open-webui:v0.11.1@sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16
```

Do not replace the digest with `main`, `latest`, or a floating release tag.
Changing it requires release-note review and a repeat of the integration smoke.

The host has the `data` volume. Open WebUI has the separate `open-webui-data`
volume mounted at `/app/backend/data`. It has no Docker socket, host filesystem,
Tether data volume, Pi credential mount, or Ollama service.

## Local deployment check

Use this path to verify the production images, not for each Python edit.

```sh
just bootstrap
```

Generate a different value for each required secret and set:

```dotenv
TETHER_API_TOKEN=<Android Health Connect token>
TETHER_OPEN_WEBUI_TOKEN=<Open WebUI tool token>
WEBUI_SECRET_KEY=<Open WebUI session secret>
WEBUI_URL=http://127.0.0.1:3000
```

Then build and start both services:

```sh
docker compose config --quiet
just app-start
docker compose ps
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:3000/health
```

Open <http://127.0.0.1:3000> and complete
[the one-time setup](#one-time-open-webui-setup). `docker compose down` keeps
all volumes. `docker compose down -v` deletes local Tether and Open WebUI data.

## Required secrets and provider

1Password is the source of truth for production secrets. Copy
`deploy/.env.example` to `/srv/tether/.env`, set mode `0600`, and fill it by
hand. Never commit the rendered file.

Generate these independently:

- `TETHER_API_TOKEN` authenticates Android Health Connect at the host origin.
- `TETHER_OPEN_WEBUI_TOKEN` authenticates Open WebUI schema fetches and tool
  calls inside the Compose network.
- `WEBUI_SECRET_KEY` signs Open WebUI sessions.

`TETHER_OPEN_WEBUI_TOKEN` must not equal `TETHER_API_TOKEN`. The browser must
never receive either token from the Tether host.

Set the production Open WebUI origin:

```dotenv
WEBUI_URL=https://<host>.<tailnet>.ts.net:8443
```

Open WebUI also needs an API credential for a supported model provider. Pi's
ChatGPT/Codex subscription login does not work with Open WebUI. Before cutover,
choose one API-backed model with reliable native function calling and record
its expected pricing. Store its credential through Open WebUI's supported
environment settings or admin UI, never in source control.

## Open WebUI safety settings

Compose starts the pinned release with:

```dotenv
ENABLE_SIGNUP=false
ENABLE_PERSISTENT_CONFIG=true
ENABLE_AUTOMATIONS=false
ENABLE_CODE_EXECUTION=false
ENABLE_CODE_INTERPRETER=false
ENABLE_OLLAMA_API=false
ENABLE_TOOL_PERMISSIONS=true
```

Do not enable arbitrary code execution, terminal access, external MCP servers,
community Functions, Ollama, or Automations during the first release. Open
WebUI `v0.11.1` tool permissions and approvals are experimental. They pause
interactive chat tool calls but do not protect Automations.

## One-time Open WebUI setup

Complete this privately before publishing the HTTPS 8443 Funnel listener. Keep
the `open-webui-data` volume afterward; it preserves the setup.

1. Open local Open WebUI and create the first account.
2. Confirm the first account is an admin and a second signup is rejected.
3. Add one API-backed provider and one default model with native function
   calling.
4. Add an OpenAPI tool server with these exact values:

   | Setting        | Value                                   |
   | -------------- | --------------------------------------- |
   | URL            | `http://host:8000`                      |
   | Spec path      | `tools/openapi.json`                    |
   | Authentication | Bearer                                  |
   | Key            | the dedicated `TETHER_OPEN_WEBUI_TOKEN` |
   | ID             | `tether`                                |

5. Create one Workspace Model named `Tether`.
6. Paste the checked-in
   [`deploy/open-webui/tether-system-prompt.md`](../deploy/open-webui/tether-system-prompt.md)
   as its system prompt.
7. Attach only the `tether` tool server to that model and enable native function
   calling.
8. Set tool approval mode to `ask`. During the trial, accept confirmation for
   read tools as well as mutations.
9. Enable Open WebUI native memory. Configure voice transcription and TTS for
   the production acceptance gate.
10. Configure Open WebUI's built-in web search only if wanted. Do not expose the
    former Tether Tavily tool.
11. Disable temporary chats and public sharing for the trial.
12. Reconfirm that Automations, code execution, the code interpreter, Ollama,
    external MCP servers, and community Functions are disabled.

Do not bootstrap Open WebUI by writing its private database or calling
undocumented admin routes.

## VM provisioning

The existing production VM and HTTPS 443 Funnel listener remain in place. For a
new VM, use `deploy/cloud-init.yaml`, a persistent Tailscale node, Docker with
the Compose plugin, restic, and an x86 host with enough memory for both
containers. Keep SSH private to the tailnet and leave the public firewall with
no inbound rules.

Initialize `/srv/tether` without deleting a preserved Pi directory:

```sh
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

Keep `/srv/tether/pi-agent` intact through the migration trial. The new stack
does not mount or use it, but full rollback needs its old credentials and mode.

## Migration cutover

Cutover is a future maintenance operation. Do not run these steps while the
migration remains local.

1. Confirm the complete migration gate passes on the merged commit.
2. Record the merge SHA and preserve the locked pre-migration Git revision,
   image, and `/srv/tether/pi-agent` directory.
3. Run the final old-stack backup and verify that restic lists it.
4. Pull the VM checkout because `compose.yaml` and `deploy/` change.
5. Deploy the new host image and pinned Open WebUI image with a fresh
   `open-webui-data` volume.
6. Complete the private one-time Open WebUI setup.
7. Run any explicitly approved linked-Todo cleanup after reviewing its report.
8. Publish Open WebUI on HTTPS 8443 without changing the existing 443 listener:

   ```sh
   sudo tailscale funnel --bg --https=8443 3000
   sudo tailscale funnel status
   ```

9. Confirm signup remains closed.
10. Run the production acceptance checks below.
11. Begin the trial. Do not run the old Pi chat stack in parallel.

Do not import old Tether chats, mirror Open WebUI transcripts, or copy Open
WebUI state into Tether.

## Production acceptance gates

Local implementation and the standalone smoke do not satisfy these gates. Run
all of them against the proposed production deployment before declaring cutover
healthy:

- Open WebUI login works on desktop and a physical phone at HTTPS 8443.
- Signup is disabled and only the expected account exists.
- The actual selected provider and model stream chat and perform native function
  calls with Tether's schema.
- Interactive approval pauses, survives refresh, and resumes the same tool call.
- Todo create and list, Bucket capture and search, and current Health analysis
  work through Tether tools.
- Open WebUI voice transcription and TTS both work from a physical phone over
  Funnel HTTPS 8443.
- Conversations survive browser refresh and an Open WebUI container restart.
- The browser does not receive `TETHER_OPEN_WEBUI_TOKEN`.
- An invalid token cannot read `/tools/openapi.json` or call a tool.
- Open WebUI cannot reach the Docker socket, Tether volume, Pi directory, or
  host filesystem.
- Host and Open WebUI logs contain no secrets, prompts, request bodies, or
  health values.
- Android Capture completes a real Health Connect sync at the unchanged HTTPS
  443 origin with `TETHER_API_TOKEN`.
- The full restore drill recovers both Tether SQLite databases, `.env`, the
  complete Open WebUI volume, admin access, provider configuration, tool-server
  configuration, and a persisted conversation.
- The user explicitly approves production cutover after reviewing these results.

## Funnel operations

The existing HTTPS 443 listener belongs to the host and must survive both
cutover and rollback. Add only the Open WebUI listener with:

```sh
sudo tailscale funnel --bg --https=8443 3000
```

Remove only that listener by repeating its flags and target before `off`:

```sh
sudo tailscale funnel --bg --https=8443 3000 off
```

Do not use `tailscale funnel reset`; it would also remove the retained 443
listener. Check `sudo tailscale funnel status` after either command.

## Routine updates after cutover

Deploy only clean, validated, merged `main`. If a release changes
`compose.yaml` or `deploy/`, update the VM checkout before restarting services:

```sh
ssh tether@tether 'cd /srv/tether && git pull --ff-only'
```

The existing deployment command publishes the host image and runs Compose:

```sh
TETHER_DEPLOY_HOST=tether@tether just deploy
```

Open WebUI remains pinned by `compose.yaml`; Compose pulls the exact digest.
Routine updates preserve `data` and `open-webui-data`.

## Full migration rollback

This is an operational rollback, not a mode in the new application. An image pin
alone is insufficient because the old host needs its old Compose definition and
Pi credentials.

1. Stop Open WebUI and remove only its Funnel listener:

   ```sh
   cd /srv/tether
   docker compose stop open-webui
   sudo tailscale funnel --bg --https=8443 3000 off
   ```

2. Preserve `open-webui-data`. Do not delete the volume.
3. Check out the recorded pre-migration VM Git revision so the old
   `compose.yaml`, deploy files, and image definition return.
4. Pin `TETHER_IMAGE_TAG` to the recorded pre-migration image tag.
5. Confirm `/srv/tether/pi-agent` still contains the old credentials, owner, and
   `0700` permissions.
6. Run `docker compose pull && docker compose up -d`.
7. Verify the old HTTPS login, chat, tools, and Android Health Connect sync.
8. Restore the pre-migration database only if the live database is corrupt.
   Ordinary rollback should reuse it because the migration makes additive
   schema changes and leaves old tables inert.

The locked rollback point is Git commit and image tag `c956fff`, with image
digest
`sha256:9c684b0ac3bb1863ff56eeb48dbcbf0bab4d523fd836948bda108ba7f39d238c`.
Retain that image and the old Pi directory for the entire trial.

Domain mutations made through Open WebUI tools may appear in the restored old
UI. Open WebUI chats and native memories will not. This is accepted.

## Android Health Connect

Android Capture continues to use the host's existing HTTPS 443 origin and
`TETHER_API_TOKEN`. Do not point it at Open WebUI or port 8443. Test the
authenticated seam without exposing health values:

```sh
origin=https://<host>.<tailnet>.ts.net
curl -i -H "Authorization: Bearer $TETHER_API_TOKEN" \
  "$origin/api/telemetry/health-connect/sync-state?installation_id=funnel-smoke&record_types=steps"
```

Expect HTTP 200, then run a real sync from the physical Android app. The active
wire contract is [`health-connect-wire-v3.md`](./health-connect-wire-v3.md).

## Backups

The VM's systemd timer runs `deploy/backup.sh` outside Compose and sends one
client-side encrypted restic backup to Backblaze B2. Each run contains:

- consistent `VACUUM INTO` snapshots of `tether.sqlite3` and
  `telemetry.sqlite3`
- a complete `open-webui-data.tar` archive of the Open WebUI volume
- production `.env`

The script briefly stops only `open-webui`, mounts its volume read-only in a
short-lived container, archives it, and restarts the service. EXIT and error
traps restart Open WebUI if a later command fails. It no longer backs up Pi
sessions. Keep `/srv/tether/pi-agent` separately until the migration trial is
accepted because full rollback still needs it.

Restic retains seven daily and four weekly snapshots. healthchecks.io receives
start, success, and failure pings.

### One-time backup setup

```sh
cp deploy/restic.env.example /srv/tether/restic.env
chmod 600 /srv/tether/restic.env
$EDITOR /srv/tether/restic.env
set -a; source /srv/tether/restic.env; set +a
restic init
sudo cp deploy/tether-backup.service deploy/tether-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tether-backup.timer
sudo systemctl start tether-backup.service
journalctl -u tether-backup.service -e
```

Store `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, B2 credentials, and the
healthchecks.io URL in 1Password before copying them to the VM.

### Restore drill

Restore into scratch space first and locate files rather than assuming restic's
temporary root name:

```sh
set -a; source /srv/tether/restic.env; set +a
restic snapshots
rm -rf /tmp/tether-restore
restic restore latest --target /tmp/tether-restore
tether_db=$(find /tmp/tether-restore -type f -name tether.sqlite3 -print -quit)
telemetry_db=$(find /tmp/tether-restore -type f -name telemetry.sqlite3 -print -quit)
open_webui_archive=$(find /tmp/tether-restore -type f \
  -name open-webui-data.tar -print -quit)
environment_file=$(find /tmp/tether-restore -type f -name env -print -quit)
test -n "$tether_db"
test -n "$telemetry_db"
test -n "$open_webui_archive"
test -n "$environment_file"
```

Run `pragma integrity_check` on both SQLite snapshots. Restore them with the host
stopped, using a helper container to stage each file before replacing the live
copy.

Restore Open WebUI only with its service stopped. Preserve the current volume
before clearing it, then extract the complete archive into the fresh target
volume. Start Open WebUI and verify:

- admin login
- provider and default-model configuration
- the `tether` tool-server configuration
- the Tether Workspace Model and checked-in prompt
- one restored conversation and native memory

A backup is not accepted until this drill succeeds in a fresh Compose project.

### Total-loss recovery

Recovery requires the repository, 1Password secrets, and restic data. Restore
the two Tether databases, production environment, and the full Open WebUI volume
before starting both services.

During the migration trial, total-loss recovery of the old stack additionally
requires the recorded old Git revision and image plus a protected copy of
`/srv/tether/pi-agent`. The Open WebUI backup alone cannot restore the Pi stack.

## Logs and resource checks

Both containers log to Docker:

```sh
docker compose logs -f host
docker compose logs -f open-webui
docker stats --no-stream
free -h
sudo journalctl -k --grep="out of memory\|oom-kill"
```

The host logs structured requests, tool outcomes, and ingestion work. Open
WebUI logs assistant activity. Neither log may contain bearer credentials,
provider keys, prompts, request bodies, or health values.
