# ADR 0001: First-class KPI calculations

## Status

Accepted (experimental 0.1.x / candidate 0.2.0)

## Context

Metric Runtime originally calculated KPI values only via fact-table
`Formula` aggregates (`sum` / `ratio` / `difference`). That model fits demos
like PyPizza, but real company catalogs also need:

- arbitrary parameterized SQL
- one expensive query feeding many KPIs
- derived metrics from other KPI observations

Without a first-class calculation layer, these concerns either leak into
semantic KPI models or bypass the authoritative `process()` lifecycle.

## Decision

Introduce typed calculation specs:

- `FormulaCalculation`
- `SqlCalculation`
- `BatchCalculation`
- `DerivedCalculation`

plus:

- `EvaluationContext` — typed evaluation request
- `EvaluationSession` — dependency closure, batch dedupe, topo order
- `BatchRegistry` — application-owned shared sources

Observations produced by the session feed the existing detector/state/incident
runtime unchanged.

## Consequences

### Positive

- Supports realistic KPI catalogs
- Batching reduces repeated warehouse work
- SQL and derived metrics stay outside detection/state semantics
- Preserves EvaluationKey idempotency and ordered metric/scope streams

### Trade-offs

- Execution orchestration is more complex
- SQL portability is intentionally limited (no transpiler)
- Derived expressions must remain a small safe language

## Non-goals

- SQL transpilation across warehouses
- Distributed query planning / optimization
- Arbitrary Python `eval` from config
- Warehouse-specific adapters in core (Salesforce, HubSpot, …)
- Presentation / traffic-light thresholds
