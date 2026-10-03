"""Presentation helpers for the evaluation console. No marimo imports."""

from __future__ import annotations

import html
from typing import Any

from examples.metric_judgment_eval.evaluation import score_label
from examples.metric_judgment_eval.models import (
    BenchmarkCase,
    GraphAnalysis,
    JudgmentRun,
    MetricStateGraphSnapshot,
    OperationalProposal,
    ProposalExpectation,
    RobustnessStat,
)
from examples.metric_judgment_eval.policy import (
    PolicyDecision,
    apply_comparison_policy,
    apply_policy,
)
from examples.metric_judgment_eval.providers import (
    ProviderAvailability,
    jev_confidence_map,
    jev_probabilities_map,
    min_jev_confidence,
    openai_token_usage,
)
from examples.metric_judgment_eval.snapshot import ACTIVE_STATES

STYLE = {
    "ink": "#1b2430",
    "muted": "#5c6b7a",
    "line": "#d8e0e8",
    "paper": "#ffffff",
    "accent": "#1f4e79",
    "good": "#2f6f4e",
    "bad": "#9b3a32",
    "warn": "#8a6d1f",
    "focal": "#1f4e79",
    "open": "#9b3a32",
    "detected": "#c56a1a",
    "normal": "#6b7c8a",
    "font": "IBM Plex Sans, Segoe UI, system-ui, sans-serif",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:,.1f}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1%}"


def _yn(value: bool | None) -> str:
    if value is None:
        return "—"
    return "YES" if value else "NO"


def _state_class(state: str) -> str:
    mapping = {
        "OPEN": "st-open",
        "DETECTED": "st-detected",
        "ACKNOWLEDGED": "st-detected",
        "NORMAL": "st-normal",
        "RESOLVED": "st-resolved",
        "NO_DATA": "st-missing",
        "SUPPRESSED": "st-missing",
    }
    return mapping.get(state, "st-normal")


def availability_html(availability: ProviderAvailability) -> str:
    jev = "configured ✓" if availability.jev_ready else "not configured"
    openai = "configured ✓" if availability.openai_ready else "not configured"
    extra = ""
    if not availability.pydantic_ai:
        extra = "<div class='status-fail'>pydantic-ai is not installed. Live mode needs the judgment-demo extra.</div>"
    return (
        f"{extra}"
        f"<div class='avail'>Jev {html.escape(jev)} · OpenAI {html.escape(openai)} · "
        f"models <code>{html.escape(availability.jev_model)}</code> / "
        f"<code>{html.escape(availability.openai_model)}</code></div>"
    )


def metric_table_html(snapshot: MetricStateGraphSnapshot) -> str:
    focals = set(snapshot.focal_metric_ids)
    rows = []
    for node in snapshot.metrics:
        mark = "focal" if node.metric_id in focals else ""
        rows.append(
            f"<tr class='{mark}'>"
            f"<td><code>{_esc(node.metric_id)}</code></td>"
            f"<td>{_esc(node.name or '')}</td>"
            f"<td><span class='pill {_state_class(node.state)}'>{_esc(node.state)}</span></td>"
            f"<td>{_esc(node.quality or '—')}</td>"
            f"<td class='num'>{_esc(_fmt_num(node.current_value))}</td>"
            f"<td class='num'>{_esc(_fmt_num(node.baseline_value))}</td>"
            f"<td class='num'>{_esc(_fmt_pct(node.relative_change))}</td>"
            f"<td>{_esc(node.unit or '')}</td>"
            "</tr>"
        )
    body = "".join(rows)
    return (
        "<table class='grid-table'><thead><tr>"
        "<th>id</th><th>name</th><th>state</th><th>quality</th>"
        "<th>value</th><th>baseline</th><th>rel Δ</th><th>unit</th>"
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


def payload_html(payload: str, digest: str, *, snapshot_hash: str | None = None) -> str:
    extra = (
        f"<div class='hash'>Snapshot SHA-256 <code>{_esc(snapshot_hash)}</code></div>"
        if snapshot_hash
        else ""
    )
    return (
        "<div class='payload-block'>"
        f"<div class='hash'>Context SHA-256 <code>{_esc(digest)}</code></div>"
        f"{extra}"
        f"<pre class='payload'>{_esc(payload)}</pre>"
        "</div>"
    )


def analysis_html(analysis: GraphAnalysis) -> str:
    candidates = ", ".join(item.metric_id for item in analysis.investigation_candidates) or "none"
    deepest = ", ".join(analysis.deepest_active_metric_ids) or "—"
    shared = ", ".join(analysis.shared_active_dependency_ids) or "—"
    missing = ", ".join(analysis.missing_evidence_metric_ids) or "none"
    direction = "known" if analysis.directional_support_known else "unknown"
    paths = "<br>".join(" → ".join(path.metric_ids) for path in analysis.active_paths) or "—"
    return (
        "<section class='baseline-panel'>"
        "<h3>Metric Runtime graph analysis</h3>"
        "<p class='tiny'>STRUCTURAL FACT — computed without a model. "
        "If Python can prove it from the graph, it is not a model judgment.</p>"
        f"<div class='kv'><span class='k'>Active direct branches</span><span class='v'>{analysis.active_branch_count}</span></div>"
        f"<div class='kv'><span class='k'>Active paths</span><span class='v'>{len(analysis.active_paths)}</span></div>"
        f"<div class='kv'><span class='k'>Max active depth</span><span class='v'>{analysis.max_active_depth}</span></div>"
        f"<div class='kv'><span class='k'>Deepest active metrics</span><span class='v'><code>{_esc(deepest)}</code></span></div>"
        f"<div class='kv'><span class='k'>Investigation candidates</span><span class='v'><code>{_esc(candidates)}</code></span></div>"
        f"<div class='kv'><span class='k'>Shared dependencies</span><span class='v'>{_esc(shared)}</span></div>"
        f"<div class='kv'><span class='k'>Missing evidence</span><span class='v'>{_esc(missing)}</span></div>"
        f"<div class='kv'><span class='k'>Competing branches</span><span class='v'>{_yn(analysis.competing_active_branches)}</span></div>"
        f"<div class='kv'><span class='k'>Directional support</span><span class='v'>{_esc(direction)}</span></div>"
        f"<p class='tiny'>Active paths</p><p class='notes'>{paths}</p>"
        "</section>"
    )


def proposal_html(
    proposal: OperationalProposal | None, *, model_required: bool, force_model: bool
) -> str:
    if proposal is None:
        return (
            "<section class='expect-panel'>"
            "<h3>Proposed operation</h3>"
            "<p>No operational proposal was generated. Metric Runtime resolved this graph "
            "without a judgment provider.</p>"
            "</section>"
        )
    facts = "".join(f"<li>{_esc(item)}</li>" for item in proposal.rationale_facts)
    gate = (
        "Model required — operational ambiguity remains."
        if model_required
        else "Structurally resolved — model not required."
    )
    if force_model and not model_required:
        gate += " force_model=True so the experiment still records what a provider would answer."
    target = proposal.target_metric_id or "—"
    return (
        "<section class='expect-panel'>"
        "<h3>Proposed operation</h3>"
        f"<div class='kv'><span class='k'>Kind</span><span class='v'><code>{_esc(proposal.kind.value)}</code></span></div>"
        f"<div class='kv'><span class='k'>Focal</span><span class='v'><code>{_esc(', '.join(proposal.focal_metric_ids))}</code></span></div>"
        f"<div class='kv'><span class='k'>Target</span><span class='v'><code>{_esc(target)}</code></span></div>"
        f"<p class='tiny'>{_esc(gate)}</p>"
        f"<p class='tiny'>Deterministic rationale</p><ul class='facts'>{facts}</ul>"
        "</section>"
    )


def _run_card(title: str, run: JudgmentRun | None) -> str:
    if run is None:
        return (
            f"<section class='judge-card'><h3>{_esc(title)}</h3>"
            "<p class='muted'>No run yet. In Live mode, click Run evaluation.</p></section>"
        )
    if run.skipped_reason:
        return (
            f"<section class='judge-card'><h3>{_esc(title)}</h3>"
            "<div class='sim-banner'>Not invoked — graph already resolved this proposal.</div>"
            "</section>"
        )
    if run.error and run.judgment is None:
        return (
            f"<section class='judge-card'><h3>{_esc(title)}</h3>"
            f"<div class='status-fail'>{_esc(run.error)}</div></section>"
        )
    sim = (
        "<div class='sim-banner'>SIMULATED — not measured provider performance</div>"
        if run.simulated
        else ""
    )
    judgment = run.judgment
    disp = judgment.disposition.value.upper().replace("_", " ") if judgment else "—"
    return (
        f"<section class='judge-card'><h3>{_esc(title)}</h3>{sim}"
        f"<div class='kv'><span class='k'>Evidence sufficient</span><span class='v'>{_yn(None if judgment is None else judgment.evidence_sufficient)}</span></div>"
        f"<div class='kv'><span class='k'>Human review</span><span class='v'>{_yn(None if judgment is None else judgment.human_review_required)}</span></div>"
        f"<div class='kv'><span class='k'>Disposition</span><span class='v'><code>{_esc(disp)}</code></span></div>"
        "</section>"
    )


def comparison_html(jev: JudgmentRun | None, openai: JudgmentRun | None) -> str:
    disagree = False
    if jev is not None and openai is not None and jev.judgment and openai.judgment:
        disagree = (
            jev.judgment.evidence_sufficient != openai.judgment.evidence_sufficient
            or jev.judgment.human_review_required != openai.judgment.human_review_required
            or jev.judgment.disposition != openai.judgment.disposition
        )
    banner = (
        "<div class='sim-banner'>MODEL DISAGREEMENT — comparison policy chooses HUMAN REVIEW</div>"
        if disagree
        else ""
    )
    return (
        f"{banner}"
        "<div class='compare-grid'>"
        f"{_run_card('Jev', jev)}"
        f"{_run_card('OpenAI', openai)}"
        "</div>"
    )


def expectation_html(case: BenchmarkCase, expected: ProposalExpectation | None) -> str:
    if expected is None:
        return (
            "<section class='expect-panel'>"
            "<p class='sim-banner'>NOT SENT TO PROVIDER.</p>"
            f"<p class='tiny'>{_esc(case.why_it_exists)}</p>"
            f"<p class='notes'><strong>Evaluator notes</strong> — NOT SENT TO MODEL. {_esc(case.notes)}</p>"
            "</section>"
        )
    disp = ", ".join(sorted(item.value for item in expected.accepted_dispositions))
    return (
        "<section class='expect-panel'>"
        "<p class='sim-banner'>NOT SENT TO PROVIDER.</p>"
        f"<p class='tiny'>{_esc(case.why_it_exists)}</p>"
        f"<div class='kv'><span class='k'>Accepted dispositions</span><span class='v'>{_esc(disp)}</span></div>"
        f"<p class='notes'><strong>Evaluator notes</strong> — NOT SENT TO MODEL. {_esc(case.notes)}</p>"
        "</section>"
    )


def jev_panel_html(run: JudgmentRun | None) -> str:
    if run is None:
        return "<section class='side-panel'><h3>Jev metadata</h3><p class='muted'>No run.</p></section>"
    details = run.provider_details or {}
    conf = jev_confidence_map(details)
    probs = jev_probabilities_map(details)
    conf_rows = (
        "".join(
            f"<div class='kv'><span class='k'>{_esc(k)}</span><span class='v'>{v:.2f}</span></div>"
            for k, v in conf.items()
        )
        or "<p class='muted'>No confidence returned.</p>"
    )
    prob_rows = []
    for field, dist in probs.items():
        items = ", ".join(f"{k} {v:.2f}" for k, v in sorted(dist.items(), key=lambda kv: -kv[1]))
        prob_rows.append(
            f"<div class='kv'><span class='k'>{_esc(field)}</span><span class='v'>{_esc(items)}</span></div>"
        )
    sim = "<div class='sim-banner'>SIMULATED metadata</div>" if run.simulated else ""
    return (
        "<section class='side-panel'><h3>Jev metadata</h3>"
        f"{sim}"
        f"<p class='tiny'>Model <code>{_esc(run.model)}</code>. "
        "Confidence is a margin, not P(the answer is correct). "
        "Do not compare these numbers to OpenAI.</p>"
        f"{conf_rows}"
        f"<p class='tiny'>Option probabilities</p>{''.join(prob_rows) or '<p class=muted>none</p>'}"
        f"<div class='kv'><span class='k'>Latency</span><span class='v'>"
        f"{_esc(f'{run.latency_ms:.0f} ms' if run.latency_ms is not None else '—')}</span></div>"
        "</section>"
    )


def openai_panel_html(run: JudgmentRun | None) -> str:
    if run is None:
        return "<section class='side-panel'><h3>OpenAI metadata</h3><p class='muted'>No run.</p></section>"
    tokens = openai_token_usage(run.usage)
    sim = (
        "<div class='sim-banner'>SIMULATED — usage not from a live call</div>"
        if run.simulated
        else ""
    )
    token_rows = (
        "".join(
            f"<div class='kv'><span class='k'>{_esc(k.replace('_', ' '))}</span>"
            f"<span class='v'>{v}</span></div>"
            for k, v in tokens.items()
        )
        or "<p class='muted'>No token usage returned.</p>"
    )
    validation = "yes" if run.validation_ok else ("no" if run.validation_ok is False else "—")
    return (
        "<section class='side-panel'><h3>OpenAI metadata</h3>"
        f"{sim}"
        "<p class='tiny'>No manufactured confidence. Structured output only.</p>"
        f"<div class='kv'><span class='k'>Model</span><span class='v'><code>{_esc(run.model)}</code></span></div>"
        f"<div class='kv'><span class='k'>Latency</span><span class='v'>"
        f"{_esc(f'{run.latency_ms:.0f} ms' if run.latency_ms is not None else '—')}</span></div>"
        f"{token_rows}"
        f"<div class='kv'><span class='k'>Structured-output validation</span>"
        f"<span class='v'>{_esc(validation)}</span></div>"
        "</section>"
    )


def policy_html(
    proposal: OperationalProposal | None,
    jev: JudgmentRun | None,
    openai: JudgmentRun | None,
) -> str:
    if proposal is None:
        return ""
    if jev is not None and openai is not None:
        decision: PolicyDecision = apply_comparison_policy(proposal, jev, openai)
    elif jev is not None and jev.judgment is not None:
        decision = apply_policy(
            jev.judgment, jev_confidence=min_jev_confidence(jev.provider_details)
        )
    elif openai is not None and openai.judgment is not None:
        decision = apply_policy(openai.judgment)
    else:
        return ""
    return (
        "<section class='policy-panel'>"
        "<h3>Illustrative policy</h3>"
        "<p class='tiny'>Does not suppress or close incidents. Experimental optional judgment only.</p>"
        f"<div class='kv'><span class='k'>Route</span><span class='v'><code>{_esc(decision.route.value)}</code></span></div>"
        f"<div class='kv'><span class='k'>Reason</span><span class='v'>{_esc(decision.reason_code)}</span></div>"
        f"<p class='tiny'>{_esc(decision.detail)}</p>"
        "</section>"
    )


def inventory_html(inventory: dict[str, int]) -> str:
    return (
        "<section class='baseline-panel'>"
        "<h3>Model avoidance</h3>"
        "<p class='tiny'>Model avoidance is a feature. Do not maximize provider calls.</p>"
        f"<div class='kv'><span class='k'>Cases</span><span class='v'>{inventory['cases']}</span></div>"
        f"<div class='kv'><span class='k'>Resolved structurally</span><span class='v'>{inventory['resolved_structurally']}</span></div>"
        f"<div class='kv'><span class='k'>Generated operational proposals</span><span class='v'>{inventory['generated_proposals']}</span></div>"
        f"<div class='kv'><span class='k'>Proposals requiring judgment</span><span class='v'>{inventory['proposals_requiring_judgment']}</span></div>"
        "</section>"
    )


def overview_table_html(rows: list[dict[str, Any]]) -> str:
    body = []
    simulated = any(row.get("simulated") for row in rows)
    for row in rows:
        pair = row.get("pair")
        pair_txt = f"{pair.field_hits}/{pair.field_total}" if pair else "—"
        required = "yes" if row.get("model_required") else "no"
        body.append(
            "<tr>"
            f"<td>{_esc(row['title'])}</td>"
            f"<td class='num'>{required}</td>"
            f"<td class='num'>{_esc(score_label(row.get('jev')))}</td>"
            f"<td class='num'>{_esc(score_label(row.get('openai')))}</td>"
            f"<td class='num'>{_esc(pair_txt)}</td>"
            "</tr>"
        )
    banner = (
        "<div class='sim-banner'>SIMULATED fixture scores — not live provider performance</div>"
        if simulated
        else ""
    )
    return (
        f"{banner}"
        "<table class='grid-table'><thead><tr>"
        "<th>Case</th><th>Model required</th><th>Jev expectation</th><th>OpenAI expectation</th><th>Pair agree</th>"
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
        "<p class='tiny'>Expectation agreement is membership in the evaluator's accepted set. "
        "Pair agree is Jev vs OpenAI. Neither is ranked as a winner.</p>"
    )


def raw_enriched_table_html(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_esc(row['title'])}</td>"
            f"<td class='num'>{_esc(score_label(row.get('jev_raw')))}</td>"
            f"<td class='num'>{_esc(score_label(row.get('jev_enriched')))}</td>"
            f"<td class='num'>{_esc(score_label(row.get('openai_raw')))}</td>"
            f"<td class='num'>{_esc(score_label(row.get('openai_enriched')))}</td>"
            "</tr>"
        )
    return (
        "<div class='sim-banner'>SIMULATED fixture ablation — live mode measures real robustness</div>"
        "<table class='grid-table'><thead><tr>"
        "<th>Case</th><th>Jev raw</th><th>Jev enriched</th><th>OpenAI raw</th><th>OpenAI enriched</th>"
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
        "<p class='tiny'>Does deterministic GraphAnalysis make provider judgment more robust?</p>"
    )


def robustness_html(stats: list[RobustnessStat]) -> str:
    by_test: dict[str, dict[str, RobustnessStat]] = {}
    for item in stats:
        by_test.setdefault(item.test, {})[item.provider] = item
    labels = {
        "name_anonymization": "Name anonymization",
        "order_shuffle": "Order shuffle",
        "noise_nodes": "Noise nodes",
    }
    simulated = any(item.simulated for item in stats)
    rows = []
    for key, label in labels.items():
        jev = by_test.get(key, {}).get("jev")
        openai = by_test.get(key, {}).get("openai")

        def pct(stat: RobustnessStat | None) -> str:
            if stat is None or stat.same_answer is None:
                return "—"
            return f"{stat.same_answer:.0%}"

        rows.append(
            "<tr>"
            f"<td>{_esc(label)}</td>"
            f"<td class='num'>{_esc(pct(jev))}</td>"
            f"<td class='num'>{_esc(pct(openai))}</td>"
            "</tr>"
        )
    banner = (
        "<div class='sim-banner'>SIMULATED fixture robustness — live mode measures real stability</div>"
        if simulated
        else ""
    )
    return (
        f"{banner}"
        "<table class='grid-table'><thead><tr>"
        "<th></th><th>Jev same answer</th><th>OpenAI same answer</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        "<p class='tiny'>Same answer means identical proposal judgment. This is stability, not accuracy.</p>"
    )


def sequence_html(title: str, rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        jev = row.get("jev")
        oa = row.get("openai")
        jev_d = jev.judgment.disposition.value if jev and jev.judgment else "—"
        oa_d = oa.judgment.disposition.value if oa and oa.judgment else "—"
        body.append(
            "<tr>"
            f"<td>{_esc(row['label'])}</td>"
            f"<td class='num'>{_esc(jev_d)}</td>"
            f"<td class='num'>{_esc(oa_d)}</td>"
            "</tr>"
        )
    return (
        f"<h3>{_esc(title)}</h3>"
        "<div class='sim-banner'>SIMULATED fixture sequence — inspect monotonic/sensible behavior, not a correct threshold.</div>"
        "<table class='grid-table'><thead><tr>"
        "<th>Step</th><th>Jev disposition</th><th>OpenAI disposition</th>"
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def graph_figure(snapshot: MetricStateGraphSnapshot):
    import plotly.graph_objects as go

    nodes = snapshot.metric_map()
    focals = set(snapshot.focal_metric_ids)
    children: dict[str, list[str]] = {mid: [] for mid in nodes}
    for edge in snapshot.dependencies:
        children.setdefault(edge.metric_id, []).append(edge.depends_on)

    depth: dict[str, int] = {}

    def walk(metric_id: str, d: int, seen: set[str]) -> None:
        depth[metric_id] = max(d, depth.get(metric_id, 0))
        if metric_id in seen:
            return
        nxt = seen | {metric_id}
        for child in children.get(metric_id, []):
            walk(child, d + 1, nxt)

    for focal in snapshot.focal_metric_ids:
        walk(focal, 0, set())
    for metric_id in nodes:
        depth.setdefault(metric_id, max(depth.values(), default=0) + 1)

    layers: dict[int, list[str]] = {}
    for metric_id, d in depth.items():
        layers.setdefault(d, []).append(metric_id)
    layout: dict[str, tuple[float, float]] = {}
    for d, ids in layers.items():
        ids = sorted(ids)
        width = max(len(ids) - 1, 1)
        for i, metric_id in enumerate(ids):
            layout[metric_id] = (i / width, -float(d))

    traces: list[Any] = []
    xs: list[float | None] = []
    ys: list[float | None] = []
    for edge in snapshot.dependencies:
        if edge.metric_id not in layout or edge.depends_on not in layout:
            continue
        x0, y0 = layout[edge.metric_id]
        x1, y1 = layout[edge.depends_on]
        xs.extend([x0, x1, None])
        ys.extend([y0, y1, None])
    traces.append(
        go.Scatter(
            x=xs,
            y=ys,
            mode="lines",
            line=dict(color=STYLE["line"], width=1.6),
            hoverinfo="skip",
            showlegend=False,
        )
    )
    node_x, node_y, colors, sizes, labels, hover = [], [], [], [], [], []
    for metric_id, (x, y) in layout.items():
        node = nodes[metric_id]
        node_x.append(x)
        node_y.append(y)
        if metric_id in focals:
            colors.append(STYLE["focal"])
            sizes.append(28)
        elif node.state in ACTIVE_STATES:
            colors.append(STYLE["open"] if node.state == "OPEN" else STYLE["detected"])
            sizes.append(22)
        elif node.state == "NO_DATA":
            colors.append(STYLE["warn"])
            sizes.append(18)
        else:
            colors.append(STYLE["normal"])
            sizes.append(16)
        labels.append(f"<b>{metric_id}</b><br>{node.state}")
        hover.append(
            f"{metric_id}<br>state {node.state}<br>"
            f"value {_fmt_num(node.current_value)}  Δ {_fmt_pct(node.relative_change)}"
        )
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
        title="Metric state graph",
        template="plotly_white",
        height=340,
        margin=dict(l=20, r=20, t=40, b=20),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        font=dict(family=STYLE["font"], size=12, color=STYLE["ink"]),
    )
    return fig


def header_html() -> str:
    return (
        "<div class='page-head'>"
        "<h1>Metric Runtime — Judgment Evaluation</h1>"
        "<p>After Metric Runtime has computed everything it can prove, is there still "
        "a useful class of operational decisions for an experimental optional judgment provider?</p>"
        "</div>"
    )
