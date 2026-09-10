# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added

- Executable Pydantic KPI semantic model (`KPI`)
- `KPICatalog` with dependency and cycle validation
- Pluggable detectors (`SeasonalZScoreDetector`, `ThresholdDetector`)
- KPI state machine (NORMAL → DETECTED → OPEN → …)
- Semantic dependency graph (NetworkX) and graph-aware investigation
- Incident model with ownership routing helpers
- In-memory state store and notifier extension points
- Optional DuckDB execution backend
- Project/connections configuration (`metric-runtime.yaml` + `connections.yaml`)
- Small CLI (`validate`, `config show`, `run`, `connections test`)
- PyPizza / Great Lunch flagship example
- MIT license and open-source documentation baseline
