from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import StrategyConfig
from .indicators import add_indicators


@dataclass(frozen=True)
class Signal:
    symbol: str
    score: float
    target_weight: float
    reason: str


class TrendMomentumStrategy:
    """Long-only A-share multi-factor trend, momentum, and risk selector."""

    def __init__(self, config: StrategyConfig):
        self.config = config

    def prepare(self, histories: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        prepared: dict[str, pd.DataFrame] = {}
        for symbol, frame in histories.items():
            prepared[symbol] = add_indicators(
                frame,
                ma_fast=self.config.ma_fast,
                ma_slow=self.config.ma_slow,
                momentum_days=self.config.momentum_days,
            )
        return prepared

    def select(self, prepared: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> list[Signal]:
        candidates: list[Signal] = []
        for symbol, frame in prepared.items():
            data = frame.loc[frame["date"] <= as_of].tail(self.config.lookback_days)
            if len(data) < max(self.config.ma_slow, self.config.momentum_days) + 5:
                continue
            latest = data.iloc[-1]
            needed = ["ma_fast", "ma_slow", "ma_120", "momentum", "momentum_60", "momentum_120"]
            if any(pd.isna(latest[item]) for item in needed):
                continue

            trend = (
                float(latest["close"] > latest["ma_fast"])
                + float(latest["ma_fast"] > latest["ma_slow"])
                + float(latest["ma_slow"] > latest["ma_120"])
            )
            mom20 = max(float(latest["momentum"]), -0.3)
            mom60 = max(float(latest["momentum_60"]), -0.4)
            mom120 = max(float(latest["momentum_120"]), -0.5)
            momentum_score = 0.45 * mom20 + 0.35 * mom60 + 0.20 * mom120
            volatility_penalty = min(float(latest.get("volatility_60", 0) or 0), 0.08)
            drawdown_penalty = abs(min(float(latest.get("drawdown_60", 0) or 0), 0))
            score = trend + momentum_score * 3.5 - volatility_penalty * 6 - drawdown_penalty * 1.2

            if trend >= 2 and score > 1.4:
                candidates.append(
                    Signal(
                        symbol=symbol,
                        score=score,
                        target_weight=0.0,
                        reason=f"trend={trend:.0f}/3, mom20={mom20:.2%}, mom60={mom60:.2%}, dd60={-drawdown_penalty:.2%}",
                    )
                )

        candidates.sort(key=lambda item: item.score, reverse=True)
        selected = candidates[: self.config.max_positions]
        if not selected:
            return []

        gross_weight = max(0.0, 1.0 - self.config.cash_buffer)
        equal_weight = min(self.config.max_position_weight, gross_weight / len(selected))
        return [
            Signal(symbol=item.symbol, score=item.score, target_weight=equal_weight, reason=item.reason)
            for item in selected
        ]

    def risk_exit(self, frame: pd.DataFrame, as_of: pd.Timestamp, entry_price: float) -> str | None:
        data = frame.loc[frame["date"] <= as_of]
        if data.empty:
            return None
        close = float(data.iloc[-1]["close"])
        pnl = close / entry_price - 1
        if pnl <= -self.config.stop_loss:
            return f"stop_loss {pnl:.2%}"
        if pnl >= self.config.take_profit:
            return f"take_profit {pnl:.2%}"
        return None
