# Metric Runtime: Adapter-First Platform Architecture

## Goal

Refactor Metric Runtime so the core package is data-platform agnostic.

The intended user experience is:

```text
Metric Runtime
    ↓
install/use adapter for your platform
    ↓
read business facts from that platform
run calculations / detection / state
write runtime results back to that platform
```

The common production setup should be:

```yaml
profiles:
  production:
    metric_source: warehouse
    runtime_store: warehouse
```

The same connection may serve both roles.

Examples:

```text
Snowflake   → Metric Runtime → Snowflake
BigQuery    → Metric Runtime → BigQuery
Databricks  → Metric Runtime → Databricks
Postgres    → Metric Runtime → Postgres
```

Separate source/store platforms must still be supported.

## 1. Inspect the current code first

Before editing, review:

- executor abstractions
- `RuntimeStore`
- Postgres executor/store
- config models and factories
- CLI/profile loading
- migrations
- tests
- production docs

Preserve current runtime behavior, idempotency, ordering, state transitions, and backward compatibility where practical.

## 2. Make adapters the platform extension point

Introduce a clear adapter abstraction representing the capabilities a data platform can provide.

Conceptually:

```python
class MetricRuntimeAdapter(Protocol):
    def build_executor(...) -> MetricExecutor: ...
    def build_runtime_store(...) -> RuntimeStore: ...
```

Exact API may differ after inspecting the code.

An adapter may support:

- metric source / query execution
- runtime persistence
- both

Do not require every adapter to support both roles.

Keep `MetricExecutor` and `RuntimeStore` as separate core contracts.

## 3. Keep the core database-neutral

The core `metric-runtime` package must not assume Postgres, DuckDB, Snowflake, or any other platform.

Core owns:

- metric semantics
- calculations
- detection
- state
- incidents
- scheduling
- runtime orchestration
- executor/store contracts

Platform-specific code owns:

- connections
- SQL dialect behavior
- query execution
- schema/database creation
- locking/concurrency implementation
- persistence implementation

Postgres-specific mechanisms such as advisory locks and `SKIP LOCKED` must remain implementation details, not core semantics.

## 4. Same connection, two logical roles

Allow one configured connection to serve as both:

```yaml
metric_source: warehouse
runtime_store: warehouse
```

The roles remain logically separate:

```text
business/source namespace
    → Metric Runtime reads

runtime namespace
    ← Metric Runtime writes
```

Require a dedicated runtime schema/dataset/catalog namespace where appropriate.

Do not require a separate Postgres database just to persist Metric Runtime state.

## 5. Keep mixed-platform setups supported

This must remain valid:

```yaml
profiles:
  production:
    metric_source: warehouse
    runtime_store: operational_db
```

For example:

```text
Snowflake → Metric Runtime → Postgres
```

The adapter system must resolve each role independently.

## 6. Adapter registration and discovery

Replace hard-coded platform branching where practical with an adapter registry.

Conceptually:

```python
register_adapter("postgres", PostgresAdapter)
register_adapter("duckdb", DuckDBAdapter)
```

The factory should resolve the adapter from the connection `type`.

Design this so adapters can later live outside the core repository/package.

Avoid requiring changes to central factory conditionals for every future platform.

## 7. Packaging direction

Keep current built-in adapters working, but structure them so future installation can look like:

```bash
pip install metric-runtime
pip install metric-runtime-postgres
pip install metric-runtime-snowflake
pip install metric-runtime-bigquery
```

It is acceptable to keep current extras temporarily:

```bash
pip install "metric-runtime[postgres]"
```

Do not split packages in this change unless necessary.

The goal is to make the architecture ready for external adapters.

## 8. Adapter capabilities

Add a lightweight capability model if useful.

Example:

```python
AdapterCapabilities(
    metric_source=True,
    runtime_store=True,
    distributed_claims=True,
    migrations=True,
)
```

Use capabilities to validate profiles and produce clear errors.

Do not encode Postgres-specific implementation details in capabilities.

## 9. Configuration

Keep dbt-style separation:

```text
metric-runtime.yaml
    → project semantics/runtime settings

connections.yaml
    → environment/platform connections
```

A typical production config should be simple:

```yaml
connections:
  warehouse:
    type: postgres
    dsn: ${DATABASE_URL}
    source_schema: analytics
    runtime_schema: metric_runtime

profiles:
  production:
    metric_source: warehouse
    runtime_store: warehouse
```

Equivalent adapters should be able to use their own platform-specific fields.

Credentials remain environment-side only.

## 10. RuntimeStore contract

Review the `RuntimeStore` API and make sure it expresses guarantees, not database mechanisms.

Core guarantees include:

- claim an evaluation uniquely
- commit evaluations idempotently
- maintain ordered metric state
- persist observations
- persist incidents
- lease notification work
- recover after worker failure

Each adapter decides how to satisfy those guarantees.

## 11. Postgres becomes the reference adapter

Refactor the existing Postgres source/store implementation to use the adapter architecture.

Postgres should demonstrate:

```text
one connection
├── read from analytics/business schema
└── write to metric_runtime schema
```

Preserve:

- read-only analytical execution
- dedicated runtime schema
- migrations
- transaction safety
- evaluation claims
- ordered stream commits
- outbox leases

Do not weaken existing Postgres guarantees.

## 12. DuckDB

Keep DuckDB as the lightweight local adapter.

It may support only the capabilities that are appropriate for DuckDB.

Do not force production/distributed `RuntimeStore` semantics onto DuckDB if they are not safe.

## 13. CLI and factory behavior

These commands should remain platform-neutral:

```bash
metric-runtime validate --profile production
metric-runtime store migrate --profile production
metric-runtime run --once --profile production
metric-runtime run --profile production
```

The CLI must not contain platform-specific branching beyond adapter resolution.

Adapter-specific errors should be clear and actionable.

## 14. Tests

Add tests for:

- adapter registration/resolution
- same connection used for source and store
- separate connections used for source and store
- capability validation
- unsupported role errors
- Postgres adapter preserving current behavior
- DuckDB adapter preserving current behavior
- external/mock adapter registration without editing the core factory
- config serialization and secret redaction

Existing runtime/store/executor tests must remain green.

## 15. Documentation

Update the architecture and quickstart to lead with this model:

> Metric Runtime runs against your existing data platform. Install the adapter for that platform, read business facts from it, and write Metric Runtime observations, state, incidents, and outbox records back to a dedicated runtime namespace.

Show the common case first:

```text
Your Data Platform
├── business data      → Metric Runtime reads
└── metric_runtime     ← Metric Runtime writes
```

Then show the advanced split-platform case.

Do not present Postgres as a required dependency of Metric Runtime.

## Out of Scope

Do not implement Snowflake, BigQuery, or Databricks adapters in this change.

Do not add AI, ontology, UI, or unrelated runtime features.

## Acceptance Criteria

The change is complete when:

1. Core Metric Runtime is platform-neutral.
2. Platform-specific behavior is resolved through adapters.
3. One connection can serve both `metric_source` and `runtime_store`.
4. Source and runtime roles remain logically separate.
5. Separate source/store platforms still work.
6. Postgres works as the reference full adapter.
7. DuckDB remains supported for local use.
8. Adding a future adapter does not require rewriting core runtime logic.
9. CLI commands remain platform-neutral.
10. Existing runtime guarantees and tests remain intact.

## Guiding Principle

> Metric Runtime should run where the metrics already live: read business facts from the user's data platform, evaluate metrics, and write runtime conclusions back to that same platform through an adapter.
