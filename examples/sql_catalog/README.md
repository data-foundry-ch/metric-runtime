# SQL / Batch / Derived catalog example

Domain-neutral demonstration of first-class KPI calculations.

```bash
python examples/sql_catalog/demo.py
```

Shows:

1. `FormulaCalculation` over a tiny fact table
2. `SqlCalculation` for a scalar target
3. Shared `BatchCalculation` (`sales_metrics` runs once)
4. `DerivedCalculation` (`revenue_attainment = closed_won / target`)

Core stays generic — no company-specific adapters.
