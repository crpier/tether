# Health Connect sync wire contract v3

v3 extends [`health-connect-wire-v2.md`](./health-connect-wire-v2.md). The cursor,
baseline, idempotency, deletion, and logging rules are unchanged.

## Record type coverage

`record_types` may name any readable record type in the pinned AndroidX Health
Connect inventory. The four v2 typed records keep their typed shapes:
`exercise`, `heart_rate`, `sleep`, and `steps`.

All other record types use generic raw storage until Tether grows typed tables:

```json
{
  "metadata": {"id": "weight-1", "data_origin_package": "com.example.scale"},
  "start_time": 1700000000000,
  "end_time": null,
  "start_zone_offset_seconds": 0,
  "end_zone_offset_seconds": null,
  "payload": {"time": 1700000000000, "weight": {"kilograms": 72.5}}
}
```

The host persists generic records append-only in `hc_generic_record` with a
`hc_generic_record_current` view. `payload` is stored verbatim as canonical JSON;
it is never included in operational logs.

## Partial grants

Every stream is scoped to exactly the granted record-type set. Baseline
completion `ranges` must contain exactly those record types, not every possible
Health Connect type. This lets one unavailable, unsupported, or denied category
avoid blocking sync for granted categories.

## Canonical step projection

When the granted set includes `steps`, a successful record sync is followed by
`POST /api/telemetry/health-connect/step-aggregates`. This projection is
separate from the opaque changes cursor. The Android client obtains it through
Health Connect's Aggregate API without a data-origin filter, so Health Connect
applies the user's Activity app priority and removes overlapping sources.

```json
{
  "installation_id": "pixel-installation",
  "request_id": "stable-request-id",
  "start_time": 1777507200000,
  "end_time": 1787855585000,
  "buckets": [
    {
      "start_time": 1787850000000,
      "end_time": 1787853600000,
      "zone_offset_seconds": 10800,
      "count": 321
    }
  ]
}
```

The range is authoritative. Missing bucket starts inside it retire prior
canonical values. Repeated request IDs are idempotent and conflict if their
payload changes. The host keeps bucket versions append-only and ordinary
summaries expose only canonical totals. Raw `steps` records remain unchanged and
queryable for provenance.
