"""Metric Runtime judgment evaluation console.

Core evaluation logic lives in the sibling Python modules. This file only
renders controls and results. Opening the app never calls Jev or OpenAI.
"""

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

    from examples.metric_judgment_eval.cases import all_cases, get_case
    from examples.metric_judgment_eval.evaluation import (
        evaluate_suite_fixture,
        overview_rows,
        raw_vs_enriched_rows,
        robustness_from_fixture,
        run_selected,
        sequence_fixture_rows,
        suite_inventory,
    )
    from examples.metric_judgment_eval.fixtures import FixtureJudgmentProvider
    from examples.metric_judgment_eval.models import CaseEvaluation
    from examples.metric_judgment_eval.proposals import proposal_requires_model
    from examples.metric_judgment_eval.providers import (
        JevJudgmentProvider,
        OpenAIJudgmentProvider,
        detect_availability,
    )
    from examples.metric_judgment_eval.viz import (
        analysis_html,
        availability_html,
        comparison_html,
        expectation_html,
        graph_figure,
        header_html,
        inventory_html,
        jev_panel_html,
        metric_table_html,
        openai_panel_html,
        overview_table_html,
        payload_html,
        policy_html,
        proposal_html,
        raw_enriched_table_html,
        robustness_html,
        sequence_html,
    )

    get_live, set_live = mo.state(None)
    cases = all_cases()
    fixture_suite = evaluate_suite_fixture(n=1)
    fixture_overview = overview_rows(fixture_suite, cases)
    fixture_robust = robustness_from_fixture()
    fixture_inventory = suite_inventory(cases)
    fixture_ablation = raw_vs_enriched_rows(cases)
    fixture_sequences = sequence_fixture_rows()

    return (
        CaseEvaluation,
        FixtureJudgmentProvider,
        JevJudgmentProvider,
        OpenAIJudgmentProvider,
        analysis_html,
        availability_html,
        cases,
        comparison_html,
        detect_availability,
        expectation_html,
        fixture_ablation,
        fixture_inventory,
        fixture_overview,
        fixture_robust,
        fixture_sequences,
        get_case,
        get_live,
        graph_figure,
        header_html,
        inventory_html,
        jev_panel_html,
        metric_table_html,
        openai_panel_html,
        overview_table_html,
        payload_html,
        policy_html,
        proposal_html,
        proposal_requires_model,
        raw_enriched_table_html,
        robustness_html,
        run_selected,
        sequence_html,
        set_live,
        mo,
    )


@app.cell
def _controls(cases, detect_availability, mo):
    case_options = {item.title: item.case_id for item in cases}
    case = mo.ui.dropdown(options=case_options, value=cases[0].title, label="Case")
    provider = mo.ui.radio(
        options=["Jev", "OpenAI", "Compare"],
        value="Compare",
        label="Provider",
        inline=True,
    )
    input_mode = mo.ui.radio(
        options=["Realistic", "Anonymized"],
        value="Realistic",
        label="Input",
        inline=True,
    )
    input_level = mo.ui.radio(
        options=["Enriched", "Raw"],
        value="Enriched",
        label="Context",
        inline=True,
    )
    runs = mo.ui.radio(options=["1", "3", "5"], value="1", label="Runs", inline=True)
    execution = mo.ui.radio(
        options=["Fixture", "Live"],
        value="Fixture",
        label="Execution",
        inline=True,
    )
    view = mo.ui.radio(
        options=["Case", "All cases", "Robustness"],
        value="Case",
        label="View",
        inline=True,
    )
    run_eval = mo.ui.run_button(label="Run evaluation")
    availability = detect_availability()
    return availability, case, execution, input_level, input_mode, provider, run_eval, runs, view


@app.cell
def _live_gate(
    JevJudgmentProvider,
    OpenAIJudgmentProvider,
    availability,
    case,
    execution,
    get_case,
    input_level,
    input_mode,
    provider,
    run_eval,
    run_selected,
    runs,
    set_live,
):
    if execution.value == "Live" and run_eval.value:
        _selected = get_case(case.value)
        _mode = "anonymized" if input_mode.value == "Anonymized" else "realistic"
        _level = "raw" if input_level.value == "Raw" else "enriched"
        _n = int(runs.value)
        _wanted = provider.value
        _live_providers = []
        if _wanted in {"Jev", "Compare"}:
            _live_providers.append(JevJudgmentProvider(availability.jev_model))
        if _wanted in {"OpenAI", "Compare"}:
            _live_providers.append(OpenAIJudgmentProvider(availability.openai_model))
        _result = run_selected(
            _selected, providers=_live_providers, input_mode=_mode, n=_n, input_level=_level
        )
        set_live(
            {
                "key": (_selected.case_id, _mode, _level, _n, _wanted),
                "result": _result,
            }
        )
    return


@app.cell
def _page(
    CaseEvaluation,
    FixtureJudgmentProvider,
    analysis_html,
    availability,
    availability_html,
    case,
    comparison_html,
    execution,
    expectation_html,
    fixture_ablation,
    fixture_inventory,
    fixture_overview,
    fixture_robust,
    fixture_sequences,
    get_case,
    get_live,
    graph_figure,
    header_html,
    input_level,
    input_mode,
    inventory_html,
    jev_panel_html,
    metric_table_html,
    mo,
    openai_panel_html,
    overview_table_html,
    payload_html,
    policy_html,
    proposal_html,
    proposal_requires_model,
    provider,
    raw_enriched_table_html,
    robustness_html,
    run_eval,
    run_selected,
    runs,
    sequence_html,
    view,
):
    _selected = get_case(case.value)
    _mode = "anonymized" if input_mode.value == "Anonymized" else "realistic"
    _level = "raw" if input_level.value == "Raw" else "enriched"
    _n = int(runs.value)
    _wanted = provider.value

    _live_blocks = []
    if execution.value == "Live":
        _live_blocks.append(run_eval)
        _live_blocks.append(mo.Html(availability_html(availability)))

    _controls = mo.hstack(
        [view, case, provider, input_mode, input_level, runs, execution, *_live_blocks],
        justify="start",
        gap=1.0,
    )

    if view.value == "All cases":
        page = mo.vstack(
            [
                _controls,
                mo.Html(header_html()),
                mo.Html(inventory_html(fixture_inventory)),
                mo.Html(overview_table_html(fixture_overview)),
                mo.Html(raw_enriched_table_html(fixture_ablation)),
                mo.Html(
                    "<p class='tiny'>Live all-case scores are not auto-run. "
                    "Use Case view + Run evaluation for live calls.</p>"
                ),
            ],
            gap=0.7,
        )
    elif view.value == "Robustness":
        page = mo.vstack(
            [
                _controls,
                mo.Html(header_html()),
                mo.Html(robustness_html(fixture_robust)),
                mo.Html(sequence_html("Magnitude perturbation", fixture_sequences["magnitude"])),
                mo.Html(sequence_html("Missing-evidence degradation", fixture_sequences["degradation"])),
            ],
            gap=0.7,
        )
    else:
        _fixture_result = run_selected(
            _selected,
            providers=(FixtureJudgmentProvider("jev"), FixtureJudgmentProvider("openai")),
            input_mode=_mode,
            n=1,
            input_level=_level,
        )
        _stored = get_live()
        _live_key = (_selected.case_id, _mode, _level, _n, _wanted)
        _live_match = isinstance(_stored, dict) and _stored.get("key") == _live_key
        if execution.value == "Fixture":
            _result = _fixture_result
        elif _live_match:
            _result = _stored["result"]
        else:
            _result = {
                **_fixture_result,
                "evaluation": CaseEvaluation(case_id=_selected.case_id, runs=[]),
            }

        _snapshot = _result["snapshot"]
        _analysis = _result["analysis"]
        _expected = _result["expected"]
        _context = _result["context"]
        _payload = _result["wire_json"] or ""
        _runs_out = _result["evaluation"].runs
        _jev_run = next((item for item in _runs_out if item.provider == "jev"), None)
        _openai_run = next((item for item in _runs_out if item.provider == "openai"), None)
        _proposal = _expected.proposal if _expected else None
        _required = (
            proposal_requires_model(_analysis, _proposal) if _proposal is not None else False
        )

        _waiting = execution.value == "Live" and not _live_match
        _wait_html = (
            "<p class='tiny'>Live mode: no provider call until you click Run evaluation. "
            "Graph analysis and the proposal below are computed locally first.</p>"
            if _waiting
            else ""
        )
        if _waiting:
            _jev_run = None
            _openai_run = None

        _digest = _context.sha256() if _context is not None else _snapshot.sha256()
        _snap_hash = _snapshot.sha256()
        _notes = mo.accordion({"Benchmark expectation": mo.Html(expectation_html(_selected, _expected))})

        page = mo.vstack(
            [
                _controls,
                mo.Html(header_html()),
                mo.Html(f"<h2 style='margin:0.2rem 0 0'>{_selected.title}</h2>"),
                mo.Html(_wait_html),
                mo.ui.plotly(graph_figure(_snapshot)),
                mo.Html(metric_table_html(_snapshot)),
                mo.Html(analysis_html(_analysis)),
                mo.Html(
                    proposal_html(
                        _proposal,
                        model_required=_required,
                        force_model=_selected.force_model,
                    )
                ),
                mo.Html(comparison_html(_jev_run, _openai_run)),
                mo.Html(
                    payload_html(_payload, _digest, snapshot_hash=_snap_hash)
                    if _payload
                    else ""
                ),
                mo.Html(
                    "<div class='side-grid'>"
                    f"{jev_panel_html(_jev_run) if _wanted in {'Jev', 'Compare'} else ''}"
                    f"{openai_panel_html(_openai_run) if _wanted in {'OpenAI', 'Compare'} else ''}"
                    "</div>"
                ),
                mo.Html(policy_html(_proposal, _jev_run, _openai_run)),
                _notes,
            ],
            gap=0.7,
        )
    page
    return


if __name__ == "__main__":
    app.run()
