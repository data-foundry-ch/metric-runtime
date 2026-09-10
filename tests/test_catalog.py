"""Catalog validation tests."""

from __future__ import annotations

import pytest

from metric_runtime import KPI, KPICatalog
from metric_runtime.exceptions import (
    DependencyCycleError,
    InvalidMetricDefinitionError,
    UnknownMetricError,
)
from metric_runtime.models import Directionality


def test_unique_names_required():
    with pytest.raises(InvalidMetricDefinitionError):
        KPICatalog(
            [
                KPI(name="a", owner="x"),
                KPI(name="a", owner="y"),
            ]
        )


def test_unknown_dependency():
    with pytest.raises(UnknownMetricError):
        KPICatalog([KPI(name="a", dependencies=("missing",))])


def test_cycle_rejected():
    with pytest.raises(DependencyCycleError):
        KPICatalog(
            [
                KPI(name="a", dependencies=("b",)),
                KPI(name="b", dependencies=("a",)),
            ]
        )


def test_valid_catalog():
    cat = KPICatalog(
        [
            KPI(name="leaf", owner="ops"),
            KPI(
                name="parent",
                owner="finance",
                dependencies=("leaf",),
                directionality=Directionality.LOWER_IS_BAD,
            ),
        ]
    )
    assert cat["parent"].owner == "finance"
