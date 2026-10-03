"""Metric Runtime × Jev × LLM — dashboard and comparison."""

import marimo

__generated_with = "0.23.16"
app = marimo.App(width="full", css_file="app.css")


@app.cell
def _bootstrap():
    import sys
    from pathlib import Path

    import marimo as mo

    here = Path(__file__).resolve().parent
    root = here.parents[1]
    for path in (str(root), str(here)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from examples.judgment_comparison.evidence import build_evidence, packet_for_models
    from examples.judgment_comparison.judgments import (
        LiveJevProvider,
        LiveLLMProvider,
        detect_availability,
        fixture_run,
    )
    from examples.judgment_comparison.scenarios import get_scenario, list_scenarios, run_scenario_runtime
    from examples.judgment_comparison.viz import (
        availability_html,
        graph_figure,
        history_figure,
        judgment_details_html,
        metric_cards_html,
        spend_time_html,
    )

    get_live, set_live = mo.state(None)

    return (
        LiveJevProvider,
        LiveLLMProvider,
        availability_html,
        build_evidence,
        detect_availability,
        fixture_run,
        get_live,
        get_scenario,
        graph_figure,
        history_figure,
        judgment_details_html,
        list_scenarios,
        metric_cards_html,
        mo,
        packet_for_models,
        run_scenario_runtime,
        set_live,
        spend_time_html,
    )


@app.cell
def _controls(detect_availability, list_scenarios, mo):
    scenarios = list_scenarios()
    scenario = mo.ui.dropdown(
        options={item.name: item.id for item in scenarios},
        value="Great Lunch",
        label="Scenario",
    )
    execution = mo.ui.radio(
        options=["Fixture", "Live"],
        value="Fixture",
        label="Mode",
        inline=True,
    )
    run_comparison = mo.ui.run_button(label="Run comparison")
    availability = detect_availability()
    return availability, execution, run_comparison, scenario


@app.cell
def _runtime(build_evidence, get_scenario, packet_for_models, run_scenario_runtime, scenario):
    selected = get_scenario(scenario.value)
    snapshot = run_scenario_runtime(selected)
    evidence = build_evidence(snapshot)
    _packet, packet_text, evidence_digest = packet_for_models(evidence)
    return evidence, evidence_digest, packet_text, selected, snapshot


@app.cell
def _fixtures(evidence_digest, fixture_run, selected):
    jev_fixture = fixture_run("jev", selected.id, evidence_digest)
    llm_fixture = fixture_run("llm", selected.id, evidence_digest)
    return jev_fixture, llm_fixture


@app.cell
def _live_gate(
    LiveJevProvider,
    LiveLLMProvider,
    availability,
    evidence_digest,
    execution,
    packet_text,
    run_comparison,
    set_live,
):
    if execution.value == "Live" and run_comparison.value:
        set_live(
            {
                "hash": evidence_digest,
                "jev": LiveJevProvider(availability.jev_model).judge(
                    packet_text, evidence_hash=evidence_digest
                ),
                "llm": LiveLLMProvider(availability.llm_model).judge(
                    packet_text, evidence_hash=evidence_digest
                ),
            }
        )
    return


@app.cell
def _page(
    availability,
    availability_html,
    evidence,
    evidence_digest,
    execution,
    get_live,
    graph_figure,
    history_figure,
    jev_fixture,
    judgment_details_html,
    llm_fixture,
    metric_cards_html,
    mo,
    run_comparison,
    scenario,
    selected,
    snapshot,
    spend_time_html,
):
    _stored = get_live()
    _live_match = isinstance(_stored, dict) and _stored.get("hash") == evidence_digest
    if execution.value == "Fixture":
        jev_run = jev_fixture
        llm_run = llm_fixture
    else:
        jev_run = _stored.get("jev") if _live_match else None
        llm_run = _stored.get("llm") if _live_match else None

    live_blocks = []
    if execution.value == "Live":
        live_blocks.append(run_comparison)
        if not availability.jev_ready or not availability.llm_ready:
            live_blocks.append(mo.Html(availability_html(availability)))

    page = mo.vstack(
        [
            mo.hstack([scenario, execution, *live_blocks], justify="start", gap=1.2),
            mo.Html(f"<div class='page-head'><h1>{selected.name}</h1></div>"),
            mo.Html(metric_cards_html(evidence)),
            mo.hstack(
                [mo.ui.plotly(history_figure(snapshot)), mo.ui.plotly(graph_figure(snapshot))],
                widths="equal",
                gap=0.8,
            ),
            mo.Html(spend_time_html(jev_run, llm_run)),
            mo.Html(judgment_details_html(jev_run, llm_run)),
        ],
        gap=0.7,
    )
    page
    return


if __name__ == "__main__":
    app.run()
