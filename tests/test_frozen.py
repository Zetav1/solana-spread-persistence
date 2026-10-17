"""Step 2: frozen-quote detection, independent of the ``stale`` column."""

from __future__ import annotations

import pytest

import analyze_spreads as az
from conftest import HEADER_12, frozen_series, iso, scan


def phoenix(analysis: az.Analysis):
    return analysis.quotes.loc[analysis.quotes["venue"] == "Phoenix"].reset_index(drop=True)


def test_identical_for_exactly_stale_scans_while_market_moves_is_frozen(run_analysis) -> None:
    analysis = run_analysis(frozen_series(5))
    quotes = phoenix(analysis)
    assert quotes["repeats"].tolist() == [0, 1, 2, 3, 4]
    assert quotes["frozen"].tolist() == [False, False, False, False, True]
    assert len(analysis.frozen_periods) == 1
    period = analysis.frozen_periods.iloc[0]
    assert (period["pair"], period["venue"]) == ("SOL/USDC", "Phoenix")
    assert period["frozen_out_amount"] == 89.0
    assert period["duration_s"] == 400.0
    assert period["mean_vs_best_bps"] < -1000


def test_one_scan_fewer_is_not_frozen(run_analysis) -> None:
    analysis = run_analysis(frozen_series(4))
    assert not phoenix(analysis)["frozen"].any()
    assert analysis.frozen_periods.empty


def test_identical_quotes_in_a_flat_market_are_not_frozen(run_analysis) -> None:
    analysis = run_analysis(frozen_series(8, best_moves=False))
    assert phoenix(analysis)["repeats"].max() == 0
    assert not phoenix(analysis)["frozen"].any()


def test_flat_market_keeps_counter_and_changed_quote_resets_it(run_analysis) -> None:
    best = [103.0, 103.1, 103.2, 103.2, 103.2, 103.3, 103.4, 103.5]
    phx = [89.0, 89.0, 89.0, 89.0, 89.0, 89.0, 90.0, 90.0]
    rows = [
        r
        for i, (b, p) in enumerate(zip(best, phx))
        for r in scan(i * 100, {"Whirlpool": b, "Raydium CLMM": b * 0.9998, "Phoenix": p},
                      best=b)
    ]
    quotes = phoenix(run_analysis(rows))
    # moved, moved, flat (stays), flat (stays), moved, quote changed (reset), moved
    assert quotes["repeats"].tolist() == [0, 1, 2, 2, 2, 3, 0, 1]
    assert not quotes["frozen"].any()


def test_venue_missing_from_a_scan_does_not_reset_the_counter(run_analysis) -> None:
    rows = frozen_series(6)
    rows = [r for r in rows if not (r["venue"] == "Phoenix" and r["timestamp"] == iso(200))]
    quotes = phoenix(run_analysis(rows))
    assert len(quotes) == 5
    assert quotes["frozen"].tolist() == [False, False, False, False, True]


def test_custom_stale_scans(run_analysis) -> None:
    analysis = run_analysis(frozen_series(3), stale_scans=3)
    assert phoenix(analysis)["frozen"].tolist() == [False, False, True]


def test_frozen_venue_fake_spread_disappears_after_exclusion(run_analysis) -> None:
    analysis = run_analysis(frozen_series(8))
    scans = analysis.scans
    # Phoenix at 89 vs ~103 would be a ~1,500 bps spread in every scan ...
    assert (scans["spread_incl_frozen_bps"] > 1000).all()
    # ... but the whole frozen period is excluded, so no scan is anomalous.
    assert scans["spread_bps"].max() < 30
    assert not scans["anomalous"].any()
    assert analysis.runs.empty
    assert analysis.frozen_periods.iloc[0]["excluded_scans"] == 8
    assert analysis.pair_stats.iloc[0]["frozen_excluded"] == 8
    assert scans["outlier_venue"].ne("Phoenix").all()


def test_strict_mode_excludes_only_flagged_scans(run_analysis) -> None:
    analysis = run_analysis(frozen_series(8), strict_frozen=True)
    assert analysis.scans["anomalous"].tolist() == [True] * 4 + [False] * 4
    assert analysis.frozen_periods.iloc[0]["excluded_scans"] == 4


def test_stale_column_disagreements_are_counted_not_fatal(run_analysis) -> None:
    rows = frozen_series(6)
    phoenix_rows = [r for r in rows if r["venue"] == "Phoenix"]
    phoenix_rows[3]["stale"] = "true"   # scanner flags too early: true but not frozen
    phoenix_rows[4]["stale"] = "true"   # agreement: frozen and true
    phoenix_rows[5]["stale"] = "false"  # scanner misses it: frozen but false
    analysis = run_analysis(rows)
    check = analysis.stale_check
    assert check.available
    assert (check.flagged_not_frozen, check.frozen_not_flagged, check.disagreements) == (1, 1, 2)
    assert "**Disagreements with the `stale` column:** 2 of" in az.render_report(analysis)


def test_detection_ignores_stale_column(run_analysis) -> None:
    rows = frozen_series(3)
    for r in rows:
        if r["venue"] == "Phoenix":
            r["stale"] = "true"
    analysis = run_analysis(rows)
    assert not analysis.quotes["frozen"].any()
    assert analysis.stale_check.flagged_not_frozen == 3


def test_old_format_without_stale_column(run_analysis) -> None:
    analysis = run_analysis(frozen_series(6), header=HEADER_12)
    assert phoenix(analysis)["frozen"].sum() == 2
    assert not analysis.stale_check.available


def test_frozen_detection_is_per_pair(run_analysis) -> None:
    rows = frozen_series(5, pair="SOL/USDC") + frozen_series(4, pair="JUP/USDC", start=50)
    frozen = run_analysis(rows).quotes.groupby("pair", observed=True)["frozen"].sum()
    assert frozen["SOL/USDC"] == 1 and frozen["JUP/USDC"] == 0


@pytest.mark.parametrize("stale_scans", [2, 5, 7])
def test_repeat_threshold_matches_stale_scans(run_analysis, stale_scans: int) -> None:
    exact = phoenix(run_analysis(frozen_series(stale_scans), stale_scans=stale_scans))
    fewer = phoenix(run_analysis(frozen_series(stale_scans - 1), stale_scans=stale_scans))
    assert exact["frozen"].iloc[-1] and not fewer["frozen"].any()
