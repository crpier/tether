# Health Connect sync wire contract v2

Base path: `/api/telemetry/health-connect`. All routes require Tether's existing
session cookie or static bearer token. JSON uses snake_case. `contract_version`
must equal `2`; unknown versions and fields are rejected by the host contract.
Requests are bounded to 1,000 parent records per type, 10,000 deletions, and the
nested limits represented by the checked-in fixture at
`apps/host/tests/fixtures/health_connect/v2/representative-batch.json`.

## Stream identity and baseline

A cursor stream is identified by `installation_id` plus the exact set of
`record_types`: `exercise`, `heart_rate`, `sleep`, and `steps`.

- `GET /sync-state?installation_id=…&record_types=heart_rate,sleep,steps,exercise`
  returns `initial`, `baseline`, or `changes`, the generation, and the complete
  opaque token.
- `POST /sync-state/baselines` accepts `contract_version`, `installation_id`,
  `record_types`, `request_id`, and the fresh pre-read `starting_token`. It
  increments the generation. Repeating the same request is idempotent.
- Baseline pages use `mode: "baseline"` and must keep `next_token` equal to
  `expected_token`. This preserves the pre-baseline token while records page in.
- `POST /sync-state/baselines/complete` carries the expected token/generation
  plus, for every record type, an inclusive authoritative `start_time`/
  `end_time`. The host durably indexes record IDs as bounded baseline pages
  arrive; completion uses that generation index to tombstone missing current IDs
  wholly inside those bounds, then removes the temporary index. Records outside
  remain current. Completion
  changes the stream to `changes` without consuming the pre-baseline token.
- Live pages use `mode: "changes"`; their `expected_token` must equal durable
  state and `next_token` becomes durable in the same transaction as records.

## Batch

`POST /batches`:

```json
{
  "contract_version": 2,
  "mode": "baseline",
  "installation_id": "random-installation-id",
  "record_types": ["heart_rate", "sleep", "steps", "exercise"],
  "request_id": "random-page-id",
  "expected_token": "opaque",
  "next_token": "opaque",
  "records": {"heart_rate": [], "sleep": [], "steps": [], "exercise": []},
  "deletions": [{"record_type": "steps", "record_id": "upstream-id"}]
}
```

A committed `request_id` replay returns success without appending. Reusing it
with different content conflicts. A stale token conflicts. Validation,
parent/child appends, tombstones, request identity, and cursor mutation share one
transaction.

## Common metadata and units

Every live record has:

- `metadata.id`, `data_origin_package`, nullable epoch-millisecond
  `last_modified_time`, nullable `client_record_id`/`client_record_version`,
  nullable `recording_method`, and nullable `device` (`manufacturer`, `model`,
  original integer `type`).
- UTC epoch-millisecond start/end instants and separate nullable start/end zone
  offsets in seconds.

Metric fields:

- Heart rate: ordered samples (`time`, integer `beats_per_minute`).
- Sleep: nullable `title`/`notes`; ordered stages with start/end and original
  integer `stage`.
- Steps: integer `count` over its source interval.
- Exercise: original integer `exercise_type`, nullable `title`, `notes`, and
  `planned_exercise_session_id`; ordered segments (original integer type and
  repetitions), laps (`length_meters`), and route points (latitude/longitude,
  meter-valued altitude and accuracies).

The representative fixture is normative for nullability, ordering, nested
shape, enum preservation, and canonical units. Contract changes are additive
and require a new fixture/version; supported fields must never be silently
ignored.

## Storage/read contract

History is append-only in typed `hc_` tables. Every accepted parent version has
a server-monotonic `version_id`, upstream ID, nullable origin, upstream modified
time, server received time, request identity, payload identity, and tombstone
flag. Each parent table has a `_current` view selecting its latest live version.
Every child `_current` view joins through that selected parent version. Raw
measurements, notes, and complete opaque tokens are excluded from operational
logs.
