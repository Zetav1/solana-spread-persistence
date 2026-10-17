"""Report content and formatting, CLI behaviour, offline guarantee and speed."""

from __future__ import annotations

import csv
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

import analyze_spreads as az
from conftest import ROOT, frozen_series, spread_scan

SECTIONS = [
    "# Spread Persistence Report",
    "## Per-pair summary",
    "## Persistent runs (longest first)",
    "## Outlier venues in anomalous scans",
    "## Frozen quotes",
    "## Findings",
]


def demo_rows() -> list[dict[str, str]]:
    rows = []
    for i in range(6):
        rows += spread_scan(i * 100, 45 if i < 4 else 10, pair="JUP/USDC", round_trip=-3.0)
    rows += frozen_series(6, start=1000)
    return rows


# --- formatting helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(3900, "1h 05m"), (0, "0h 00m"), (89, "0h 01m"), (29, "0h 00m"), (86_400, "24h 00m"),
     (1229, "0h 20m")],
)
def test_fmt_duration(seconds: float, expected: str) -> None:
    assert az.fmt_duration(seconds) == expected


def test_fmt_num_rounds_to_two_decimals() -> None:
    assert az.fmt_num(29.005) in ("29.00", "29.01")
    assert az.fmt_num(1500) == "1500.00"
    assert az.fmt_num(float("nan")) == "—"


def test_md_table_is_aligned_and_escapes_pipes() -> None:
    table = az.md_table(["A", "B"], [["x|y", "1.00"]], [False, True])
    lines = table.splitlines()
    assert lines[1] == "| ---- | ---: |"
    assert "x\\|y" in lines[2]
    assert len({len(line) for line in lines}) == 1  # fixed-width columns


# --- report content --------------------------------------------------------------------


def test_report_has_all_six_sections_in_order(run_analysis) -> None:
    report = az.render_report(run_analysis(demo_rows()))
    positions = [report.index(s) for s in SECTIONS]
    assert positions == sorted(positions)
    assert "- **Parameters:** threshold ≥ 30.00 bps · min-scans 3 · max-gap 300.00 s" in report
    assert "**0 skipped**" in report


def test_report_tables_are_valid_markdown(run_analysis) -> None:
    report = az.render_report(run_analysis(demo_rows()))
    tables = re.findall(r"((?:^\|.*\|\n)+)", report, flags=re.M)
    assert len(tables) == 4  # summary, runs, outliers, frozen
    for table in tables:
        rows = table.strip().splitlines()
        assert re.fullmatch(r"\|( :?-+:? \|)+", rows[1].replace("-:", "-:"))
        assert len({row.count(" | ") for row in rows}) == 1


def test_report_numbers_use_two_decimals(run_analysis) -> None:
    report = az.render_report(run_analysis(demo_rows()))
    body = report.split("## Per-pair summary")[1].split("## Findings")[0]
    body = body.replace("89.0", "")  # raw frozen out_amount is shown as logged
    for number in re.findall(r"(?<![\w:.-])-?\d+\.\d+(?![\w:])", body):
        assert re.fullmatch(r"-?\d+\.\d{2}", number), number


def test_findings_are_derived_from_data(run_analysis) -> None:
    first = run_analysis(demo_rows()).findings
    assert 2 <= len(first) <= 4
    assert first[0].startswith("JUP/USDC: spread ≥ 30.00 bps in 66.67% of scans (4/6)")
    assert "Raydium was the outlier in 100.00% of them" in first[0]
    assert any("Phoenix on SOL/USDC was frozen at 89" in f for f in first)

    other_rows = [r for i in range(5) for r in spread_scan(i * 100, 80, pair="BONK/SOL")]
    second = run_analysis(other_rows).findings
    assert second[0].startswith("BONK/SOL: spread ≥ 30.00 bps in 100.00% of scans")
    assert second != first


def test_frozen_section_lists_periods(run_analysis) -> None:
    report = az.render_report(run_analysis(demo_rows()))
    frozen = report.split("## Frozen quotes")[1].split("## Findings")[0]
    assert re.search(r"\| SOL/USDC \| Phoenix \|.*\|\s+0h 08m \|\s+6 \|\s+89 \|\s+-1369\.69 \|", frozen)
    # the fixture logs stale=false everywhere, so the two flagged scans disagree
    assert "**Disagreements with the `stale` column:** 2 of 36 rows" in frozen


# --- CLI -----------------------------------------------------------------------------------


def test_cli_options_and_defaults() -> None:
    args = az.build_parser().parse_args(["spreads.csv"])
    assert (args.threshold, args.min_scans, args.max_gap, args.stale_scans) == (30, 3, 300, 5)
    assert args.out is None and args.runs_csv is None and args.strict_frozen is False


@pytest.mark.parametrize("bad", [["--min-scans", "0"], ["--stale-scans", "1"],
                                 ["--max-gap", "-1"], ["--threshold", "abc"]])
def test_cli_rejects_invalid_parameters(bad: list[str]) -> None:
    with pytest.raises(SystemExit):
        az.build_parser().parse_args(["spreads.csv", *bad])


def test_cli_writes_report_and_runs_csv(write_csv, tmp_path: Path) -> None:
    path = write_csv(demo_rows())
    out, runs_csv = tmp_path / "report.md", tmp_path / "runs.csv"
    code = az.main([str(path), "--threshold", "40", "--min-scans", "2", "--max-gap", "200",
                    "--stale-scans", "4", "--out", str(out), "--runs-csv", str(runs_csv)])
    assert code == 0
    report = out.read_text(encoding="utf-8")
    assert "threshold ≥ 40.00 bps · min-scans 2 · max-gap 200.00 s · stale-scans 4" in report
    with runs_csv.open(encoding="utf-8") as fh:
        exported = list(csv.DictReader(fh))
    assert list(exported[0]) == az.RUN_COLUMNS
    assert exported[0]["pair"] == "JUP/USDC" and exported[0]["scans"] == "4"
    assert exported[0]["mean_spread_bps"] == "45.0"


def test_cli_missing_file() -> None:
    assert az.main(["does-not-exist.csv"]) == 2


def test_script_runs_as_documented() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "analyze_spreads.py"), str(ROOT / "examples" / "sample_spreads.csv")],
        capture_output=True, encoding="utf-8", check=True,
    )
    assert result.stdout.startswith("# Spread Persistence Report")


# --- constraints ------------------------------------------------------------------------


def test_analysis_makes_no_network_calls(write_csv, monkeypatch) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    path = write_csv(demo_rows())
    assert az.main([str(path), "--out", str(path.with_suffix(".md"))]) == 0


def test_source_has_no_network_or_extra_dependencies() -> None:
    source = (ROOT / "analyze_spreads.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import) (\w+)", source, flags=re.M))
    assert imports <= {"__future__", "argparse", "sys", "warnings", "dataclasses", "pathlib",
                       "typing", "pandas"}
    assert "pandas" in imports


def test_500k_rows_under_10_seconds(tmp_path: Path) -> None:
    """Performance requirement: >= 500,000 rows in under 10 s (load + analyse + report)."""
    n_scans, venues = 28_000, ["Raydium", "Raydium CLMM", "Whirlpool", "Meteora DLMM", "Phoenix"]
    pairs = ["SOL/USDC", "BONK/SOL", "JUP/USDC"]
    scans = pd.DataFrame({"i": range(n_scans * len(pairs))})
    scans["pair"] = [pairs[i % 3] for i in range(len(scans))]
    scans["timestamp"] = (
        pd.Timestamp("2026-10-01", tz="UTC") + pd.to_timedelta(scans["i"] * 33.3, unit="s")
    ).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    scans["best"] = 100 + (scans["i"] % 97) * 0.01
    frames = [scans.assign(venue="Jupiter (best)", out=scans["best"], vs="")]
    for k, venue in enumerate(venues):
        out = scans["best"] * (1 - (k + (scans["i"] % 11)) / 2000)
        frames.append(scans.assign(venue=venue, out=out, vs=((out / scans["best"] - 1) * 1e4).round(2)))
    df = pd.concat(frames).sort_values(["i", "venue"])
    df = pd.DataFrame({
        "timestamp": df["timestamp"], "slot": df["i"], "pair": df["pair"], "amount": 1,
        "venue": df["venue"], "out_amount": df["out"], "price": df["out"], "vs_best_bps": df["vs"],
        "impact_pct": 0.1, "route": "Raydium CLMM → Whirlpool, split", "cross_venue_spread_bps": 30,
        "round_trip_bps": -1.5, "stale": "false",
    })
    path = tmp_path / "big.csv"
    df.to_csv(path, index=False)
    assert len(df) >= 500_000

    started = time.perf_counter()
    data = az.load_spreads(path)
    report = az.render_report(az.analyze(data, az.Params()))
    elapsed = time.perf_counter() - started
    assert len(data.rows) == len(df) and data.skipped.total == 0
    assert report.startswith("# Spread Persistence Report")
    assert elapsed < 10, f"took {elapsed:.1f}s"
