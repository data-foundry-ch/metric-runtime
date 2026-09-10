"""
Generate deterministic PyPizza half-hourly warehouse data for the Great Lunch demo.

Target campaign shape (Amsterdam weekend lunch vs prior weekends):
  orders ↑ ~25–35%, revenue ↑ ~10–20%, weekend profit ↓ ~10–25%,
  profit margin ↓↓, AOV ↓, platform cost/order ↑, threshold concentration ↑↑
  marketing leads / opportunities / restaurant leads stay roughly flat.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 42
START = pd.Timestamp("2026-02-16 00:00:00")
# Runs through the Monday after the campaign. that is when the humans notice.
END = pd.Timestamp("2026-05-18 23:30:00")
CAMPAIGN_START = pd.Timestamp("2026-05-15 00:00:00")  # Friday
CAMPAIGN_END = pd.Timestamp("2026-05-17 23:30:00")  # Sunday

CITIES = {
    "Amsterdam": {"share": 0.42, "aov": 1.05, "lunch": 1.18},
    "Rotterdam": {"share": 0.22, "aov": 0.98, "lunch": 1.05},
    "Utrecht": {"share": 0.18, "aov": 1.00, "lunch": 1.08},
    "The Hague": {"share": 0.18, "aov": 0.97, "lunch": 1.02},
}
CHANNELS = {
    "organic": {"share": 0.34, "conv": 1.05, "new": 0.85},
    "paid_search": {"share": 0.30, "conv": 0.95, "new": 1.35},
    "email": {"share": 0.16, "conv": 1.15, "new": 0.70},
    "direct": {"share": 0.20, "conv": 1.02, "new": 0.90},
}
DEVICES = {"mobile": 0.72, "desktop": 0.28}
CUSTOMER_TYPES = {"new": 0.28, "returning": 0.72}
CATEGORIES = {
    "pizza": 0.34,
    "burgers": 0.18,
    "healthy": 0.16,
    "asian": 0.20,
    "other": 0.12,
}

BASE_BASKET = {
    "<20": 0.14,
    "20-24.99": 0.18,
    "25-29.99": 0.28,
    "30-39.99": 0.26,
    "40+": 0.14,
}
RETURNING_BASKET = {
    "<20": 0.10,
    "20-24.99": 0.16,
    "25-29.99": 0.28,
    "30-39.99": 0.28,
    "40+": 0.18,
}
# Cliff just above €20. visible on stage, not total wipeout.
CAMPAIGN_BASKET = {
    "<20": 0.08,
    "20-24.99": 0.42,
    "25-29.99": 0.27,
    "30-39.99": 0.15,
    "40+": 0.08,
}
BASKET_MID = {
    "<20": 16.0,
    "20-24.99": 22.5,
    "25-29.99": 27.5,
    "30-39.99": 34.5,
    "40+": 48.0,
}
BANDS = list(BASE_BASKET)
# One order-count column per basket band, so band mix is a first-class
# measure instead of something you can only reach through a dimension filter.
BAND_COLUMNS = {
    "<20": "band_under_20_orders",
    "20-24.99": "threshold_band_orders",
    "25-29.99": "band_25_29_orders",
    "30-39.99": "band_30_39_orders",
    "40+": "band_40_plus_orders",
}


def meal_period(ts: pd.Timestamp) -> str:
    h = ts.hour + ts.minute / 60
    if 6 <= h < 10.5:
        return "breakfast"
    if 10.5 <= h < 14.5:
        return "lunch"
    if 14.5 <= h < 17.5:
        return "afternoon"
    if 17.5 <= h < 22:
        return "dinner"
    return "late_night"


def demand_factor(ts: pd.Timestamp) -> float:
    period = meal_period(ts)
    weekday = ts.weekday()
    base = {
        "breakfast": 0.35,
        "lunch": 0.85,
        "afternoon": 0.40,
        "dinner": 1.25,
        "late_night": 0.28,
    }[period]
    if period == "lunch" and weekday < 5:
        base *= 1.15
    if period == "dinner" and weekday >= 4:
        base *= 1.22
    if period == "lunch" and weekday >= 5:
        base *= 0.72
    if period == "lunch":
        h = ts.hour + ts.minute / 60
        base *= 0.75 + 0.55 * np.exp(-(((h - 12.5) / 1.4) ** 2))
    return base


def is_great_lunch(ts: pd.Timestamp, city: str, period: str) -> bool:
    if city != "Amsterdam" or period != "lunch":
        return False
    if ts < CAMPAIGN_START or ts > CAMPAIGN_END:
        return False
    if ts.weekday() not in (4, 5, 6):  # Friday, Saturday, Sunday
        return False
    h = ts.hour + ts.minute / 60
    return 11.5 <= h < 14.0


def _probs(gl: bool, ctype: str) -> np.ndarray:
    if gl:
        src = CAMPAIGN_BASKET
    elif ctype == "returning":
        src = RETURNING_BASKET
    else:
        src = BASE_BASKET
    p = np.array([src[b] for b in BANDS], dtype=float)
    return p / p.sum()


def _emit_band_row(
    *,
    base: dict[str, object],
    band: str,
    orders: int,
    sessions_share: float,
    gl: bool,
    ctype: str,
    ccfg: dict,
    rng: np.random.Generator,
) -> dict[str, object]:
    mid = BASKET_MID[band] * ccfg["aov"]
    if ctype == "returning":
        mid *= 1.08
    # Slight pull toward the €20 threshold during Great Lunch.
    if gl and band == "20-24.99":
        mid = 22.4

    gmv = float(orders * mid * max(rng.lognormal(0, 0.025), 0.90))
    promo_orders = 0
    discount = 0.0
    campaign = "none"
    if gl and orders > 0:
        campaign = "great_lunch"
        if band == "<20":
            redeem_p, disc_per = 0.02, 0.0
        else:
            # Auto-applied at checkout, so most eligible baskets take it.
            redeem_p = 0.52 if ctype == "new" else 0.47
            disc_per = 5.0
        promo_orders = int(rng.binomial(orders, redeem_p))
        discount = float(promo_orders * disc_per)
    elif orders > 0:
        # Steady organic promo baseline. keeps discount/order % realistic.
        organic_p = 0.18 if ctype == "new" else 0.12
        promo_orders = int(rng.binomial(orders, organic_p))
        discount = float(promo_orders * rng.uniform(3.5, 5.0))

    delivery_fee = float(orders * rng.uniform(1.9, 2.4))
    # Restaurant ~70% of GMV. shrinks with smaller baskets.
    restaurant = float(gmv * rng.uniform(0.68, 0.72))
    # Delivery is largely fixed per order. hurts small baskets.
    delivery_cost = float(orders * rng.uniform(4.6, 5.2))
    payment = float(gmv * rng.uniform(0.013, 0.016))
    refunds = float(gmv * abs(rng.normal(0.010, 0.002)))
    net_revenue = gmv + delivery_fee - refunds
    platform_cost = discount + delivery_cost + payment
    variable_cost = platform_cost + restaurant
    weekend_profit = net_revenue - variable_cost

    # Ops: mild lunch-rush degradation during Great Lunch (yellow, not the story).
    if orders > 0:
        mean_mins = 31.0
        late_p = 0.09
        if gl:
            mean_mins = 37.5
            late_p = 0.16
        elif base.get("meal_period") == "lunch":
            mean_mins = 33.0
            late_p = 0.11
        delivery_minutes = float(orders * mean_mins * max(rng.lognormal(0, 0.04), 0.88))
        late_orders = int(rng.binomial(orders, late_p))
    else:
        delivery_minutes = 0.0
        late_orders = 0

    band_orders = {column: 0 for column in BAND_COLUMNS.values()}
    band_orders[BAND_COLUMNS[band]] = orders
    scale = sessions_share

    return {
        **base,
        "campaign": campaign,
        "basket_band": band,
        **band_orders,
        "sessions": max(int(round(base["sessions"] * scale)), 0),  # type: ignore[operator]
        "menu_views": int(round(base["menu_views"] * scale)),  # type: ignore[operator]
        "cart_starts": int(round(base["cart_starts"] * scale)),  # type: ignore[operator]
        "checkout_starts": int(round(base["checkout_starts"] * scale)),  # type: ignore[operator]
        "leads": max(int(round(base["leads"] * scale)), 0),  # type: ignore[operator]
        "opportunities": max(int(round(base["opportunities"] * scale)), 0),  # type: ignore[operator]
        "restaurant_leads": max(int(round(base["restaurant_leads"] * scale)), 0),  # type: ignore[operator]
        "orders": orders,
        "promo_orders": promo_orders,
        "new_customer_orders": orders if ctype == "new" else 0,
        "gross_order_value_eur": gmv,
        "discount_cost_eur": discount,
        "delivery_fee_revenue_eur": delivery_fee,
        "restaurant_payout_eur": restaurant,
        "delivery_cost_eur": delivery_cost,
        "payment_processing_cost_eur": payment,
        "refunds_eur": refunds,
        "net_revenue_eur": net_revenue,
        "platform_cost_eur": platform_cost,
        "variable_cost_eur": variable_cost,
        "weekend_profit_eur": weekend_profit,
        "delivery_minutes": delivery_minutes,
        "late_orders": late_orders,
        # keep legacy alias column for any leftover queries
        "contribution_margin_eur": weekend_profit,
    }


def build_frame() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    timestamps = pd.date_range(START, END, freq="30min")
    rows: list[dict[str, object]] = []

    for ts in timestamps:
        period = meal_period(ts)
        weeks = (ts - START).days / 7.0
        growth = 1.0 + 0.003 * weeks
        total_sessions = 3800 * demand_factor(ts) * growth

        for city, ccfg in CITIES.items():
            for channel, ch in CHANNELS.items():
                for device, dshare in DEVICES.items():
                    for ctype, ctshare in CUSTOMER_TYPES.items():
                        for cat, catshare in CATEGORIES.items():
                            gl = is_great_lunch(ts, city, period)

                            ctype_w = ctshare * (
                                ch["new"] if ctype == "new" else (2.0 - min(ch["new"], 1.6))
                            )
                            expected_base = (
                                total_sessions
                                * ccfg["share"]
                                * ch["share"]
                                * dshare
                                * ctype_w
                                * catshare
                                * 0.55
                            )
                            if period == "lunch":
                                expected_base *= ccfg["lunch"]

                            # Volume uplift. orders ↑, revenue still only modestly ↑.
                            uplift = 1.0
                            if gl:
                                uplift = 1.30
                                if ctype == "new":
                                    uplift *= 1.16
                                if channel == "paid_search":
                                    uplift *= 1.12
                            expected = expected_base * uplift

                            sessions = int(rng.poisson(max(expected, 0.35)))
                            if sessions == 0:
                                continue

                            menu_views = int(
                                rng.binomial(
                                    sessions,
                                    float(np.clip(0.72 + rng.normal(0, 0.02), 0.45, 0.95)),
                                )
                            )
                            cart_starts = int(
                                rng.binomial(
                                    menu_views,
                                    float(np.clip(0.38 * ch["conv"], 0.12, 0.7)),
                                )
                            )
                            checkout_starts = int(
                                rng.binomial(
                                    cart_starts,
                                    float(np.clip(0.70 + rng.normal(0, 0.02), 0.45, 0.9)),
                                )
                            )

                            # Funnel KPIs from underlying demand. not Great Lunch inflated.
                            lead_base = max(expected_base, 0.4)
                            leads = int(rng.poisson(lead_base * 0.20))
                            opportunities = int(
                                rng.binomial(
                                    max(leads, 1),
                                    float(np.clip(0.38 * ch["conv"], 0.2, 0.55)),
                                )
                            )
                            restaurant_leads = int(rng.poisson(0.12 + 0.55 * ccfg["share"]))

                            conv = 0.055 * ch["conv"]
                            conv *= 1.35 if ctype == "returning" else 0.78
                            if device == "mobile":
                                conv *= 0.96
                            if gl:
                                conv *= 1.12 if ctype == "new" else 1.06
                            conv = float(np.clip(conv, 0.01, 0.35))
                            orders = int(rng.binomial(sessions, conv))

                            base = {
                                "ts": ts,
                                "city": city,
                                "day_of_week": int(ts.weekday()),
                                "meal_period": period,
                                "channel": channel,
                                "customer_type": ctype,
                                "device": device,
                                "restaurant_category": cat,
                                "sessions": sessions,
                                "menu_views": menu_views,
                                "cart_starts": cart_starts,
                                "checkout_starts": checkout_starts,
                                "leads": leads,
                                "opportunities": opportunities,
                                "restaurant_leads": restaurant_leads,
                            }

                            if orders <= 0:
                                rows.append(
                                    _emit_band_row(
                                        base=base,
                                        band="25-29.99",
                                        orders=0,
                                        sessions_share=1.0,
                                        gl=gl,
                                        ctype=ctype,
                                        ccfg=ccfg,
                                        rng=rng,
                                    )
                                )
                                continue

                            p = _probs(gl, ctype)
                            split = rng.multinomial(orders, p)
                            for band, n_ord in zip(BANDS, split):
                                if n_ord <= 0:
                                    continue
                                rows.append(
                                    _emit_band_row(
                                        base=base,
                                        band=band,
                                        orders=int(n_ord),
                                        sessions_share=n_ord / orders,
                                        gl=gl,
                                        ctype=ctype,
                                        ccfg=ccfg,
                                        rng=rng,
                                    )
                                )

    return pd.DataFrame(rows)


def build_events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "event_ts": pd.Timestamp("2026-05-15 09:00:00"),
                "event_type": "campaign",
                "title": "Great Lunch campaign launched",
                "detail": ("Amsterdam weekend lunch 11:30–14:00 · €5 off orders of €20 or more"),
                "scope_city": "Amsterdam",
                "scope_meal_period": "lunch",
                "relevance_tags": "promo,discount,amsterdam,lunch",
            },
            {
                "event_ts": pd.Timestamp("2026-05-15 08:30:00"),
                "event_type": "marketing",
                "title": "Paid-search budget increased",
                "detail": "Lunch keywords +35% in Amsterdam geo-target",
                "scope_city": "Amsterdam",
                "scope_meal_period": "lunch",
                "relevance_tags": "paid_search,amsterdam",
            },
            {
                "event_ts": pd.Timestamp("2026-05-12 14:00:00"),
                "event_type": "marketplace",
                "title": "Restaurant acquisition wave",
                "detail": "42 new healthy restaurants onboarded in Utrecht",
                "scope_city": "Utrecht",
                "scope_meal_period": None,
                "relevance_tags": "restaurants,utrecht",
            },
            {
                "event_ts": pd.Timestamp("2026-05-16 22:15:00"),
                "event_type": "deployment",
                "title": "Courier app 6.4.1",
                "detail": "Routine courier-app release · all cities",
                "scope_city": None,
                "scope_meal_period": None,
                "relevance_tags": "deploy,courier",
            },
        ]
    )


def main() -> None:
    root = Path(__file__).resolve().parent
    out = root / "data" / "pypizza.duckdb"
    out.parent.mkdir(parents=True, exist_ok=True)

    print("Building PyPizza Great Lunch dataset...")
    frame = build_frame()
    events = build_events()
    print(f"  fact rows: {len(frame):,}")
    print(f"  range: {frame['ts'].min()} -> {frame['ts'].max()}")

    gl = frame[frame["campaign"] == "great_lunch"]
    if len(gl):
        print(
            f"  Great Lunch orders: {gl['orders'].sum():,.0f} · "
            f"discount EUR {gl['discount_cost_eur'].sum():,.0f} · "
            f"profit EUR {gl['weekend_profit_eur'].sum():,.0f}"
        )
        band_share = gl.groupby("basket_band")["orders"].sum() / gl["orders"].sum()
        print("  basket mix during Great Lunch:")
        print(band_share.round(3).to_string())

    if out.exists():
        out.unlink()
    con = duckdb.connect(str(out))
    con.register("frame_df", frame)
    con.register("events_df", events)
    con.execute("CREATE TABLE pypizza_halfhourly AS SELECT * FROM frame_df")
    con.execute("CREATE INDEX idx_pypizza_ts ON pypizza_halfhourly(ts)")
    con.execute("CREATE TABLE business_events AS SELECT * FROM events_df")
    con.close()
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
