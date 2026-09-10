from __future__ import annotations

# Lightweight formatters shared by CLI and marimo surfaces.


def money(v: float) -> str:
    return f"€{v:,.0f}"


def pct(v: float) -> str:
    return f"{v:+.1%}"


def metric_fmt(v: float, unit: str) -> str:
    if unit == "eur":
        return money(v)
    if unit == "ratio":
        return f"{v:.2%}"
    return f"{v:,.0f}"


def segment_label(filters: dict[str, str]) -> str:
    if not filters:
        return "all"
    order = (
        "city",
        "meal_period",
        "customer_type",
        "basket_band",
        "channel",
        "restaurant_category",
    )
    parts = [filters[k] for k in order if k in filters]
    return " / ".join(parts) if parts else " / ".join(filters.values())
