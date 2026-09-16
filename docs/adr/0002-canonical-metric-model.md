# ADR 0002: Canonical Metric model and Python-native metric repositories

## Status

Accepted

## Context

The original `KPI` model primarily served runtime execution. Users need
metric-runtime to also act as a **repository** for business metric definitions.
External products need a stable semantic contract they can consume without
copying fields into a parallel schema.

## Decision

- `Metric` becomes the canonical semantic model.
- Stable `id` is separate from display `name`.
- `MetricCatalog` is the canonical in-memory repository.
- Catalogs support deterministic serialization/export, hashes, and diffs.
- `KPI` / `KPICatalog` remain compatibility aliases temporarily.
- Product lifecycle / UI / tenancy metadata remains outside core.
- Project config loads catalogs via `catalog.entrypoint: module.path:attribute`.
- Units are extensible `UnitSpec` values (no conversion algebra).

## Consequences

### Positive

- metrics-as-code with Git-native governance
- reusable semantic contract across runtime, APIs, products, and tools
- display renames do not break identity, history, or derived expressions

### Trade-offs

- identity migration from `KPI.name` → `Metric.id`
- public API evolution while still pre-1.0
- more importance placed on schema stability

## Non-goals

- draft/publish workflow
- RBAC / multi-tenancy
- metric editor UI
- collection management
- replacing Git as version control
