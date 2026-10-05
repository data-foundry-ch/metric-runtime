"""metric-runtime specific exceptions."""

from __future__ import annotations


class MetricRuntimeError(Exception):
    """Base error for metric-runtime."""


class InvalidMetricDefinitionError(MetricRuntimeError):
    """A KPI definition failed validation."""


class UnknownMetricError(MetricRuntimeError):
    """Referenced metric id does not exist in the catalog."""


class UnknownDependencyError(UnknownMetricError):
    """A metric declares a dependency that is not in the catalog."""


class DependencyCycleError(MetricRuntimeError):
    """Metric dependency graph contains a cycle."""


class InsufficientSupportError(MetricRuntimeError):
    """Observation lacked enough support to treat as actionable."""


class NoDataError(MetricRuntimeError):
    """A calculation produced NO_DATA (no rows / NULL / missing inputs).

    ``reason`` is ``"no_data"`` for the current window or ``"no_baseline"``
    when no baseline window produced a value. ``KPIEngine.process()`` records
    this as a committed NO_DATA evaluation instead of failing.
    """

    def __init__(self, message: str, *, reason: str = "no_data") -> None:
        super().__init__(message)
        self.reason = reason


class ConfigurationError(MetricRuntimeError):
    """Project or connections configuration is invalid."""


class MissingEnvironmentVariableError(ConfigurationError):
    """A required ${ENV_VAR} placeholder could not be resolved."""


class UnknownConnectionError(ConfigurationError):
    """A profile references a connection that does not exist."""


class UnsupportedConnectionTypeError(ConfigurationError):
    """No adapter is registered for the connection type."""


class UnsupportedRoleError(ConfigurationError):
    """The connection's adapter cannot serve the requested profile role."""


class EvaluationInProgressError(MetricRuntimeError):
    """Another worker currently owns this EvaluationKey."""


class StreamCommitConflict(EvaluationInProgressError):
    """The stream changed between the ordered wait and the commit.

    Raised by durable stores when, while holding the commit-time stream lock,
    an earlier claim appeared or the metric-state version moved. The engine
    re-enters the ordered section (bounded) and recomputes the transition.
    """


class NotificationLeaseLostError(MetricRuntimeError):
    """An outbox write used a claim token that no longer holds the lease."""


class RuntimeStoreNotMigratedError(ConfigurationError):
    """The durable runtime store schema has pending migrations."""


class StaleEvaluationError(MetricRuntimeError):
    """An older evaluation window cannot overwrite newer metric state.

    Operational processing for one (metric, scope) stream is monotonic in
    ``effective_at`` (evaluation window time). A late or out-of-order older
    window is rejected after a newer window has already been committed.
    """
