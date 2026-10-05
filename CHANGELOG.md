# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

### Added — adapter-first architecture

- ``metric_runtime.adapters``: ``MetricRuntimeAdapter`` protocol, ``BaseAdapter``,
  ``AdapterCapabilities`` (``metric_source`` / ``runtime_store`` / ``notifier`` / ``durable`` /
  ``distributed_claims`` / ``migrations``), ``AdapterContext`` and a registry
  (``register_adapter``, ``get_adapter``, ``registered_adapters``) keyed by connection ``type``
- External adapters register via the ``metric_runtime.adapters`` entry-point group, discovered
  on first use of an unknown type; built-in adapters are imported lazily, so
  ``import metric_runtime`` loads no database driver
- Built-in adapters: ``postgres`` (reference: source + durable store), ``duckdb`` (source only),
  ``memory`` (store only), ``webhook`` (notifier only)
- One connection may serve both ``metric_source`` and ``runtime_store``; each role builds its own
  client resources (e.g. read-only executor session vs writable store pool)
- Adapter-owned role boundary (``role_conflicts`` on the runtime store's adapter); Postgres
  rejects a ``runtime_schema`` equal to the effective source schema on the same database
- Postgres connections: ``source_schema`` (search path of the read-only session; qualifies an
  unqualified ``fact_table``) and ``runtime_schema``
- ``ManagedRuntimeStore`` runtime-checkable protocol (``namespace``, ``schema_status()``,
  ``migrate()``, ``ensure_ready()``); ``metric_runtime.migrations`` with the platform-neutral
  ``Migration`` / ``SchemaStatus`` / checksum verification
- ``UnsupportedRoleError``; ``config show`` also redacts every ``SecretStr`` field an adapter's
  config model declares
- ``docs/adapters.md``

### Changed — adapter-first architecture

- Platform code moved into ``metric_runtime/adapters/<platform>/`` (Postgres executor, store,
  migrations and SQL files; DuckDB executor; webhook notifier; connection config models). Old
  import paths (``metric_runtime.execution.{DuckDBExecutor,PostgresExecutor}``,
  ``metric_runtime.stores.postgres``, ``metric_runtime.stores.migrations``,
  ``metric_runtime.notifications.WebhookNotifier``, ``metric_runtime.config.models.*ConnectionConfig``)
  remain as re-exports
- Profile validation, the factory and the CLI resolve every role through adapter capabilities;
  no platform branching remains in ``config/factory.py`` or ``cli.py``
- ``RuntimeStore`` documentation states guarantees (unique claims, idempotent atomic commits,
  ordered state, leased outbox, recovery) instead of Postgres mechanisms
- Postgres ``schema:`` is a deprecated alias for ``runtime_schema:``
- ``store migrate`` / ``store status`` print the store's namespace (e.g. ``schema metric_runtime``)
- Core no longer assumes DuckDB SQL: ``EvaluationSession`` defers to the executor's
  ``sql_dialects``, and ``SqlBatchSource(dialect=...)`` defaults to ``None`` like
  ``SqlCalculation`` (pass ``dialect="duckdb"`` explicitly to pin it)

### Added — durable runtime

- ``RuntimeStore`` protocol (``StateStore`` kept as alias) with an evaluation cursor
  (``latest_committed_evaluation``), leased outbox API (``claim_pending_notifications``,
  token-guarded ``mark_notification_delivered`` / ``record_notification_attempt``,
  ``release_notification_claim``, ``next_notification_due_at``) and ``close()``;
  ``InMemoryRuntimeStore`` alias
- ``PostgresRuntimeStore`` (``pip install "metric-runtime[postgres]"``): leased evaluation
  claims, non-locking ordered wait, commit under ``pg_advisory_xact_lock`` with
  claim/duplicate/staleness/earlier-claim re-checks and a version-guarded state upsert,
  outbox claims via ``UPDATE … FOR UPDATE SKIP LOCKED RETURNING``
- Numbered SQL migrations (``001_initial.sql``) applied in one transaction under an advisory
  lock, with ``schema_migrations`` checksums
- ``MetricRuntime`` orchestration: cursor-driven due windows, per-tick shared
  ``EvaluationSession``, per-metric failure isolation, outbox drain, ``RunReport``;
  ``run_forever`` waking at the next metric window / outbox retry (capped by
  ``idle_interval``), SIGINT/SIGTERM shutdown
- ``metric_runtime.scheduling`` (``align_tick``, ``due_ticks``)
- Outbox delivery policy: exponential backoff, ``max_attempts`` dead-lettering, leases
  (``OutboxEvent.claim_token`` / ``claimed_until`` / ``dead_lettered_at``)
- CLI: ``run --once [--now TS] [--json]``, continuous ``run``, ``--log-level``,
  ``store migrate``, ``store status [--check]``; ``validate`` checks role wiring and
  schedule metric ids
- Config: ``runtime_store`` profile role, ``type: postgres`` connections,
  ``runtime.evaluation_lag`` / ``max_catchup_windows`` / ``idle_interval`` / ``schedules`` /
  ``notifications``; ``build_metric_runtime()``
- NO_DATA is a committed evaluation outcome (state carried forward, no incidents/outbox,
  ignored by streaks); NO_DATA baseline windows are excluded from the baseline
- CI: Postgres 16 service, Postgres-backed tests and a production smoke run
- Docs: rewritten ``docs/production.md``
- ``WebhookNotifier`` (``type: webhook`` connections, stdlib only): JSON POST per outbox event
  with ``Idempotency-Key: <event_key>``, optional ``X-Metric-Runtime-Signature: sha256=<hmac>``,
  custom headers, timeout; redirects are not followed and errors never echo the URL
- ``EventNotifier`` protocol (``notify_event(OutboxEvent)``): delivery receives the full event
  including ``event_key``; plain ``Notifier`` implementations keep working
- ``notifier`` profile role (webhook connection, or inline ``type: logging`` / ``type: none``)
- ``PostgresExecutor`` (``metric_source`` role on ``type: postgres``): read-only sessions
  (``default_transaction_read_only`` + ``READ ONLY`` transactions), ``statement_timeout``,
  UTC session time zone, server-side parameter binding, SQL-pushed formula aggregates,
  ``dialect: postgres`` SQL calculations and batch sources
- ``metric_runtime.execution.sql``: shared identifier quoting and ``:name`` parameter binding
- Validation rejects a profile whose ``runtime_store`` and ``metric_source`` resolve to the same
  Postgres database **and** schema

### Changed — durable runtime

- ``KPIEngine(runtime_store=...)``; ``state_store=`` / ``.state_store`` remain aliases
- ``KPIEngine.process()`` retries the ordered section up to 3 times on
  ``StreamCommitConflict``; execution errors commit nothing
- ``KPIEngine.deliver_notifications()`` uses the leased delivery path
- CLI ``--profile`` defaults to ``runtime.default_profile``; ``run`` without flags now starts
  the continuous runner (``run --evaluate`` still works but is deprecated in favour of
  ``evaluate``)
- ``${ENV}`` placeholders in ``connections.yaml`` are resolved per connection and only fail
  when a profile uses that connection
- ``config show`` redacts ``dsn``, ``url``, ``headers`` and ``authorization`` values
- ``EvaluationSession`` accepts SQL dialects advertised by the executor
  (``executor.sql_dialects``) instead of assuming ``duckdb``
- ``DuckDBExecutor`` uses the shared ``execution.sql`` helpers (behaviour unchanged)

### Added

- Canonical ``Metric`` model with stable ``id`` and display ``name``
- ``MetricCatalog`` repository API (graph ops, snapshot, JSON/JSONL, schema, hashes, diff, Markdown docs)
- Extensible ``UnitSpec`` (arbitrary business units; no conversion algebra)
- ``tags`` on Metric; ``Metric.semantic_hash()`` / ``content_hash()``
- Catalog entrypoint ``module.path:attribute`` in ``metric-runtime.yaml``
- CLI: ``catalog list|show|export|schema|diff|docs``
- Offline catalog validation without warehouse I/O
- ``examples/company_metrics`` Python metric repository
- Docs: ``metric-repositories.md``, ``product-integration.md``, ``migration-metric-model.md``, ADR 0002
- Optional ``KPIObservation.metric_definition_hash`` provenance
- ``KPIEngine.process_many()`` shares one ``EvaluationSession`` (batch cache) across metrics
- Currency-agnostic ``estimate_impact`` / ``min_impact`` / ``impact`` (``*_eur`` aliases retained)
- ``examples/judgment_comparison`` marimo demo: Metric Runtime evidence → Jev / LLM typed judgment → Python policy, including live cost comparison from pydantic-ai usage
- ``examples/metric_judgment_eval`` experimental harness: deterministic ``GraphAnalysis`` + operational proposals, with Jev/OpenAI judged only on remaining sufficiency / human-review / disposition (no incident narratives)

### Changed

- ``Metric.id`` is machine identity for dependencies, graph, observations, state, and incidents
- ``Metric.calculation`` is a mandatory discriminated ``Calculation`` union; model is frozen
- ``formula`` is authoring sugar only (excluded from serialized Metric output)
- Metadata must be strictly JSON-safe (no silent ``default=str`` coercion)
- Units are no longer a closed ``eur|percent|…`` enum
- Version bump to **0.2.0**
- CI matrix includes Python 3.13

### Deprecated

- ``KPI`` / ``KPICatalog`` — temporary aliases for ``Metric`` / ``MetricCatalog`` (see migration guide)

### Migration

See [docs/migration-metric-model.md](docs/migration-metric-model.md).

## [0.1.0] - Unreleased

### Added

- Executable Pydantic KPI semantic model (`KPI`)
- Generic measure references + `Formula.sum` / `Formula.ratio` / `Formula.difference`
- First-class ``Calculation`` model: ``FormulaCalculation``, ``SqlCalculation``, ``BatchCalculation``, ``DerivedCalculation``
- ``SqlCalculation.bindings`` / ``SqlBatchSource.bindings`` for KPI-local SQL parameters (request context overrides)
- Optional ``DuckDBExecutor(fact_table=None)`` for SQL/batch-only sessions
- ``EvaluationContext`` + ``EvaluationSession`` for catalog-level calculation orchestration
- Shared ``BatchRegistry`` / ``SqlBatchSource`` / ``CallableBatchSource``
- Safe derived expression language (AST whitelist)
- Domain-neutral ``examples/sql_catalog`` demo
- Docs: ``docs/calculations.md``, ADR ``0001-first-class-calculations``
- ``PresentationThreshold`` + ``classify_presentation_band`` (presentation policy only; not detectors/alerts)
- Optional KPI ``format`` display hint; ``KPI.display()`` for label/unit/format
- ``EvaluationContext`` binding alias ``as_of_date`` (= ``effective_at``)
- ``KPICatalog`` JSON Schema + JSON/JSONL export/import (calculations included)
- ``KPIObservation.measured_value`` / ``is_no_data`` / ``is_error`` for nullable UI bridges
- Per-KPI detector specs (`SeasonalZScore`, `Threshold`) with engine-level runtime default
- `KPICatalog` with dependency and cycle validation
- Pluggable runtime detectors (`SeasonalZScoreDetector`, `ThresholdDetector`)
- KPI JSON round-trip (`model_dump_json` / `model_validate_json`) without arbitrary types
- KPI state machine (NORMAL → DETECTED → OPEN → …)
- Semantic dependency graph (NetworkX) and graph-aware investigation
- Incident model with ownership routing helpers
- In-memory state store and notifier extension points
- Optional DuckDB execution backend (schema-agnostic; ``fact_table`` required only for formula/measure paths)
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
- Ordered metric-state processing per ``(metric, scope)`` via stream lease + ``last_evaluation_at`` / ``version``
- ``StaleEvaluationError`` when an older window would overwrite newer state
- Outbox ``state_changed`` only when an incident payload exists (notifier contract)
- Deprecated legacy store helpers: ``append_observation``, ``has_observation``, ``set_state``
- ``KPI.calculation`` is the canonical calculation field; ``formula=`` remains sugar that normalizes to ``FormulaCalculation``
