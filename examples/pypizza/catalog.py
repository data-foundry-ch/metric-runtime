from __future__ import annotations

from metric_runtime.models import (
    DetectorConfig,
    Directionality,
    Formula,
    ImpactModel,
    KPIDefinition,
    Measure,
    SupportRequirement,
)


def build_catalog() -> dict[str, KPIDefinition]:
    """
    Semantic business network for the Great Lunch talk.

    Tree (one parent edge per KPI) with Profit at the center:

        weekend_profit
        ├── revenue ………………… marketing (left)
        │ └── orders
        │ ├── opportunities
        │ ├── marketing_leads
        │ └── new_customers
        ├── profit_margin ……… finance (right)
        │ ├── average_order_value → one KPI per basket band
        │ └── average_cost_per_order → discount / delivery cost
        └── average_delivery_time … operations (bottom)
        └── late_delivery_rate
    """
    dims = (
        "city",
        "meal_period",
        "customer_type",
        "basket_band",
        "channel",
        "campaign",
    )
    seasonal = DetectorConfig(baseline_weeks=6, z_threshold=2.2, min_relative_change=0.06)
    sharp = DetectorConfig(baseline_weeks=6, z_threshold=2.0, min_relative_change=0.10)
    # Weekend lunch is lower volume, so the outcome metric gets a slightly
    # lower z bar. otherwise a real profit hit hides inside Friday's noise.
    outcome = DetectorConfig(baseline_weeks=6, z_threshold=2.0, min_relative_change=0.06)

    metrics = [
        # ── Center ────────────────────────────────────────────────────────
        KPIDefinition(
            name="weekend_profit",
            label="Weekend Profit",
            description=(
                "Contribution profit Finance reviews on Monday morning "
                "(net revenue − restaurant payout − delivery − payment − discount)."
            ),
            owner="Finance",
            formula=Formula(kind="sum", measure=Measure.WEEKEND_PROFIT),
            dimensions=dims,
            # One trunk per side. no multi-parent hairball.
            dependencies=("revenue", "profit_margin", "average_delivery_time"),
            directionality=Directionality.LOWER_IS_BAD,
            detector=outcome,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=40),
            impact=ImpactModel(kind="margin_delta"),
            unit="eur",
            graph_ring=0,
            graph_side="center",
        ),
        # ── Marketing trunk (left) ────────────────────────────────────────
        KPIDefinition(
            name="revenue",
            label="Revenue",
            description="Gross order value before discounts.",
            owner="Commercial",
            formula=Formula(kind="sum", measure=Measure.GROSS_ORDER_VALUE),
            dimensions=dims,
            dependencies=("orders",),
            directionality=Directionality.TWO_SIDED,
            detector=seasonal,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=30),
            impact=ImpactModel(kind="revenue_delta"),
            unit="eur",
            graph_ring=1,
            graph_side="marketing",
        ),
        KPIDefinition(
            name="orders",
            label="Orders",
            description="Completed food-delivery orders.",
            owner="Growth",
            formula=Formula(kind="sum", measure=Measure.ORDERS),
            dimensions=dims,
            dependencies=("opportunities", "new_customers", "marketing_leads"),
            directionality=Directionality.TWO_SIDED,
            detector=seasonal,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=30),
            impact=ImpactModel(kind="orders_delta"),
            unit="count",
            graph_ring=2,
            graph_side="marketing",
        ),
        KPIDefinition(
            name="opportunities",
            label="Opportunities",
            description=("High-intent ordering opportunities (qualified demand)."),
            owner="Growth",
            formula=Formula(kind="sum", measure=Measure.OPPORTUNITIES),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.TWO_SIDED,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=3.2, min_relative_change=0.18),
            support=SupportRequirement(measure=Measure.OPPORTUNITIES, minimum=40),
            impact=ImpactModel(kind="none"),
            unit="count",
            graph_ring=3,
            graph_side="marketing",
        ),
        KPIDefinition(
            name="marketing_leads",
            label="Marketing Leads",
            description="Top-of-funnel acquisition leads into the ordering funnel.",
            owner="Growth",
            formula=Formula(kind="sum", measure=Measure.LEADS),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.TWO_SIDED,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=3.2, min_relative_change=0.18),
            support=SupportRequirement(measure=Measure.LEADS, minimum=50),
            impact=ImpactModel(kind="none"),
            unit="count",
            graph_ring=3,
            graph_side="marketing",
        ),
        KPIDefinition(
            name="new_customers",
            label="New Customers",
            description="Orders from first-time customers. Marketing's acquisition win.",
            owner="Growth",
            formula=Formula(kind="sum", measure=Measure.NEW_CUSTOMER_ORDERS),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.TWO_SIDED,
            detector=seasonal,
            support=SupportRequirement(measure=Measure.NEW_CUSTOMER_ORDERS, minimum=20),
            impact=ImpactModel(kind="none"),
            unit="count",
            graph_ring=3,
            graph_side="marketing",
        ),
        # ── Finance trunk (right) ─────────────────────────────────────────
        KPIDefinition(
            name="profit_margin",
            label="Profit Margin",
            description="Weekend profit divided by revenue.",
            owner="Finance / Commercial Strategy",
            formula=Formula(
                kind="ratio",
                numerator=Measure.WEEKEND_PROFIT,
                denominator=Measure.GROSS_ORDER_VALUE,
            ),
            dimensions=dims,
            dependencies=("average_order_value", "average_cost_per_order"),
            directionality=Directionality.LOWER_IS_BAD,
            detector=sharp,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=40),
            impact=ImpactModel(kind="margin_delta"),
            unit="ratio",
            graph_ring=1,
            graph_side="finance",
        ),
        KPIDefinition(
            name="average_order_value",
            label="Average Order Size",
            description="Gross order value per completed order.",
            owner="Commercial Growth",
            formula=Formula(
                kind="ratio",
                numerator=Measure.GROSS_ORDER_VALUE,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            # The full basket mix. average order size is just its centre.
            dependencies=(
                "basket_share_under_20",
                "basket_threshold_concentration",
                "basket_share_25_29",
                "basket_share_30_39",
                "basket_share_40_plus",
            ),
            directionality=Directionality.LOWER_IS_BAD,
            detector=sharp,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=30),
            impact=ImpactModel(kind="none"),
            unit="eur",
            graph_ring=2,
            graph_side="finance",
        ),
        KPIDefinition(
            name="average_cost_per_order",
            label="Average Cost / Order",
            description=(
                "Platform cost per order: promo discount + delivery + payment "
                "processing (excludes restaurant payout)."
            ),
            owner="Commercial Operations",
            formula=Formula(
                kind="ratio",
                numerator=Measure.PLATFORM_COST,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(
                "discount_cost_per_order",
                "delivery_cost_per_order",
            ),
            directionality=Directionality.HIGHER_IS_BAD,
            detector=sharp,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=30),
            impact=ImpactModel(kind="cost_delta"),
            unit="eur",
            graph_ring=2,
            graph_side="finance",
        ),
        KPIDefinition(
            name="basket_threshold_concentration",
            label="Baskets €20–24.99",
            description=(
                "Share of orders with basket €20–€24.99, just above the "
                "Great Lunch €20 eligibility cliff."
            ),
            owner="Commercial Growth / Promotions",
            formula=Formula(
                kind="ratio",
                numerator=Measure.THRESHOLD_BAND_ORDERS,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(),
            # The one band with a direction: piling up on the cliff is what
            # pages Promotions. In the graph it still reads as a mix slice,
            # so it is coloured by movement like its siblings.
            directionality=Directionality.HIGHER_IS_BAD,
            graph_directionality=Directionality.TWO_SIDED,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=1.8, min_relative_change=0.15),
            support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
            impact=ImpactModel(kind="none"),
            unit="ratio",
            graph_ring=3,
            graph_side="finance",
        ),
        *(
            KPIDefinition(
                name=f"basket_share_{_slug}",
                label=f"Baskets {_band_label}",
                description=(
                    f"Share of orders in the {_band_label} basket band: one "
                    "slice of the basket mix around the €20 cliff."
                ),
                owner="Commercial Growth / Promotions",
                formula=Formula(
                    kind="ratio",
                    numerator=_measure,
                    denominator=Measure.ORDERS,
                ),
                dimensions=dims,
                dependencies=(),
                # Two-sided on purpose: basket mix is evidence, not a target.
                directionality=Directionality.TWO_SIDED,
                detector=DetectorConfig(
                    baseline_weeks=6, z_threshold=1.8, min_relative_change=0.15
                ),
                support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
                impact=ImpactModel(kind="none"),
                unit="ratio",
                graph_ring=3,
                graph_side="finance",
            )
            for _slug, _band_label, _measure in (
                ("under_20", "under €20", Measure.BAND_UNDER_20_ORDERS),
                ("25_29", "€25–29.99", Measure.BAND_25_29_ORDERS),
                ("30_39", "€30–39.99", Measure.BAND_30_39_ORDERS),
                ("40_plus", "€40+", Measure.BAND_40_PLUS_ORDERS),
            )
        ),
        KPIDefinition(
            name="discount_cost_per_order",
            label="Discount Cost / Order",
            description="Average promotional discount per order (€10 on redemptions).",
            owner="Commercial Growth / Promotions",
            formula=Formula(
                kind="ratio",
                numerator=Measure.DISCOUNT_COST,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.HIGHER_IS_BAD,
            detector=sharp,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
            impact=ImpactModel(kind="cost_delta"),
            unit="eur",
            graph_ring=3,
            graph_side="finance",
        ),
        KPIDefinition(
            name="delivery_cost_per_order",
            label="Delivery Cost / Order",
            description="Courier cost per completed order.",
            owner="Operations",
            formula=Formula(
                kind="ratio",
                numerator=Measure.DELIVERY_COST,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.HIGHER_IS_BAD,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=3.0, min_relative_change=0.12),
            support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
            impact=ImpactModel(kind="cost_delta"),
            unit="eur",
            graph_ring=3,
            graph_side="finance",
        ),
        # ── Operations trunk (bottom). holistic context, yellow warnings ─
        KPIDefinition(
            name="average_delivery_time",
            label="Avg Time to Delivery",
            description="Average minutes from order placed to customer delivery.",
            owner="Operations",
            formula=Formula(
                kind="ratio",
                numerator=Measure.DELIVERY_MINUTES,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=("late_delivery_rate",),
            directionality=Directionality.HIGHER_IS_BAD,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=2.0, min_relative_change=0.08),
            support=SupportRequirement(measure=Measure.ORDERS, minimum=30),
            impact=ImpactModel(kind="none"),
            unit="count",
            graph_ring=1,
            graph_side="operations",
            graph_directionality="neutral",
        ),
        KPIDefinition(
            name="late_delivery_rate",
            label="Late Delivery Rate",
            description="Share of orders delivered after the promised ETA window.",
            owner="Operations",
            formula=Formula(
                kind="ratio",
                numerator=Measure.LATE_ORDERS,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.HIGHER_IS_BAD,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=2.0, min_relative_change=0.10),
            support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
            impact=ImpactModel(kind="none"),
            unit="ratio",
            graph_ring=2,
            graph_side="operations",
            graph_directionality="neutral",
        ),
        # ── Off-talk helpers (still in catalog, not in talk graph) ────────
        KPIDefinition(
            name="restaurant_payout_per_order",
            label="Restaurant Payout / Order",
            description="Average restaurant payout per order.",
            owner="Marketplace",
            formula=Formula(
                kind="ratio",
                numerator=Measure.RESTAURANT_PAYOUT,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.TWO_SIDED,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=3.0, min_relative_change=0.12),
            support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
            impact=ImpactModel(kind="cost_delta"),
            unit="eur",
            graph_ring=3,
            graph_side="finance",
        ),
        KPIDefinition(
            name="restaurant_leads",
            label="Restaurant Leads",
            description=(
                "Marketplace restaurant-acquisition leads. "
                "B2B pipeline. orthogonal to lunch promo economics."
            ),
            owner="Marketplace",
            formula=Formula(kind="sum", measure=Measure.RESTAURANT_LEADS),
            dimensions=("city", "meal_period", "channel"),
            dependencies=(),
            directionality=Directionality.TWO_SIDED,
            detector=DetectorConfig(baseline_weeks=6, z_threshold=3.5, min_relative_change=0.25),
            support=SupportRequirement(measure=Measure.RESTAURANT_LEADS, minimum=5),
            impact=ImpactModel(kind="none"),
            unit="count",
            graph_ring=2,
            graph_side="marketing",
        ),
        KPIDefinition(
            name="conversion_rate",
            label="Conversion Rate",
            description="Orders divided by sessions.",
            owner="Growth",
            formula=Formula(
                kind="ratio",
                numerator=Measure.ORDERS,
                denominator=Measure.SESSIONS,
            ),
            dimensions=dims,
            dependencies=("orders",),
            directionality=Directionality.TWO_SIDED,
            detector=seasonal,
            support=SupportRequirement(measure=Measure.SESSIONS, minimum=80),
            impact=ImpactModel(kind="none"),
            unit="ratio",
            graph_ring=4,
            graph_side="marketing",
        ),
        KPIDefinition(
            name="promo_redemption_rate",
            label="Promo Redemption Rate",
            description="Share of orders that redeemed a promotion.",
            owner="Commercial Growth / Promotions",
            formula=Formula(
                kind="ratio",
                numerator=Measure.PROMO_ORDERS,
                denominator=Measure.ORDERS,
            ),
            dimensions=dims,
            dependencies=(),
            directionality=Directionality.HIGHER_IS_BAD,
            detector=sharp,
            support=SupportRequirement(measure=Measure.ORDERS, minimum=25),
            impact=ImpactModel(kind="none"),
            unit="ratio",
            graph_ring=4,
            graph_side="finance",
        ),
    ]
    return {m.name: m for m in metrics}


# Visible talk graph: marketing left, finance right, operations bottom.
TALK_GRAPH_METRICS = (
    "weekend_profit",
    # marketing
    "revenue",
    "orders",
    "opportunities",
    "marketing_leads",
    "new_customers",
    # finance
    "profit_margin",
    "average_order_value",
    "average_cost_per_order",
    "basket_share_under_20",
    "basket_threshold_concentration",
    "basket_share_25_29",
    "basket_share_30_39",
    "basket_share_40_plus",
    "discount_cost_per_order",
    "delivery_cost_per_order",
    # operations
    "average_delivery_time",
    "late_delivery_rate",
)
