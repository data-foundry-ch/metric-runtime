"""PyPizza presentation graph (Plotly). Demo-only — not part of core."""

from __future__ import annotations

from typing import Any

import networkx as nx

from metric_runtime.models import KPIStatus

try:
    from .graph_layout import dependency_depths, presentation_dependency_layout
except ImportError:  # pragma: no cover
    from graph_layout import dependency_depths, presentation_dependency_layout

STYLE = {
    "marketing": "#2E7D32",
    "finance": "#C62828",
    "operations": "#F9A825",
    "watch": "#FFE082",  # light yellow: z within ±2.5
    "path": "#EF6C00",
    "root": "#B71C1C",
    "center": "#C62828",
    "muted_marketing": "#A5D6A7",
    "muted_finance": "#EF9A9A",
    "muted_operations": "#FFE082",
    "edge": "#BDBDBD",
    "edge_path": "#BF360C",
    "text": "#212121",
    "text_muted": "#616161",
    "paper": "#FFFFFF",
    "bg": "#FFFFFF",
    "font": "IBM Plex Sans, Helvetica Neue, Arial, sans-serif",
}

PLOTLY_DISPLAY_CONFIG: dict[str, Any] = {
    "responsive": True,
    "scrollZoom": False,
    "displaylogo": False,
    "modeBarButtonsToRemove": [
        "lasso2d",
        "select2d",
        "autoScale2d",
        "hoverClosestCartesian",
        "hoverCompareCartesian",
    ],
    "toImageButtonOptions": {
        "format": "png",
        "filename": "pypizza_kpi_network",
        "height": 900,
        "width": 1400,
        "scale": 2,
    },
}


def _fmt_value(value: float, unit: str) -> str:
    if unit == "ratio":
        return f"{value:.1%}"
    if unit == "eur":
        return f"€{value:,.0f}"
    return f"{value:,.0f}"


def _fmt_change(relative_change: float) -> str:
    """Keep stage labels readable when baselines are near zero."""
    if abs(relative_change) > 9.99:
        return f"{relative_change:+.0f}×"
    return f"{relative_change:+.0%}"


def _color_from_z(
    z_score: float,
    directionality: str,
    relative_change: float = 0.0,
) -> str:
    """Traffic-light from z:

    - light yellow when z is in [-2.5, +2.5], or when the metric is context
      rather than a scored outcome
    - outside that band: green if the move is good for the KPI, else red
    """
    _ = relative_change
    z = round(float(z_score), 1)
    if directionality == "neutral" or abs(z) <= 2.5:
        return STYLE["watch"]
    if directionality == "lower_is_bad":
        return STYLE["marketing"] if z > 0 else STYLE["root"]
    if directionality == "higher_is_bad":
        return STYLE["marketing"] if z < 0 else STYLE["root"]
    # two_sided (talk graph: mostly growth metrics): up → green, down → red
    return STYLE["marketing"] if z > 0 else STYLE["root"]


def build_plotly_network(
    graph: nx.DiGraph,
    statuses: dict[str, KPIStatus],
    *,
    center_metric: str = "weekend_profit",
    selected_metric: str | None = None,
    emphasize_path: list[str] | None = None,
    max_depth: int | None = 3,
    mute_outside_path: bool = False,
) -> Any:
    import plotly.graph_objects as go

    _ = selected_metric
    emphasize = list(emphasize_path or [])
    emphasize_set = set(emphasize)
    depths = dependency_depths(graph, center_metric, max_depth=max_depth)
    visible = [n for n in graph.nodes if n in depths]
    sub = graph.subgraph(visible).copy()
    pos = presentation_dependency_layout(sub, center_metric, max_depth=max_depth)

    path_edges: set[tuple[str, str]] = set()
    for a, b in zip(emphasize, emphasize[1:]):
        if sub.has_edge(b, a):
            path_edges.add((b, a))
        elif sub.has_edge(a, b):
            path_edges.add((a, b))

    def side_of(node: str) -> str:
        return str(sub.nodes[node].get("graph_side") or "finance")

    def node_color(node: str, st: KPIStatus | None) -> str:
        if st is None:
            return STYLE["edge"]
        directionality = str(
            sub.nodes[node].get("graph_directionality")
            or sub.nodes[node].get("directionality")
            or st.directionality.value
        )
        return _color_from_z(st.z_score, directionality, st.relative_change)

    def node_opacity(node: str, st: KPIStatus | None) -> float:
        if not mute_outside_path or not emphasize:
            return 0.95
        if node in emphasize_set or node == center_metric:
            return 0.95
        if st is not None and abs(round(st.z_score, 1)) > 2.5:
            return 0.9
        return 0.55

    def node_size(node: str, st: KPIStatus | None) -> float:
        if node == center_metric:
            return 34
        if st and (st.root_candidate or (emphasize and node == emphasize[-1])):
            return 22
        if node in emphasize_set:
            return 18
        if st and abs(round(st.z_score, 1)) > 2.5:
            return 17
        return 15

    # Edges: neutral structure + stronger explanatory path (not department paint).
    edge_base_x: list[float | None] = []
    edge_base_y: list[float | None] = []
    edge_path_x: list[float | None] = []
    edge_path_y: list[float | None] = []
    for u, v in sub.edges():
        if u not in pos or v not in pos:
            continue
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        if (u, v) in path_edges:
            edge_path_x += [x0, x1, None]
            edge_path_y += [y0, y1, None]
        else:
            edge_base_x += [x0, x1, None]
            edge_base_y += [y0, y1, None]

    traces: list[Any] = [
        go.Scatter(
            x=edge_base_x,
            y=edge_base_y,
            mode="lines",
            line=dict(width=1.15, color=STYLE["edge"]),
            hoverinfo="skip",
            showlegend=False,
        )
    ]
    if edge_path_x:
        traces.append(
            go.Scatter(
                x=edge_path_x,
                y=edge_path_y,
                mode="lines",
                line=dict(width=2.6, color=STYLE["edge_path"]),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    ordered = [n for n in sub.nodes() if n in pos]
    node_x = [pos[n][0] for n in ordered]
    node_y = [pos[n][1] for n in ordered]
    colors: list[str] = []
    sizes: list[float] = []
    opacities: list[float] = []
    custom: list[list[str]] = []
    for n in ordered:
        meta = sub.nodes[n]
        st = statuses.get(n)
        side = side_of(n)
        colors.append(node_color(n, st))
        sizes.append(node_size(n, st))
        opacities.append(node_opacity(n, st))
        change = _fmt_change(st.relative_change) if st else "-"
        z_disp = f"{round(st.z_score, 1):+.1f}" if st else "-"
        custom.append(
            [
                str(meta.get("label", n)),
                side.title(),
                change,
                str(meta.get("owner", "")),
                _fmt_value(st.value, str(meta.get("unit", ""))) if st else "-",
                _fmt_value(st.baseline_mean, str(meta.get("unit", ""))) if st else "-",
                z_disp,
                (
                    "lowest explanatory"
                    if st and st.root_candidate
                    else ("anomaly" if st and st.anomaly else "normal")
                ),
            ]
        )

    traces.append(
        go.Scatter(
            x=node_x,
            y=node_y,
            mode="markers",
            marker=dict(
                size=sizes,
                color=colors,
                opacity=opacities,
                line=dict(width=1.0, color="#424242"),
            ),
            customdata=custom,
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Department: %{customdata[1]}<br>"
                "Change: %{customdata[2]}<br>"
                "Owner: %{customdata[3]}<br>"
                "Value: %{customdata[4]} · expected %{customdata[5]}<br>"
                "<b>z %{customdata[6]}</b> · %{customdata[7]}"
                "<extra></extra>"
            ),
            name="KPIs",
            showlegend=False,
        )
    )

    for name, color in (
        ("Within ±2.5", STYLE["watch"]),
        ("Good (|z| > 2.5)", STYLE["marketing"]),
        ("Bad (|z| > 2.5)", STYLE["root"]),
        ("Explanatory path", STYLE["edge_path"]),
    ):
        traces.append(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(size=11, color=color, line=dict(width=0)),
                name=name,
                hoverinfo="skip",
            )
        )

    annotations: list[dict[str, Any]] = []

    def add_label(
        *,
        x: float,
        y: float,
        text: str,
        xanchor: str,
        yanchor: str,
        xshift: float = 0,
        yshift: float = 0,
        color: str = STYLE["text"],
        size: int = 12,
    ) -> None:
        annotations.append(
            {
                "x": x,
                "y": y,
                "xref": "x",
                "yref": "y",
                "text": text,
                "showarrow": False,
                "xanchor": xanchor,
                "yanchor": yanchor,
                "xshift": xshift,
                "yshift": yshift,
                "align": (
                    "left" if xanchor == "left" else ("right" if xanchor == "right" else "center")
                ),
                "font": {"size": size, "color": color, "family": STYLE["font"]},
                "bgcolor": "rgba(255,255,255,0.78)",
                "borderpad": 2,
            }
        )

    tweaks: dict[str, tuple[str, str, float, float]] = {
        "new_customers": ("left", "bottom", 8, 6),
        "opportunities": ("right", "middle", -8, 0),
        "marketing_leads": ("left", "top", 8, -4),
        "delivery_cost_per_order": ("left", "bottom", 8, 6),
        "discount_cost_per_order": ("left", "middle", 10, 0),
        "basket_share_under_20": ("left", "middle", 10, 0),
        "basket_threshold_concentration": ("left", "middle", 10, 0),
        "basket_share_25_29": ("left", "middle", 10, 0),
        "basket_share_30_39": ("left", "top", 8, -6),
        "basket_share_40_plus": ("center", "top", 0, -12),
        "average_delivery_time": ("center", "top", 0, -12),
        "late_delivery_rate": ("center", "top", 0, -12),
        "revenue": ("right", "bottom", -8, 8),
        "profit_margin": ("left", "bottom", 8, 8),
        "orders": ("right", "middle", -8, 0),
        "average_cost_per_order": ("left", "bottom", 8, 6),
        "average_order_value": ("right", "middle", -10, 0),
    }

    for n in ordered:
        x, y = pos[n]
        meta = sub.nodes[n]
        st = statuses.get(n)
        label = str(meta.get("label", n))
        side = side_of(n)
        muted = bool(
            mute_outside_path
            and emphasize
            and n not in emphasize_set
            and not (side == "operations" and st and st.anomaly)
        )
        color = STYLE["text_muted"] if muted else STYLE["text"]

        if n == center_metric:
            change = _fmt_change(st.relative_change) if st else "-"
            add_label(
                x=x,
                y=y,
                text=f"<b>Weekend Profit</b><br>{change}",
                xanchor="center",
                yanchor="top",
                yshift=-28,
                color=STYLE["text"],
                size=13,
            )
            continue

        norm = (x * x + y * y) ** 0.5 or 1.0
        ux, uy = x / norm, y / norm
        lx, ly = x + 0.22 * ux, y + 0.22 * uy

        if n in tweaks:
            xanchor, yanchor, xshift, yshift = tweaks[n]
        elif abs(ux) >= abs(uy) * 0.75:
            xanchor = "right" if ux < 0 else "left"
            yanchor = "middle"
            xshift = -10 if ux < 0 else 10
            yshift = 0.0
        elif uy > 0:
            xanchor, yanchor, xshift, yshift = "center", "bottom", 0.0, 10.0
        else:
            xanchor, yanchor, xshift, yshift = "center", "top", 0.0, -10.0

        if st is not None and (st.anomaly or n in emphasize_set or side == "operations"):
            body = f"<b>{label}</b><br>{_fmt_change(st.relative_change)}"
        elif st is not None:
            body = (
                f"<b>{label}</b><br>"
                f"<span style='color:#757575'>{_fmt_change(st.relative_change)}</span>"
            )
        else:
            body = f"<b>{label}</b>"

        add_label(
            x=lx,
            y=ly,
            text=body,
            xanchor=xanchor,
            yanchor=yanchor,
            xshift=xshift,
            yshift=yshift,
            color=color,
            size=12,
        )

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]

    # Region labels ride the outer edge so they never land on a node label.
    ops_xs = [pos[n][0] for n in ordered if side_of(n) == "operations"] or [0.0]
    region_labels = (
        (min(xs), max(ys) + 0.95, "Marketing", "left", "bottom", "marketing"),
        (max(xs), max(ys) + 0.95, "Finance", "right", "bottom", "finance"),
        (
            sum(ops_xs) / len(ops_xs),
            min(ys) - 1.05,
            "Operations",
            "center",
            "top",
            "operations",
        ),
    )
    annotations.extend(
        {
            "x": rx,
            "y": ry,
            "text": f"<b>{text}</b>",
            "showarrow": False,
            "xanchor": xanchor,
            "yanchor": yanchor,
            "font": {
                "size": 14,
                "color": STYLE[style_key],
                "family": STYLE["font"],
            },
            "bgcolor": "rgba(255,255,255,0)",
        }
        for rx, ry, text, xanchor, yanchor, style_key in region_labels
    )

    x_range = [min(xs) - 2.3, max(xs) + 2.3]
    y_range = [min(ys) - 2.05, max(ys) + 1.85]

    fig = go.Figure(data=traces)
    fig.update_layout(
        template="plotly_white",
        title={
            "text": "PyPizza business KPI network",
            "x": 0.01,
            "xanchor": "left",
            "font": {"size": 18, "color": STYLE["text"], "family": STYLE["font"]},
        },
        font={"family": STYLE["font"], "color": STYLE["text"], "size": 12},
        showlegend=True,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1.0,
            "bgcolor": "rgba(255,255,255,0.85)",
            "borderwidth": 0,
            "font": {"size": 11},
            "itemsizing": "constant",
        },
        hovermode="closest",
        hoverlabel={
            "bgcolor": "white",
            "bordercolor": "#90A4AE",
            "font": {"size": 12, "family": STYLE["font"], "color": STYLE["text"]},
        },
        autosize=True,
        height=720,
        margin={"l": 48, "r": 48, "t": 78, "b": 56},
        paper_bgcolor=STYLE["bg"],
        plot_bgcolor=STYLE["paper"],
        xaxis={
            "range": x_range,
            "visible": False,
            "showgrid": False,
            "zeroline": False,
            "showticklabels": False,
        },
        yaxis={
            "range": y_range,
            "scaleanchor": "x",
            "scaleratio": 1,
            "visible": False,
            "showgrid": False,
            "zeroline": False,
            "showticklabels": False,
        },
        annotations=annotations,
        uirevision="kpi-network-v2",
        clickmode="event",
    )
    return fig
