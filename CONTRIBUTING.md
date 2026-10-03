# Contributing

Thanks for contributing to metric-runtime.

## Setup

```bash
git clone <repository-url>
cd metric-runtime
pip install -e ".[demo,dev,postgres]"
```

### Postgres tests

The runtime-store contract, migration, outbox-lease, run-once and CLI tests
run against both the in-memory store and Postgres. The Postgres cases are
skipped unless `METRIC_RUNTIME_TEST_POSTGRES_DSN` is set; every test uses its
own throwaway schema.

```bash
docker run -d --name metric-runtime-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=metric_runtime_test -p 55432:5432 postgres:16
export METRIC_RUNTIME_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:55432/metric_runtime_test
pytest -q
```

CI runs a `postgres:16` service, so these cases always run there.

## Checks

```bash
ruff check src tests examples/pypizza examples/judgment_comparison examples/metric_judgment_eval
ruff format --check src tests examples/pypizza examples/judgment_comparison examples/metric_judgment_eval
mypy src/metric_runtime
pytest -q
```

Generate PyPizza data before integration tests:

```bash
python examples/pypizza/generate_data.py
```

## Extending metric-runtime

### Add a detector

Implement `detect(...)` / `evaluate(...)` following
`metric_runtime.detectors.DetectorStrategy`. Add unit tests in
`tests/test_detectors.py`.

### Add an executor

Implement the `MetricExecutor` protocol in `metric_runtime.execution`.
Keep credentials out of KPI definitions. Wire via `connections.yaml` + factory
only when the adapter is real.

### Add a runtime store

Implement the `RuntimeStore` protocol (`metric_runtime.stores.base`; the
`StateStore` name is an alias). Reuse `stores.staging.StagedTransaction` for
read-your-writes staging, and add your store to the `store_factory` fixture in
`tests/conftest.py` so it runs the shared contract tests
(`test_runtime_store_contract.py`, `test_outbox_leases.py`,
`test_no_data_outcome.py`, `test_run_once.py`).

### Add a runtime store migration

Add the next numbered file under
`src/metric_runtime/stores/migrations/postgres/` (e.g. `002_add_index.sql`).
Never edit an applied file (checksums are verified). Migrations run in a
single transaction, so avoid `CREATE INDEX CONCURRENTLY` / `VACUUM`. Use
unqualified table names; `search_path` points at the runtime schema.

## Pull requests

- Keep changes focused
- Include tests for behavior changes
- Update docs when the public API or concepts change
- Call out breaking API changes explicitly

No heavy process — clear diffs and green checks are enough.
