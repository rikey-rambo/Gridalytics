"""Calendar and holiday feature engineering.

Merges holiday/festival data from the database and creates features
that capture demand impacts from holidays, weekends, events, and
the days surrounding them.
"""

import logging

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from src.data.db.models import HolidayRecord, AQIRecord

logger = logging.getLogger(__name__)


def add_holiday_features(df: pd.DataFrame, session: Session) -> pd.DataFrame:
    """Add holiday and festival features from the database.

    Features:
    - is_holiday: binary flag for any holiday
    - is_national_holiday: government holidays (offices closed)
    - is_festival: cultural/religious festivals (Diwali, Holi, etc.)
    - is_ipl_season: IPL cricket matches (evening demand spikes)
    - days_to_next_holiday: proximity to upcoming holiday
    - days_since_last_holiday: proximity to recent holiday
    """
    # Get all dates in the DataFrame
    dates = pd.Series(df.index.date).unique()
    min_date, max_date = dates.min(), dates.max()

    # Query holidays from DB
    holidays = (
        session.query(HolidayRecord)
        .filter(HolidayRecord.date >= min_date)
        .filter(HolidayRecord.date <= max_date)
        .all()
    )

    # Build lookup sets
    holiday_dates = set()
    national_dates = set()
    festival_dates = set()
    ipl_dates = set()

    for h in holidays:
        holiday_dates.add(h.date)
        if h.type == "national":
            national_dates.add(h.date)
        if h.category == "festival":
            festival_dates.add(h.date)
        if h.category == "sporting":
            ipl_dates.add(h.date)

    # Map to DataFrame
    df_dates = df.index.date
    df["is_holiday"] = pd.Series(
        [1 if d in holiday_dates else 0 for d in df_dates], index=df.index
    )
    df["is_national_holiday"] = pd.Series(
        [1 if d in national_dates else 0 for d in df_dates], index=df.index
    )
    df["is_festival"] = pd.Series(
        [1 if d in festival_dates else 0 for d in df_dates], index=df.index
    )
    df["is_ipl_season"] = pd.Series(
        [1 if d in ipl_dates else 0 for d in df_dates], index=df.index
    )

    # Days to/since nearest holiday
    if holiday_dates:
        sorted_holidays = sorted(holiday_dates)
        holiday_series = pd.Series(sorted_holidays)

        days_to = []
        days_since = []
        for d in df_dates:
            future = holiday_series[holiday_series >= d]
            past = holiday_series[holiday_series <= d]
            days_to.append((future.iloc[0] - d).days if len(future) > 0 else 30)
            days_since.append((d - past.iloc[-1]).days if len(past) > 0 else 30)

        df["days_to_next_holiday"] = days_to
        df["days_since_last_holiday"] = days_since
    else:
        df["days_to_next_holiday"] = 30
        df["days_since_last_holiday"] = 30

    # Pre/post holiday effect (day before and after holidays have different demand)
    df["is_pre_holiday"] = (df["days_to_next_holiday"] == 1).astype(int)
    df["is_post_holiday"] = (df["days_since_last_holiday"] == 1).astype(int)

    return df


def add_event_effect_feature(df: pd.DataFrame, session: Session) -> pd.DataFrame:
    """Add `expected_event_effect_pct`: the historically-learned demand impact
    of the day's event (festival/holiday), as a signed fraction.

    Unlike the binary `is_holiday`/`is_festival` flags, this carries *magnitude
    and direction* — e.g. Diwali ~ -0.10 (demand drops ~10%) vs Id-ul-Fitr
    ~ +0.03 (demand rises). The value is learned from the long daily series
    (`psp_daily`, ~10 yrs) and estimated **causally** (expanding window), so a
    row never sees its own or any future year's outcome. Non-event days are 0.

    Fails safe: on any error the column is added as all-zeros, so training and
    inference never break if the long series or estimator is unavailable.
    """
    try:
        from src.forecasting.event_effects import (
            load_daily, load_events, causal_effect_feature,
        )
        daily = load_daily(session=session)
        events = load_events(session=session)
        eff = causal_effect_feature(daily, events)  # normalized Timestamp -> fraction
        eff_map = {pd.Timestamp(k).normalize(): float(v) for k, v in eff.items()}

        idx_norm = df.index.normalize()
        df["expected_event_effect_pct"] = [eff_map.get(t, 0.0) for t in idx_norm]
        n_nonzero = int((df["expected_event_effect_pct"].abs() > 1e-9).sum())
        logger.info(f"expected_event_effect_pct set on {n_nonzero} rows "
                    f"(from {len(eff_map)} learned event-days)")
    except Exception as exc:
        logger.warning(f"Event-effect feature unavailable ({exc}); defaulting to 0")
        df["expected_event_effect_pct"] = 0.0
    return df


def add_aqi_features(df: pd.DataFrame, session: Session) -> pd.DataFrame:
    """Add Air Quality Index features from the database.

    Delhi's extreme pollution events (Oct-Nov) affect behavior:
    - People stay indoors → more residential electricity
    - Air purifier usage increases
    - Severe AQI days have measurably different demand patterns
    """
    dates = pd.Series(df.index.date).unique()
    min_date, max_date = dates.min(), dates.max()

    aqi_records = (
        session.query(AQIRecord)
        .filter(AQIRecord.date >= min_date)
        .filter(AQIRecord.date <= max_date)
        .all()
    )

    if not aqi_records:
        # No AQI data yet - fill with neutral defaults
        df["aqi_value"] = 100.0  # "Moderate" default
        df["aqi_severe"] = 0
        df["aqi_poor"] = 0
        return df

    aqi_map = {r.date: r for r in aqi_records}
    df_dates = df.index.date

    df["aqi_value"] = pd.Series(
        [aqi_map[d].aqi_value if d in aqi_map else np.nan for d in df_dates],
        index=df.index,
    )
    df["aqi_severe"] = pd.Series(
        [1 if d in aqi_map and aqi_map[d].aqi_value and aqi_map[d].aqi_value > 300 else 0 for d in df_dates],
        index=df.index,
    )
    df["aqi_poor"] = pd.Series(
        [1 if d in aqi_map and aqi_map[d].aqi_value and aqi_map[d].aqi_value > 200 else 0 for d in df_dates],
        index=df.index,
    )

    # Forward-fill missing AQI values
    df["aqi_value"] = df["aqi_value"].ffill().fillna(100)

    return df
