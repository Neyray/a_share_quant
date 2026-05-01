from __future__ import annotations

from datetime import date, datetime
from pathlib import Path


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def normalize_date(value: str | None) -> str:
    if not value:
        return today_yyyymmdd()
    cleaned = value.replace("-", "").replace("/", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise ValueError(f"Date must be YYYYMMDD or YYYY-MM-DD: {value}")
    return cleaned


def yyyymmdd_to_date(value: str) -> date:
    return datetime.strptime(normalize_date(value), "%Y%m%d").date()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def money(value: float) -> str:
    return f"{value:,.2f}"


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def to_jsonable_records(frame: pd.DataFrame) -> list[dict]:
    import pandas as pd

    copy = frame.copy()
    for column in copy.columns:
        if pd.api.types.is_datetime64_any_dtype(copy[column]):
            copy[column] = copy[column].dt.strftime("%Y-%m-%d")
    return copy.to_dict(orient="records")
