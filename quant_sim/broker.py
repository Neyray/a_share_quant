from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import TradingConfig


@dataclass
class Position:
    symbol: str
    shares: int
    avg_price: float
    last_price: float

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price

    @property
    def cost_value(self) -> float:
        return self.shares * self.avg_price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_value


@dataclass
class Order:
    time: str
    symbol: str
    side: str
    price: float
    shares: int
    amount: float
    fee: float
    tax: float
    note: str = ""


@dataclass
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)

    def equity(self) -> float:
        return self.cash + sum(pos.market_value for pos in self.positions.values())

    def update_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self.positions and price > 0:
                self.positions[symbol].last_price = float(price)

    def buy(self, symbol: str, price: float, shares: int, trading: TradingConfig, note: str = "") -> Order | None:
        if shares <= 0:
            return None
        execution_price = price * (1 + trading.slippage_rate)
        amount = execution_price * shares
        fee = max(trading.min_commission, amount * trading.commission_rate)
        total = amount + fee
        if total > self.cash:
            affordable = int((self.cash - trading.min_commission) / execution_price / trading.lot_size) * trading.lot_size
            shares = max(0, affordable)
            if shares <= 0:
                return None
            amount = execution_price * shares
            fee = max(trading.min_commission, amount * trading.commission_rate)
            total = amount + fee

        self.cash -= total
        current = self.positions.get(symbol)
        if current:
            new_shares = current.shares + shares
            current.avg_price = (current.avg_price * current.shares + amount) / new_shares
            current.shares = new_shares
            current.last_price = execution_price
        else:
            self.positions[symbol] = Position(symbol=symbol, shares=shares, avg_price=execution_price, last_price=execution_price)

        order = Order(_now(), symbol, "BUY", execution_price, shares, amount, fee, 0.0, note)
        self.orders.append(order)
        return order

    def sell(self, symbol: str, price: float, shares: int, trading: TradingConfig, note: str = "") -> Order | None:
        current = self.positions.get(symbol)
        if not current:
            return None
        shares = min(shares, current.shares)
        shares = int(shares / trading.lot_size) * trading.lot_size
        if shares <= 0:
            return None

        execution_price = price * (1 - trading.slippage_rate)
        amount = execution_price * shares
        fee = max(trading.min_commission, amount * trading.commission_rate)
        tax = amount * trading.stamp_tax_rate
        self.cash += amount - fee - tax
        current.shares -= shares
        current.last_price = execution_price
        if current.shares <= 0:
            del self.positions[symbol]

        order = Order(_now(), symbol, "SELL", execution_price, shares, amount, fee, tax, note)
        self.orders.append(order)
        return order

    def rebalance_to_weights(
        self,
        target_weights: dict[str, float],
        prices: dict[str, float],
        trading: TradingConfig,
        note: str = "rebalance",
    ) -> list[Order]:
        self.update_prices(prices)
        before_orders = len(self.orders)
        equity = self.equity()

        for symbol, pos in list(self.positions.items()):
            price = prices.get(symbol, pos.last_price)
            target_value = equity * target_weights.get(symbol, 0.0)
            current_value = pos.shares * price
            if current_value > target_value:
                diff_value = current_value - target_value
                shares = int(diff_value / price / trading.lot_size) * trading.lot_size
                self.sell(symbol, price, shares, trading, note)

        equity = self.equity()
        for symbol, weight in target_weights.items():
            price = prices.get(symbol)
            if not price or price <= 0:
                continue
            current_value = self.positions.get(symbol).shares * price if symbol in self.positions else 0.0
            target_value = equity * weight
            if target_value > current_value:
                diff_value = target_value - current_value
                shares = int(diff_value / price / trading.lot_size) * trading.lot_size
                self.buy(symbol, price, shares, trading, note)

        return self.orders[before_orders:]

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {symbol: asdict(position) for symbol, position in self.positions.items()},
            "orders": [asdict(order) for order in self.orders],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        return cls(
            cash=float(data["cash"]),
            positions={symbol: Position(**raw) for symbol, raw in data.get("positions", {}).items()},
            orders=[Order(**raw) for raw in data.get("orders", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, initial_cash: float) -> "Account":
        if not path.exists():
            return cls(cash=initial_cash)
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
