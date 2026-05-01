from __future__ import annotations

import pandas as pd


def add_indicators(frame: pd.DataFrame, ma_fast: int, ma_slow: int, momentum_days: int) -> pd.DataFrame:
    data = frame.sort_values("date").copy()
    data["ma_fast"] = data["close"].rolling(ma_fast).mean()
    data["ma_slow"] = data["close"].rolling(ma_slow).mean()
    data["momentum"] = data["close"].pct_change(momentum_days)
    data["daily_ret"] = data["close"].pct_change()
    data["volatility"] = data["daily_ret"].rolling(20).std()
    data["high_60"] = data["close"].rolling(60).max()
    data["drawdown_60"] = data["close"] / data["high_60"] - 1
    return data
