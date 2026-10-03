# Metric Runtime × Jev × LLM

A marimo application showing how **deterministic Metric Runtime evidence** can feed the
**same typed business-decision contract** through TypeSafe Jev and a general-purpose LLM.

This is business observability plus a typed decision system. It is not a chatbot.

## What this demonstrates

Metric Runtime establishes deterministic evidence.

Jev and a classic LLM receive the same evidence and the same typed Pydantic
decision contract.

Their outputs feed into deterministic Python policy.

```
Metric Runtime
      ↓
Evidence Packet
   ┌──┴──┐
   ↓     ↓
  Jev   LLM
   └──┬──┘
      ↓
Python Policy
```

Conceptual message:

> Use deterministic systems to establish what happened.
> Use judgment models to assess what it means.
> Use deterministic policy to decide what the system may do.

## Architecture

```
1. FACTS          Metric Runtime     deterministic
2. JUDGMENT       Jev / LLM          probabilistic / model-based
3. ACTION         Python policy      deterministic
```

Metric Runtime does **not** ask a model to rediscover metric definitions, SQL,
calculation logic, anomaly thresholds, graph dependencies, or runtime state.

Those are already executable semantics. The judgment layer answers narrow
evaluative questions. Python decides what the system may do.

The judgment layer is optional. Metric Runtime remains useful with no AI provider at all.

## Why compare Jev and an LLM?

- Jev is designed around typed judgments with per-field probabilities.
- The classic LLM uses structured output through the **same** Pydantic model.
- This demo compares observed behaviour on a narrow judgment task.
- It is **not** a benchmark, ranking, or claim that one approach is universally better.

Both systems receive identical evidence, identical field descriptions, and no
hidden extra business context.

## Scenarios

| Scenario | Design expectation | What the facts look like |
|---|---|---|
| Great Lunch | promotion behaviour | Revenue/orders up; margin, AOV, costs, threshold concentration deteriorate |
| Healthy Growth | observe | Volume up; unit economics stable or improving |
| Cost Pressure | investigate costs | Margin down with cost/order up; basket cliff quiet |
| Ambiguous | human review | Campaign on; facts clear; causal reading is not |

Scenario design expectation is **not** experimentally verified ground truth.

The important Great Lunch story: everything worked exactly as designed. The
business outcome was still undesirable. The semantic graph is explanatory
context, not causal identification.

## Fixture mode

No API credentials required. Opening the notebook does not call Jev or an LLM.

From the repository root:

```bash
pip install -e ".[judgment-demo]"
uv run marimo edit examples/judgment_comparison/app.py
# or
uv run marimo run examples/judgment_comparison/app.py
```

Equivalent with pip:

```bash
pip install -e ".[judgment-demo]"
marimo edit examples/judgment_comparison/app.py
marimo run examples/judgment_comparison/app.py
```

Default view: Great Lunch in fixture mode — dashboard plus Jev vs LLM.

Fixture results are labelled **SIMULATED RESULT**. Authored differences are for
the demo UI; they do not claim measured provider behaviour.

## Live mode

Live requests run only after **Run comparison**.

Environment variables:

| Variable | Purpose |
|---|---|
| `TYPESAFE_API_KEY` | TypeSafe Jev |
| `JEV_MODEL` | Optional. Defaults to `typesafe:jev-latest` |
| `DEMO_LLM_MODEL` | Pydantic AI model, e.g. `openai:gpt-4o` |
| Provider key for that model | e.g. `OPENAI_API_KEY` for `openai:…` |

Do not put real key values in source, screenshots, or logs. The UI only shows
configured / not configured.

Example:

```bash
export TYPESAFE_API_KEY=...
export JEV_MODEL=typesafe:jev-latest
export DEMO_LLM_MODEL=openai:gpt-4o
export OPENAI_API_KEY=...
uv run marimo run examples/judgment_comparison/app.py
```

Production / calibrated systems should pin a Jev version (`typesafe:jev-1.13.0`)
rather than relying permanently on `jev-latest`. Aliases move when TypeSafe
ships a release.

Jev integration: `pydantic_ai.Agent("typesafe:jev-latest", output_type=BusinessJudgment)`.

Classic LLM integration: the same `Agent(..., output_type=BusinessJudgment)` with
`DEMO_LLM_MODEL`.

## Interpreting confidence

Jev probabilities are provider output on the model response (`provider_details`).

Do **not** compare them directly with LLM self-reported confidence. This demo
never invents an LLM confidence field on `BusinessJudgment`.

## Cost comparison

Live mode shows a **Cost** panel next to the two judgments.

- Classic LLM amounts are best-effort **USD** from Pydantic AI usage
  (`usage.cost`, filled via [genai-prices](https://github.com/pydantic/genai-prices))
  or a provider-reported cost when that is present.
- Jev billed cost is used when the provider returns one. Otherwise Jev spend is
  **estimated from actual input tokens at $0.042/MTok input**. The UI labels this
  `*Estimated at $0.042/MTok input`. Tokens are never invented.
- If usage tokens and billed cost are both missing, the cell is **not reported**.
  Missing is never treated as `$0`.
- Fixture / simulated results are labelled **n/a (simulated)** and are not priced.
- This is **this run only**, not a provider ranking.

## Limitations

- Not causal inference
- Not formal model benchmarking
- No autonomous consequential action
- Small demo scenarios
- External model behaviour may change
- Fixture differences are authored, not measured
- The Jev confidence threshold in policy is illustrative, not a calibrated gate

Don't make the model understand your business from scratch.
Make your business understandable to software.
