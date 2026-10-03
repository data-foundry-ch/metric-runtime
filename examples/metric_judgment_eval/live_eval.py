"""Opt-in live evaluation. Never executed by pytest/CI.

Usage::

    RUN_LIVE_JUDGMENT_EVAL=1 python -m examples.metric_judgment_eval.live_eval --input-level enriched
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

LIVE_FLAG = "RUN_LIVE_JUDGMENT_EVAL"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Opt-in live metric-judgment evaluation")
    parser.add_argument("--input-level", choices=("raw", "enriched"), default="enriched")
    parser.add_argument("--case", default=None, help="Optional case_id to evaluate a single case")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output", default=None, help="Optional JSONL path for live results")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.environ.get(LIVE_FLAG) != "1":
        sys.stderr.write(
            f"Refusing to contact providers. Set {LIVE_FLAG}=1 to run live evaluation.\n"
        )
        return 2

    from examples.metric_judgment_eval.cases import all_cases, get_case
    from examples.metric_judgment_eval.evaluation import evaluate_case, overview_rows, score_label
    from examples.metric_judgment_eval.providers import (
        JevJudgmentProvider,
        OpenAIJudgmentProvider,
        detect_availability,
    )

    availability = detect_availability()
    if not availability.pydantic_ai:
        sys.stderr.write("pydantic-ai is not installed.\n")
        return 1
    providers = []
    if availability.jev_ready:
        providers.append(JevJudgmentProvider(availability.jev_model))
    if availability.openai_ready:
        providers.append(OpenAIJudgmentProvider(availability.openai_model))
    if not providers:
        sys.stderr.write("No provider keys configured.\n")
        return 1

    cases = [get_case(args.case)] if args.case else all_cases()
    evaluations = [
        evaluate_case(case, providers, n=args.runs, input_level=args.input_level) for case in cases
    ]
    rows = overview_rows(evaluations, cases)
    print("case\tjev\topenai\tpair")
    for row in rows:
        pair = row["pair"]
        pair_txt = f"{pair.field_hits}/{pair.field_total}" if pair else "—"
        print(
            f"{row['case_id']}\t{score_label(row['jev'])}\t{score_label(row['openai'])}\t{pair_txt}"
        )

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for evaluation in evaluations:
                for run in evaluation.runs:
                    record = run.model_dump(mode="json")
                    record["recorded_at"] = datetime.now(tz=UTC).isoformat()
                    handle.write(json.dumps(record) + "\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
