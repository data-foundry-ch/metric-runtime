import marimo

__generated_with = "0.23.16"
app = marimo.App(
    width="full",
    css_file="app.css",
    layout_file="layouts/app.slides.json",
)


@app.cell
def _():
    import inspect
    from datetime import datetime, timedelta
    from pathlib import Path

    import altair as alt
    import duckdb
    import marimo as mo

    import sys
    from pathlib import Path as _P

    _here = _P(__file__).resolve().parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))

    from metric_runtime import detectors as detectors_module
    from metric_runtime import state as state_module
    from metric_runtime.detectors import (
        DetectorStrategy,
        SeasonalZScoreDetector,
        ThresholdDetector,
    )
    from metric_runtime.engine import KPIEngine
    from metric_runtime.graph import (
        evaluate_graph_state_window,
        find_explanatory_paths,
        mark_root_candidates,
    )
    from metric_runtime.models import (
        DetectorConfig,
        Directionality,
        Formula,
        ImpactModel,
        IncidentState,
        KPIDefinition,
        Measure,
        MetricState,
        SupportRequirement,
    )
    from metric_runtime.state import StatePolicy, collect_window_history, evolve_state

    from catalog import TALK_GRAPH_METRICS, build_catalog
    from demo import build_talk_graph, investigate_window, open_pypizza_incident, preferred_explanatory_path
    from formatters import money, metric_fmt, pct
    from impact import campaign_impact_decomposition
    from quality import check_data_quality
    from queries import basket_distribution
    from viz import PLOTLY_DISPLAY_CONFIG, build_plotly_network

    open_smart_incident = open_pypizza_incident

    return (
        DetectorConfig,
        DetectorStrategy,
        Directionality,
        Formula,
        ImpactModel,
        IncidentState,
        KPIDefinition,
        KPIEngine,
        Measure,
        MetricState,
        PLOTLY_DISPLAY_CONFIG,
        Path,
        SeasonalZScoreDetector,
        StatePolicy,
        SupportRequirement,
        TALK_GRAPH_METRICS,
        ThresholdDetector,
        alt,
        build_catalog,
        basket_distribution,
        build_plotly_network,
        build_talk_graph,
        campaign_impact_decomposition,
        check_data_quality,
        collect_window_history,
        datetime,
        detectors_module,
        duckdb,
        evaluate_graph_state_window,
        evolve_state,
        find_explanatory_paths,
        inspect,
        investigate_window,
        mark_root_candidates,
        metric_fmt,
        mo,
        money,
        open_smart_incident,
        pct,
        preferred_explanatory_path,
        state_module,
        timedelta,
    )


@app.cell
def _(KPIEngine, Path, build_catalog, datetime, duckdb):
    root = Path(__file__).resolve().parent
    db_path = root / "data" / "pypizza.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(
            f"Missing {db_path}. Run: python examples/pypizza/generate_data.py"
        )
    con = duckdb.connect(str(db_path), read_only=True)
    catalog = build_catalog()
    engine = KPIEngine(catalog, connection=con, fact_table="pypizza_halfhourly")
    CAMPAIGN_START = datetime(2026, 5, 15, 11, 30)
    CAMPAIGN_END = datetime(2026, 5, 17, 13, 30)
    AMS_LUNCH = {"city": "Amsterdam", "meal_period": "lunch"}
    return AMS_LUNCH, CAMPAIGN_END, CAMPAIGN_START, catalog, engine


@app.cell
def _(inspect, mo):
    def kicker(step: str, title: str):
        return mo.Html(
            f'<div class="act-kicker">{step}</div>'
            f'<h2 style="margin:0 0 0.55rem 0">{title}</h2>'
        )

    def source_of(obj) -> str:
        """Live source from the shipped package. slides can never drift."""
        return inspect.getsource(obj).rstrip()

    def code_block(*objs):
        body = "\n\n\n".join(source_of(o) for o in objs)
        return mo.md(f"```python\n{body}\n```")

    def why_panel(question: str, lead: str, bullets: list[str] | None = None):
        items = "".join(f"<li>{b}</li>" for b in bullets or [])
        tail = (
            f'<ul class="muted" style="margin:0;padding-left:1.1rem;">{items}</ul>'
            if items
            else ""
        )
        return mo.Html(
            f"""
            <div class="panel panel-amber">
              <div class="act-kicker">This answers</div>
              <div class="arch-q">{question}</div>
              <p class="muted" style="margin:0.5rem 0 0.4rem 0;">{lead}</p>
              {tail}
            </div>
            """
        )

    return code_block, kicker, why_panel


# ── Title ──────────────────────────────────────────────────────────────────


@app.cell(hide_code=True)
def _(kicker, mo):
    mo.vstack(
        [
            kicker("PyData · PyPizza", "Your Dashboard Is Too Late"),
            mo.md("## The PyPizza Lunch Campaign Incident"),
            mo.md(
                "A small case study in **business semantics**, "
                "not another dashboard."
            ),
        ],
        gap=0.7,
    )
    return


# ── 1. The Call ────────────────────────────────────────────────────────────


@app.cell(hide_code=True)
def _(kicker, mo):
    _coffee = """
        <div class="panel panel-ok story-panel">
          <div class="act-kicker">Mon 09:12 · Coffee machine · Marketing</div>
          <div class="mail-subject">Did you see the weekend sales?</div>
          <p>“Amsterdam lunch went through the roof.”</p>
          <p>
            “Orders up a third. Revenue up. And the new customers. We have
            not seen numbers like that since launch.”
          </p>
          <p>
            “Best campaign from an intern we ever had. We are rolling it out to
            Rotterdam and Utrecht <b>on Friday</b>.”
          </p>
        </div>
        """
    _email = """
        <div class="panel panel-accent story-panel">
          <div class="act-kicker">Mon 09:41 · Inbox · Finance Manager</div>
          <div class="mail-subject">Weekend profitability in Amsterdam</div>
          <p>“I have been through the weekend numbers.”</p>
          <p>
            “<b>Profit is down.</b> Margin is down harder. Nothing is broken
            as far as I can tell, and no one has raised an incident.”
          </p>
          <p>
            “So I cannot explain it. Can you find out what happened?
            <b>before next weekend</b>?”
          </p>
        </div>
        """

    mo.vstack(
        [
            kicker("1 · Monday", "Two conversations, half an hour apart"),
            mo.Html(
                '<div style="display:flex;gap:0.6rem;align-items:stretch;">'
                f"{_coffee}{_email}</div>"
            ),
            mo.md(

                "**Both of them are right.** And they are both talking about "
                "the weekend."
            ),
            mo.md(
                "### Congratulations, you are a data analyst at PyPizza now."
            ),
        ],
        gap=0.55,
    )
    return


# ── Dashboard data (shared) ────────────────────────────────────────────────


@app.cell
def _(
    AMS_LUNCH,
    CAMPAIGN_END,
    CAMPAIGN_START,
    campaign_impact_decomposition,
    engine,
    timedelta,
):
    impact_summary = campaign_impact_decomposition(
        engine, CAMPAIGN_START, CAMPAIGN_END, AMS_LUNCH
    )
    chart_start = CAMPAIGN_START - timedelta(days=21)
    daily_rows = engine.con.execute(
        """
        SELECT
            CAST(ts AS DATE) AS day,
            SUM(orders)::DOUBLE AS orders,
            SUM(gross_order_value_eur)::DOUBLE AS revenue,
            SUM(weekend_profit_eur)::DOUBLE AS profit,
            SUM(new_customer_orders)::DOUBLE AS new_customers,
            SUM(promo_orders)::DOUBLE AS promo_orders
        FROM pypizza_halfhourly
        WHERE city = ?
          AND meal_period = ?
          AND ts BETWEEN ? AND ?
        GROUP BY 1
        ORDER BY 1
        """,
        [AMS_LUNCH["city"], AMS_LUNCH["meal_period"], chart_start, CAMPAIGN_END],
    ).fetchall()
    daily = []
    for r in daily_rows:
        orders = float(r[1] or 0)
        revenue = float(r[2] or 0)
        profit = float(r[3] or 0)
        daily.append(
            {
                "day": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                "orders": orders,
                "revenue": revenue,
                "profit": profit,
                "new_customers": float(r[4] or 0),
                "promo_orders": float(r[5] or 0),
                "margin_pct": (100.0 * profit / revenue) if revenue else 0.0,
            }
        )
    campaign_day = CAMPAIGN_START.date().isoformat()
    return campaign_day, daily, impact_summary


# ── Marketing dashboard (all green) ────────────────────────────────────────


@app.cell(hide_code=True)
def _(alt, campaign_day, daily, impact_summary, kicker, mo, money, pct):
    def _mkt_line(field: str, title: str, color: str):
        base = alt.Chart(alt.Data(values=daily)).encode(
            x=alt.X("day:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0)),
        )
        line = base.mark_line(color=color, strokeWidth=2.4).encode(
            y=alt.Y(f"{field}:Q", title=title, axis=alt.Axis(grid=True, tickCount=4)),
            tooltip=[
                alt.Tooltip("day:T", title="Day", format="%b %d"),
                alt.Tooltip(f"{field}:Q", title=title, format=",.0f"),
            ],
        )
        rule = (
            alt.Chart(alt.Data(values=[{"day": campaign_day}]))
            .mark_rule(color="#a98332", strokeDash=[5, 4], strokeWidth=1.6)
            .encode(x="day:T")
        )
        return (
            (line + rule)
            .properties(height=200, width="container")
            .configure_view(strokeWidth=0)
            .configure_axis(
                labelColor="#4a4238",
                titleColor="#4a4238",
                gridColor="#e8e2d6",
                domainColor="#d8d0c3",
            )
        )

    mkt_orders = _mkt_line("orders", "Orders", "#82967b")
    mkt_new = _mkt_line("new_customers", "New customer orders", "#6f8f78")

    mo.vstack(
        [
            kicker("Marketing", "The success dashboard"),
            mo.Html(
                """
                <div class="promo-ad">
                  <div class="promo-badge">€5<span>OFF</span></div>
                  <div>
                    <div class="promo-title">Great Lunch</div>
                    <div class="promo-terms">
                      on every lunch order over <b>€20</b>
                    </div>
                    <div class="promo-fine">
                      Amsterdam · Fri–Sun lunch 11:30–14:00 · applied
                      automatically at checkout · no code needed
                    </div>
                  </div>
                </div>
                """
            ),
            mo.Html(
                """
                <div class="dash-shell">
                  <div class="dash-topbar dash-topbar-ok">
                    <div class="dash-product">PyPizza · Growth & Campaigns</div>
                    <div class="dash-title">Campaign performance</div>
                  </div>
                </div>
                """
            ),
            mo.hstack(
                [
                    mo.stat(
                        value=pct(impact_summary["pct_change"]["orders"]),
                        label="Orders",
                        caption=f"{impact_summary['actual']['orders']:,.0f} orders",
                        bordered=True,
                        direction="increase",
                        target_direction="increase",
                    ),
                    mo.stat(
                        value=pct(impact_summary["pct_change"]["revenue"]),
                        label="Revenue",
                        caption=money(impact_summary["actual"]["revenue"]),
                        bordered=True,
                        direction="increase",
                        target_direction="increase",
                    ),
                    mo.stat(
                        value=pct(impact_summary["pct_change"]["new_customer_orders"]),
                        label="New Customers",
                        caption=(
                            f"{impact_summary['actual']['new_customer_orders']:,.0f} "
                            "new orders"
                        ),
                        bordered=True,
                        direction="increase",
                        target_direction="increase",
                    ),
                    mo.stat(
                        value=pct(impact_summary["pct_change"]["promo_orders"]),
                        label="Promo Orders",
                        caption=(
                            f"{impact_summary['actual']['promo_orders']:,.0f} redemptions"
                        ),
                        bordered=True,
                        direction="increase",
                        target_direction="increase",
                    ),
                ],
                widths="equal",
                gap=0.55,
            ),
            mo.hstack(
                [
                    mo.vstack(
                        [
                            mo.Html('<div class="dash-panel-label">Daily orders</div>'),
                            mo.ui.altair_chart(mkt_orders, chart_selection=False),
                        ],
                        gap=0.2,
                    ),
                    mo.vstack(
                        [
                            mo.Html(
                                '<div class="dash-panel-label">'
                                "Daily new customer orders</div>"
                            ),
                            mo.ui.altair_chart(mkt_new, chart_selection=False),
                        ],
                        gap=0.2,
                    ),
                ],
                widths="equal",
                gap=0.7,
            ),
            mo.md("**Great Lunch is a win.** More orders. More revenue. More new customers."),
            #mo.md(
            #    "From Marketing's dashboard, the campaign looks unambiguously successful."
            #).callout(kind="success"),
        ],
        gap=0.65,
    )
    return


# ── Culprit 1: Finance dashboard ───────────────────────────────────────────


@app.cell(hide_code=True)
def _(alt, campaign_day, daily, impact_summary, kicker, mo, money, pct):
    def _fin_line(field: str, title: str, color: str, fmt: str = ",.0f"):
        base = alt.Chart(alt.Data(values=daily)).encode(
            x=alt.X("day:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0)),
        )
        line = base.mark_line(color=color, strokeWidth=2.4).encode(
            y=alt.Y(f"{field}:Q", title=title, axis=alt.Axis(grid=True, tickCount=4)),
            tooltip=[
                alt.Tooltip("day:T", title="Day", format="%b %d"),
                alt.Tooltip(f"{field}:Q", title=title, format=fmt),
            ],
        )
        rule = (
            alt.Chart(alt.Data(values=[{"day": campaign_day}]))
            .mark_rule(color="#a98332", strokeDash=[5, 4], strokeWidth=1.6)
            .encode(x="day:T")
        )
        return (
            (line + rule)
            .properties(height=200, width="container")
            .configure_view(strokeWidth=0)
            .configure_axis(
                labelColor="#4a4238",
                titleColor="#4a4238",
                gridColor="#e8e2d6",
                domainColor="#d8d0c3",
            )
        )

    fin_profit = _fin_line("profit", "Weekend profit (€)", "#a96755")
    fin_margin = _fin_line("margin_pct", "Profit margin (%)", "#b07868", ",.1f")

    mo.vstack(
        [
            kicker("Culprit 1", "The finance dashboard"),
            mo.Html(
                """
                <div class="dash-shell">
                  <div class="dash-topbar dash-topbar-bad">
                    <div class="dash-product">PyPizza · Finance Control</div>
                    <div class="dash-title">Weekend profitability</div>
                  </div>
                </div>
                """
            ),
            mo.hstack(
                [
                    mo.stat(
                        value=pct(impact_summary["pct_change"]["weekend_profit"]),
                        label="Weekend Profit",
                        caption=money(impact_summary["actual"]["weekend_profit"]),
                        bordered=True,
                        direction="decrease",
                        target_direction="increase",
                    ),
                    mo.stat(
                        value=pct(impact_summary["profit_margin_pct"]),
                        label="Profit Margin",
                        caption=(
                            f"{impact_summary['profit_margin_actual'] * 100:.1f}% "
                            "margin"
                        ),
                        bordered=True,
                        direction="decrease",
                        target_direction="increase",
                    ),
                    mo.stat(
                        value=pct(impact_summary["average_order_value_pct"]),
                        label="Avg Order Value",
                        caption=money(impact_summary["average_order_value_actual"]),
                        bordered=True,
                        direction="decrease",
                        target_direction="increase",
                    ),
                    mo.stat(
                        value=pct(impact_summary["average_cost_per_order_pct"]),
                        label="Cost / Order",
                        caption=money(
                            impact_summary["average_cost_per_order_actual"]
                        ),
                        bordered=True,
                        direction="increase",
                        target_direction="decrease",
                    ),
                ],
                widths="equal",
                gap=0.55,
            ),
            mo.hstack(
                [
                    mo.vstack(
                        [
                            mo.Html(
                                '<div class="dash-panel-label">Daily weekend profit</div>'
                            ),
                            mo.ui.altair_chart(fin_profit, chart_selection=False),
                        ],
                        gap=0.2,
                    ),
                    mo.vstack(
                        [
                            mo.Html(
                                '<div class="dash-panel-label">Daily profit margin</div>'
                            ),
                            mo.ui.altair_chart(fin_margin, chart_selection=False),
                        ],
                        gap=0.2,
                    ),
                ],
                widths="equal",
                gap=0.7,
            ),
            mo.md(
                "**Finance sees profit falling.** No campaign tag. No promo flag. "
                "No Marketing context."
            ),
            #mo.md(
            #    "Two dashboards. Same weekend. Marketing celebrates. Finance panics. "
            #    "Neither can explain the other."
            #).callout(kind="warn"),
        ],
        gap=0.65,
    )
    return


# ── Interlude: What is a metric? ───────────────────────────────────────────


@app.cell(hide_code=True)
def _(mo):
    mo.Html(
        """
        <div style="
          min-height: 58vh;
          display: flex;
          align-items: center;
          justify-content: center;
          text-align: center;
        ">
          <h1 style="
            margin: 0;
            font-size: 3.2rem;
            letter-spacing: -0.03em;
            color: var(--espresso, #201811);
          ">What is a metric?</h1>
        </div>
        """
    )
    return


# ── Culprit 2a: Traditional KPI ────────────────────────────────────────────


@app.cell(hide_code=True)
def _(kicker, mo):
    mo.vstack(
        [
            kicker("Culprit 2", "The traditional KPI"),
            mo.md("The naive metric:"),
            mo.Html(
                '<pre class="formula-line"><code>'
                "profit_margin = profit / revenue"
                "</code></pre>"
            ),
            mo.md(
                "Commonly refered to as a 'measure'. And often living directly in the BI tool of choice."
            ),
            mo.md(
                "No owner. No dependencies. No detector. No notion of *good or bad*."
            ),
        ],
        gap=0.65,
    )
    return


# ── Culprit 2b: Editable semantic KPIDefinition ────────────────────────────


@app.cell
def _(catalog, mo):
    # Stable UI element. keep this cell separate so edits aren't wiped.
    _pm = catalog["profit_margin"]
    _dims = ",\n        ".join(f'"{d}"' for d in _pm.dimensions)
    _formula = _pm.formula
    if _formula.kind == "ratio":
        _formula_src = (
            "Formula(\n"
            f'        kind="{_formula.kind}",\n'
            f"        numerator={_formula.numerator!r},\n"
            f"        denominator={_formula.denominator!r},\n"
            "    )"
        )
    elif _formula.kind == "sum":
        _formula_src = (
            "Formula(\n"
            f'        kind="{_formula.kind}",\n'
            f"        measure={_formula.measure!r},\n"
            "    )"
        )
    else:
        _formula_src = (
            "Formula(\n"
            f'        kind="{_formula.kind}",\n'
            f"        left={_formula.left!r},\n"
            f"        right={_formula.right!r},\n"
            "    )"
        )
    _support = _pm.support
    _support_src = (
        "None"
        if _support is None
        else (
            "SupportRequirement(\n"
            f"        measure={_support.measure!r},\n"
            f"        minimum={_support.minimum},\n"
            "    )"
        )
    )
    kpi_definition_source = f'''metric = KPIDefinition(
    name="{_pm.name}",
    label="{_pm.label}",
    description="{_pm.description}",
    owner="{_pm.owner}",
    formula={_formula_src},
    dimensions=(
        {_dims},
    ),
    dependencies={_pm.dependencies!r},
    directionality=Directionality.{_pm.directionality.name},
    detector=DetectorConfig(
        baseline_weeks={_pm.detector.baseline_weeks},
        z_threshold={_pm.detector.z_threshold},
        min_relative_change={_pm.detector.min_relative_change},
    ),
    support={_support_src},
    impact=ImpactModel(kind="{_pm.impact.kind}"),
    unit="{_pm.unit}",
    graph_ring={_pm.graph_ring},
    graph_side="{_pm.graph_side}",
)
'''
    kpi_editor = mo.ui.code_editor(
        value=kpi_definition_source,
        language="python",
        min_height=340,
        max_height=520,
        debounce=700,
        label="KPIDefinition (edit → re-runs the detector)",
    )
    return (kpi_editor,)


@app.cell(hide_code=True)
def _(
    AMS_LUNCH,
    CAMPAIGN_END,
    CAMPAIGN_START,
    DetectorConfig,
    Directionality,
    Formula,
    ImpactModel,
    KPIDefinition,
    KPIEngine,
    Measure,
    SupportRequirement,
    engine,
    kpi_editor,
    kicker,
    metric_fmt,
    mo,
    pct,
):
    _ns = {
        "KPIDefinition": KPIDefinition,
        "Formula": Formula,
        "Measure": Measure,
        "Directionality": Directionality,
        "DetectorConfig": DetectorConfig,
        "SupportRequirement": SupportRequirement,
        "ImpactModel": ImpactModel,
    }
    _error = None
    _status = None
    _metric = None
    try:
        exec(kpi_editor.value, _ns, _ns)
        _metric = _ns.get("metric")
        if not isinstance(_metric, KPIDefinition):
            raise TypeError(
                "Assign a KPIDefinition to `metric = KPIDefinition(...)`."
            )
        _live_catalog = dict(engine.catalog)
        _live_catalog[_metric.name] = _metric
        _live = KPIEngine(engine.con, _live_catalog)
        _status = _live.evaluate_window(
            _metric.name,
            CAMPAIGN_START,
            CAMPAIGN_END,
            AMS_LUNCH,
            baseline_weeks=_metric.detector.baseline_weeks,
        )
    except Exception as exc:  # noqa: BLE001. show edit errors on the slide
        _error = exc

    if _error is not None:
        _result = mo.md(f"**Can't evaluate yet:** `{_error}`").callout(
            kind="warn"
        )
    else:
        assert _status is not None and _metric is not None
        _kind = "danger" if _status.anomaly else "success"
        _flag = "ANOMALY" if _status.anomaly else "within range"
        _result = mo.callout(
            mo.md(
                f"**Live detector · {_metric.name} · {_flag}**\n\n"
                f"value **{metric_fmt(_status.value, _metric.unit)}**"
                f" · expected **{metric_fmt(_status.baseline_mean, _metric.unit)}**"
                f" · {pct(_status.relative_change)}\n\n"
                f"z **{_status.z_score:+.2f}**"
                f" · threshold **{_metric.detector.z_threshold}**"
                f" · min Δ **{_metric.detector.min_relative_change:.0%}**\n\n"
                f"support **{_status.support:,.0f}**"
                f" (ok={_status.support_ok}"
                + (
                    f", min={_metric.support.minimum}"
                    if _metric.support
                    else ""
                )
                + f") · directionality `{_metric.directionality.value}`"
            ),
            kind=_kind,
        )

    mo.vstack(
        [
            kicker("Culprit 2", "A semantic KPI object"),
            mo.md(
                "Same arithmetic but now as a typed `KPIDefinition` object. "
                "Change the formula, detector thresholds, or support and watch "
                "the window re-evaluate."
            ),
            mo.hstack(
                [kpi_editor, _result],
                widths=[1.65, 1],
                gap=0.85,
                align="stretch",
            ),
            # mo.md(
            #    "**A metric without context isn't intelligence. It's arithmetic.**"
            #).callout(kind="info"),
        ],
        gap=0.55,
    )
    return


# ── 3. Detection. WHEN to alert ───────────────────────────────────────────


@app.cell
def _(
    AMS_LUNCH,
    CAMPAIGN_END,
    CAMPAIGN_START,
    TALK_GRAPH_METRICS,
    engine,
    timedelta,
):
    # Observations are read once. Detection is pure Python over these numbers,
    # so editing the detector re-runs instantly without touching DuckDB.
    MAX_BASELINE_WEEKS = 8
    observations = {}
    for _name in TALK_GRAPH_METRICS:
        _metric_def = engine.catalog[_name]
        _current = engine.metric_value(
            _name, filters=AMS_LUNCH, start=CAMPAIGN_START, end=CAMPAIGN_END
        )
        _baseline = [
            engine.metric_value(
                _name,
                filters=AMS_LUNCH,
                start=CAMPAIGN_START - timedelta(weeks=_i),
                end=CAMPAIGN_END - timedelta(weeks=_i),
            )
            for _i in range(1, MAX_BASELINE_WEEKS + 1)
        ]
        _support = engine.support_value(
            _name, filters=AMS_LUNCH, start=CAMPAIGN_START, end=CAMPAIGN_END
        )
        _support_ok = (
            _metric_def.support is None
            or _support >= _metric_def.support.minimum
        )
        observations[_name] = {
            "definition": _metric_def,
            "current": _current,
            "baseline": _baseline,
            "support": _support,
            "support_ok": _support_ok,
        }
    return (observations,)


@app.cell
def _(mo, observations):
    # Stable UI element. keep separate so edits aren't wiped on re-run.
    _obs = observations["profit_margin"]
    _expected = sum(_obs["baseline"][:6]) / 6
    _cfg = _obs["definition"].detector
    detector_source = f'''# Same observations. Swap the strategy (and/or the thresholds).
detector = SeasonalZScoreDetector()
config = DetectorConfig(
    baseline_weeks={_cfg.baseline_weeks},
    z_threshold={_cfg.z_threshold},
    min_relative_change={_cfg.min_relative_change},
)

# Try this instead: comment out the block above:
# detector = ThresholdDetector()
# config = DetectorConfig(
#     min_relative_change=0.05,
#     absolute_threshold={round(_expected * 0.95, 4)},  # 95% of expected margin
# )
'''
    detector_editor = mo.ui.code_editor(
        value=detector_source,
        language="python",
        min_height=300,
        max_height=440,
        debounce=700,
        label="DetectorStrategy: edit to re-detect every PyPizza KPI",
    )
    return (detector_editor,)


@app.cell(hide_code=True)
def _(
    CAMPAIGN_END,
    CAMPAIGN_START,
    DetectorConfig,
    SeasonalZScoreDetector,
    ThresholdDetector,
    detector_editor,
    kicker,
    mo,
    observations,
    pct,
):
    _ns = {
        "SeasonalZScoreDetector": SeasonalZScoreDetector,
        "ThresholdDetector": ThresholdDetector,
        "DetectorConfig": DetectorConfig,
    }
    _error = None
    _verdicts = []
    _detector = None
    try:
        exec(detector_editor.value, _ns, _ns)
        _detector = _ns.get("detector")
        _config = _ns.get("config")
        if _detector is None or _config is None:
            raise TypeError(
                "Define both `detector = ...` and `config = DetectorConfig(...)`."
            )
        if not isinstance(_config, DetectorConfig):
            raise TypeError("`config` must be a DetectorConfig.")
        _weeks = max(1, min(int(_config.baseline_weeks), 8))
        _as_of = (
            f"{CAMPAIGN_START.isoformat(sep=' ')} → "
            f"{CAMPAIGN_END.isoformat(sep=' ')}"
        )
        for _name, _obs in observations.items():
            _definition = _obs["definition"]
            _verdicts.append(
                (
                    _definition,
                    _detector.evaluate(
                        _name,
                        _obs["current"],
                        _obs["baseline"][:_weeks],
                        directionality=_definition.directionality,
                        config=_config,
                        support=_obs["support"],
                        support_ok=_obs["support_ok"],
                        as_of=_as_of,
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001. surface edit errors on the slide
        _error = exc

    if _error is not None:
        _result = mo.md(f"**Can't detect yet:** `{_error}`").callout(kind="warn")
    else:
        _verdicts.sort(key=lambda pair: (not pair[1].anomaly, pair[0].label))
        _flagged = sum(1 for _, s in _verdicts if s.anomaly)
        _chips = []
        for _definition, _status in _verdicts:
            _tone = "#a96755" if _status.anomaly else "#82967b"
            _chips.append(
                f'<div style="display:flex;justify-content:space-between;'
                f"gap:0.6rem;padding:0.26rem 0.5rem;border-left:3px solid {_tone};"
                f'background:#fcfaf6;border-radius:5px;font-size:0.78rem;">'
                f"<span>{_definition.label}</span>"
                f'<span class="muted">z {_status.z_score:+.1f}'
                f" · {pct(_status.relative_change)}</span></div>"
            )
        _result = mo.vstack(
            [
                mo.Html(
                    f'<div class="panel panel-accent">'
                    f'<div class="act-kicker">'
                    f"{type(_detector).__name__}</div>"
                    f'<div class="arch-q">{_flagged} of {len(_verdicts)} '
                    f"KPIs flagged</div></div>"
                ),
                mo.Html(
                    '<div style="display:flex;flex-direction:column;gap:0.2rem;">'
                    + "".join(_chips)
                    + "</div>"
                ),
            ],
            gap=0.35,
        )

    mo.vstack(
        [
            kicker("3 · Detection", "Same weekend, different detector"),
            mo.md(
                "Change only the **policy** that judges them. Raise `z_threshold`, or switch to an absolute "
                "floor, and watch PyPizza's whole board recolour."
                "Detection shouldnt just be based on <=/>=, it should be customizable to the business needs."
            ),
            mo.hstack(
                [detector_editor, _result],
                widths=[1.35, 1],
                gap=0.85,
                align="stretch",
            ),
        ],
        gap=0.55,
    )
    return


# ── 4. Dependencies. WHAT to alert on ─────────────────────────────────────


@app.cell(hide_code=True)
def _(
    AMS_LUNCH,
    CAMPAIGN_END,
    CAMPAIGN_START,
    build_talk_graph,
    catalog,
    code_block,
    engine,
    evaluate_graph_state_window,
    find_explanatory_paths,
    kicker,
    mark_root_candidates,
    mo,
    why_panel,
):
    _states = mark_root_candidates(
        catalog,
        evaluate_graph_state_window(
            engine,
            build_talk_graph(catalog),
            CAMPAIGN_START,
            CAMPAIGN_END,
            AMS_LUNCH,
        ),
    )
    _paths = find_explanatory_paths(catalog, _states, start="weekend_profit")

    def _row(path: list[str]) -> str:
        # Good news: everything below the start moved up and none can be paged on.
        good = all(
            catalog[n].directionality.value == "two_sided"
            and _states[n].relative_change > 0
            for n in path[1:]
        )
        last = len(path) - 1
        chain = '<span class="path-arrow">→</span>'.join(
            f'<span class="path-pill{" path-pill-leaf" if i == last else ""}">'
            f"{n}</span>"
            for i, n in enumerate(path)
        )
        tag = '<span class="path-tag">good news</span>' if good else ""
        return f'<li class="{"path-good" if good else ""}">{chain}{tag}</li>'

    _found = mo.Html(
        '<div class="panel">'
        '<div class="act-kicker">Live output · Great Lunch weekend</div>'
        f'<ol class="path-list">{"".join(_row(p) for p in _paths)}</ol>'
        "</div>"
    )

    mo.vstack(
        [
            kicker("4 · Dependencies", "A KPI knows what it depends on"),
            mo.md(
                "Detection tells us *which* metrics are unusual. "
                "But every anomaly has a story upstream, so don't report "
                "every red number. **Walk the dependencies until the "
                "anomalies stop.** That last node is the one worth a message."
            ),
            mo.hstack(
                [
                    mo.accordion(
                        {
                            "Show the code · <code>find_explanatory_paths()</code>":
                            code_block(find_explanatory_paths)
                        }
                    ),
                    mo.vstack(
                        [
                            why_panel(
                                "WHAT to alert on",
                                "The walk stops at a metric that is anomalous "
                                "while none of its own dependencies are. "
                                "Plain recursion over a typed catalog: ancestors "
                                "become <i>supporting evidence</i>. "
                                "The graph becomes the explanation."
                            ),
                            _found,
                        ],
                        gap=0.5,
                    ),
                ],
                widths=[2, 3],
                gap=0.85,
                align="start",
            ),
        ],
        gap=0.55,
    )
    return


# ── 4. Network ─────────────────────────────────────────────────────────────


@app.cell(hide_code=True)
def _(
    AMS_LUNCH,
    CAMPAIGN_END,
    CAMPAIGN_START,
    PLOTLY_DISPLAY_CONFIG,
    build_plotly_network,
    build_talk_graph,
    catalog,
    engine,
    evaluate_graph_state_window,
    investigate_window,
    kicker,
    mark_root_candidates,
    mo,
    preferred_explanatory_path,
):
    graph = build_talk_graph(catalog)
    statuses = evaluate_graph_state_window(
        engine, graph, CAMPAIGN_START, CAMPAIGN_END, AMS_LUNCH
    )
    statuses = mark_root_candidates(catalog, statuses)
    inv = investigate_window(
        engine, "weekend_profit", CAMPAIGN_START, CAMPAIGN_END, AMS_LUNCH
    )
    full_path = preferred_explanatory_path(inv)

    fig = build_plotly_network(
        graph,
        statuses,
        center_metric="weekend_profit",
        emphasize_path=full_path,
        max_depth=3,
        mute_outside_path=True,
    )
    _primary = inv.primary_explanatory
    mo.vstack(
        [
            kicker("4 · Network", "Metrics form a business network"),
            mo.md(
                "Because of the dependencies, a more hollistic business view is established"
            ),
            mo.ui.plotly(fig, config=PLOTLY_DISPLAY_CONFIG),
        ],
        gap=0.65,
    )
    return (inv,)


# ── 5. Business Resolution ─────────────────────────────────────────────────


@app.cell(hide_code=True)
def _(
    AMS_LUNCH,
    CAMPAIGN_END,
    CAMPAIGN_START,
    impact_summary,
    alt,
    check_data_quality,
    datetime,
    engine,
    inv,
    kicker,
    mo,
    money,
    open_smart_incident,
    pct,
):
    quality = check_data_quality(engine, datetime(2026, 5, 15, 12, 30))
    incident = open_smart_incident(
        engine,
        center_kpi="weekend_profit",
        scope={
            "city": "Amsterdam",
            "meal_period": "lunch",
        },
        start=datetime(2026, 5, 15, 11, 30),
        windows=12,
        persistence=2,
        min_impact_eur=40,
        quality=quality,
    )

    _aov_baseline = engine.evaluate_window(
        "average_order_value", CAMPAIGN_START, CAMPAIGN_END, AMS_LUNCH
    ).baseline_mean

    pre_start = datetime(2026, 5, 8, 11, 30)
    pre_end = datetime(2026, 5, 10, 13, 30)
    before = basket_distribution(engine, pre_start, pre_end, AMS_LUNCH)
    during = basket_distribution(
        engine,
        CAMPAIGN_START,
        CAMPAIGN_END,
        {**AMS_LUNCH, "campaign": "great_lunch"},
    )

    def _shares(dist, label):
        total = sum(r["orders"] for r in dist) or 1
        return [
            {
                "basket_band": r["basket_band"],
                "share": r["orders"] / total,
                "period": label,
            }
            for r in dist
        ]

    _rows = _shares(before, "Before") + _shares(during, "Great Lunch")
    _chart = (
        alt.Chart(alt.Data(values=_rows))
        .mark_bar()
        .encode(
            x=alt.X(
                "basket_band:N",
                sort=["<20", "20-24.99", "25-29.99", "30-39.99", "40+"],
                title="Basket band",
            ),
            y=alt.Y("share:Q", axis=alt.Axis(format="%"), title="Order share"),
            color="period:N",
            xOffset="period:N",
        )
        .properties(height=260)
    )

    mo.vstack(
        [
            kicker("5 · Business Resolution", "Don't alert Finance about profit"),
            mo.md(
                "Customers didn't abuse the promotion. "
                "**They understood it perfectly.**"
                f"The typical basket was €{_aov_baseline:,.2f}. A €20 gate sits way below it: every average order could shrink and still qualify. A €40 gate sits <b>above</b> it: the only way to qualify is to add an item."
            ),
            mo.Html(
                f"""
                <div class="promo-ad promo-ad-fix">
                  <div class="promo-badge">€5<span>OFF</span></div>
                  <div>
                    <div class="promo-title">What shipped · what it needed to be</div>
                    <div class="promo-terms">
                      on every lunch order over
                      <s>€20</s> <b class="promo-fix">€40</b>
                    </div>
                  </div>
                </div>
                """
            ),
            mo.md(
                f"Scorecard: orders {pct(impact_summary['pct_change']['orders'])} · "
                f"revenue {pct(impact_summary['pct_change']['revenue'])} · "
                f"profit {pct(impact_summary['pct_change']['weekend_profit'])} · "
                f"margin {pct(impact_summary['profit_margin_pct'])} · "
                f"AOV {pct(impact_summary['average_order_value_pct'])} · "
                f"threshold {pct(impact_summary['threshold_concentration_pct'])}"
            ),
            mo.md(
                "Nothing was technically broken. "
                "Everything worked exactly as designed.  \n"
                "Unfortunately, what we designed was a terrible business decision."
            ).callout(kind="danger"),
        ],
        gap=0.7,
    )
    return


# ── 6. State. HOW OFTEN to alert ──────────────────────────────────────────


@app.cell(hide_code=True)
def _(StatePolicy, code_block, evolve_state, kicker, mo, why_panel):
    mo.vstack(
        [
            kicker("6 · State", "A KPI remembers what it already told you"),
            mo.md(
                "A detector is stateless: it will happily report the same bad "
                "half-hour forty-eight times before lunch is over. "
                "**State is what turns a stream of observations into one "
                "conversation.**"
            ),
            mo.hstack(
                [
                    code_block(StatePolicy, evolve_state),
                    why_panel(
                        "HOW OFTEN to alert",
                        "Three gates stand between <i>unusual</i> and "
                        "<i>somebody's phone</i>.",
                        [
                            "<b>Persistence</b>: one weird window is weather, "
                            "two is climate.",
                            "<b>Impact</b>: if it isn't worth euros, "
                            "it isn't worth an interruption.",
                            "<b>Quality</b>: a broken pipeline is suppressed, "
                            "not escalated.",
                        ],
                    ),
                ],
                widths=[1.7, 1],
                gap=0.85,
                align="stretch",
            ),
            mo.md(
                "Thirty lines of state machine. **This is the part that decides "
                "whether anyone still reads your alerts in six months.**"
            ).callout(kind="info"),
        ],
        gap=0.55,
    )
    return


# ── 7. Alerts. what would actually have fired ─────────────────────────────


@app.cell(hide_code=True)
def _(
    AMS_LUNCH,
    CAMPAIGN_START,
    IncidentState,
    TALK_GRAPH_METRICS,
    catalog,
    check_data_quality,
    engine,
    kicker,
    mo,
    money,
    open_smart_incident,
    timedelta,
):
    # Naive: every anomalous talk-graph KPI at every lunch half-hour pages somebody.
    _naive = []
    for _day in range(3):
        for _slot in range(5):
            _at = CAMPAIGN_START + timedelta(days=_day, minutes=30 * _slot)
            for _name in TALK_GRAPH_METRICS:
                _defn = catalog[_name]
                _st = engine.evaluate(_name, _at, AMS_LUNCH)
                if _st.anomaly and _st.support_ok:
                    _naive.append((_at, _name, _defn.owner, _st.relative_change))

    # Smart: grow the window from Friday 11:30 and record state transitions.
    _timeline = []
    _opened = None
    for _day in range(3):
        for _slot in range(5):
            _at = CAMPAIGN_START + timedelta(days=_day, minutes=30 * _slot)
            _windows = 1 + _day * 5 + _slot
            _q = check_data_quality(engine, _at)
            _inc = open_smart_incident(
                engine,
                center_kpi="weekend_profit",
                scope=AMS_LUNCH,
                start=CAMPAIGN_START,
                windows=_windows,
                persistence=2,
                min_impact_eur=40,
                quality=_q,
            )
            _timeline.append((_at, _inc))
            if (
                _opened is None
                and _inc is not None
                and _inc.state is IncidentState.OPEN
            ):
                _opened = (_at, _inc)

    def _tl_row(at, inc, *, opened_at):
        if inc is None:
            kind, title, detail = (
                "quiet",
                "Quiet",
                "Center KPI recovered for this window: consecutive streak resets.",
            )
        elif inc.state is IncidentState.OPEN and at == opened_at:
            kind, title, detail = (
                "open",
                "OPEN · page sent",
                f"<b>{inc.owner}</b> · "
                f"<code>{inc.explanatory_kpi}</code> · "
                f"{money(inc.impact_eur)} · "
                f"suppresses {', '.join(inc.suppressed_ancestors)}",
            )
        elif inc.state is IncidentState.OPEN:
            kind, title, detail = (
                "held",
                "Already open · no re-page",
                "Same incident stays open. State keeps the phone quiet.",
            )
        else:
            kind, title, detail = (
                "watch",
                "DETECTED · watching",
                f"Persistence not met yet · leaning toward "
                f"<code>{inc.explanatory_kpi}</code>",
            )
        return (
            f'<div class="tl-row tl-{kind}">'
            f'<div class="tl-time">{at:%a %H:%M}</div>'
            f'<div class="tl-dot"></div>'
            f'<div class="tl-body">'
            f'<div class="tl-title">{title}</div>'
            f'<div class="tl-detail">{detail}</div>'
            f"</div></div>"
        )

    # Friday in full; then one Saturday + Sunday summary row.
    _rows_html = []
    for _at, _inc in _timeline:
        if _at.date() == CAMPAIGN_START.date():
            _rows_html.append(
                _tl_row(_at, _inc, opened_at=_opened[0] if _opened else None)
            )
    if _opened is not None:
        _rows_html.append(
            '<div class="tl-row tl-held">'
            '<div class="tl-time">Sat–Sun</div>'
            '<div class="tl-dot"></div>'
            '<div class="tl-body">'
            '<div class="tl-title">Already open · no re-page</div>'
            '<div class="tl-detail">'
            "Saturday and Sunday lunch stay anomalous. The system does not "
            "send eight more pages about the same cliff."
            "</div></div></div>"
        )

    # A short sample of the naive flood at the first half-hour.
    _sample = "".join(
        f'<div class="tl-naive-item">'
        f'<span class="tl-naive-kpi">{name}</span>'
        f'<span class="tl-naive-owner">{owner}</span>'
        f"</div>"
        for _at, name, owner, _ in _naive
        if _at == CAMPAIGN_START
    )

    mo.vstack(
        [
            kicker("7 · Alerts", "What would actually have fired"),
            mo.md(
                "Same warehouse. Same half-hours. Two policies."
            ),
            mo.hstack(
                [
                    mo.Html(
                        f"""
                        <div class="panel panel-accent" style="height:100%;">
                          <div class="act-kicker">Naive · every anomaly alert</div>
                          <div class="arch-q">{len(_naive)} alerts</div>
                          <div class="arch-note">
                            Three lunch services · every anomalous KPI ·
                            every half-hour · no persistence · no graph.
                          </div>
                          <div class="tl-naive-list">
                            <div class="tl-naive-label">
                              Friday 11:30 alone: {sum(1 for a,_,_,_ in _naive if a == CAMPAIGN_START)} pages
                            </div>
                            {_sample}
                          </div>
                        </div>
                        """
                    ),
                    mo.Html(
                        f"""
                        <div class="panel panel-ok" style="height:100%;">
                          <div class="act-kicker">Smart · detector + state + graph</div>
                          <div class="arch-q">1 alert</div>
                          <div class="arch-note">
                            {_opened[0]:%A %H:%M} · {_opened[1].owner} ·
                            <code>{_opened[1].explanatory_kpi}</code>
                          </div>
                          <div class="tl-rail">
                            {''.join(_rows_html)}
                          </div>
                        </div>
                        """
                    ),
                ],
                widths=[1, 1.35],
                gap=0.7,
                align="stretch",
            ),
            mo.md(
                f"**{len(_naive)} pages across "
                f"{len({owner for _, _, owner, _ in _naive})} owners, "
                f"or one page to Promotions.**  \n"
                "That is the whole talk, measured in interruptions."
            ).callout(kind="warn"),
        ],
        gap=0.55,
    )
    return


# ── 8. Architecture. how it composes ──────────────────────────────────────


@app.cell(hide_code=True)
def _(kicker, mo):
    _stages = [
        (
            "Warehouse",
            "DuckDB",
            "Numbers",
            "Half-hourly facts. Boring on purpose.",
        ),
        (
            "Semantics",
            "Pydantic · KPIDefinition",
            "WHAT a number means",
            "Owner, unit, formula, direction: typed, not tribal.",
        ),
        (
            "Graph",
            "NetworkX traversal",
            "WHY it moved",
            "Walk dependencies to the deepest anomaly.",
        ),
        (
            "Detector",
            "DetectorStrategy",
            "WHEN it's unusual",
            "Swappable. Never load-bearing.",
        ),
        (
            "State",
            "evolve_state",
            "HOW OFTEN to speak",
            "Persistence · impact · data quality.",
        ),
    ]
    _cards = []
    for _layer, _artifact, _question, _note in _stages:
        _cards.append(
            f'<div class="arch-stage">'
            f'<div class="arch-layer">{_layer}</div>'
            f'<div class="arch-artifact">{_artifact}</div>'
            f'<div class="arch-q">{_question}</div>'
            f'<div class="arch-note">{_note}</div>'
            f"</div>"
        )
    _flow = '<div class="arch-arrow">→</div>'.join(_cards)

    mo.vstack(
        [
            kicker("8 · Architecture", "Five small boxes, one honest alert"),
            mo.Html(f'<div class="arch-flow">{_flow}</div>'),
            mo.Html(
                """
                <div class="panel panel-accent" style="margin-top:0.55rem;">
                  <div class="act-kicker">Output</div>
                  <div class="arch-q">
                    One incident → the owner of the metric that explains it
                  </div>
                  <p class="muted" style="margin:0.4rem 0 0 0;">
                    Ancestors travel along as supporting evidence instead of
                    becoming four separate pages to four separate teams.
                  </p>
                </div>
                """
            ),
            mo.md(
                "Every layer is independently testable, independently "
                "replaceable, and small enough to read on a Friday.  \n"
                "**No model was trained. No prompt was written.**"
            ).callout(kind="info"),
        ],
        gap=0.5,
    )
    return


# ── Closing ────────────────────────────────────────────────────────────────


@app.cell(hide_code=True)
def _(
    AMS_LUNCH,
    CAMPAIGN_START,
    IncidentState,
    check_data_quality,
    datetime,
    engine,
    kicker,
    mo,
    money,
    open_smart_incident,
    timedelta,
):
    _audiences = [
        (
            "People",
            "Onboarding instead of folklore",
            "A new analyst opens <code>KPIDefinition</code> and learns the "
            "owner, the unit, the direction and what <i>bad</i> means. "
            "without booking time with the person who left in March.",
        ),
        (
            "Software",
            "One definition, every consumer",
            "Detectors, state machines, alerting and the graph all read the "
            "same typed objects. Nobody re-implements <i>margin</i> for the "
            "fifth time in the fifth service.",
        ),
        (
            "Agents",
            "Grounded tools, not invented SQL",
            "An agent doesn't have to guess your business from column names. "
            "It calls <code>investigate(\"weekend_profit\")</code> and gets an "
            "auditable path back: an answer you can check.",
        ),
    ]
    _cards = "".join(
        f'<div class="panel" style="flex:1;">'
        f'<div class="act-kicker">{_who}</div>'
        f'<div class="arch-q">{_headline}</div>'
        f'<div class="arch-note">{_body}</div>'
        f"</div>"
        for _who, _headline, _body in _audiences
    )

    # Counterfactual. same OPEN moment as the Alerts timeline.
    _opened = None
    for _day in range(3):
        for _slot in range(5):
            _at = CAMPAIGN_START + timedelta(days=_day, minutes=30 * _slot)
            _windows = 1 + _day * 5 + _slot
            _q = check_data_quality(engine, _at)
            _inc = open_smart_incident(
                engine,
                center_kpi="weekend_profit",
                scope=AMS_LUNCH,
                start=CAMPAIGN_START,
                windows=_windows,
                persistence=2,
                min_impact_eur=40,
                quality=_q,
            )
            if (
                _opened is None
                and _inc is not None
                and _inc.state is IncidentState.OPEN
            ):
                _opened = _at
                break
        if _opened is not None:
            break
    _alert_at = _opened

    _days = []
    for _day in range(3):
        _from = CAMPAIGN_START + timedelta(days=_day)
        _to = _from + timedelta(hours=2)
        _actual = engine.metric_value(
            "weekend_profit", filters=AMS_LUNCH, start=_from, end=_to
        )
        _expected = sum(
            engine.metric_value(
                "weekend_profit",
                filters=AMS_LUNCH,
                start=_from - timedelta(weeks=_w),
                end=_to - timedelta(weeks=_w),
            )
            for _w in range(1, 7)
        ) / 6
        _days.append((_from, _actual - _expected))

    _total = sum(_delta for _, _delta in _days)
    _avoidable = sum(
        _delta for _when, _delta in _days if _when.date() > _alert_at.date()
    )
    _found_out = datetime(2026, 5, 18, 9, 12)
    _gap = _found_out - _alert_at

    _cells = "".join(
        f'<div class="verdict-day '
        f'{"verdict-day-lost" if _when.date() <= _alert_at.date() else "verdict-day-saved"}">'
        f'<div class="verdict-dow">{_when:%a %d %b}</div>'
        f'<div class="verdict-amt">−{money(abs(_delta))}</div>'
        f'<div class="verdict-tag">'
        + (
            f"alert fires {_alert_at:%H:%M}"
            if _when.date() == _alert_at.date()
            else "never happens"
        )
        + "</div></div>"
        for _when, _delta in _days
    )

    mo.vstack(
        [
            kicker("Close", "Caught on Friday, before the weekend"),
            mo.md(
                "Great Lunch ran three lunch services. The basket mix broke "
                f"in the first one. Promotions is paged at "
                f"**{_alert_at:%A %H:%M}**, with Saturday and Sunday still "
                "ahead. Same warehouse. Same numbers. Same Monday."
            ),
            mo.Html(f'<div class="verdict-strip">{_cells}</div>'),
            mo.Html(
                f"""
                <div style="display:flex;gap:0.6rem;align-items:stretch;">
                  <div class="panel panel-accent" style="flex:1;">
                    <div class="act-kicker">What happened</div>
                    <div class="arch-q">
                      Coffee machine, {_found_out:%A %d %B %H:%M}
                    </div>
                    <div class="arch-note">
                      {_gap.days} days and {_gap.seconds // 3600} hours late.
                      All three services had run;
                      <b>{money(abs(_total))}</b> of lunch profit gone.
                    </div>
                  </div>
                  <div class="panel panel-ok" style="flex:1;">
                    <div class="act-kicker">What this system does</div>
                    <div class="arch-q">Promotions paged Friday over lunch</div>
                    <div class="arch-note">
                      One Friday afternoon to move the threshold. Saturday
                      and Sunday never happen:
                      <b>{money(abs(_avoidable))}</b> of the
                      {money(abs(_total))} still on the table.
                    </div>
                  </div>
                </div>
                """
            ),
            mo.md(
                "Everything in this talk came from one decision: writing the "
                "business down as **typed Python**. That single artifact "
                "serves three very different readers."
            ),
            mo.Html(
                f'<div style="display:flex;gap:0.5rem;align-items:stretch;">'
                f"{_cards}</div>"
            ),
        ],
        gap=0.45,
    )
    return


# ── Quote beats ────────────────────────────────────────────────────────────


@app.cell(hide_code=True)
def _(mo):
    mo.Html(
        """
        <div class="quote-slide">
          <p class="quote-line">
            Don't make the LLM understand your business.
          </p>
          <p class="quote-line">
            Make your business understandable to people, to software,
            and to whatever we're all building next year.
          </p>
        </div>
        """
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.Html(
        """
        <div class="quote-slide">
          <p class="quote-line quote-line-hero">
            Your dashboard isn't bad.
          </p>
          <p class="quote-line quote-line-hero">
            It's just too late.
          </p>
        </div>
        """
    )
    return


if __name__ == "__main__":
    app.run()
