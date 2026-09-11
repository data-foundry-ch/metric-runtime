# Production considerations

> metric-runtime v0.1 is experimental. It does **not** ship a complete
> production monitoring platform.

## What you can deploy today

A practical pattern:

```
Docker image
  + existing scheduler/orchestrator
  + warehouse or lake
  + persistent state store (e.g. Postgres — bring your own)
  + notifier adapter (bring your own)
```

Possible schedulers: Kubernetes CronJob, Airflow, Dagster, Prefect, or managed
cloud container jobs.

The metric-runtime worker can remain a disposable job. Persistent operational
state should live in a StateStore you control.

## Authoritative worker lifecycle

```
scheduler / trigger
        ↓
runtime worker
        ↓
claim EvaluationKey
        ↓
calculate (observation / detector / state / investigation)
        ↓
atomic DB transaction
        ├── observation
        ├── metric state
        ├── incident
        └── outbox intent
        ↓
commit

separate delivery worker
        ↓
Slack / Teams / email / webhook
```

A future Postgres adapter can implement both:

- transactional runtime persistence (`transaction()`)
- evaluation locking / claiming (`claim_evaluation`)

without changing `KPIEngine.process()` semantics.

External notification delivery stays **outside** the state transaction
(transactional outbox). Delivery is at-least-once unless the notifier
honors `idempotency_key`.

## Execution modes

| Mode | How observations arrive |
|---|---|
| Scheduled / micro-batch | Worker evaluates KPIs on a cadence |
| Data-arrival triggered | Downstream of a warehouse load / lake write |
| Streaming observations | Another system produces observations; metric-runtime consumes them |

**Batch versus streaming changes how an observation is obtained.
It does not change what the KPI means.**

## DuckDB’s role

DuckDB is an analytical execution backend, especially useful for:

- local analytical working sets
- querying Parquet
- dimensional investigation after a state change
- reducing repeated warehouse queries in appropriate architectures

DuckDB is **not**:

- metric-runtime’s state database
- a streaming engine
- a requirement for using the library

Install with `pip install "metric-runtime[duckdb]"` when needed.

## Configuration for deployments

```
metric-runtime.yaml     # semantics + runtime policy (commit)
connections.yaml        # resources + profiles (usually do not commit)
```

Prefer:

```bash
metric-runtime run --profile production \
  --project-config metric-runtime.yaml \
  --connections connections.yaml
```

Validate without surprising network I/O:

```bash
metric-runtime validate --profile production ...
metric-runtime connections test --profile production ...
```

Never establish external connections merely because `import metric_runtime`
executed.

## What not to expect from v0.1

metric-runtime is not:

- an AI / LLM framework
- a BI replacement
- a complete observability platform
- a production-ready distributed monitoring platform

It is an experimental open-source framework exploring executable KPI semantics,
graph-aware investigation, pluggable detection, stateful metrics, and smart
incident routing.
