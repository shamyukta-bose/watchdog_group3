"""Seasonal demand forecaster for Jacobs Industries.

The game tells us demand is highly seasonal but otherwise very stable, with
no long-run market trend. We exploit this with a day-of-year seasonal index
model:

    forecast[d] = level * seasonal_index[d mod 365]

Where:
- seasonal_index is fit by averaging demand for each day-of-year across all
  observed years, normalised so the average index = 1.
- level is the overall mean demand per day from the history.
- Residual stdev is computed around the seasonal fit (NOT the flat mean),
  which is what we actually need for a tight reorder point.

This is materially better than Group 7's "split history into 10 phases and
take a flat mean/stdev in each phase" approach because (a) it uses the FULL
two-year overlap to identify season shape and (b) it removes the seasonal
component before computing volatility, so the safety stock isn't padded by
predictable seasonal swings.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np


SEASON_LEN = 365  # game year


@dataclass
class DemandForecast:
    """Per-day forecast over a horizon."""
    days: np.ndarray          # absolute day numbers (e.g., 731 ... 1460)
    mean: np.ndarray          # forecast mean demand each day
    stdev: np.ndarray         # forecast 1-day stdev each day (residual)
    seasonal_index: np.ndarray  # length SEASON_LEN, raw seasonal pattern
    level: float              # overall mean demand
    residual_stdev: float     # stdev of residual after removing seasonality

    def window(self, day_start: int, day_end: int) -> "DemandForecast":
        """Subset to [day_start, day_end] inclusive."""
        mask = (self.days >= day_start) & (self.days <= day_end)
        return DemandForecast(
            days=self.days[mask],
            mean=self.mean[mask],
            stdev=self.stdev[mask],
            seasonal_index=self.seasonal_index,
            level=self.level,
            residual_stdev=self.residual_stdev,
        )

    def lead_time_demand(self, lead_time_days: int) -> tuple[np.ndarray, np.ndarray]:
        """For each day d, return (mean, stdev) of demand in days [d, d+L-1]."""
        L = int(round(lead_time_days))
        n = len(self.mean)
        ltd_mean = np.zeros(n)
        ltd_stdev = np.zeros(n)
        # Mean of sum = sum of means (forecast is deterministic per day)
        # Variance of sum of independent days = sum of variances
        var = self.stdev ** 2
        # cumulative trick
        cum_mean = np.concatenate([[0], np.cumsum(self.mean)])
        cum_var = np.concatenate([[0], np.cumsum(var)])
        for i in range(n):
            j = min(i + L, n)
            ltd_mean[i] = cum_mean[j] - cum_mean[i]
            ltd_stdev[i] = np.sqrt(cum_var[j] - cum_var[i])
        return ltd_mean, ltd_stdev


def _doy(day: int, season_len: int = SEASON_LEN) -> int:
    """Day-of-year index (0..season_len-1).

    The Jacobs game starts at day 1 = beginning of year 1. We use
    (day - 1) % SEASON_LEN.
    """
    return (day - 1) % season_len


def load_demand_history(csv_path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Load demand_history.csv with columns: day, demand."""
    days, demand = [], []
    with open(csv_path, "r") as f:
        r = csv.DictReader(f)
        for row in r:
            days.append(int(row["day"]))
            demand.append(float(row["demand"]))
    return np.asarray(days, dtype=int), np.asarray(demand, dtype=float)


def fit_seasonal_model(
    days: np.ndarray,
    demand: np.ndarray,
    season_len: int = SEASON_LEN,
    smooth_window: int = 7,
) -> tuple[np.ndarray, float, float]:
    """Fit a multiplicative seasonal model with a centred moving-average smoother.

    Returns (seasonal_index[season_len], level, residual_stdev).
    """
    if len(days) < season_len:
        # Not enough data for a full season; fall back to flat model.
        level = float(np.mean(demand)) if len(demand) else 0.0
        return np.ones(season_len), level, float(np.std(demand, ddof=1)) if len(demand) > 1 else 0.0

    # Bucket demand by day-of-year and average across years
    by_doy = [[] for _ in range(season_len)]
    for d, q in zip(days, demand):
        by_doy[_doy(int(d), season_len)].append(q)
    raw_index = np.array([np.mean(b) if b else np.nan for b in by_doy])

    # Fill any NaN (days with no observation) with overall mean
    overall_mean = float(np.nanmean(raw_index))
    raw_index = np.where(np.isnan(raw_index), overall_mean, raw_index)

    # Smooth with a circular moving average so the index isn't jagged
    if smooth_window > 1:
        w = smooth_window
        kernel = np.ones(w) / w
        # circular pad
        padded = np.concatenate([raw_index[-w:], raw_index, raw_index[:w]])
        smoothed = np.convolve(padded, kernel, mode="same")[w:-w]
    else:
        smoothed = raw_index

    # Normalise so the seasonal index averages to 1
    level = float(np.mean(smoothed))
    seasonal_index = smoothed / level if level > 0 else np.ones(season_len)

    # Residual stdev (after removing the seasonal expectation)
    expected = np.array([level * seasonal_index[_doy(int(d), season_len)] for d in days])
    residuals = demand - expected
    residual_stdev = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else 0.0

    return seasonal_index, level, residual_stdev


def forecast_horizon(
    seasonal_index: np.ndarray,
    level: float,
    residual_stdev: float,
    start_day: int,
    end_day: int,
    season_len: int = SEASON_LEN,
) -> DemandForecast:
    """Project a daily mean/stdev forecast from start_day..end_day inclusive."""
    days = np.arange(start_day, end_day + 1, dtype=int)
    mean = np.array([level * seasonal_index[_doy(int(d), season_len)] for d in days])
    # Treat residual stdev as roughly constant in the units of drums/day.
    # (We could scale it by mean for a CV model; the additive form is more
    #  conservative when the season is low and tends to give better safety
    #  stock at the trough.)
    stdev = np.full_like(mean, residual_stdev)
    return DemandForecast(
        days=days,
        mean=mean,
        stdev=stdev,
        seasonal_index=seasonal_index,
        level=level,
        residual_stdev=residual_stdev,
    )


def build_forecast(
    history_csv: str | Path,
    horizon_start_day: int,
    horizon_end_day: int = 1460,
    smooth_window: int = 7,
) -> DemandForecast:
    """Convenience: load history + fit + forecast in one call."""
    days, demand = load_demand_history(history_csv)
    season_index, level, resid_sd = fit_seasonal_model(days, demand, smooth_window=smooth_window)
    return forecast_horizon(season_index, level, resid_sd, horizon_start_day, horizon_end_day)


def append_observation(history_csv: str | Path, day: int, demand: float) -> None:
    """Append today's observed demand to the history CSV for future re-fits."""
    history_csv = Path(history_csv)
    write_header = not history_csv.exists()
    with open(history_csv, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["day", "demand"])
        w.writerow([int(day), float(demand)])
