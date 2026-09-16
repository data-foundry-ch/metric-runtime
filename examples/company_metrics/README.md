# Company metrics repository

Domain-neutral example of **metrics-as-code**: typed `Metric` objects composed
into a `MetricCatalog`, validated offline, and exported as JSON.

## Layout

```
examples/company_metrics/
├── metric-runtime.yaml
├── metrics/
│   ├── finance.py
│   ├── sales.py
│   └── catalog.py
└── README.md
```

## Define

```python
from metric_runtime import Metric, DerivedCalculation, SeasonalZScore

profit_margin = Metric(
    id="profit_margin",
    name="Profit Margin",
    calculation=DerivedCalculation(expression="profit / revenue"),
    dependencies=["profit", "revenue"],
    unit="percent",
    owner="finance",
    detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
)
```

`id` is stable machine identity. `name` is presentation only.

## Compose

```python
from metric_runtime import MetricCatalog
from .finance import METRICS as FINANCE_METRICS
from .sales import METRICS as SALES_METRICS

catalog = MetricCatalog([*FINANCE_METRICS, *SALES_METRICS])
```

## Validate (offline)

From the example directory (with `metric-runtime` installed and this folder on
`PYTHONPATH`):

```bash
metric-runtime validate --project-config metric-runtime.yaml
metric-runtime catalog list --project-config metric-runtime.yaml
metric-runtime catalog show profit_margin --project-config metric-runtime.yaml
metric-runtime catalog export --format json -o catalog.json --project-config metric-runtime.yaml
```

No warehouse connection is required for validate / catalog inspection.

## CI sketch

```yaml
- run: pip install metric-runtime
- run: metric-runtime validate --project-config metric-runtime.yaml
```

See also [docs/metric-repositories.md](../../docs/metric-repositories.md).
