from __future__ import annotations

import argparse
import time

from .config import ensure_dirs, load_config, write_default_config
from .utils import money, normalize_date, pct, today_yyyymmdd


try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    Console = None
    Table = None


console = Console() if Console else None


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share quantitative paper-trading simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    init_config = sub.add_parser("init-config", help="Create a default config file")
    init_config.add_argument("--path", default="config.json")

    backtest = sub.add_parser("backtest", help="Run backtest from 2018 or configured start date")
    backtest.add_argument("--config", default="config.json")
    backtest.add_argument("--start")
    backtest.add_argument("--end", default=today_yyyymmdd())

    paper_init = sub.add_parser("paper-init", help="Create a virtual paper account")
    paper_init.add_argument("--config", default="config.json")
    paper_init.add_argument("--account", default="default")
    paper_init.add_argument("--overwrite", action="store_true")

    signal = sub.add_parser("signal", help="Fetch latest data and print strategy targets")
    signal.add_argument("--config", default="config.json")

    snapshot = sub.add_parser("snapshot", help="Refresh realtime prices and show account PnL")
    snapshot.add_argument("--config", default="config.json")
    snapshot.add_argument("--account", default="default")

    rebalance = sub.add_parser("rebalance", help="Place simulated orders to target strategy weights")
    rebalance.add_argument("--config", default="config.json")
    rebalance.add_argument("--account", default="default")

    settle = sub.add_parser("settle", help="Write daily paper account settlement report")
    settle.add_argument("--config", default="config.json")
    settle.add_argument("--account", default="default")
    settle.add_argument("--date")

    watch = sub.add_parser("watch", help="Keep refreshing realtime PnL, optionally with simulated auto-rebalance")
    watch.add_argument("--config", default="config.json")
    watch.add_argument("--account", default="default")
    watch.add_argument("--interval", type=int, default=300, help="Refresh interval in seconds")
    watch.add_argument("--auto-rebalance", action="store_true", help="Run simulated rebalance every cycle")

    args = parser.parse_args()

    if args.command == "init-config":
        path = write_default_config(args.path)
        _print(f"created {path}")
        return

    config = load_config(args.config)
    ensure_dirs(config)

    if args.command == "backtest":
        from .backtest import Backtester

        result = Backtester(config).run(start_date=args.start, end_date=normalize_date(args.end))
        paths = Backtester(config).save_result(result)
        _print_metrics(result.metrics)
        _print(f"reports: {paths}")
    elif args.command == "paper-init":
        from .paper import PaperTrader

        trader = PaperTrader(config, args.account)
        account = trader.init_account(overwrite=args.overwrite)
        _print(f"paper account ready cash={money(account.cash)} path={trader.account_path}")
    elif args.command == "signal":
        from .paper import PaperTrader

        trader = PaperTrader(config)
        signals = trader.generate_signals()
        _print_signals(signals)
    elif args.command == "snapshot":
        from .paper import PaperTrader

        summary = PaperTrader(config, args.account).snapshot()
        _print_summary(summary)
    elif args.command == "rebalance":
        from .paper import PaperTrader

        result = PaperTrader(config, args.account).rebalance()
        _print_signals(result["signals"])
        _print_orders(result["orders"])
        _print_summary(result["summary"])
        _print(f"saved account: {result['account_path']}")
    elif args.command == "settle":
        from .paper import PaperTrader

        result = PaperTrader(config, args.account).settle(args.date)
        _print_summary(result["summary"])
        _print(f"settlement saved: {result['json_path']}")
        _print(f"positions saved: {result['csv_path']}")
    elif args.command == "watch":
        from .paper import PaperTrader

        trader = PaperTrader(config, args.account)
        _print(f"watching paper account every {args.interval}s; press Ctrl+C to stop")
        try:
            while True:
                if args.auto_rebalance:
                    result = trader.rebalance()
                    _print_orders(result["orders"])
                    _print_summary(result["summary"])
                else:
                    _print_summary(trader.snapshot())
                time.sleep(max(5, args.interval))
        except KeyboardInterrupt:
            _print("stopped")
    else:
        parser.error(f"Unknown command: {args.command}")


def _print_metrics(metrics: dict[str, float]) -> None:
    if Table is None:
        for key, value in metrics.items():
            print(f"{key}: {value}")
        return

    table = Table(title="Backtest Metrics")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    mapping = {
        "initial_cash": money,
        "final_equity": money,
        "total_return": pct,
        "annual_return": pct,
        "annual_volatility": pct,
        "sharpe_like": lambda v: f"{v:.2f}",
        "max_drawdown": pct,
    }
    for key, formatter in mapping.items():
        if key in metrics:
            table.add_row(key, formatter(float(metrics[key])))
    _print(table)


def _print_signals(signals: list[dict]) -> None:
    if Table is None:
        print("Strategy Targets")
        for item in signals:
            print(f"{item['symbol']} weight={pct(float(item['target_weight']))} score={float(item['score']):.2f} {item['reason']}")
        return

    table = Table(title="Strategy Targets")
    table.add_column("Symbol")
    table.add_column("Weight", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Reason")
    for item in signals:
        table.add_row(item["symbol"], pct(float(item["target_weight"])), f"{float(item['score']):.2f}", item["reason"])
    _print(table)


def _print_orders(orders: list[dict]) -> None:
    if Table is None:
        print("Simulated Orders")
        for item in orders:
            fee_tax = float(item["fee"]) + float(item["tax"])
            print(f"{item['time']} {item['side']} {item['symbol']} shares={item['shares']} price={money(float(item['price']))} fee_tax={money(fee_tax)}")
        return

    table = Table(title="Simulated Orders")
    table.add_column("Time")
    table.add_column("Side")
    table.add_column("Symbol")
    table.add_column("Shares", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Fee+Tax", justify="right")
    for item in orders:
        table.add_row(
            item["time"],
            item["side"],
            item["symbol"],
            str(item["shares"]),
            money(float(item["price"])),
            money(float(item["fee"]) + float(item["tax"])),
        )
    _print(table)


def _print_summary(summary: dict) -> None:
    if Table is None:
        print(f"cash={money(summary['cash'])} market_value={money(summary['market_value'])} equity={money(summary['equity'])}")
        for item in summary["positions"]:
            print(f"{item['symbol']} shares={item['shares']} pnl={money(item['unrealized_pnl'])} weight={pct(item['weight'])}")
        return

    table = Table(title="Paper Account")
    table.add_column("Cash", justify="right")
    table.add_column("Market Value", justify="right")
    table.add_column("Equity", justify="right")
    table.add_column("Orders", justify="right")
    table.add_row(money(summary["cash"]), money(summary["market_value"]), money(summary["equity"]), str(summary["order_count"]))
    _print(table)

    positions = Table(title="Positions")
    positions.add_column("Symbol")
    positions.add_column("Shares", justify="right")
    positions.add_column("Avg", justify="right")
    positions.add_column("Last", justify="right")
    positions.add_column("PnL", justify="right")
    positions.add_column("Weight", justify="right")
    for item in summary["positions"]:
        positions.add_row(
            item["symbol"],
            str(item["shares"]),
            money(item["avg_price"]),
            money(item["last_price"]),
            money(item["unrealized_pnl"]),
            pct(item["weight"]),
        )
    _print(positions)


def _print(value) -> None:
    if console:
        console.print(value)
    else:
        print(value)


if __name__ == "__main__":
    main()
