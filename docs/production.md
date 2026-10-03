# Running metric-runtime in production

> metric-runtime is experimental open-source software. This page describes
> what the runtime guarantees today and how to deploy it.

## The deployable unit

```
metric-runtime run --profile production
        │
        ├── metric_source   analytical data, read-only (DuckDB or Postgres)
        ├── runtime_store   durable runtime conclusions (Postgres)
        └── notifier        outbox delivery (webhook / logging / your adapter)
```

`run --profile production` survives restarts: everything it has concluded
(observations, metric state, committed evaluations, incidents, pending
notifications) lives in the Postgres runtime store, not in the process.

## 1. Configure profiles

`metric-runtime.yaml` (commit it) holds semantics and runtime policy:

```yaml
catalog:
  entrypoint: metrics.catalog:catalog

runtime:
  default_profile: production
  evaluation_interval: 15m     # window size / schedule for every metric
  evaluation_lag: 5m           # wait for late data after a window closes
  max_catchup_windows: 4       # after downtime, (re)try at most this many windows
  idle_interval: 5m            # longest sleep in continuous mode
  schedules:                   # optional per-metric overrides (global scope)
    weekly_revenue:
      evaluation_interval: 1d
    legacy_metric:
      enabled: false
  notifications:
    max_attempts: 10           # then the event is dead-lettered
    backoff_initial: 30s       # exponential: 30s, 1m, 2m, ... capped
    backoff_max: 1h
    lease: 5m                  # a claimed event is exclusive for this long
```

`connections.yaml` (do not commit secrets) wires resources into **roles**:

```yaml
connections:
  analytics:
    type: duckdb
    path: /data/analytics.duckdb
    fact_table: facts
    read_only: true

  runtime_db:
    type: postgres
    dsn: ${METRIC_RUNTIME_POSTGRES_DSN}   # or host/port/database/user/password/sslmode
    schema: metric_runtime                # dedicated schema for runtime tables
    claim_ttl: 15m

  ops_webhook:
    type: webhook
    url: ${METRIC_RUNTIME_WEBHOOK_URL}
    secret: ${METRIC_RUNTIME_WEBHOOK_SECRET}   # optional

profiles:
  production:
    metric_source: analytics
    runtime_store: runtime_db
    notifier: ops_webhook                 # omit for logging; `notifier: {type: none}` to drop
```

`state_store:` is accepted as a deprecated alias for `runtime_store:`.
Environment placeholders are resolved lazily: a missing variable only fails
for profiles that use that connection.

Install the Postgres extra: `pip install "metric-runtime[postgres]"`.

## 2. Validate offline, then migrate

```bash
metric-runtime validate --profile production      # no network I/O
metric-runtime store migrate --profile production # apply pending migrations
metric-runtime store status --profile production --check  # exit 1 if pending
```

Migrations are numbered SQL files (`001_initial.sql`, …) shipped in the
package and recorded with checksums in `<schema>.schema_migrations`.
`store migrate` applies **all pending files in one transaction** under a
transaction-scoped advisory lock:

- if any statement fails, nothing is applied (including earlier files of the
  same batch);
- concurrent `migrate` calls serialize — the second one finds nothing pending;
- editing an already-applied file is a hard error (checksum mismatch).

`run` refuses to start while migrations are pending and tells you to run
`store migrate`. Running migrations is an explicit deployment step — the
runtime never alters its schema implicitly.

## 3. Choose an execution mode

### `run --once` — one cycle, then exit (cron / CronJob / Airflow)

```bash
metric-runtime run --once --profile production
```

One cycle: evaluate every due window, drain the outbox, exit.

| Exit code | Meaning |
|---|---|
| 0 | every due evaluation committed (or nothing was due) |
| 1 | at least one metric failed, or the configuration is invalid |

`--now 2026-05-15T13:00:00Z` schedules against a fixed instant (backfills,
reproducible tests). `--json` prints a machine-readable report.

Kubernetes CronJob example:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: metric-runtime
spec:
  schedule: "*/15 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          restartPolicy: Never
          initContainers:
            - name: migrate
              image: ghcr.io/your-org/metric-runtime:latest
              args: ["store", "migrate", "--profile", "production"]
              envFrom: [{ secretRef: { name: metric-runtime } }]
          containers:
            - name: run
              image: ghcr.io/your-org/metric-runtime:latest
              args: ["run", "--once", "--profile", "production"]
              envFrom: [{ secretRef: { name: metric-runtime } }]
```

(In practice, run `store migrate` as a separate release step rather than on
every cron tick; it is idempotent either way.)

### `run` — continuous worker (Deployment / systemd)

```bash
metric-runtime run --profile production --log-level INFO
```

The loop runs a cycle, then sleeps until the next thing is due:

```
wait = clamp(min(next metric window due, next outbox retry, now + idle_interval) - now,
             1s, idle_interval)
```

- an outbox retry due in 30s does not wait for a 15-minute metric tick;
- a 24h schedule still wakes every `idle_interval` for a cheap heartbeat cycle;
- an empty catalog (or all metrics disabled) logs a warning once and keeps
  delivering the outbox;
- a cycle-level failure (e.g. store unreachable) retries with capped
  exponential backoff, bounded by `idle_interval`;
- SIGINT/SIGTERM finish the current cycle, close the executor and store, and
  exit. There is no busy loop.

Several `run` workers may share one runtime store: evaluation claims, ordered
commits and outbox leases make them cooperate safely.

## 4. Scheduling semantics

A tick `T` is the start of a window `[T, T + interval)`, aligned to the
interval in UTC. It becomes due at `T + interval + evaluation_lag`.

Each metric's **cursor** is the `effective_at` of its latest *committed*
evaluation. Due ticks are the ones after the cursor, of which at most
`max_catchup_windows` (the most recent) are evaluated; older missed windows
are logged and skipped. A never-evaluated metric starts at the most recent
windows instead of backfilling history.

| Window outcome | Committed? | Cursor | Re-run? |
|---|---|---|---|
| value (any state change or none) | yes | advances | no |
| NO_DATA (no rows / NULL / divide-by-zero / no baseline) | yes, state carried forward, no incidents/outbox | advances | no |
| execution error | no | unchanged | yes, until outside `max_catchup_windows` |

When a window of a metric fails, later windows of that metric are deferred
in the same cycle so state is always applied in `effective_at` order.

## 5. Notification delivery (transactional outbox)

```
evaluation transaction ── observation, state, incident, outbox intent ── COMMIT
                                                                           │
cycle end: claim due events (lease) ─► notify ─► mark delivered            │
                                     └─ fail ─► record attempt + backoff ◄─┘
                                                (dead-letter at max_attempts)
```

- Intent is committed atomically with the state change; delivery happens
  after commit, never inside the transaction.
- Claims are leases (`claim_token`, `claimed_until`). Only the current lease
  holder can mark an event delivered or failed; an expired lease makes the
  event claimable again.
- Delivery is **at-least-once**. Notifiers receive the stable `event_key` as
  their idempotency key so receivers can deduplicate.
- Dead-lettered events stay in the `outbox` table (`dead_lettered_at`,
  `last_error`) for inspection.

### Webhook notifier

A `type: webhook` connection in the `notifier` role POSTs one JSON document
per outbox event:

```json
{"event_id": "evt-...", "event_key": "...", "kind": "incident_opened",
 "metric": "revenue", "incident_id": "inc-0001",
 "previous_state": "DETECTED", "current_state": "OPEN",
 "message": "...", "created_at": "2026-05-15T12:30:00+00:00",
 "attempt": 1, "incident": {...}}
```

| Header | Value |
|---|---|
| `Idempotency-Key` | the event's `event_key` — deduplicate on this |
| `X-Metric-Runtime-Event` | the event `kind` |
| `X-Metric-Runtime-Signature` | `sha256=<hex HMAC of the raw body>` when `secret` is set |
| custom | any `headers:` from the connection (values are secrets) |

Any 2xx response marks the event delivered. Anything else — non-2xx,
redirect (redirects are never followed), timeout, connection error — counts
as a failed attempt and is retried with backoff. Error messages never include
the URL, since it often embeds a token.

## 6. Runtime store tables

All in the configured schema (default `metric_runtime`):

| Table | Holds |
|---|---|
| `observations` | one row per evaluated window |
| `metric_state` | current state per (metric, scope), with optimistic `version` |
| `evaluations` | committed evaluation records — the scheduler cursor |
| `evaluation_claims` | leased in-flight claims |
| `incidents` | incidents (`inc-0001` ids) |
| `outbox` | notification intents + delivery/lease columns |
| `schema_migrations` | applied migration versions + checksums |

The runtime store holds runtime conclusions only; it is never queried as the
analytical source. Use a dedicated schema (and preferably a dedicated role).

## 7. Postgres as the analytical source

```yaml
connections:
  analytics_db:
    type: postgres
    dsn: ${ANALYTICS_DB_DSN}
    fact_table: analytics.facts   # formula metrics aggregate this table
    timestamp_column: ts
    statement_timeout: 30s
```

`PostgresExecutor` opens read-only sessions (`default_transaction_read_only`
plus an explicit `READ ONLY` transaction per query), sets the session time
zone to UTC and applies `statement_timeout`. Formula metrics are aggregated in
SQL. SQL calculations and batch sources declare `dialect: postgres` and use
`:name` parameters (`:effective_at`, `:window_start`, `:window_end`, …), which
are bound server-side and never interpolated. A query must return exactly one
row; zero rows is NO_DATA.

Grant the source role `SELECT` only. Read-only sessions are defence in depth,
not a substitute for privileges.

The source and the runtime store may share a Postgres server or database, but
not a schema: `validate` (and every command that builds the runtime) rejects
a profile whose `runtime_store` and `metric_source` resolve to the same
database and schema.

## DuckDB's role

DuckDB is an analytical execution backend (local working sets, Parquet,
dimensional investigation). It is **not** metric-runtime's state database:
a profile that points `runtime_store` at a DuckDB connection fails
validation.

## What not to expect

metric-runtime is not an AI/LLM framework, a BI replacement, or a complete
observability platform. Warehouse adapters beyond DuckDB and Postgres
(Snowflake, BigQuery, Databricks), per-dimension scheduled streams and config
hot-reload are not part of this release.
