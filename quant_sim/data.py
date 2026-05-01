from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, TypeVar

import pandas as pd

from .config import AppConfig
from .utils import normalize_date, today_yyyymmdd


CANONICAL_COLUMNS = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "pct_chg",
    "换手率": "turnover",
}

COMMON_NAME_TO_SYMBOL = {
    "贵州茅台": "600519",
    "五粮液": "000858",
    "招商银行": "600036",
    "中国平安": "601318",
    "宁德时代": "300750",
    "比亚迪": "002594",
    "恒瑞医药": "600276",
    "隆基绿能": "601012",
    "中信证券": "600030",
    "紫金矿业": "601899",
    "万华化学": "600309",
    "美的集团": "000333",
    "伊利股份": "600887",
    "立讯精密": "002475",
    "中国神华": "601088",
}

SYMBOL_TO_NAME = {symbol: name for name, symbol in COMMON_NAME_TO_SYMBOL.items()}
T = TypeVar("T")


class MarketData:
    def __init__(self, config: AppConfig):
        self.config = config
        self.cache_dir = config.paths.cache_dir
        self._spot_cache: pd.DataFrame | None = None

    def resolve_symbol(self, symbol_or_name: str) -> str:
        text = str(symbol_or_name).strip()
        if re.fullmatch(r"\d{6}", text):
            return text
        if text in COMMON_NAME_TO_SYMBOL:
            return COMMON_NAME_TO_SYMBOL[text]

        # This fallback is intentionally last: the realtime full-market endpoint
        # can be flaky. Backtests should work with code-only configs offline from it.
        spot = self.fetch_spot()
        match = spot.loc[(spot["name"] == text) | (spot["symbol"] == text)]
        if match.empty:
            contains = spot.loc[spot["name"].astype(str).str.contains(text, regex=False, na=False)]
            if contains.empty:
                raise ValueError(f"Cannot resolve symbol or stock name: {symbol_or_name}")
            return str(contains.iloc[0]["symbol"]).zfill(6)
        return str(match.iloc[0]["symbol"]).zfill(6)

    def stock_name(self, symbol_or_name: str) -> str:
        symbol = self.resolve_symbol(symbol_or_name)
        if symbol in SYMBOL_TO_NAME:
            return SYMBOL_TO_NAME[symbol]
        try:
            spot = self.fetch_spot()
            row = spot.loc[spot["symbol"] == symbol]
            if not row.empty and "name" in row:
                return str(row.iloc[0]["name"])
        except Exception:
            pass
        return symbol

    def fetch_history(
        self,
        symbol_or_name: str,
        start_date: str,
        end_date: str | None = None,
        adjust: str = "qfq",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        symbol = self.resolve_symbol(symbol_or_name)
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        cache_path = self.cache_dir / f"{symbol}_{start}_{end}_{adjust}.csv"

        if use_cache and cache_path.exists():
            return self._read_history_cache(cache_path)

        raw = _fetch_history_from_any_provider(symbol, start, end, adjust)
        if raw.empty:
            raise ValueError(f"No history data returned for {symbol} from {start} to {end}")

        frame = _canonical_history(raw, symbol)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(cache_path, index=False, encoding="utf-8-sig")
        return frame

    def fetch_many_history(self, symbols: list[str], start_date: str, end_date: str | None = None) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        for item in symbols:
            symbol = self.resolve_symbol(item)
            result[symbol] = self.fetch_history(symbol, start_date, end_date)
        return result

    def fetch_spot(self) -> pd.DataFrame:
        if self._spot_cache is not None:
            return self._spot_cache.copy()

        ak = _akshare()
        raw = _with_retries(lambda: ak.stock_zh_a_spot_em(), label="realtime spot")
        if raw.empty:
            raise ValueError("No realtime spot data returned")

        columns = {
            "代码": "symbol",
            "名称": "name",
            "最新价": "price",
            "涨跌幅": "pct_chg",
            "涨跌额": "chg",
            "成交量": "volume",
            "成交额": "amount",
            "最高": "high",
            "最低": "low",
            "今开": "open",
            "昨收": "prev_close",
        }
        frame = raw.rename(columns=columns)
        keep = [col for col in columns.values() if col in frame.columns]
        frame = frame[keep].copy()
        frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
        for col in ["price", "pct_chg", "chg", "volume", "amount", "high", "low", "open", "prev_close"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        self._spot_cache = frame
        return frame.copy()

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        resolved = [self.resolve_symbol(item) for item in symbols]
        try:
            spot = self.fetch_spot()
            prices: dict[str, float] = {}
            for symbol in resolved:
                row = spot.loc[spot["symbol"] == symbol]
                if not row.empty and pd.notna(row.iloc[0]["price"]):
                    prices[symbol] = float(row.iloc[0]["price"])
            if prices:
                return prices
        except Exception:
            pass

        # Fallback for bad realtime connectivity: use the latest available daily close.
        prices = {}
        for symbol in resolved:
            try:
                history = self.fetch_history(symbol, self.config.start_date, today_yyyymmdd())
                if not history.empty:
                    prices[symbol] = float(history.iloc[-1]["close"])
            except Exception:
                continue
        return prices

    def watchlist_snapshot(self, symbols: list[str]) -> list[dict]:
        resolved = [self.resolve_symbol(item) for item in symbols]
        rows: list[dict] = []
        try:
            spot = self.fetch_spot()
        except Exception:
            spot = pd.DataFrame()

        for symbol in resolved:
            row = spot.loc[spot["symbol"] == symbol] if not spot.empty else pd.DataFrame()
            if not row.empty:
                item = row.iloc[0]
                price = float(item["price"]) if pd.notna(item.get("price")) else 0.0
                prev_close = float(item["prev_close"]) if pd.notna(item.get("prev_close")) else 0.0
                pct_chg = float(item["pct_chg"]) / 100 if pd.notna(item.get("pct_chg")) else _pct_from_prices(price, prev_close)
                rows.append(
                    {
                        "symbol": symbol,
                        "name": str(item.get("name") or self.stock_name(symbol)),
                        "price": price,
                        "prev_close": prev_close,
                        "pct_chg": pct_chg,
                        "source": "realtime",
                    }
                )
                continue

            try:
                history = self.fetch_history(symbol, self.config.start_date, today_yyyymmdd())
                last = history.tail(2)
                price = float(last.iloc[-1]["close"])
                prev_close = float(last.iloc[-2]["close"]) if len(last) > 1 else price
                rows.append(
                    {
                        "symbol": symbol,
                        "name": self.stock_name(symbol),
                        "price": price,
                        "prev_close": prev_close,
                        "pct_chg": _pct_from_prices(price, prev_close),
                        "source": "daily_close",
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "symbol": symbol,
                        "name": self.stock_name(symbol),
                        "price": 0.0,
                        "prev_close": 0.0,
                        "pct_chg": 0.0,
                        "source": f"unavailable: {exc}",
                    }
                )
        return rows

    @staticmethod
    def _read_history_cache(path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame["date"] = pd.to_datetime(frame["date"])
        return frame


def _canonical_history(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    frame = raw.rename(columns=CANONICAL_COLUMNS).copy()
    if "volume" not in frame.columns and "amount" in frame.columns:
        frame["volume"] = frame["amount"]
    missing = {"date", "open", "high", "low", "close", "volume"} - set(frame.columns)
    if missing:
        raise ValueError(f"History data for {symbol} misses columns: {sorted(missing)}")

    keep = [col for col in ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"] if col in frame.columns]
    frame = frame[keep]
    frame["symbol"] = symbol
    frame["date"] = pd.to_datetime(frame["date"])
    numeric_cols = [col for col in keep if col != "date"]
    for col in numeric_cols:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return frame


def _fetch_history_from_any_provider(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    ak = _akshare()
    market_symbol = _market_symbol(symbol)
    providers: list[tuple[str, Callable[[], pd.DataFrame]]] = [
        (
            "eastmoney",
            lambda: ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjust),
        ),
        (
            "sina",
            lambda: ak.stock_zh_a_daily(symbol=market_symbol, start_date=start, end_date=end, adjust=adjust),
        ),
        (
            "tencent",
            lambda: ak.stock_zh_a_hist_tx(symbol=market_symbol, start_date=start, end_date=end, adjust=adjust),
        ),
    ]
    errors: list[str] = []
    for name, fn in providers:
        try:
            frame = _with_retries(fn, label=f"{name} history {symbol}", attempts=2)
            if not frame.empty:
                return frame
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(f"All history providers failed for {symbol}: {' | '.join(errors)}")


def _market_symbol(symbol: str) -> str:
    prefix = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{symbol}"


def _with_retries(fn: Callable[[], T], label: str, attempts: int = 4, delay: float = 1.5) -> T:
    last_exc: Exception | None = None
    for idx in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if idx < attempts - 1:
                time.sleep(delay * (idx + 1))
    raise RuntimeError(f"Failed to fetch {label} after {attempts} attempts: {last_exc}") from last_exc


def _pct_from_prices(price: float, prev_close: float) -> float:
    if prev_close <= 0:
        return 0.0
    return price / prev_close - 1


def _akshare():
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("Missing dependency: run `pip install -r requirements.txt` first.") from exc
    return ak
