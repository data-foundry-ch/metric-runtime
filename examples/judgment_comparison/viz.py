"""Presentation helpers for the judgment comparison marimo app."""

from __future__ import annotations

from typing import Any

from examples.judgment_comparison.evidence import InvestigationEvidence, MetricEvidence
from examples.judgment_comparison.judgments import (
    JUDGMENT_FIELDS,
    CostLine,
    JudgmentRun,
    ProviderAvailability,
    compare_costs,
    compare_judgments,
    format_cost_usd,
    jev_probabilities,
)
from examples.judgment_comparison.scenarios import (
    FOCAL_METRIC_ID,
    GRAPH_LAYOUT,
    RuntimeSnapshot,
    catalog_graph,
)

STYLE = {
    "ink": "#1b2430",
    "muted": "#5c6b7a",
    "line": "#d8e0e8",
    "paper": "#ffffff",
    "bg": "#f4f6f8",
    "accent": "#1f4e79",
    "good": "#2f6f4e",
    "bad": "#9b3a32",
    "warn": "#8a6d1f",
    "focal": "#1f4e79",
    "changed": "#9b3a32",
    "path": "#c56a1a",
    "normal": "#6b7c8a",
    "font": "IBM Plex Sans, Segoe UI, system-ui, sans-serif",
}


def format_value(value: float | None, unit: str | None) -> str:
    if value is None:
        return "—"
    if unit in {"percent", "ratio"}:
        return f"{value:.1f}%"
    if unit in {"eur", "EUR"}:
        return f"€{value:,.0f}"
    if unit == "count":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def format_change(metric: MetricEvidence) -> tuple[str, str]:
    """Return (display, css class) for the headline change."""
    if metric.unit in {"percent", "ratio"} and metric.change_percentage_points is not None:
        text = f"{metric.change_percentage_points:+.1f} pp"
    elif metric.change_pct is not None:
        text = f"{metric.change_pct:+.1f}%"
    else:
        return "—", "neutral"
    if metric.change_pct is None:
        return text, "neutral"
    if metric.metric_id in {"profit_margin", "average_order_value"}:
        tone = "bad" if metric.change_pct < 0 else "good"
    elif metric.metric_id in {"average_cost_per_order", "basket_threshold_concentration"}:
        tone = "bad" if metric.change_pct > 0 else "good"
    else:
        tone = "good" if metric.change_pct > 0 else "bad"
    return text, tone


def _card(title: str, change: str, tone: str, subtitle: str) -> str:
    return (
        f'<div class="metric-card">'
        f'<div class="metric-label">{title}</div>'
        f'<div class="metric-change {tone}">{change}</div>'
        f'<div class="metric-sub">{subtitle}</div>'
        f"</div>"
    )


def metric_cards_html(evidence: InvestigationEvidence) -> str:
    items = [evidence.focal_metric, *evidence.related_metrics]
    order = [
        "revenue",
        "orders",
        "profit_margin",
        "average_order_value",
        "average_cost_per_order",
        "basket_threshold_concentration",
    ]
    by_id = {item.metric_id: item for item in items}
    cards = []
    for metric_id in order:
        item = by_id[metric_id]
        change, tone = format_change(item)
        current = format_value(item.current_value, item.unit)
        cards.append(_card(item.name, change, tone, f"now {current}"))
    return '<div class="metric-grid">' + "".join(cards) + "</div>"


FIELD_COPY = {
    "material": (
        "Should we pay attention?",
        "Is the change material enough for the business?",
    ),
    "promotion_related": (
        "Is the promotion involved?",
        "Does the evidence point at the active campaign?",
    ),
    "primary_driver": (
        "What's driving this?",
        "Best-fit mechanism from the evidence",
    ),
    "needs_human_review": (
        "Does a person need to review?",
        "Before any consequential action",
    ),
    "action": (
        "What should happen next?",
        "The predefined next-step category",
    ),
}

DRIVER_LABELS = {
    "promotion_behavior": "Promotion behaviour",
    "volume": "Volume",
    "pricing": "Pricing",
    "cost_pressure": "Cost pressure",
    "product_mix": "Product mix",
    "data_quality": "Data quality",
    "unclear": "Unclear",
}

ACTION_LABELS = {
    "observe": "Keep watching",
    "human_review": "Ask a person to review",
    "review_promotion": "Review the promotion",
    "investigate_costs": "Look into costs",
    "escalate_finance": "Escalate to finance",
}


def _bool_label(value: bool) -> str:
    return "Yes" if value else "No"


def friendly_value(field: str, raw: Any) -> str:
    if raw is None:
        return "—"
    if field in {"material", "promotion_related", "needs_human_review"}:
        return _bool_label(bool(raw))
    key = raw.value if hasattr(raw, "value") else str(raw)
    if field == "primary_driver":
        return DRIVER_LABELS.get(key, key.replace("_", " "))
    if field == "action":
        return ACTION_LABELS.get(key, key.replace("_", " "))
    return str(raw)


def format_duration_ms(ms: float | None, *, simulated: bool = False) -> str:
    if simulated:
        return "n/a (simulated)"
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000.0
    if seconds < 10:
        return f"{seconds:.1f} s"
    return f"{seconds:.0f} s"


def _prob_bars(dist: dict[str, float]) -> str:
    if not dist:
        return ""
    ordered = sorted(dist.items(), key=lambda item: (-item[1], item[0]))
    rows = []
    for name, value in ordered:
        width = max(2.0, min(100.0, value * 100.0))
        label = DRIVER_LABELS.get(name, name.replace("_", " "))
        rows.append(
            "<div class='prob-row'>"
            f"<span class='prob-name'>{label}</span>"
            f"<span class='prob-bar'><span style='width:{width:.1f}%'></span></span>"
            f"<span class='prob-val'>{value:.0%}</span>"
            "</div>"
        )
    return "<div class='prob-block'>" + "".join(rows) + "</div>"


def _solution_metrics(run: JudgmentRun | None, line: CostLine) -> tuple[str, str, str]:
    """Return (cost display, cost note, time display)."""
    if run is None:
        return "—", "", "—"
    if run.error and run.judgment is None and not run.simulated:
        return line.display, line.note or "", format_duration_ms(run.latency_ms, simulated=False)
    return (
        line.display,
        line.note or "",
        format_duration_ms(run.latency_ms, simulated=run.simulated),
    )


def spend_time_html(jev: JudgmentRun | None, llm: JudgmentRun | None) -> str:
    costs = compare_costs(jev, llm)
    jev_cost, jev_note, jev_time = _solution_metrics(jev, costs.jev)
    llm_cost, llm_note, llm_time = _solution_metrics(llm, costs.llm)

    def card(title: str, cost: str, note: str, time: str) -> str:
        extra = f"<div class='spend-note'>{note}</div>" if note else ""
        return (
            "<div class='spend-card'>"
            f"<div class='spend-kicker'>{title}</div>"
            "<div class='spend-pair'>"
            "<div class='spend-metric'>"
            "<div class='spend-label'>Cost</div>"
            f"<div class='spend-value'>{cost}</div>"
            f"{extra}"
            "</div>"
            "<div class='spend-metric'>"
            "<div class='spend-label'>Time</div>"
            f"<div class='spend-value'>{time}</div>"
            "</div>"
            "</div>"
            "</div>"
        )

    delta = ""
    if costs.comparable and costs.delta_usd is not None:
        estimated = " · Jev estimated" if costs.jev.state == "estimated" else ""
        delta = (
            f"<p class='spend-delta'>LLM − Jev{estimated} = {format_cost_usd(costs.delta_usd)}</p>"
        )
    return (
        "<div class='spend-board'>"
        "<h2>Cost and time</h2>"
        "<div class='spend-grid'>"
        f"{card('Jev', jev_cost, jev_note, jev_time)}"
        f"{card('Classic LLM', llm_cost, llm_note, llm_time)}"
        "</div>"
        f"{delta}"
        "</div>"
    )


def _answer_cell(run: JudgmentRun | None, field: str) -> str:
    if run is None:
        return "<div class='judge-a muted'>Not run</div>"
    if run.error and run.judgment is None:
        return "<div class='judge-a status-fail'>Request failed</div>"
    if run.judgment is None:
        return "<div class='judge-a muted'>—</div>"
    value = friendly_value(field, getattr(run.judgment, field))
    extra = ""
    if field == "primary_driver":
        extra = _prob_bars(jev_probabilities(run, "primary_driver"))
    return f"<div class='judge-a'><div class='judge-answer'>{value}</div>{extra}</div>"


def judgment_details_html(jev: JudgmentRun | None, llm: JudgmentRun | None) -> str:
    simulated = ""
    if (jev and jev.simulated) or (llm and llm.simulated):
        simulated = "<div class='sim-banner'>SIMULATED RESULT · No API request was made.</div>"
    if jev and jev.error and jev.judgment is None:
        simulated += f"<p class='status-fail'>Jev · {jev.error}</p>"
    if llm and llm.error and llm.judgment is None:
        simulated += f"<p class='status-fail'>Classic LLM · {llm.error}</p>"

    agreement = None
    if jev and llm and jev.judgment and llm.judgment:
        agreement = compare_judgments(jev.judgment, llm.judgment)

    rows = ["<div class='judge-head'><div></div><div>Jev</div><div>Classic LLM</div></div>"]
    for field in JUDGMENT_FIELDS:
        question, hint = FIELD_COPY[field]
        match_cls = ""
        mark = ""
        if agreement is not None:
            same = getattr(agreement, field)
            match_cls = "agree" if same else "differ"
            mark = (
                "<span class='judge-pill agree'>Agree</span>"
                if same
                else "<span class='judge-pill differ'>Differ</span>"
            )
        rows.append(
            f"<div class='judge-row {match_cls}'>"
            "<div class='judge-q'>"
            f"<div class='judge-question'>{question}</div>"
            f"<div class='judge-hint'>{hint}</div>"
            f"{mark}"
            "</div>"
            f"{_answer_cell(jev, field)}"
            f"{_answer_cell(llm, field)}"
            "</div>"
        )

    summary = ""
    if agreement is not None:
        summary = f"<p class='agree-total'>{agreement.summary.replace('decisions', 'answers')}</p>"
    return (
        "<div class='judge-board'>"
        "<h2>What each model decided</h2>"
        f"{simulated}"
        "<div class='judge-table'>"
        f"{''.join(rows)}"
        "</div>"
        f"{summary}"
        "</div>"
    )


def availability_html(availability: ProviderAvailability) -> str:
    missing = []
    if not availability.pydantic_ai:
        missing.append("Pydantic AI")
    if not availability.jev_key:
        missing.append("TYPESAFE_API_KEY")
    if not availability.llm_model:
        missing.append("DEMO_LLM_MODEL")
    elif not availability.llm_key:
        missing.append(availability.llm_key_env or "LLM key")
    if not missing:
        return ""
    return f"<p class='controls-note'>Live needs: {', '.join(missing)}</p>"


def history_figure(snapshot: RuntimeSnapshot):
    import plotly.graph_objects as go

    xs = [point.at for point in snapshot.history]
    ys = [point.value for point in snapshot.history]
    current = snapshot.statuses[FOCAL_METRIC_ID]
    baseline = current.baseline_mean
    colors = ["#9b3a32" if point.anomalous else "#1f4e79" for point in snapshot.history]
    fig = go.Figure()
    fig.add_hline(
        y=baseline,
        line_dash="dot",
        line_color="#8a6d1f",
        annotation_text="seasonal baseline (comparable lunch weeks)",
        annotation_position="top left",
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="lines+markers",
            line=dict(color="#1f4e79", width=2),
            marker=dict(size=9, color=colors),
            name="Profit margin",
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[current.as_of],
            y=[current.value],
            mode="markers",
            marker=dict(size=14, color="#9b3a32", symbol="diamond"),
            name="current observation",
            hovertemplate="current %{y:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        title="Profit margin",
        template="plotly_white",
        height=280,
        margin=dict(l=40, r=20, t=50, b=40),
        yaxis_title="percent",
        showlegend=True,
        legend=dict(orientation="h", y=1.12),
        font=dict(family=STYLE["font"], color=STYLE["ink"]),
    )
    return fig


def graph_figure(snapshot: RuntimeSnapshot):
    import plotly.graph_objects as go

    graph = catalog_graph()
    path = set(snapshot.explanatory_path)
    path_edges = set(zip(snapshot.explanatory_path, snapshot.explanatory_path[1:], strict=False))
    traces: list[Any] = []

    def edge_trace(points: list[tuple[float, float]], color: str, width: float) -> go.Scatter:
        xs: list[float | None] = []
        ys: list[float | None] = []
        for index in range(0, len(points), 2):
            start, end = points[index], points[index + 1]
            xs.extend([start[0], end[0], None])
            ys.extend([start[1], end[1], None])
        return go.Scatter(
            x=xs,
            y=ys,
            mode="lines",
            line=dict(color=color, width=width),
            hoverinfo="skip",
            showlegend=False,
        )

    normal_pts: list[tuple[float, float]] = []
    path_pts: list[tuple[float, float]] = []
    for src, dst in graph.edges:
        if src not in GRAPH_LAYOUT or dst not in GRAPH_LAYOUT:
            continue
        pair = (GRAPH_LAYOUT[src], GRAPH_LAYOUT[dst])
        if (src, dst) in path_edges or (dst, src) in path_edges:
            path_pts.extend(pair)
        else:
            normal_pts.extend(pair)
    if normal_pts:
        traces.append(edge_trace(normal_pts, STYLE["line"], 1.5))
    if path_pts:
        traces.append(edge_trace(path_pts, STYLE["path"], 3.0))

    node_x, node_y, colors, sizes, labels, hover = [], [], [], [], [], []
    for metric_id, (x, y) in GRAPH_LAYOUT.items():
        status = snapshot.statuses.get(metric_id)
        node_x.append(x)
        node_y.append(y)
        if metric_id == FOCAL_METRIC_ID:
            colors.append(STYLE["focal"])
            sizes.append(28)
        elif status is not None and status.anomaly:
            colors.append(STYLE["changed"])
            sizes.append(22)
        elif metric_id in path:
            colors.append(STYLE["path"])
            sizes.append(20)
        else:
            colors.append(STYLE["normal"])
            sizes.append(16)
        name = snapshot.metric_names.get(metric_id, metric_id)
        change = f"{status.relative_change:+.1%}" if status is not None else ""
        labels.append(f"<b>{name}</b><br>{change}")
        state = status.state.value if status is not None else ""
        hover.append(f"{name}<br>state {state}<br>{change}")
    traces.append(
        go.Scatter(
            x=node_x,
            y=node_y,
            mode="markers+text",
            marker=dict(size=sizes, color=colors, line=dict(width=1, color="#ffffff")),
            text=labels,
            textposition="top center",
            hovertext=hover,
            hoverinfo="text",
            showlegend=False,
        )
    )
    fig = go.Figure(traces)
    fig.update_layout(
        title="Investigation graph",
        template="plotly_white",
        height=280,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(l=20, r=20, t=50, b=20),
        font=dict(family=STYLE["font"], color=STYLE["ink"]),
    )
    fig.update_xaxes(range=[-3.8, 2.6])
    fig.update_yaxes(range=[-2.1, 4.0])
    return fig
