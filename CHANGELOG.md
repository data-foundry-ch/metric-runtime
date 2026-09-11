# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added

- Executable Pydantic KPI semantic model (`KPI`)
- Generic measure references + `Formula.sum` / `Formula.ratio` / `Formula.difference`
- Per-KPI detector specs (`SeasonalZScore`, `Threshold`) with engine-level runtime default
- `KPICatalog` with dependency and cycle validation
- Pluggable runtime detectors (`SeasonalZScoreDetector`, `ThresholdDetector`)
- KPI JSON round-trip (`model_dump_json` / `model_validate_json`) without arbitrary types
- KPI state machine (NORMAL → DETECTED → OPEN → …)
- Semantic dependency graph (NetworkX) and graph-aware investigation
- Incident model with ownership routing helpers
- In-memory state store and notifier extension points
- Optional DuckDB execution backend (schema-agnostic; explicit `fact_table`)
- Project/connections configuration (`metric-runtime.yaml` + `connections.yaml`)
- Small CLI (`validate`, `config show`, `run`, `connections test`)
- PyPizza / Great Lunch flagship example (domain measures live under `examples/pypizza/`)
- MIT license and open-source documentation baseline

### Changed

- Core package no longer ships a domain `Measure` enum or PyPizza warehouse defaults
- Support gates require an explicit `SupportRequirement` (no implicit `orders` column)
- `KPI.detector` is a typed serializable spec (`SeasonalZScore` | `Threshold`); runtime strategies are built via factory/registry
- Presentation layout fields (`graph_ring` / `graph_side` / `graph_directionality`) moved out of core KPI into example `metadata["presentation"]`
- DuckDB SQL generation validates and quotes identifiers
- Authoritative ``KPIEngine.process`` / ``tick`` loop: observe → persist → state → investigate → incident → notify
- Split observation / metric-state / incident store protocols (composed by ``StateStore``)
- ``KPIStateTransition`` and resolve / acknowledge / suppress semantics
- ``StatePolicy`` wired from ``metric-runtime.yaml`` into ``build_runtime``
- True idempotency via ``EvaluationKey`` + committed ``EvaluationRecord``
- Notification outbox (``enqueue`` then ``deliver_notifications``)
- ``MetricStateRecord`` with lifecycle metadata and streaks
- Scope identity via canonical JSON + SHA-256
- Timezone-aware datetime fields (naive datetimes rejected)
- Impact ``quantity_delta`` + ``unit_value_metric`` (no hardcoded ``average_order_value``)
- Atomic runtime transactions (``RuntimeTransaction`` / ``TransactionalRuntimeStore``)
- Evaluation claims for concurrency-safe commits
- Strict config duration parsing (``30s`` / ``15m`` / ``2h`` / ``1d``)
- Quality gaps break consecutive detection streaks
- ``KPIEngine.process`` commits observation + state + incident + outbox atomically
- External notification delivery is explicitly at-least-once; notifiers receive ``idempotency_key``
- Preferred-leaf hints are a weak tie-breaker only in investigation ranking
- ``ProcessResult.transition`` is a typed ``KPIStateTransition`` (no ``arbitrary_types_allowed``)
