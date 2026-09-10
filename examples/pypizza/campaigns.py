from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CampaignOffer:
    name: str
    min_basket_eur: float
    discount_eur: float
    discount_cap_eur: float | None = None
    percent_off: float | None = None


GREAT_LUNCH = CampaignOffer(
    name="Great Lunch",
    min_basket_eur=20.0,
    discount_eur=10.0,
)


def estimate_redesign(
    *,
    baseline_orders: float,
    baseline_gmv: float,
    baseline_margin: float,
    baseline_discount: float,
    old: CampaignOffer,
    new: CampaignOffer,
    elasticity_orders: float = 0.55,
    elasticity_basket: float = 0.25,
) -> dict[str, Any]:
    """
    Lightweight what-if for campaign redesign.

    Not an optimizer. a stage-friendly estimator:
    - deeper discounts / lower thresholds → more orders, worse margin
    - higher thresholds / smaller discounts → fewer incremental orders, better margin
    """
    old_value = old.discount_eur / max(old.min_basket_eur, 1.0)
    new_value = new.discount_eur / max(new.min_basket_eur, 1.0)
    if new.percent_off is not None:
        new_value = min(
            new.percent_off,
            (new.discount_cap_eur or 1e9) / max(new.min_basket_eur, 1.0),
        )

    relative_generosity = new_value / max(old_value, 1e-6)
    # Order response scales sub-linearly with generosity.
    order_mult = 1.0 + elasticity_orders * (relative_generosity - 1.0)
    # Higher minimum basket lifts average basket slightly.
    basket_mult = 1.0 + elasticity_basket * (
        (new.min_basket_eur / max(old.min_basket_eur, 1.0)) - 1.0
    )

    est_orders = baseline_orders * max(order_mult, 0.5)
    est_gmv = baseline_gmv * max(order_mult * basket_mult, 0.5)

    # Discount cost roughly tracks redeemed orders × effective discount.
    effective_discount = new.discount_eur
    if new.percent_off is not None:
        effective_discount = min(
            new.percent_off * new.min_basket_eur,
            new.discount_cap_eur or 1e9,
        )
    # Scale redemption intensity with generosity.
    redemption_mult = max(relative_generosity, 0.3)
    est_discount = baseline_discount * redemption_mult * (est_orders / max(baseline_orders, 1))

    # Margin ≈ baseline margin scaled by order growth, minus extra discount burden,
    # plus basket-quality uplift.
    volume_margin = baseline_margin * (est_orders / max(baseline_orders, 1))
    discount_delta = est_discount - baseline_discount
    basket_bonus = baseline_gmv * (basket_mult - 1.0) * 0.22
    est_margin = volume_margin - discount_delta + basket_bonus

    return {
        "offer": new,
        "orders": est_orders,
        "gross_revenue": est_gmv,
        "discount_cost": est_discount,
        "contribution_margin": est_margin,
        "orders_pct_vs_baseline": est_orders / baseline_orders - 1.0,
        "revenue_pct_vs_baseline": est_gmv / baseline_gmv - 1.0,
        "discount_pct_vs_baseline": est_discount / max(baseline_discount, 1) - 1.0,
        "margin_pct_vs_baseline": est_margin / max(baseline_margin, 1) - 1.0,
        "margin_vs_old_campaign": est_margin - baseline_margin,
    }
