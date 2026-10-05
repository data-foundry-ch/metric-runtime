# Adapters

Metric Runtime's core is platform-neutral. Everything that depends on a data
platform or delivery channel lives behind an **adapter**, resolved from a
connection's `type` in `connections.yaml`.

```
connections.yaml            adapter registry              core contracts
type: postgres   ───────►   PostgresAdapter   ──┬──►  MetricExecutor   (metric_source)
                                                └──►  RuntimeStore     (runtime_store)
type: webhook    ───────►   WebhookAdapter    ─────►  Notifier         (notifier)
```

## Built-in adapters

| `type` | Roles | Notes |
|---|---|---|
| `postgres` | `metric_source`, `runtime_store` | reference full adapter: read-only source sessions, durable multi-worker runtime store, migrations (`pip install "metric-runtime[postgres]"`) |
| `duckdb` | `metric_source` | local analytical source; not offered as a runtime store (single-writer, not safe for shared claims) |
| `memory` | `runtime_store` | process-local, not durable; development and tests |
| `webhook` | `notifier` | signed JSON POST per outbox event (stdlib only) |

`import metric_runtime` never imports a database driver: built-in adapters
are imported on first use.

## Roles and capabilities

An adapter declares what it can do:

```python
AdapterCapabilities(
    metric_source=True,      # can build a MetricExecutor
    runtime_store=True,      # can build a RuntimeStore
    notifier=False,          # can build a Notifier
    durable=True,            # runtime conclusions survive restarts
    distributed_claims=True, # several workers may share the store
    migrations=True,         # versioned schema: store migrate / status
)
```

Profile validation (`metric-runtime validate`, and every command that builds
the runtime) checks each role against the capabilities of the connection's
adapter and names the adapters that would support it:

```
profile 'local': runtime_store 'analytics' has type 'duckdb': DuckDB is an
analytical source only; ... (adapters supporting runtime_store: memory, postgres)
```

## One connection, two roles

A single connection can serve as both source and runtime store:

```yaml
connections:
  warehouse:
    type: postgres
    dsn: ${DATABASE_URL}
    source_schema: analytics       # business data is read here
    runtime_schema: metric_runtime # Metric Runtime writes here

profiles:
  production:
    metric_source: warehouse
    runtime_store: warehouse
```

Two rules keep the roles logically separate:

1. **Role-specific clients.** Building each role is a separate adapter call
   that returns its own resources. A shared connection *config* never means a
   shared physical connection or pool: for Postgres the executor holds its own
   read-only session, and the runtime store its own writable pool (writes,
   migrations, locks, claims).
2. **Adapter-owned boundary.** Core only requires that source data and
   runtime-owned objects never collide. When a profile has both roles, core
   asks the **runtime store's** adapter via `role_conflicts(...)`. Postgres
   rejects a runtime schema equal to the *effective* source schema on the
   same database — the schema of a qualified `fact_table` if given, otherwise
   `source_schema`. Another platform could instead allow a shared dataset
   with dedicated table prefixes.

Split platforms work the same way — each role is resolved independently:

```yaml
profiles:
  production:
    metric_source: warehouse   # type: snowflake (external adapter)
    runtime_store: ops_db      # type: postgres
```

## Writing an adapter

Subclass `BaseAdapter` (unsupported roles raise `UnsupportedRoleError`
automatically) and implement the roles you support:

```python
from pydantic import BaseModel, SecretStr
from metric_runtime.adapters import AdapterCapabilities, BaseAdapter


class SnowflakeConnectionConfig(BaseModel):
    type: str = "snowflake"
    account: str
    user: str
    password: SecretStr          # SecretStr fields are redacted in `config show`
    database: str
    source_schema: str
    runtime_schema: str = "METRIC_RUNTIME"


class SnowflakeAdapter(BaseAdapter):
    type_name = "snowflake"
    capabilities = AdapterCapabilities(metric_source=True, runtime_store=True, durable=True)
    config_model = SnowflakeConnectionConfig
    install_hint = "pip install metric-runtime-snowflake"

    def build_executor(self, config, context):
        return SnowflakeExecutor(config, fact_table=context.project.runtime.fact_table)

    def build_runtime_store(self, config, context):
        return SnowflakeRuntimeStore(config)   # its own session, never the executor's

    def role_conflicts(self, *, source_type, source_config, runtime_config, same_connection):
        if source_type == "snowflake" and source_config.source_schema == runtime_config.runtime_schema:
            return ["runtime_schema must differ from source_schema"]
        return []
```

- `build_executor` returns a `MetricExecutor`. Set `sql_dialects` on it so
  `SqlCalculation(dialect=...)` routes correctly; Metric Runtime never
  transpiles SQL.
- `build_runtime_store` returns a `RuntimeStore` that satisfies the
  guarantees in `metric_runtime/stores/base.py` (unique claims, idempotent
  atomic commits, ordered state per stream, leased outbox, recovery after
  worker failure). Implement `ManagedRuntimeStore` if it has a versioned
  schema; `metric_runtime.migrations` provides numbered, checksummed
  migration files and verification.
- `context` (`AdapterContext`) carries the project config, the base directory
  for relative paths and the connection name.
- Never share a connection, pool or session between roles.

### Registering

Explicitly, e.g. in application start-up code:

```python
from metric_runtime.adapters import register_adapter

register_adapter("snowflake", SnowflakeAdapter)
```

Or, for an installable package, through the `metric_runtime.adapters` entry
point group — no core change and no import needed:

```toml
# pyproject.toml of metric-runtime-snowflake
[project.entry-points."metric_runtime.adapters"]
snowflake = "metric_runtime_snowflake:SnowflakeAdapter"
```

Entry points are discovered the first time a connection uses an unregistered
type. The registry accepts an adapter instance, a class / zero-argument
factory, or a `"module:attribute"` path.
