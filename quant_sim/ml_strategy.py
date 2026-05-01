from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import BacktestResult, calculate_metrics
from .broker import Account
from .config import AppConfig, StrategyConfig, ensure_dirs
from .data import MarketData
from .indicators import add_indicators
from .utils import normalize_date, today_yyyymmdd


FEATURE_COLUMNS = [
    "ret_1",
    "ret_5",
    "ret_10",
    "ret_20",
    "ret_60",
    "ret_120",
    "ma_fast_gap",
    "ma_slow_gap",
    "ma_120_gap",
    "ma_fast_slow_gap",
    "momentum",
    "momentum_60",
    "momentum_120",
    "volatility",
    "volatility_60",
    "drawdown_60",
    "range_20",
    "volume_ratio_20",
]


@dataclass(frozen=True)
class MLSignal:
    symbol: str
    score: float
    target_weight: float
    expected_return: float
    reason: str


@dataclass(frozen=True)
class MLConfig:
    horizon_days: int = 5
    train_window_days: int = 720
    min_train_samples: int = 240
    ridge_alpha: float = 8.0
    min_expected_return: float = 0.005
    target_clip: float = 0.25


class RidgeReturnModel:
    def __init__(self, alpha: float):
        self.alpha = alpha
        self.x_mean: np.ndarray | None = None
        self.x_scale: np.ndarray | None = None
        self.y_mean = 0.0
        self.coef: np.ndarray | None = None

    def fit(self, x: pd.DataFrame, y: pd.Series) -> None:
        x_arr = x.to_numpy(dtype=float)
        y_arr = y.to_numpy(dtype=float)
        self.x_mean = np.nanmean(x_arr, axis=0)
        self.x_scale = np.nanstd(x_arr, axis=0)
        self.x_scale[self.x_scale < 1e-8] = 1.0
        self.y_mean = float(np.nanmean(y_arr))
        x_norm = (x_arr - self.x_mean) / self.x_scale
        y_centered = y_arr - self.y_mean
        xtx = x_norm.T @ x_norm
        penalty = np.eye(xtx.shape[0]) * self.alpha
        self.coef = np.linalg.solve(xtx + penalty, x_norm.T @ y_centered)

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        if self.coef is None or self.x_mean is None or self.x_scale is None:
            raise RuntimeError("Model has not been fitted")
        x_arr = x.to_numpy(dtype=float)
        x_norm = (x_arr - self.x_mean) / self.x_scale
        return x_norm @ self.coef + self.y_mean


class MLReturnStrategy:
    """Rolling ridge model that predicts forward return from price/volume features.

    The model only trains on rows whose forward target is known at the decision date,
    so the backtest does not peek into future prices.
    """

    def __init__(self, strategy_config: StrategyConfig, ml_config: MLConfig | None = None):
        self.strategy_config = strategy_config
        self.ml_config = ml_config or MLConfig()

    def prepare(self, histories: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        return {symbol: _feature_frame(frame, self.strategy_config, self.ml_config.horizon_days) for symbol, frame in histories.items()}

    def select(self, prepared: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> list[MLSignal]:
        train_x, train_y = self._training_data(prepared, as_of)
        if len(train_x) < self.ml_config.min_train_samples:
            return []

        model = RidgeReturnModel(alpha=self.ml_config.ridge_alpha)
        model.fit(train_x, train_y)

        rows = []
        for symbol, frame in prepared.items():
            data = frame.loc[frame["date"] <= as_of].tail(self.strategy_config.lookback_days)
            if data.empty:
                continue
            latest = data.iloc[-1]
            if latest[FEATURE_COLUMNS].isna().any():
                continue
            if latest["close"] <= 0:
                continue
            rows.append((symbol, latest))

        if not rows:
            return []

        latest_x = pd.DataFrame([row[1][FEATURE_COLUMNS] for row in rows], columns=FEATURE_COLUMNS)
        preds = model.predict(latest_x)
        candidates: list[MLSignal] = []
        for (symbol, latest), pred in zip(rows, preds):
            pred = float(pred)
            risk_penalty = min(float(latest.get("volatility_60", 0) or 0), 0.08) * 0.5
            score = pred - risk_penalty
            trend_ok = latest["close"] > latest["ma_slow"] or latest["ma_fast"] > latest["ma_slow"]
            if pred >= self.ml_config.min_expected_return and trend_ok:
                candidates.append(
                    MLSignal(
                        symbol=symbol,
                        score=score,
                        target_weight=0.0,
                        expected_return=pred,
                        reason=(
                            f"ml_pred_{self.ml_config.horizon_days}d={pred:.2%}, "
                            f"mom20={float(latest['momentum']):.2%}, "
                            f"vol60={float(latest['volatility_60']):.2%}, "
                            f"dd60={float(latest['drawdown_60']):.2%}"
                        ),
                    )
                )

        candidates.sort(key=lambda item: item.score, reverse=True)
        selected = candidates[: self.strategy_config.max_positions]
        if not selected:
            return []

        gross_weight = max(0.0, 1.0 - self.strategy_config.cash_buffer)
        equal_weight = min(self.strategy_config.max_position_weight, gross_weight / len(selected))
        return [
            MLSignal(
                symbol=item.symbol,
                score=item.score,
                target_weight=equal_weight,
                expected_return=item.expected_return,
                reason=item.reason,
            )
            for item in selected
        ]

    def risk_exit(self, frame: pd.DataFrame, as_of: pd.Timestamp, entry_price: float) -> str | None:
        data = frame.loc[frame["date"] <= as_of]
        if data.empty:
            return None
        close = float(data.iloc[-1]["close"])
        pnl = close / entry_price - 1
        if pnl <= -self.strategy_config.stop_loss:
            return f"stop_loss {pnl:.2%}"
        if pnl >= self.strategy_config.take_profit:
            return f"take_profit {pnl:.2%}"
        return None

    def _training_data(self, prepared: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.Series]:
        start = as_of - pd.Timedelta(days=self.ml_config.train_window_days)
        frames = []
        for frame in prepared.values():
            rows = frame.loc[
                (frame["date"] >= start)
                & (frame["date"] < as_of)
                & (frame["target_date"] <= as_of)
            ]
            keep = rows[FEATURE_COLUMNS + ["forward_return"]].replace([np.inf, -np.inf], np.nan).dropna()
            if not keep.empty:
                frames.append(keep)
        if not frames:
            return pd.DataFrame(columns=FEATURE_COLUMNS), pd.Series(dtype=float)
        data = pd.concat(frames, ignore_index=True)
        y = data["forward_return"].clip(-self.ml_config.target_clip, self.ml_config.target_clip)
        return data[FEATURE_COLUMNS], y


class MLBacktester:
    def __init__(self, config: AppConfig, ml_config: MLConfig | None = None):
        self.config = config
        self.market = MarketData(config)
        self.strategy = MLReturnStrategy(config.strategy, ml_config)

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
                signals = self.strategy.select(prepared, pd.Timestamp(as_of))
                target_weights = {signal.symbol: signal.target_weight for signal in signals}
                account.rebalance_to_weights(target_weights, prices, self.config.trading, "ml_rebalance")
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
        equity_path = target_dir / "ml_backtest_equity.csv"
        orders_path = target_dir / "ml_backtest_orders.csv"
        metrics_path = target_dir / "ml_backtest_metrics.json"
        result.equity_curve.to_csv(equity_path, index=False, encoding="utf-8-sig")
        result.orders.to_csv(orders_path, index=False, encoding="utf-8-sig")
        pd.Series(result.metrics).to_json(metrics_path, force_ascii=False, indent=2)
        return {"equity": equity_path, "orders": orders_path, "metrics": metrics_path}


class MLPaperTrader:
    def __init__(self, config: AppConfig, account_name: str = "default", ml_config: MLConfig | None = None):
        ensure_dirs(config)
        self.config = config
        self.account_path = config.paths.state_dir / f"{account_name}_account.json"
        self.market = MarketData(config)
        self.strategy = MLReturnStrategy(config.strategy, ml_config)

    def load_account(self) -> Account:
        return Account.load(self.account_path, self.config.initial_cash)

    def generate_signals(self, end_date: str | None = None) -> list[dict]:
        end = normalize_date(end_date or today_yyyymmdd())
        histories = self.market.fetch_many_history(self.config.symbols, self.config.start_date, end)
        prepared = self.strategy.prepare(histories)
        as_of = max(frame["date"].max() for frame in prepared.values())
        signals = self.strategy.select(prepared, pd.Timestamp(as_of))
        return [signal.__dict__ for signal in signals]

    def rebalance(self) -> dict:
        account = self.load_account()
        signals = self.generate_signals()
        target_weights = {signal["symbol"]: float(signal["target_weight"]) for signal in signals}
        prices = self.market.latest_prices(list(set(list(target_weights) + list(account.positions))))
        orders = account.rebalance_to_weights(target_weights, prices, self.config.trading, "ml_rebalance")
        account.save(self.account_path)
        return {
            "summary": _account_summary(account),
            "signals": signals,
            "orders": [order.__dict__ for order in orders],
            "account_path": str(self.account_path),
        }


def _feature_frame(frame: pd.DataFrame, strategy_config: StrategyConfig, horizon_days: int) -> pd.DataFrame:
    data = add_indicators(frame, strategy_config.ma_fast, strategy_config.ma_slow, strategy_config.momentum_days)
    data = data.sort_values("date").copy()
    for days in [1, 5, 10, 20, 60, 120]:
        data[f"ret_{days}"] = data["close"].pct_change(days)
    data["ma_fast_gap"] = data["close"] / data["ma_fast"] - 1
    data["ma_slow_gap"] = data["close"] / data["ma_slow"] - 1
    data["ma_120_gap"] = data["close"] / data["ma_120"] - 1
    data["ma_fast_slow_gap"] = data["ma_fast"] / data["ma_slow"] - 1
    data["range_20"] = data["high"].rolling(20).max() / data["low"].rolling(20).min() - 1
    data["volume_ratio_20"] = data["volume"] / data["volume"].rolling(20).mean() - 1
    data["forward_return"] = data["close"].shift(-horizon_days) / data["close"] - 1
    data["target_date"] = data["date"].shift(-horizon_days)
    return data


def _prices_on(histories: dict[str, pd.DataFrame], as_of: pd.Timestamp) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol, frame in histories.items():
        data = frame.loc[frame["date"] <= as_of]
        if not data.empty:
            prices[symbol] = float(data.iloc[-1]["close"])
    return prices


def _account_summary(account: Account) -> dict:
    equity = account.equity()
    positions = []
    for pos in account.positions.values():
        positions.append(
            {
                "symbol": pos.symbol,
                "shares": pos.shares,
                "avg_price": pos.avg_price,
                "last_price": pos.last_price,
                "market_value": pos.market_value,
                "unrealized_pnl": pos.unrealized_pnl,
                "weight": pos.market_value / equity if equity else 0.0,
            }
        )
    return {
        "cash": account.cash,
        "market_value": sum(pos["market_value"] for pos in positions),
        "equity": equity,
        "positions": positions,
        "order_count": len(account.orders),
    }
