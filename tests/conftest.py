"""Helpers that build synthetic ``spreads.csv`` fixtures inside the tests."""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analyze_spreads as az  # noqa: E402

HEADER_13 = [
    "timestamp", "slot", "pair", "amount", "venue", "out_amount", "price", "vs_best_bps",
    "impact_pct", "route", "cross_venue_spread_bps", "round_trip_bps", "stale",
]
HEADER_12 = HEADER_13[:-1]
T0 = datetime(2026, 10, 17, 16, 0, 0, tzinfo=timezone.utc)


def iso(seconds: float) -> str:
    ts = T0 + timedelta(seconds=seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def scan(
    t: float,
    venues: dict[str, float],
    *,
    pair: str = "SOL/USDC",
    best: float | None = None,
    round_trip: float | str | None = 1.5,
    stale: dict[str, str] | None = None,
    routes: dict[str, str] | None = None,
    amount: float = 1,
) -> list[dict[str, str]]:
    """Rows of one scan: the ``Jupiter (best)`` row plus one row per venue.

    ``venues`` maps venue -> out_amount; price = out_amount / amount.
    """
    best = max(venues.values()) if best is None else best
    stale = stale or {}
    routes = routes or {}
    prices = [out / amount for out in venues.values()]
    logged = (max(prices) / min(prices) - 1) * 10_000 if len(prices) > 1 else 0.0
    rt = "" if round_trip is None else str(round_trip)

    def row(venue: str, out: float, vs_best: str, flag: str) -> dict[str, str]:
        return {
            "timestamp": iso(t), "slot": str(458_000_000 + int(t * 2.5)), "pair": pair,
            "amount": f"{amount:g}", "venue": venue, "out_amount": repr(out),
            "price": repr(out / amount), "vs_best_bps": vs_best, "impact_pct": "0.1",
            "route": routes.get(venue, venue), "cross_venue_spread_bps": f"{logged:.2f}",
            "round_trip_bps": rt, "stale": flag,
        }

    rows = [row("Jupiter (best)", best, "", "")]
    for venue, out in venues.items():
        rows.append(row(venue, out, f"{(out / best - 1) * 10_000:.2f}", stale.get(venue, "false")))
    return rows


def spread_scan(t: float, bps: float, **kwargs) -> list[dict[str, str]]:
    """A scan whose recomputed spread is exactly ``bps`` (Raydium is the outlier).

    Prices drift with ``t`` so that no venue looks like a frozen quote.
    """
    low = 100.0 + t / 1000
    return scan(t, {"Raydium": low, "Whirlpool": low * (1 + bps / 10_000),
                    "Meteora DLMM": low * (1 + bps / 20_000)}, **kwargs)


def frozen_series(
    n: int,
    *,
    frozen_value: float = 89.0,
    best_moves: bool = True,
    pair: str = "SOL/USDC",
    start: float = 0,
    step: float = 100,
) -> list[dict[str, str]]:
    """``n`` scans where Phoenix returns the same quote; the market may move."""
    rows: list[dict[str, str]] = []
    for i in range(n):
        mid = 103.0 + (0.05 * i if best_moves else 0.0)
        rows += scan(start + i * step, {"Whirlpool": mid, "Raydium CLMM": mid * 0.9998,
                                        "Phoenix": frozen_value}, pair=pair, best=mid)
    return rows


@pytest.fixture
def write_csv(tmp_path: Path) -> Callable[..., Path]:
    """Write fixture rows to a CSV file and return its path."""
    counter = iter(range(1_000_000))

    def _write(
        rows: list[dict[str, str]],
        header: list[str] = HEADER_13,
        extra_lines: list[str] | None = None,
    ) -> Path:
        path = tmp_path / f"spreads_{next(counter)}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore",
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            for line in extra_lines or []:
                fh.write(line + "\n")
        return path

    return _write


@pytest.fixture
def run_analysis(write_csv: Callable[..., Path]) -> Callable[..., az.Analysis]:
    """Write rows to CSV, load them and run the full analysis."""

    def _run(rows: list[dict[str, str]], header: list[str] = HEADER_13,
             extra_lines: list[str] | None = None, **params) -> az.Analysis:
        path = write_csv(rows, header, extra_lines)
        return az.analyze(az.load_spreads(path), az.Params(**params), source=path.name)

    return _run
