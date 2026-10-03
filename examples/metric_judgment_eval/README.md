# Metric-state judgment evaluation

Experimental harness for a possible future optional judgment layer in Metric
Runtime.

marimo visualizes and runs the experiment. The benchmark itself is ordinary
Python, testable without marimo, and does not belong in Metric Runtime core.

The first version of this benchmark asked models to rediscover graph facts.
Live evaluation showed that was the wrong abstraction.

> If Python can prove it from the graph, it should not be a model judgment.

## Research question

Once Metric Runtime has deterministically analyzed the metric graph, can an
experimental optional judgment provider help decide whether the available
evidence is sufficient for a **specific operational decision**?

The most important question is not "Is Jev better than OpenAI?"

It is:

> After Metric Runtime has computed everything it can prove, is there still a
> small, useful class of operational decisions where probabilistic judgment
> adds value?

## Architecture being tested

```
RAW METRIC STATE
      │
      ▼
Metric Runtime deterministic GraphAnalysis
      │
      ├── active branches
      ├── active paths
      ├── shared dependencies
      ├── deepest active metrics
      ├── candidate investigation targets
      └── missing / no-data evidence
      │
      ▼
OperationalProposal
      │
      ▼
experimental optional judgment provider
   ┌─────────┴─────────┐
   ▼                   ▼
  Jev               OpenAI
   │                   │
   └── ProposalJudgment ┘
      │
      ▼
deterministic Python policy
```

Metric Runtime remains authoritative for values, calculations, anomaly
detection, states, graph relationships, and persistence.

Jev and OpenAI are **not** part of Metric Runtime's required architecture.
They are experimental optional judgment providers evaluated only for remaining
operational ambiguity.

## What Metric Runtime computes

`GraphAnalysis` contains only facts derivable from the dependency graph and
runtime state:

- **Active dependency branch**: a *direct* dependency of a focal metric whose
  subtree contains at least one active metric (`OPEN` / `DETECTED` /
  `ACKNOWLEDGED`, quality ≠ `no_data`).
- Active paths from each focal to active descendants
- Unique deepest active descendants as investigation candidates
- Shared active dependencies across focals
- Missing / `NO_DATA` observations

It does **not** contain likely-cause claims, materiality, or "needs human
review". Those are not structural facts.

Example:

```
gross_margin OPEN
├── revenue NORMAL
└── cost_of_goods OPEN
    ├── unit_cost OPEN
    └── volume NORMAL

active_branch_count = 1
investigation_candidates = (unit_cost,)
```

`cost_of_goods` and `unit_cost` are both active, but they belong to the **same**
direct-dependency branch.

Directional calculation polarity is **not** currently recoverable from
`MetricStateGraphSnapshot` (case formulas are dummy `sum(self)` shapes).
`directional_support_known=False`.

## What a judgment provider evaluates

Providers are **not** asked:

- graph pattern
- whether an active dependency exists
- whether focals share a dependency
- which unique deepest node to investigate

They receive a concrete `OperationalProposal` and answer only:

- Is evidence sufficient for **this** proposal?
- Should a human review **this** proposal?
- Accept / reject / request more evidence / human review

## Operational proposals

Generated conservatively from `GraphAnalysis`:

| Situation | Proposal |
| --- | --- |
| Unique deepest active descendant, complete data | `route_investigation` — structurally resolved, model not required |
| Multiple focals share an active dependency | `group_incidents` — model judges sufficiency |
| Unique active descendant already in an incident state | `suppress_redundant_notification` — evaluated only, never applied |
| Competing branches | no silent unique route |
| Unrelated focals | no grouping proposal |
| No active descendant | no route proposal |

`model_required=False` when Python already settled the decision. Cases may set
`force_model=True` so the experiment can still record what a provider would
have answered.

## Raw vs enriched evaluation

Both providers receive identical `JudgmentContext` for a given run.

- **Raw**: snapshot + proposal (`graph_analysis` absent)
- **Enriched**: snapshot + proposal + deterministic `GraphAnalysis`

Canonical SHA-256 is taken over the context payload. Jev and OpenAI hashes
must match.

## Why model avoidance is a feature

If Metric Runtime can prove a unique candidate from the graph, calling a
probabilistic model to rediscover `unit_cost` is not a useful judgment
difference. The All cases view reports:

- cases
- resolved structurally
- generated operational proposals
- proposals requiring judgment

Do not maximize the last number.

## Input contract

`MetricStateGraphSnapshot` is unchanged: focals, metric facts, dependency
edges. Canonical JSON + SHA-256.

`BenchmarkCase` notes, accepted dispositions, category, and `case_id` are
**not** in the prompt.

### How real runtime state is converted

`build_state_snapshot(catalog, runtime_state, focal_metric_ids, dependency_depth=3)`
walks semantic dependencies from OPEN focals. `NO_DATA` observations stay
`None`, never a placeholder zero.

## Running

```bash
marimo run examples/metric_judgment_eval/app.py
# or: marimo edit examples/metric_judgment_eval/app.py
```

Opening the app never contacts Jev or OpenAI. Fixture mode is the default.
Live calls require **Run evaluation** plus provider keys.

```bash
pytest tests/test_metric_judgment_eval.py -q
```

```bash
RUN_LIVE_JUDGMENT_EVAL=1 python -m examples.metric_judgment_eval.live_eval \
  --input-level enriched --runs 1 --output path/to/results.jsonl
```

JSONL result files are optional and must not be committed by default.

## Policy

Comparison policy is conservative and illustrative: any human-review flag or
disposition disagreement → `HUMAN_REVIEW`. It does not suppress or close
incidents.
