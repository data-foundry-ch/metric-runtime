# Contributing

Thanks for contributing to metric-runtime.

## Setup

```bash
git clone <repository-url>
cd metric-runtime
pip install -e ".[demo,dev]"
```

## Checks

```bash
ruff check src tests examples/pypizza
ruff format --check src tests examples/pypizza
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

### Add a state store

Implement `StateStore` in `metric_runtime.stores`. Prefer tests with
`InMemoryStateStore` for core logic.

## Pull requests

- Keep changes focused
- Include tests for behavior changes
- Update docs when the public API or concepts change
- Call out breaking API changes explicitly

No heavy process — clear diffs and green checks are enough.
