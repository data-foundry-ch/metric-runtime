# The Great Lunch incident

PyPizza launches:

> **€10 off Amsterdam lunch orders over €20.**

Orders rise.
Revenue rises.
Profitability deteriorates.

Customers cluster just above the promotion threshold.

## Semantic investigation

```
Weekend Profit ↓
└── Profit Margin ↓
    ├── Average Order Value ↓
    │   └── Basket Threshold Concentration ↑
    └── Average Cost / Order ↑
```

Smart routing alerts **Commercial Growth / Promotions** — the owner of the
deepest explanatory KPI — not Finance alone.

## Run the demo

From the repository root:

```bash
pip install -e ".[demo,dev]"
python examples/pypizza/generate_data.py
marimo run examples/pypizza/app.py
```

Optional config-based runtime (same public loader the library uses):

```bash
metric-runtime validate \
  --project-config examples/pypizza/metric-runtime.yaml \
  --connections examples/pypizza/connections.yaml \
  --profile local

metric-runtime run \
  --project-config examples/pypizza/metric-runtime.yaml \
  --connections examples/pypizza/connections.yaml \
  --profile local
```

The marimo deck imports `metric_runtime` from `src/` — it does not reimplement
the framework.
