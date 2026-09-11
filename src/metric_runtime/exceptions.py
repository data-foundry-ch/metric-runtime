"""metric-runtime specific exceptions."""

from __future__ import annotations


class MetricRuntimeError(Exception):
    """Base error for metric-runtime."""


class InvalidMetricDefinitionError(MetricRuntimeError):
    """A KPI definition failed validation."""


class UnknownMetricError(MetricRuntimeError):
    """Referenced metric name does not exist in the catalog."""


class DependencyCycleError(MetricRuntimeError):
    """KPI dependency graph contains a cycle."""


class InsufficientSupportError(MetricRuntimeError):
    """Observation lacked enough support to treat as actionable."""


class ConfigurationError(MetricRuntimeError):
    """Project or connections configuration is invalid."""


class MissingEnvironmentVariableError(ConfigurationError):
    """A required ${ENV_VAR} placeholder could not be resolved."""


class UnknownConnectionError(ConfigurationError):
    """A profile references a connection that does not exist."""


class UnsupportedConnectionTypeError(ConfigurationError):
    """Connection type is not implemented in this version."""


class EvaluationInProgressError(MetricRuntimeError):
    """Another worker currently owns this EvaluationKey."""


class StaleEvaluationError(MetricRuntimeError):
    """An older evaluation window cannot overwrite newer metric state.

    Operational processing for one (metric, scope) stream is monotonic in
    ``effective_at`` (evaluation window time). A late or out-of-order older
    window is rejected after a newer window has already been committed.
    """
