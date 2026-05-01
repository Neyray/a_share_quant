from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradingConfig:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_rate: float = 0.0005
    lot_size: int = 100


@dataclass(frozen=True)
class StrategyConfig:
    max_positions: int = 5
    rebalance_days: int = 5
    lookback_days: int = 120
    ma_fast: int = 20
    ma_slow: int = 60
    momentum_days: int = 20
    stop_loss: float = 0.12
    take_profit: float = 0.35
    max_position_weight: float = 0.22
    cash_buffer: float = 0.05


@dataclass(frozen=True)
class PathConfig:
    cache_dir: Path = Path("data/cache")
    state_dir: Path = Path("data/state")
    report_dir: Path = Path("reports")


@dataclass(frozen=True)
class AppConfig:
    initial_cash: float = 200000.0
    start_date: str = "20180101"
    symbols: list[str] = field(default_factory=list)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    paths: PathConfig = field(default_factory=PathConfig)


def _dataclass_from_dict(cls: type, data: dict[str, Any]):
    allowed = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{key: value for key, value in data.items() if key in allowed})


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)

    paths_raw = raw.get("paths", {})
    paths = PathConfig(
        cache_dir=_resolve_path(config_path.parent, paths_raw.get("cache_dir", "data/cache")),
        state_dir=_resolve_path(config_path.parent, paths_raw.get("state_dir", "data/state")),
        report_dir=_resolve_path(config_path.parent, paths_raw.get("report_dir", "reports")),
    )

    return AppConfig(
        initial_cash=float(raw.get("initial_cash", 200000)),
        start_date=str(raw.get("start_date", "20180101")),
        symbols=[str(item).strip() for item in raw.get("symbols", []) if str(item).strip()],
        strategy=_dataclass_from_dict(StrategyConfig, raw.get("strategy", {})),
        trading=_dataclass_from_dict(TradingConfig, raw.get("trading", {})),
        paths=paths,
    )


def write_default_config(path: str | Path) -> Path:
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"Config already exists: {target}")

    source = Path(__file__).resolve().parents[1] / "config.example.json"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def ensure_dirs(config: AppConfig) -> None:
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    config.paths.state_dir.mkdir(parents=True, exist_ok=True)
    config.paths.report_dir.mkdir(parents=True, exist_ok=True)


def _resolve_path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()
