# Product integration

Metric Runtime owns the **semantic contract**. Products may add workflow, UI,
and tenancy around it — they should not redefine the semantic fields.

## Boundary

| Metric Runtime (`Metric`) | Product wrapper |
|---|---|
| id, name, description | draft / published / archived |
| calculation | organization / tenant |
| dependencies, dimensions | collections / overlays |
| unit, owner, detector | layout / display_order |
| tags, metadata (semantic) | RBAC / approvals |
| | editor coordinates / traffic lights |

## Composition pattern

```python
from typing import Literal
from pydantic import BaseModel
from metric_runtime import Metric

class ProductMetric(BaseModel):
    metric: Metric
    lifecycle: Literal["draft", "published", "archived"] = "draft"
    version: int = 1
    etag: str | None = None
    parent_id: str | None = None
    display_order: int | None = None
```

The product stores or compiles to:

```json
{
  "metric": { "...Metric JSON..." },
  "lifecycle": "published",
  "permissions": {},
  "layout": {}
}
```

## Source of truth

Prefer **one** semantic payload:

- Python/Git repositories author `Metric`
- APIs exchange `Metric` JSON
- UIs edit through the same schema (`Metric.model_json_schema()`)
- Runtime evaluates the same `Metric.id`

Avoid independently duplicating `calculation`, `dependencies`, `dimensions`,
`unit`, `detector`, or `owner` outside the Metric document.

## Non-goals for core

Metric Runtime deliberately does **not** implement:

- draft/publish workflow
- multi-tenancy / organizations
- collections managers
- visual metric editors
- dashboarding
- RBAC

Those are product concerns built **around** the semantic contract.
