"""PyPizza data quality gate (demo helper)."""

from __future__ import annotations

from datetime import datetime, timedelta

from metric_runtime.engine import KPIEngine
from metric_runtime.models import QualityReport


def check_data_quality(
    engine: KPIEngine,
    at: datetime,
    *,
    max_lag_hours: float = 2.0,
    min_rows: int = 50,
    max_missing_rate: float = 0.02,
) -> QualityReport:
    """Lightweight warehouse health gate before business incidents may open."""
    if engine.con is None:
        return QualityReport(
            healthy=False,
            freshness_ok=False,
            completeness_ok=False,
            volume_ok=False,
            row_count=0,
            missing_rate=1.0,
            latest_ts="",
            message="No executor connection",
        )

    latest = engine.con.execute(f"SELECT MAX(ts) FROM {engine.fact_table}").fetchone()[0]
    if latest is None:
        return QualityReport(
            healthy=False,
            freshness_ok=False,
            completeness_ok=False,
            volume_ok=False,
            row_count=0,
            missing_rate=1.0,
            latest_ts="",
            message="No data in fact table",
        )

    latest_ts = latest if isinstance(latest, datetime) else datetime.fromisoformat(str(latest))
    lag = at - latest_ts if at >= latest_ts else timedelta(0)
    freshness_ok = lag <= timedelta(hours=max_lag_hours) or latest_ts >= at

    row = engine.con.execute(
        f"""
        SELECT
            COUNT(*)::INTEGER,
            AVG(CASE WHEN orders IS NULL THEN 1.0 ELSE 0.0 END)::DOUBLE
        FROM {engine.fact_table}
        WHERE ts = ?
        """,
        [at],
    ).fetchone()
    row_count = int(row[0] or 0)
    missing_rate = float(row[1] or 0.0)
    volume_ok = row_count >= min_rows
    completeness_ok = missing_rate <= max_missing_rate
    healthy = freshness_ok and volume_ok and completeness_ok

    if healthy:
        message = "Data healthy ✓"
    else:
        parts = []
        if not freshness_ok:
            parts.append("stale")
        if not volume_ok:
            parts.append("low volume")
        if not completeness_ok:
            parts.append("missing values")
        message = "Data quality gate failed: " + ", ".join(parts)

    return QualityReport(
        healthy=healthy,
        freshness_ok=freshness_ok,
        completeness_ok=completeness_ok,
        volume_ok=volume_ok,
        row_count=row_count,
        missing_rate=missing_rate,
        latest_ts=latest_ts.isoformat(sep=" "),
        message=message,
    )


def assert_quality_allows_incidents(quality: QualityReport) -> bool:
    return quality.healthy
