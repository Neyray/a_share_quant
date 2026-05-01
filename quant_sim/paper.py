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
        account = self.load_account()
        todays_orders = _orders_for_day(account, day)
        watchlist = self.market.watchlist_snapshot(self.config.symbols)
        previous = _previous_settlement(self.config.paths.report_dir, day)
        daily_pnl = None
        daily_return = None
        if previous:
            previous_equity = float(previous["summary"]["equity"])
            daily_pnl = summary["equity"] - previous_equity
            daily_return = daily_pnl / previous_equity if previous_equity else 0.0

        result = {
            "date": day,
            "summary": summary,
            "daily_pnl": daily_pnl,
            "daily_return": daily_return,
            "orders": [order.__dict__ for order in todays_orders],
            "watchlist": watchlist,
            "account_path": str(self.account_path),
        }

        json_path = self.config.paths.report_dir / f"paper_settlement_{day}.json"
        csv_path = self.config.paths.report_dir / f"paper_positions_{day}.csv"
        md_path = self.config.paths.report_dir / f"paper_report_{day}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        with csv_path.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.DictWriter(
                fp,
                fieldnames=["symbol", "shares", "avg_price", "last_price", "market_value", "unrealized_pnl", "weight"],
            )
            writer.writeheader()
            writer.writerows(summary["positions"])

        md_path.write_text(_build_markdown_report(result), encoding="utf-8")
        result["json_path"] = str(json_path)
        result["csv_path"] = str(csv_path)
        result["md_path"] = str(md_path)
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


def _orders_for_day(account: Account, day: str):
    prefix = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    return [order for order in account.orders if order.time.startswith(prefix)]


def _previous_settlement(report_dir: Path, day: str) -> dict | None:
    candidates = sorted(path for path in report_dir.glob("paper_settlement_*.json") if path.stem[-8:] < day)
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_markdown_report(result: dict) -> str:
    summary = result["summary"]
    orders = result["orders"]
    positions = summary["positions"]
    watchlist = result["watchlist"]
    daily_pnl = result["daily_pnl"]
    daily_return = result["daily_return"]

    lines = [
        f"# A股模拟盘日报 {result['date']}",
        "",
        "## 账户概览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 总资产 | {summary['equity']:,.2f} |",
        f"| 现金 | {summary['cash']:,.2f} |",
        f"| 股票市值 | {summary['market_value']:,.2f} |",
        f"| 持仓数量 | {len(positions)} |",
    ]
    if daily_pnl is not None:
        lines.extend(
            [
                f"| 今日盈亏 | {daily_pnl:,.2f} |",
                f"| 今日收益率 | {daily_return:.2%} |",
            ]
        )

    lines.extend(["", "## 今日模拟交易", ""])
    if orders:
        lines.extend(["| 动作 | 股票 | 数量 | 价格 | 手续费税费 | 说明 |", "| --- | --- | ---: | ---: | ---: | --- |"])
        for order in orders:
            action = "买入" if order["side"] == "BUY" else "卖出"
            lines.append(
                f"| {action} | {order['symbol']} | {order['shares']} | {order['price']:,.2f} | "
                f"{float(order['fee']) + float(order['tax']):,.2f} | {order.get('note', '')} |"
            )
    else:
        lines.append("今日没有产生新的模拟买卖。")

    lines.extend(["", "## 当前持仓", ""])
    if positions:
        lines.extend(["| 股票 | 股数 | 成本价 | 最新价 | 市值 | 浮动盈亏 | 仓位 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for pos in positions:
            lines.append(
                f"| {pos['symbol']} | {pos['shares']} | {pos['avg_price']:,.2f} | {pos['last_price']:,.2f} | "
                f"{pos['market_value']:,.2f} | {pos['unrealized_pnl']:,.2f} | {pos['weight']:.2%} |"
            )
    else:
        lines.append("当前没有持仓。")

    lines.extend(["", "## 股票池变化", ""])
    lines.extend(["| 股票 | 名称 | 最新价 | 涨跌幅 | 数据源 |", "| --- | --- | ---: | ---: | --- |"])
    for item in sorted(watchlist, key=lambda row: row["pct_chg"], reverse=True):
        lines.append(
            f"| {item['symbol']} | {item['name']} | {item['price']:,.2f} | {item['pct_chg']:.2%} | {item['source']} |"
        )

    lines.extend(["", "> 说明：这是虚拟模拟盘日报，不是实盘交易记录，也不构成投资建议。"])
    return "\n".join(lines) + "\n"
