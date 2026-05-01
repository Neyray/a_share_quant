from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from .broker import Account
from .config import AppConfig, ensure_dirs
from .data import MarketData
from .strategy import TrendMomentumStrategy
from .utils import normalize_date, today_yyyymmdd


class PaperTrader:
    def __init__(self, config: AppConfig, account_name: str = "default"):
        ensure_dirs(config)
        self.config = config
        self.account_path = config.paths.state_dir / f"{account_name}_account.json"
        self.market = MarketData(config)
        self.strategy = TrendMomentumStrategy(config.strategy)

    def init_account(self, overwrite: bool = False) -> Account:
        if self.account_path.exists() and not overwrite:
            return Account.load(self.account_path, self.config.initial_cash)
        account = Account(cash=self.config.initial_cash)
        account.save(self.account_path)
        return account

    def load_account(self) -> Account:
        return Account.load(self.account_path, self.config.initial_cash)

    def snapshot(self) -> dict:
        account = self.load_account()
        prices = self.market.latest_prices(self._tracked_symbols(account))
        account.update_prices(prices)
        account.save(self.account_path)
        return _account_summary(account)

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
        orders = account.rebalance_to_weights(target_weights, prices, self.config.trading, "paper_rebalance")
        account.save(self.account_path)
        return {
            "summary": _account_summary(account),
            "signals": signals,
            "orders": [order.__dict__ for order in orders],
            "account_path": str(self.account_path),
        }

    def settle(self, settlement_date: str | None = None) -> dict:
        day = normalize_date(settlement_date or today_yyyymmdd())
        summary = self.snapshot()
        result = {
            "date": day,
            "summary": summary,
            "account_path": str(self.account_path),
        }

        json_path = self.config.paths.report_dir / f"paper_settlement_{day}.json"
        csv_path = self.config.paths.report_dir / f"paper_positions_{day}.csv"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        with csv_path.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.DictWriter(
                fp,
                fieldnames=["symbol", "shares", "avg_price", "last_price", "market_value", "unrealized_pnl", "weight"],
            )
            writer.writeheader()
            writer.writerows(summary["positions"])

        result["json_path"] = str(json_path)
        result["csv_path"] = str(csv_path)
        return result

    def _tracked_symbols(self, account: Account) -> list[str]:
        return sorted(set(self.config.symbols) | set(account.positions))


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
