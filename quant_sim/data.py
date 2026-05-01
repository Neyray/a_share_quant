from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .config import AppConfig
from .utils import normalize_date


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


class MarketData:
    def __init__(self, config: AppConfig):
        self.config = config
        self.cache_dir = config.paths.cache_dir
        self._spot_cache: pd.DataFrame | None = None

    def resolve_symbol(self, symbol_or_name: str) -> str:
        text = str(symbol_or_name).strip()
        if re.fullmatch(r"\d{6}", text):
            return text

        spot = self.fetch_spot()
        match = spot.loc[(spot["name"] == text) | (spot["symbol"] == text)]
        if match.empty:
            contains = spot.loc[spot["name"].astype(str).str.contains(text, regex=False, na=False)]
            if contains.empty:
                raise ValueError(f"Cannot resolve symbol or stock name: {symbol_or_name}")
            return str(contains.iloc[0]["symbol"]).zfill(6)
        return str(match.iloc[0]["symbol"]).zfill(6)

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

        ak = _akshare()
        raw = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjust)
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
        raw = ak.stock_zh_a_spot_em()
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
        spot = self.fetch_spot()
        prices: dict[str, float] = {}
        for symbol in resolved:
            row = spot.loc[spot["symbol"] == symbol]
            if not row.empty and pd.notna(row.iloc[0]["price"]):
                prices[symbol] = float(row.iloc[0]["price"])
        return prices

    @staticmethod
    def _read_history_cache(path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame["date"] = pd.to_datetime(frame["date"])
        return frame


def _canonical_history(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    frame = raw.rename(columns=CANONICAL_COLUMNS).copy()
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


def _akshare():
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("Missing dependency: run `pip install -r requirements.txt` first.") from exc
    return ak
