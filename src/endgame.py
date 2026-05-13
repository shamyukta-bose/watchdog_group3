"""End-game cleanout optimizer.

The game terminates on day 1460. All inventory and capacity is then worth $0.
That means in the final stretch we want to:

1. STOP producing once expected residual demand can be met from existing
   pipeline + warehouse inventory. Any drum produced that doesn't sell is
   a pure loss of $1000 + the holding cost it accumulated.

2. SWITCH from truck (7-day transit) to mail (1-day transit) once total
   lead time stops fitting in the remaining horizon. A truck shipped on
   day 1454 arrives on day 1461 — too late.

3. DO NOT EXPAND capacity once the productive-days window (days_remaining -
   90-day lead time) is shorter than payback.

This module computes those thresholds and emits the recommended "phase":
   GROWTH  -> normal seasonal ROP, fill trucks, expand if needed
   STEADY  -> normal seasonal ROP, fill trucks, no more capacity adds
   RAMPDOWN-> reduce ROP and Q, switch to mail when needed
   CLEANOUT-> ROP=0, Q=0, mail any remaining warehouse-bound shipments

The math: expected remaining demand E[D_rem] = sum over remaining days of
forecast mean. If on-hand + in-pipeline >= E[D_rem] + safety, stop producing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np


class Phase(str, Enum):
    GROWTH = "GROWTH"
    STEADY = "STEADY"
    RAMPDOWN = "RAMPDOWN"
    CLEANOUT = "CLEANOUT"


@dataclass
class EndGameRecommendation:
    current_day: int
    end_day: int
    days_remaining: int
    phase: Phase
    expected_remaining_demand: float
    pipeline_plus_warehouse: float
    safety_margin: float
    recommended_rop: float
    recommended_q: float
    use_mail: bool
    halt_production: bool
    note: str


def expected_remaining_demand(forecast, current_day: int, end_day: int) -> float:
    """Sum of forecast mean demand from current_day to end_day inclusive."""
    fwin = forecast.window(current_day, end_day)
    return float(np.sum(fwin.mean))


def recommend_endgame(
    forecast,
    current_day: int,
    end_day: int,
    warehouse_inventory: float,
    pipeline_inventory: float,
    avg_daily_demand_forward: float,
    production_lead_time: float = 5,
    shipping_lead_time_truck: float = 7,
    shipping_lead_time_mail: float = 1,
    capacity_lead_time: float = 90,
    safety_days: float = 7.0,
    cleanout_threshold_drums: float = 50,
) -> EndGameRecommendation:
    """Pick the end-game phase + parameters.

    Args:
        warehouse_inventory: drums physically in warehouse.
        pipeline_inventory: drums in transit + WIP that will become saleable
                            before end_day.
        avg_daily_demand_forward: average daily demand in the next ~30 days
                                  (used as safety buffer scale).
    """
    days_remaining = max(0, end_day - current_day)
    erd = expected_remaining_demand(forecast, current_day + 1, end_day)
    on_hand_plus_pipe = warehouse_inventory + pipeline_inventory
    safety_buffer = avg_daily_demand_forward * safety_days

    # Total lead time required for one fresh truck-shipped batch:
    truck_total_lt = production_lead_time + shipping_lead_time_truck
    mail_total_lt = production_lead_time + shipping_lead_time_mail

    if days_remaining > 365:
        phase = Phase.GROWTH
    elif days_remaining > capacity_lead_time + 30:
        phase = Phase.STEADY
    elif days_remaining > truck_total_lt + 14:
        # We can still safely truck things in if we need them
        phase = Phase.STEADY
    elif days_remaining > mail_total_lt + 3:
        phase = Phase.RAMPDOWN
    else:
        phase = Phase.CLEANOUT

    # Recommend per phase
    halt_production = False
    use_mail = False

    if phase == Phase.CLEANOUT:
        recommended_rop = 0.0
        recommended_q = 0.0
        halt_production = True
        use_mail = True
        note = ("CLEANOUT: too few days for another production+ship cycle. "
                "Set OP=0, Q=0. Drain warehouse to customers.")
    elif phase == Phase.RAMPDOWN:
        # Only produce what's needed to fill remaining expected demand.
        shortfall = max(0.0, erd + safety_buffer - on_hand_plus_pipe)
        recommended_q = round(min(shortfall, 75))  # small final batches
        recommended_rop = round(max(0.0, shortfall / 2))
        use_mail = True
        halt_production = shortfall <= cleanout_threshold_drums
        note = (f"RAMPDOWN: expected remaining demand {erd:.0f} + safety {safety_buffer:.0f} = "
                f"{erd + safety_buffer:.0f} drums; pipeline+WH = {on_hand_plus_pipe:.0f}; "
                f"shortfall = {shortfall:.0f}. Use mail (1-day transit). "
                f"{'Halt production.' if halt_production else f'One more small batch of ~{recommended_q:.0f}.'}")
    else:
        # GROWTH / STEADY: normal ops. ROP/Q decided upstream by rop_calculator.
        recommended_rop = -1  # sentinel: let upstream decide
        recommended_q = -1
        use_mail = False
        note = f"{phase.value}: normal operations. {days_remaining} days remaining."

    return EndGameRecommendation(
        current_day=current_day,
        end_day=end_day,
        days_remaining=days_remaining,
        phase=phase,
        expected_remaining_demand=erd,
        pipeline_plus_warehouse=on_hand_plus_pipe,
        safety_margin=safety_buffer,
        recommended_rop=recommended_rop,
        recommended_q=recommended_q,
        use_mail=use_mail,
        halt_production=halt_production,
        note=note,
    )


def merge_with_baseline(
    endgame: EndGameRecommendation,
    baseline_rop: float,
    baseline_q: float,
    use_truck_baseline: bool,
) -> tuple[float, float, bool, str]:
    """Combine end-game phase override with baseline ROP/Q recommendation.

    Returns (final_rop, final_q, use_truck, note).
    """
    if endgame.phase in (Phase.RAMPDOWN, Phase.CLEANOUT):
        return (
            endgame.recommended_rop,
            endgame.recommended_q,
            not endgame.use_mail,
            f"End-game override applied ({endgame.phase.value}). {endgame.note}",
        )
    return baseline_rop, baseline_q, use_truck_baseline, f"Normal phase ({endgame.phase.value}); baseline ROP/Q used."
