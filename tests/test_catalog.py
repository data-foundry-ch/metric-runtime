"""Catalog validation tests."""

from __future__ import annotations

import pytest

from metric_runtime import KPI, Formula, KPICatalog
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
                KPI(name="a", owner="x", formula=Formula.sum("a")),
                KPI(name="a", owner="y", formula=Formula.sum("a")),
            ]
        )


def test_unknown_dependency():
    with pytest.raises(UnknownMetricError):
        KPICatalog([KPI(name="a", dependencies=("missing",), formula=Formula.sum("a"))])


def test_cycle_rejected():
    with pytest.raises(DependencyCycleError):
        KPICatalog(
            [
                KPI(name="a", dependencies=("b",), formula=Formula.sum("a")),
                KPI(name="b", dependencies=("a",), formula=Formula.sum("b")),
            ]
        )


def test_valid_catalog():
    cat = KPICatalog(
        [
            KPI(name="leaf", owner="ops", formula=Formula.sum("leaf")),
            KPI(
                name="parent",
                owner="finance",
                dependencies=("leaf",),
                directionality=Directionality.LOWER_IS_BAD,
                formula=Formula.sum("parent"),
            ),
        ]
    )
    assert cat["parent"].owner == "finance"
