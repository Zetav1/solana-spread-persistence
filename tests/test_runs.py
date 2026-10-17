"""Steps 3-6: per-scan aggregation, persistent runs and per-pair statistics."""

from __future__ import annotations

import pytest

from conftest import scan, spread_scan


def series(spreads: list[float], *, step: float = 100, start: float = 0, **kwargs) -> list[dict]:
    """One scan per spread value, ``step`` seconds apart."""
    return [r for i, bps in enumerate(spreads) for r in spread_scan(start + i * step, bps, **kwargs)]


# --- per-scan aggregation -------------------------------------------------------


def test_collapses_to_one_row_per_scan_without_double_counting(run_analysis) -> None:
    analysis = run_analysis(series([40, 10], round_trip=2.5))
    scans = analysis.scans
    assert len(scans) == 2  # 8 quote rows -> 2 scans
    # round trip is repeated on every row of the scan but taken once, not summed
    assert scans["round_trip_bps"].tolist() == [2.5, 2.5]
    assert scans["spread_bps"].tolist() == pytest.approx([40.0, 10.0])


def test_spread_is_recomputed_not_taken_from_the_log(run_analysis) -> None:
    rows = spread_scan(0, 12)
    for r in rows:
        r["cross_venue_spread_bps"] = "999.99"
    assert run_analysis(rows).scans["spread_bps"].iloc[0] == pytest.approx(12.0)


def test_jupiter_best_is_not_part_of_spread_or_outlier(run_analysis) -> None:
    # The aggregated route is far better than every single venue; it must be ignored.
    rows = scan(0, {"Raydium": 100.0, "Whirlpool": 100.05}, best=110.0)
    s = run_analysis(rows).scans.iloc[0]
    assert s["spread_bps"] == pytest.approx(5.0)
    assert s["outlier_venue"] == "Raydium"


def test_outlier_is_the_venue_with_lowest_vs_best(run_analysis) -> None:
    rows = scan(0, {"Raydium": 100.2, "Whirlpool": 100.5, "Meteora DLMM": 99.9})
    assert run_analysis(rows).scans.iloc[0]["outlier_venue"] == "Meteora DLMM"


def test_missing_venues_and_single_venue_scan(run_analysis) -> None:
    rows = (
        spread_scan(0, 40)
        + scan(100, {"Raydium": 100.0, "Whirlpool": 100.5})  # Meteora missing: still 2 venues
        + scan(200, {"Whirlpool": 100.0})                    # one venue: spread undefined
    )
    scans = run_analysis(rows).scans
    assert scans["venues"].tolist() == [3, 2, 1]
    assert scans["spread_bps"].iloc[1] == pytest.approx(50.0)
    assert scans["spread_bps"].isna().iloc[2]
    assert scans["anomalous"].tolist() == [True, True, False]


# --- persistent runs --------------------------------------------------------------


def test_run_of_exactly_min_scans_is_persistent(run_analysis) -> None:
    runs = run_analysis(series([10, 35, 35, 35, 10])).runs
    assert len(runs) == 1 and runs.iloc[0]["scans"] == 3


def test_run_of_min_scans_minus_one_is_not_persistent(run_analysis) -> None:
    assert run_analysis(series([10, 35, 35, 10])).runs.empty


@pytest.mark.parametrize("min_scans,expected", [(1, 2), (2, 1), (4, 0)])
def test_min_scans_parameter(run_analysis, min_scans: int, expected: int) -> None:
    runs = run_analysis(series([35, 35, 35, 10, 35]), min_scans=min_scans).runs
    assert len(runs) == expected


def test_run_broken_by_gap_greater_than_max_gap(run_analysis) -> None:
    rows = series([35, 35, 35]) + series([35, 35, 35], start=200 + 301)
    runs = run_analysis(rows).runs
    assert len(runs) == 2
    assert runs["scans"].tolist() == [3, 3]


def test_gap_equal_to_max_gap_does_not_break_run(run_analysis) -> None:
    rows = series([35, 35, 35]) + series([35, 35, 35], start=200 + 300)
    runs = run_analysis(rows).runs
    assert len(runs) == 1 and runs.iloc[0]["scans"] == 6


def test_max_gap_parameter(run_analysis) -> None:
    rows = series([35, 35, 35, 35], step=120)
    assert len(run_analysis(rows, max_gap=100).runs) == 0
    assert len(run_analysis(rows, max_gap=120).runs) == 1


def test_run_broken_by_one_scan_below_threshold(run_analysis) -> None:
    runs = run_analysis(series([35, 35, 35, 29.99, 35, 35, 35])).runs
    assert runs["scans"].tolist() == [3, 3]


def test_threshold_boundary_equal_counts(run_analysis) -> None:
    analysis = run_analysis(series([30, 30, 30]))
    assert analysis.scans["spread_bps"].tolist() == [30.0, 30.0, 30.0]
    assert analysis.scans["anomalous"].all()
    assert len(analysis.runs) == 1


def test_just_below_threshold_does_not_count(run_analysis) -> None:
    analysis = run_analysis(series([29.99, 29.99, 29.99]))
    assert not analysis.scans["anomalous"].any() and analysis.runs.empty


def test_threshold_parameter(run_analysis) -> None:
    rows = series([25, 25, 25])
    assert run_analysis(rows).runs.empty
    assert len(run_analysis(rows, threshold=20).runs) == 1


def test_several_pairs_mixed_in_one_file(run_analysis) -> None:
    # Interleaved scans: SOL/USDC anomalous, JUP/USDC calm, BONK/SOL one long run.
    rows = []
    for i in range(4):
        rows += spread_scan(i * 100, 35, pair="SOL/USDC")
        rows += spread_scan(i * 100 + 10, 5, pair="JUP/USDC")
        rows += spread_scan(i * 100 + 20, 50, pair="BONK/SOL", amount=10_000_000)
    analysis = run_analysis(rows)
    runs = analysis.runs.set_index("pair")
    assert set(runs.index) == {"SOL/USDC", "BONK/SOL"}
    assert runs.loc["SOL/USDC", "scans"] == 4
    stats = analysis.pair_stats.set_index("pair")
    assert stats.loc["JUP/USDC", "pct_anomalous"] == 0
    assert stats.loc["BONK/SOL", "pct_anomalous"] == 100


def test_run_fields(run_analysis) -> None:
    rows = (
        spread_scan(0, 10)
        + spread_scan(100, 40, round_trip=2.0)
        + spread_scan(190, 50, round_trip=None)  # empty round trip is ignored in the mean
        + scan(300, {"Raydium": 100.0, "Whirlpool": 100.2, "Meteora DLMM": 99.7},
               round_trip=-4.0)  # 50.15 bps, Meteora is the outlier
        + spread_scan(410, 31, round_trip=5.0)
        + spread_scan(500, 1)
    )
    run = run_analysis(rows).runs.iloc[0]
    assert str(run["start"]) == "2026-10-17 16:01:40+00:00"
    assert str(run["end"]) == "2026-10-17 16:06:50+00:00"
    assert run["duration_s"] == 310
    assert run["scans"] == 4
    expected = [40, 50, (100.2 / 99.7 - 1) * 10_000, 31]
    assert run["mean_spread_bps"] == pytest.approx(sum(expected) / 4, abs=1e-6)
    assert run["max_spread_bps"] == pytest.approx(max(expected), abs=1e-6)
    assert run["dominant_outlier"] == "Raydium"  # 3 of 4 scans
    assert run["mean_round_trip_bps"] == pytest.approx((2.0 - 4.0 + 5.0) / 3)


def test_runs_sorted_by_duration_longest_first(run_analysis) -> None:
    rows = series([35] * 3) + series([10], start=300) + series([35] * 6, start=400)
    runs = run_analysis(rows).runs
    assert runs["scans"].tolist() == [6, 3]
    assert runs["duration_s"].is_monotonic_decreasing


# --- per-pair statistics -------------------------------------------------------------


def test_pair_statistics(run_analysis) -> None:
    spreads = [10, 20, 35, 35, 35, 40, 5, 60]
    rts = [1.0, -2.0, 3.0, None, 4.0, -1.0, 2.0, 0.0]
    rows = [r for i, (bps, rt) in enumerate(zip(spreads, rts))
            for r in spread_scan(i * 100, bps, round_trip=rt)]
    s = run_analysis(rows).pair_stats.iloc[0]
    assert s["scans"] == 8
    assert s["covered_s"] == 700
    assert s["pct_anomalous"] == pytest.approx(5 / 8 * 100)
    assert s["median_spread_bps"] == pytest.approx(35.0)
    assert s["max_spread_bps"] == pytest.approx(60.0)
    assert s["p95_spread_bps"] == pytest.approx(53.0)  # linear interpolation
    assert s["persistent_runs"] == 1
    assert (s["longest_run_scans"], s["longest_run_s"]) == (4, 300)
    assert s["median_round_trip_bps"] == pytest.approx(1.0)
    assert s["pct_round_trip_positive"] == pytest.approx(4 / 7 * 100)
    assert s["frozen_excluded"] == 0
