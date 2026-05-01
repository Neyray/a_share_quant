from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .broker import Account
from .config import AppConfig
from .data import MarketData
from .strategy import TrendMomentumStrategy
from .utils import normalize_date


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict[str, float]


class Backtester:
    def __init__(self, config: AppConfig):
        self.config = config
        self.market = MarketData(config)
        self.strategy = TrendMomentumStrategy(config.strategy)

    def run(self, start_date: str | None = None, end_date: str | None = None) -> BacktestResult:
        start = normalize_date(start_date or self.config.start_date)
        end = normalize_date(end_date)
        histories = self.market.fetch_many_history(self.config.symbols, start, end)
        prepared = self.strategy.prepare(histories)

        calendar = sorted(set().union(*(set(frame["date"]) for frame in prepared.values())))
        account = Account(cash=self.config.initial_cash)
        equity_rows: list[dict] = []
        last_rebalance_idx = -10_000

        for idx, as_of in enumerate(calendar):
            prices = _prices_on(prepared, as_of)
            account.update_prices(prices)

            for symbol, pos in list(account.positions.items()):
                exit_reason = self.strategy.risk_exit(prepared[symbol], as_of, pos.avg_price)
                if exit_reason and symbol in prices:
                    account.sell(symbol, prices[symbol], pos.shares, self.config.trading, exit_reason)

            if idx - last_rebalance_idx >= self.config.strategy.rebalance_days:
                signals = self.strategy.select(prepared, as_of)
                target_weights = {signal.symbol: signal.target_weight for signal in signals}
                account.rebalance_to_weights(target_weights, prices, self.config.trading, "strategy_rebalance")
                last_rebalance_idx = idx

            equity_rows.append(
                {
                    "date": as_of,
                    "cash": account.cash,
                    "market_value": sum(pos.market_value for pos in account.positions.values()),
                    "equity": account.equity(),
                    "positions": len(account.positions),
                }
            )

        equity_curve = pd.DataFrame(equity_rows)
        orders = pd.DataFrame([order.__dict__ for order in account.orders])
        metrics = calculate_metrics(equity_curve, self.config.initial_cash)
        return BacktestResult(equity_curve=equity_curve, orders=orders, metrics=metrics)

    def save_result(self, result: BacktestResult, report_dir: Path | None = None) -> dict[str, Path]:
        target_dir = report_dir or self.config.paths.report_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        equity_path = target_dir / "backtest_equity.csv"
        orders_path = target_dir / "backtest_orders.csv"
        metrics_path = target_dir / "backtest_metrics.json"
        result.equity_curve.to_csv(equity_path, index=False, encoding="utf-8-sig")
        result.orders.to_csv(orders_path, index=False, encoding="utf-8-sig")
        pd.Series(result.metrics).to_json(metrics_path, force_ascii=False, indent=2)
        return {"equity": equity_path, "orders": orders_path, "metrics": metrics_path}


def calculate_metrics(equity_curve: pd.DataFrame, initial_cash: float) -> dict[str, float]:
    if equity_curve.empty:
        return {}
    curve = equity_curve.copy()
    curve["ret"] = curve["equity"].pct_change().fillna(0)
    total_return = float(curve.iloc[-1]["equity"] / initial_cash - 1)
    days = max((curve.iloc[-1]["date"] - curve.iloc[0]["date"]).days, 1)
    annual_return = float((1 + total_return) ** (365 / days) - 1)
    annual_vol = float(curve["ret"].std() * (252 ** 0.5))
    sharpe = float(annual_return / annual_vol) if annual_vol > 0 else 0.0
    rolling_high = curve["equity"].cummax()
    max_drawdown = float((curve["equity"] / rolling_high - 1).min())
    return {
        "initial_cash": float(initial_cash),
        "final_equity": float(curve.iloc[-1]["equity"]),
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "sharpe_like": sharpe,
        "max_drawdown": max_drawdown,
    }


def _prices_on(histories: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol, frame in histories.items():
        data = frame.loc[frame["date"] <= as_of]
        if not data.empty:
            prices[symbol] = float(data.iloc[-1]["close"])
    return prices
