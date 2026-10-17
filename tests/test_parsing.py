"""Step 1: loading, validation and the exact column layout."""

from __future__ import annotations

import math

import pytest

import analyze_spreads as az
from conftest import HEADER_12, ROOT, scan, spread_scan

SAMPLE = ROOT / "examples" / "sample_spreads.csv"


def test_spec_sample_parses_and_recomputes_logged_spread() -> None:
    data = az.load_spreads(SAMPLE)
    assert len(data.rows) == 11 and data.skipped.total == 0 and data.has_stale_column
    analysis = az.analyze(data, az.Params())
    spreads = analysis.scans.set_index("pair")["spread_bps"]
    # The recomputed spread matches the scanner's logged value (no frozen venue here).
    assert spreads["SOL/USDC"] == pytest.approx(29.01, abs=0.01)
    assert spreads["BONK/SOL"] == pytest.approx(27.24, abs=0.01)


def test_scientific_notation_prices() -> None:
    rows = az.load_spreads(SAMPLE).rows
    bonk = rows.loc[(rows["pair"] == "BONK/SOL") & (rows["venue"] == "Jupiter (best)")]
    assert bonk["price"].iloc[0] == pytest.approx(2.86421177e-8, rel=1e-12)


def test_timestamps_are_utc() -> None:
    rows = az.load_spreads(SAMPLE).rows
    assert str(rows["timestamp"].dt.tz) == "UTC"
    assert rows["timestamp"].min().isoformat().startswith("2026-10-17T16:03:44.476")


def test_utf8_arrow_and_quoted_comma_in_route(write_csv) -> None:
    route = "Meteora DLMM, Whirlpool → Raydium CLMM"
    rows = scan(0, {"Raydium": 100.0, "Whirlpool": 100.1}, routes={"Raydium": route})
    data = az.load_spreads(write_csv(rows))
    assert data.skipped.total == 0
    assert data.rows.loc[data.rows["venue"] == "Raydium", "route"].iloc[0] == route


def test_empty_vs_best_and_round_trip(run_analysis) -> None:
    analysis = run_analysis(spread_scan(0, 40, round_trip=None) + spread_scan(100, 40))
    best_rows = analysis.data.rows.loc[analysis.data.rows["venue"] == "Jupiter (best)"]
    assert best_rows["vs_best_bps"].isna().all()
    assert analysis.scans["round_trip_bps"].isna().sum() == 1
    stats = analysis.pair_stats.iloc[0]
    assert stats["median_round_trip_bps"] == pytest.approx(1.5)  # empty one ignored
    assert stats["pct_round_trip_positive"] == pytest.approx(100.0)


@pytest.mark.parametrize("missing", ["price", "venue", "round_trip_bps"])
def test_missing_required_column_fails_clearly(write_csv, missing: str) -> None:
    header = [c for c in HEADER_12 if c != missing]
    path = write_csv(scan(0, {"Raydium": 100.0, "Whirlpool": 100.1}), header=header)
    with pytest.raises(az.InputError, match=f"missing required column.*{missing}"):
        az.load_spreads(path)


def test_missing_column_cli_exit_code(write_csv, capsys) -> None:
    header = [c for c in HEADER_12 if c != "pair"]
    path = write_csv(scan(0, {"Raydium": 100.0}), header=header)
    assert az.main([str(path)]) == 2
    assert "missing required column(s): pair" in capsys.readouterr().err


def test_malformed_rows_are_skipped_and_counted(write_csv) -> None:
    good = spread_scan(0, 40) + spread_scan(100, 40)
    extra = [
        "2026-10-17T16:10:00.000Z,1,SOL/USDC,1,Raydium,100,100,,0.1,Raydium,1,1,false,EXTRA",
        "not-a-date,1,SOL/USDC,1,Raydium,100,100,,0.1,Raydium,1,1,false",
        "2026-10-17T16:11:00.000Z,1,SOL/USDC,1,Raydium,abc,abc,,0.1,Raydium,1,1,false",
        "2026-10-17T16:12:00.000Z,1,SOL/USDC,1,Raydium,-5,-5,,0.1,Raydium,1,1,false",
        "2026-10-17T16:13:00.000Z,1,SOL/USDC",
    ]
    data = az.load_spreads(write_csv(good, extra_lines=extra))
    assert len(data.rows) == len(good)
    assert data.skipped.bad_field_count == 1
    assert data.skipped.invalid_values == 4
    assert data.skipped.total == 5
    report = az.render_report(az.analyze(data, az.Params()))
    assert "**5 skipped**" in report


def test_duplicate_quote_rows_are_counted_once(write_csv) -> None:
    rows = spread_scan(0, 40)
    data = az.load_spreads(write_csv(rows + [rows[1]]))
    assert data.skipped.duplicates == 1
    assert len(data.rows) == len(rows)


def test_legacy_12_column_file(run_analysis) -> None:
    rows = [r for t in range(4) for r in spread_scan(t * 100, 35)]
    analysis = run_analysis(rows, header=HEADER_12)
    assert not analysis.data.has_stale_column
    assert len(analysis.runs) == 1
    assert not analysis.stale_check.available
    assert "n/a (legacy file without `stale`)" in az.render_report(analysis)


def test_empty_file_and_header_only(write_csv, tmp_path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(az.InputError, match="empty"):
        az.load_spreads(empty)
    analysis = az.analyze(az.load_spreads(write_csv([])), az.Params())
    assert analysis.scans.empty and analysis.runs.empty
    assert "No valid scans" in az.render_report(analysis)


def test_whitespace_and_bom_are_tolerated(tmp_path) -> None:
    text = SAMPLE.read_text(encoding="utf-8").replace(",SOL/USDC,", ", SOL/USDC,")
    path = tmp_path / "bom.csv"
    path.write_text("﻿" + text, encoding="utf-8")
    data = az.load_spreads(path)
    assert data.skipped.total == 0
    assert set(data.rows["pair"]) == {"SOL/USDC", "BONK/SOL"}


def test_infinite_values_are_malformed(write_csv) -> None:
    rows = spread_scan(0, 40)
    rows[1] = dict(rows[1], price="inf")
    data = az.load_spreads(write_csv(rows))
    assert data.skipped.invalid_values == 1
    assert not any(math.isinf(p) for p in data.rows["price"])
