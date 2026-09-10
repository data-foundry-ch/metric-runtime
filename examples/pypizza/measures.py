"""PyPizza measure identifiers (example domain — not part of core)."""

from __future__ import annotations

from enum import Enum


class Measure(str, Enum):
    """Warehouse columns used by the PyPizza Great Lunch catalog."""

    SESSIONS = "sessions"
    ORDERS = "orders"
    PROMO_ORDERS = "promo_orders"
    THRESHOLD_BAND_ORDERS = "threshold_band_orders"
    BAND_UNDER_20_ORDERS = "band_under_20_orders"
    BAND_25_29_ORDERS = "band_25_29_orders"
    BAND_30_39_ORDERS = "band_30_39_orders"
    BAND_40_PLUS_ORDERS = "band_40_plus_orders"
    NEW_CUSTOMER_ORDERS = "new_customer_orders"
    LEADS = "leads"
    OPPORTUNITIES = "opportunities"
    RESTAURANT_LEADS = "restaurant_leads"
    GROSS_ORDER_VALUE = "gross_order_value_eur"
    DISCOUNT_COST = "discount_cost_eur"
    DELIVERY_FEE_REVENUE = "delivery_fee_revenue_eur"
    RESTAURANT_PAYOUT = "restaurant_payout_eur"
    DELIVERY_COST = "delivery_cost_eur"
    PAYMENT_PROCESSING_COST = "payment_processing_cost_eur"
    REFUNDS = "refunds_eur"
    NET_REVENUE = "net_revenue_eur"
    VARIABLE_COST = "variable_cost_eur"
    PLATFORM_COST = "platform_cost_eur"
    WEEKEND_PROFIT = "weekend_profit_eur"
    DELIVERY_MINUTES = "delivery_minutes"
    LATE_ORDERS = "late_orders"


# Dimensions valid for PyPizza fact table filters.
PYPIZZA_DIMENSIONS = (
    "city",
    "meal_period",
    "customer_type",
    "basket_band",
    "channel",
    "restaurant_category",
    "device",
    "campaign",
    "day_of_week",
)

FACT_TABLE = "pypizza_halfhourly"
