"""Reorder point, batch size, capacity, and truck/mail decision math.

All formulas tie back to the Jacobs game's specific economics:
- Price = $1450/drum, fulfilment = $150/drum so margin = $1300/drum sold.
- Lost order = 100% lost (customer goes to competitor, no backorder).
- Holding cost = $100/drum/year ≈ $0.274/drum/day from production onwards.
- Batch cost = $1500 + $1000 × drums.
- Truck = $15,000 fixed, 7-day transit; Mail = $150/drum, 1-day transit.
- Capacity expansion = $50,000 per drum/day, 90-day lead time, irreversible.
- Game ends day 1460 (everything obsolete).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np


# --- Service-level / Z-score ------------------------------------------------

# Acklam's inverse normal CDF approximation. Max relative error ~1.15e-9
# across (0,1). Pure-math, no scipy dependency.
_ACKLAM_A = [-3.969683028665376e+01,  2.209460984245205e+02,
             -2.759285104469687e+02,  1.383577518672690e+02,
             -3.066479806614716e+01,  2.506628277459239e+00]
_ACKLAM_B = [-5.447609879822406e+01,  1.615858368580409e+02,
             -1.556989798598866e+02,  6.680131188771972e+01,
             -1.328068155288572e+01]
_ACKLAM_C = [-7.784894002430293e-03, -3.223964580411365e-01,
             -2.400758277161838e+00, -2.549732539343734e+00,
              4.374664141464968e+00,  2.938163982698783e+00]
_ACKLAM_D = [ 7.784695709041462e-03,  3.224671290700398e-01,
              2.445134137142996e+00,  3.754408661907416e+00]


def _acklam_inv_norm(p: float) -> float:
    """Inverse standard normal CDF (quantile function)."""
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((_ACKLAM_C[0]*q+_ACKLAM_C[1])*q+_ACKLAM_C[2])*q+_ACKLAM_C[3])*q+_ACKLAM_C[4])*q+_ACKLAM_C[5]) / \
               ((((_ACKLAM_D[0]*q+_ACKLAM_D[1])*q+_ACKLAM_D[2])*q+_ACKLAM_D[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q*q
        return (((((_ACKLAM_A[0]*r+_ACKLAM_A[1])*r+_ACKLAM_A[2])*r+_ACKLAM_A[3])*r+_ACKLAM_A[4])*r+_ACKLAM_A[5])*q / \
               (((((_ACKLAM_B[0]*r+_ACKLAM_B[1])*r+_ACKLAM_B[2])*r+_ACKLAM_B[3])*r+_ACKLAM_B[4])*r+1)
    q = math.sqrt(-2.0 * math.log(1 - p))
    return -(((((_ACKLAM_C[0]*q+_ACKLAM_C[1])*q+_ACKLAM_C[2])*q+_ACKLAM_C[3])*q+_ACKLAM_C[4])*q+_ACKLAM_C[5]) / \
            ((((_ACKLAM_D[0]*q+_ACKLAM_D[1])*q+_ACKLAM_D[2])*q+_ACKLAM_D[3])*q+1)


def z_for_service_level(service_level: float) -> float:
    """Inverse normal CDF for a target in-stock probability."""
    sl = max(min(service_level, 0.9999), 0.50)
    return float(_acklam_inv_norm(sl))


def critical_ratio_service_level(margin_per_drum: float, holding_cost_per_drum_per_day: float,
                                  expected_review_days: float) -> float:
    """Newsvendor critical ratio applied to a lead-time review window.

    SL = margin / (margin + holding_cost * expected_review_days)

    The intuition: if we hold one extra drum, we pay the holding cost across
    the average review window; if we miss a sale, we lose the margin. The
    optimal cycle-service level equates the two.
    """
    h_over_window = max(holding_cost_per_drum_per_day, 0.0) * max(expected_review_days, 1.0)
    return margin_per_drum / (margin_per_drum + h_over_window)


# --- Reorder point ----------------------------------------------------------

@dataclass
class ROPResult:
    """Output of an ROP calculation for a particular day/lookahead."""
    day: int
    lead_time_days: float
    ltd_mean: float        # expected lead-time demand
    ltd_stdev: float       # stdev of lead-time demand
    service_level: float
    z: float
    safety_stock: float
    reorder_point: float
    daily_mean_local: float  # forward-looking avg/day in window


def reorder_point(ltd_mean: float, ltd_stdev: float, service_level: float) -> ROPResult:
    z = z_for_service_level(service_level)
    ss = z * ltd_stdev
    return ROPResult(
        day=-1,
        lead_time_days=-1,
        ltd_mean=ltd_mean,
        ltd_stdev=ltd_stdev,
        service_level=service_level,
        z=z,
        safety_stock=ss,
        reorder_point=ltd_mean + ss,
        daily_mean_local=ltd_mean / max(1.0, 1.0),
    )


def rop_from_forecast(forecast, day: int, lead_time_days: float,
                      service_level: float) -> ROPResult:
    """Compute ROP at a particular day using a DemandForecast (from forecasting.py)."""
    idx = int(day - forecast.days[0])
    if idx < 0 or idx >= len(forecast.days):
        raise ValueError(f"day {day} outside forecast horizon")
    L = int(round(lead_time_days))
    j = min(idx + L, len(forecast.mean))
    ltd_mean = float(forecast.mean[idx:j].sum())
    ltd_var = float((forecast.stdev[idx:j] ** 2).sum())
    ltd_stdev = math.sqrt(ltd_var)
    result = reorder_point(ltd_mean, ltd_stdev, service_level)
    result.day = day
    result.lead_time_days = lead_time_days
    result.daily_mean_local = ltd_mean / max(L, 1)
    return result


# --- Batch size / EOQ -------------------------------------------------------

@dataclass
class BatchResult:
    eoq: float
    truck_break_even: float
    recommended_batch: float
    fills_truck: bool
    note: str


def eoq(annual_demand: float, setup_cost: float, holding_cost_per_unit_per_year: float) -> float:
    """Standard EOQ: sqrt(2 * D * S / h)."""
    if holding_cost_per_unit_per_year <= 0 or annual_demand <= 0:
        return 0.0
    return math.sqrt(2.0 * annual_demand * setup_cost / holding_cost_per_unit_per_year)


def truck_break_even_quantity(truck_fixed_cost: float,
                              mail_per_drum: float,
                              truck_per_drum: float = 0.0) -> float:
    """Minimum batch size at which truck is cheaper than mail."""
    delta = mail_per_drum - truck_per_drum
    return truck_fixed_cost / delta if delta > 0 else float("inf")


def recommend_batch_size(
    forecast_daily_mean: float,
    setup_cost: float,
    holding_cost_per_unit_per_day: float,
    truck_capacity: int = 200,
    truck_fixed_cost: float = 15000,
    mail_per_drum: float = 150,
    days_remaining: int | None = None,
) -> BatchResult:
    """Pick a batch size that balances setup vs holding and considers transport.

    For the Jacobs game the setup cost is $1500 per batch (truck cost is paid
    PER SHIPMENT, not per batch — they're conceptually separate). We use EOQ
    as the unconstrained ideal, then nudge to either fill a truck (200) or
    drop below the truck break-even (so we'd just mail).
    """
    annual_demand = forecast_daily_mean * 365.0
    holding_per_year = holding_cost_per_unit_per_day * 365.0
    q_eoq = eoq(annual_demand, setup_cost, holding_per_year)
    q_be = truck_break_even_quantity(truck_fixed_cost, mail_per_drum)

    # If EOQ is near or above truck capacity, fill the truck.
    if q_eoq >= 0.85 * truck_capacity:
        return BatchResult(
            eoq=q_eoq,
            truck_break_even=q_be,
            recommended_batch=truck_capacity,
            fills_truck=True,
            note="EOQ near/above truck capacity -> fill truck (200 drums).",
        )
    # If EOQ is below the truck break-even, we're mailing. Use EOQ.
    if q_eoq < q_be:
        return BatchResult(
            eoq=q_eoq,
            truck_break_even=q_be,
            recommended_batch=max(1.0, round(q_eoq)),
            fills_truck=False,
            note=f"EOQ {q_eoq:.0f} < truck break-even {q_be:.0f} -> mail.",
        )
    # Otherwise round to EOQ.
    return BatchResult(
        eoq=q_eoq,
        truck_break_even=q_be,
        recommended_batch=max(1.0, round(q_eoq)),
        fills_truck=False,
        note=f"EOQ {q_eoq:.0f} above truck BE {q_be:.0f}; ship by truck.",
    )


# --- Capacity ---------------------------------------------------------------

@dataclass
class CapacityResult:
    current_capacity: float
    peak_daily_demand: float
    recommended_capacity: float
    drums_to_add: float
    expansion_cost: float
    payback_days: float
    note: str


def recommend_capacity(
    current_capacity: float,
    peak_daily_demand: float,
    cost_per_drum_per_day: float,
    lead_time_days: float,
    days_remaining_in_game: int,
    margin_per_drum: float,
    safety_buffer: float = 1.10,
) -> CapacityResult:
    """Recommend capacity that meets peak demand with a small buffer.

    Capacity is irreversible AND has a 90-day lead time. Before recommending
    an expansion we check that the expected uplift in fulfilled drums during
    the post-lead-time remaining window covers the capacity cost.
    """
    target = peak_daily_demand * safety_buffer
    add = max(0.0, target - current_capacity)
    cost = add * cost_per_drum_per_day

    # Days the new capacity will actually be productive.
    productive_days = max(0, days_remaining_in_game - int(round(lead_time_days)))
    expected_uplift_drums = add * productive_days
    expected_gross_margin = expected_uplift_drums * margin_per_drum
    payback_days = (cost / (add * margin_per_drum)) if add > 0 else float("inf")

    if add <= 0:
        note = "Current capacity already exceeds peak demand × buffer."
    elif expected_gross_margin < cost:
        note = (f"Expected margin ${expected_gross_margin:,.0f} < cost ${cost:,.0f} "
                f"after {lead_time_days:.0f}-day lead time -> DO NOT EXPAND.")
        add = 0.0
        cost = 0.0
    else:
        note = (f"Add {add:.1f} drum/day at cost ${cost:,.0f}; "
                f"expected uplift margin ${expected_gross_margin:,.0f}; "
                f"payback ≈ {payback_days:.0f} day-equivalents of full-capacity sales.")

    return CapacityResult(
        current_capacity=current_capacity,
        peak_daily_demand=peak_daily_demand,
        recommended_capacity=current_capacity + add,
        drums_to_add=add,
        expansion_cost=cost,
        payback_days=payback_days,
        note=note,
    )


# --- Top-level decision wrapper --------------------------------------------

@dataclass
class PolicyRecommendation:
    day: int
    reorder_point: float
    batch_size: float
    fills_truck: bool
    use_truck: bool
    target_capacity: float
    expansion_drums: float
    notes: list[str]


def recommend_policy(
    forecast,                       # DemandForecast
    current_day: int,
    lead_time_days: float,
    service_level: float,
    current_capacity: float,
    days_remaining: int,
    policy_cfg: dict,
) -> PolicyRecommendation:
    """End-to-end policy recommendation for a given day."""
    notes = []

    # 1) Reorder point with forward-looking lead-time demand
    rop = rop_from_forecast(forecast, current_day, lead_time_days, service_level)
    notes.append(
        f"ROP day {current_day}: LTD μ={rop.ltd_mean:.1f}, σ={rop.ltd_stdev:.1f}, "
        f"Z={rop.z:.2f}, SS={rop.safety_stock:.1f}, ROP={rop.reorder_point:.1f}"
    )

    # 2) Batch size + transport
    fwd_window = forecast.window(current_day, min(current_day + 30, forecast.days[-1]))
    daily_mean = float(np.mean(fwd_window.mean)) if len(fwd_window.mean) else 0.0
    h_per_day = policy_cfg["holding_cost_per_drum_per_year"] / 365.0
    batch = recommend_batch_size(
        forecast_daily_mean=daily_mean,
        setup_cost=policy_cfg["batch_setup_cost"],
        holding_cost_per_unit_per_day=h_per_day,
        truck_capacity=policy_cfg["truck_capacity_drums"],
        truck_fixed_cost=policy_cfg["truck_fixed_cost"],
        mail_per_drum=policy_cfg["mail_cost_per_drum"],
        days_remaining=days_remaining,
    )
    notes.append(f"Batch: EOQ={batch.eoq:.0f}, BE={batch.truck_break_even:.0f}, Q={batch.recommended_batch:.0f}, {batch.note}")

    # 3) Capacity
    peak = float(np.max(forecast.window(current_day, min(current_day + 365, forecast.days[-1])).mean))
    margin = policy_cfg["price_per_drum"] - policy_cfg["fulfillment_cost_per_drum"] - policy_cfg["batch_per_drum_cost"]
    cap = recommend_capacity(
        current_capacity=current_capacity,
        peak_daily_demand=peak,
        cost_per_drum_per_day=policy_cfg["capacity_cost_per_drum_per_day"],
        lead_time_days=policy_cfg["capacity_lead_time_days"],
        days_remaining_in_game=days_remaining,
        margin_per_drum=margin,
    )
    notes.append(f"Capacity: peak μ={peak:.1f}, current={current_capacity:.0f}, target={cap.recommended_capacity:.0f}. {cap.note}")

    use_truck = batch.recommended_batch >= batch.truck_break_even

    return PolicyRecommendation(
        day=current_day,
        reorder_point=rop.reorder_point,
        batch_size=batch.recommended_batch,
        fills_truck=batch.fills_truck,
        use_truck=use_truck,
        target_capacity=cap.recommended_capacity,
        expansion_drums=cap.drums_to_add,
        notes=notes,
    )
