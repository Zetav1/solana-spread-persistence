#!/usr/bin/env python3
"""Spread Persistence Report for Solana DEX quote scans.

Reads the ``spreads.csv`` log written by a read-only Jupiter quote scanner
(one row per venue quote per scan), removes frozen venue quotes, recomputes
the cross-venue spread per scan and finds spreads that *persist* across
consecutive scans.

Usage::

    python analyze_spreads.py spreads.csv [--threshold 30] [--min-scans 3]
        [--max-gap 300] [--stale-scans 5] [--out report.md] [--runs-csv runs.csv]

The script is analysis only: it reads one local CSV file, makes no network
calls and never touches wallets or blockchains.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pandas as pd

BEST_VENUE = "Jupiter (best)"
SCAN_KEYS = ["pair", "timestamp"]

REQUIRED_COLUMNS = [
    "timestamp",
    "slot",
    "pair",
    "amount",
    "venue",
    "out_amount",
    "price",
    "vs_best_bps",
    "impact_pct",
    "route",
    "cross_venue_spread_bps",
    "round_trip_bps",
]
STALE_COLUMN = "stale"  # optional: absent in the legacy 12-column format
TEXT_COLUMNS = ["timestamp", "pair", "venue", "route", STALE_COLUMN]
NUMERIC_COLUMNS = [
    "slot",
    "amount",
    "out_amount",
    "price",
    "vs_best_bps",
    "impact_pct",
    "cross_venue_spread_bps",
    "round_trip_bps",
]

RUN_COLUMNS = [
    "pair",
    "start",
    "end",
    "duration_s",
    "scans",
    "mean_spread_bps",
    "max_spread_bps",
    "dominant_outlier",
    "mean_round_trip_bps",
]
FROZEN_COLUMNS = [
    "pair",
    "venue",
    "start",
    "flagged_at",
    "end",
    "duration_s",
    "excluded_scans",
    "frozen_out_amount",
    "mean_vs_best_bps",
]

# Recomputed spreads are rounded to this many decimals before comparing with
# the threshold, so float artefacts (29.9999999999) do not break ``>=``.
SPREAD_PRECISION = 6


class InputError(Exception):
    """The input file cannot be analysed (missing file or columns)."""


# --------------------------------------------------------------------------
# Data containers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    threshold: float = 30.0
    min_scans: int = 3
    max_gap: float = 300.0
    stale_scans: int = 5
    # False: once a quote is confirmed frozen, exclude the whole period of
    # identical quotes (from the first one). True: exclude only the scans
    # flagged by the repeat rule itself.
    strict_frozen: bool = False


@dataclass
class SkipCounts:
    bad_field_count: int = 0
    invalid_values: int = 0
    duplicates: int = 0

    @property
    def total(self) -> int:
        return self.bad_field_count + self.invalid_values + self.duplicates


@dataclass
class LoadedData:
    rows: pd.DataFrame
    rows_read: int
    skipped: SkipCounts
    has_stale_column: bool


@dataclass
class StaleCheck:
    available: bool
    compared: int = 0
    flagged_not_frozen: int = 0  # stale=true, our logic: not frozen
    frozen_not_flagged: int = 0  # stale=false, our logic: frozen

    @property
    def disagreements(self) -> int:
        return self.flagged_not_frozen + self.frozen_not_flagged


@dataclass
class Analysis:
    source: str
    params: Params
    data: LoadedData
    quotes: pd.DataFrame
    scans: pd.DataFrame
    runs: pd.DataFrame
    pair_stats: pd.DataFrame
    outliers: pd.DataFrame
    frozen_periods: pd.DataFrame
    stale_check: StaleCheck
    findings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1. Load and validate
# --------------------------------------------------------------------------


def _normalize(column: pd.Series) -> pd.Series:
    """Strip a low-cardinality text column (pair, venue, stale) quickly.

    Stripping each unique value once and mapping is much faster than
    ``.str.strip()`` on 500k rows.
    """
    column = column.fillna("")
    return column.map({value: value.strip() for value in column.unique()})


def load_spreads(path: str | Path) -> LoadedData:
    """Read ``spreads.csv``, validate its layout and skip malformed rows.

    Rows with the wrong number of fields, unparseable timestamps, missing
    pair/venue, non-numeric values or non-positive prices are skipped and
    counted. A missing required column raises :class:`InputError`.
    """
    path = Path(path)
    if not path.is_file():
        raise InputError(f"input file not found: {path}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", pd.errors.ParserWarning)
        try:
            # Text columns stay strings; numeric columns are parsed by the fast
            # C parser and only fall back to to_numeric if they contain junk.
            raw = pd.read_csv(
                path,
                dtype={c: str for c in TEXT_COLUMNS},
                keep_default_na=False,
                na_values=[""],
                skipinitialspace=True,
                on_bad_lines="warn",
                encoding="utf-8-sig",  # also accepts a UTF-8 byte order mark
            )
        except pd.errors.EmptyDataError as exc:
            raise InputError(f"input file is empty: {path}") from exc
        except UnicodeDecodeError as exc:
            raise InputError(f"input file is not valid UTF-8: {path}") from exc
    bad_field_count = sum(
        str(w.message).count("Skipping line")
        for w in caught
        if issubclass(w.category, pd.errors.ParserWarning)
    )

    raw.columns = [str(c).strip() for c in raw.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise InputError(
            f"missing required column(s): {', '.join(missing)}. "
            f"Expected: {', '.join(REQUIRED_COLUMNS)}[, {STALE_COLUMN}]"
        )
    has_stale = STALE_COLUMN in raw.columns

    rows = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                raw["timestamp"], utc=True, format="ISO8601", errors="coerce"
            ),
            "pair": _normalize(raw["pair"]),
            "venue": _normalize(raw["venue"]),
            "route": raw["route"].fillna(""),
        }
    )
    invalid = rows["timestamp"].isna() | rows["pair"].eq("") | rows["venue"].eq("")
    for col in NUMERIC_COLUMNS:
        column = raw[col]
        if pd.api.types.is_numeric_dtype(column):
            values = column.astype("float64")
        else:
            # Junk in the column: a non-empty cell that is not a number is malformed.
            values = pd.to_numeric(column.str.strip(), errors="coerce")
            invalid |= column.notna() & column.str.strip().ne("") & values.isna()
        invalid |= values.abs().eq(float("inf"))
        rows[col] = values
    invalid |= ~(rows["price"] > 0) | ~(rows["out_amount"] > 0)

    if has_stale:
        flag = raw[STALE_COLUMN].fillna("")
        values = {"true": True, "false": False}
        mapping = {v: values.get(v.strip().lower(), pd.NA) for v in flag.unique()}
        rows[STALE_COLUMN] = flag.map(mapping).astype("boolean")
    else:
        rows[STALE_COLUMN] = pd.array([pd.NA] * len(rows), dtype="boolean")

    valid = rows.loc[~invalid]
    deduped = valid.drop_duplicates(subset=["pair", "timestamp", "venue"], keep="first")

    skipped = SkipCounts(
        bad_field_count=bad_field_count,
        invalid_values=int(invalid.sum()),
        duplicates=len(valid) - len(deduped),
    )
    rows = deduped.sort_values(SCAN_KEYS + ["venue"], kind="mergesort").reset_index(drop=True)
    # Categorical keys make the many groupbys below several times faster.
    rows["pair"] = rows["pair"].astype("category")
    rows["venue"] = rows["venue"].astype("category")
    return LoadedData(
        rows=rows,
        rows_read=len(raw) + bad_field_count,
        skipped=skipped,
        has_stale_column=has_stale,
    )


# --------------------------------------------------------------------------
# 2. Frozen-quote detection
# --------------------------------------------------------------------------


def detect_frozen(rows: pd.DataFrame, stale_scans: int, strict: bool = False) -> pd.DataFrame:
    """Return the single-venue quotes with frozen-quote flags.

    Per (pair, venue) in time order, a quote is a *repeat* when its
    ``out_amount`` equals that venue's previous quote **and** the
    ``Jupiter (best)`` output changed in between. A changed quote resets the
    counter to 0; an unchanged quote in an unchanged market keeps it. A quote
    is frozen once the counter reaches ``stale_scans - 1``.

    Added columns: ``best_out``, ``vs_best_eff``, ``repeats``, ``segment`` (id
    of the run of identical quotes), ``frozen`` (the repeat rule, compared with
    the ``stale`` column) and ``excluded`` (what steps 3-6 drop: the whole
    frozen period, or only the ``frozen`` rows when ``strict``).
    """
    best = rows.loc[rows["venue"] == BEST_VENUE, SCAN_KEYS + ["out_amount"]].rename(
        columns={"out_amount": "best_out"}
    )
    quotes = rows.loc[rows["venue"] != BEST_VENUE].merge(best, on=SCAN_KEYS, how="left")
    quotes = quotes.sort_values(["pair", "venue", "timestamp"], kind="mergesort").reset_index(
        drop=True
    )

    by_venue = quotes.groupby(["pair", "venue"], sort=False, observed=True)
    prev_out = by_venue["out_amount"].shift()
    prev_best = by_venue["best_out"].shift()

    same_quote = quotes["out_amount"].eq(prev_out)
    market_moved = (
        quotes["best_out"].notna() & prev_best.notna() & quotes["best_out"].ne(prev_best)
    )
    # Every changed quote (and the first quote of each venue) opens a segment.
    segment = (~same_quote).cumsum()
    repeats = (same_quote & market_moved).astype("int64").groupby(segment).cumsum()

    quotes["segment"] = segment
    quotes["repeats"] = repeats
    quotes["frozen"] = repeats >= stale_scans - 1
    if strict:
        quotes["excluded"] = quotes["frozen"]
    else:
        confirmed = quotes.loc[quotes["frozen"], "segment"].unique()
        quotes["excluded"] = quotes["segment"].isin(confirmed)
    computed_vs_best = (quotes["out_amount"] / quotes["best_out"] - 1.0) * 10_000
    quotes["quote_vs_best_bps"] = computed_vs_best
    quotes["vs_best_eff"] = quotes["vs_best_bps"].fillna(computed_vs_best)
    return quotes


def check_stale_flag(quotes: pd.DataFrame, available: bool) -> StaleCheck:
    """Compare our frozen flags with the scanner's own ``stale`` column."""
    if not available:
        return StaleCheck(available=False)
    logged = quotes.loc[quotes[STALE_COLUMN].notna()]
    flag = logged[STALE_COLUMN].astype(bool)
    ours = logged["frozen"].astype(bool)
    return StaleCheck(
        available=True,
        compared=len(logged),
        flagged_not_frozen=int((flag & ~ours).sum()),
        frozen_not_flagged=int((~flag & ours).sum()),
    )


def summarize_frozen(quotes: pd.DataFrame) -> pd.DataFrame:
    """One row per frozen period: a run of identical quotes that got flagged.

    ``start`` is the first scan with the identical quote, ``flagged_at`` the
    first scan where the repeat rule marks it frozen and ``end`` the last scan
    before the quote changes (repeats never decrease inside a run, so every
    scan from ``flagged_at`` to ``end`` is frozen).
    """
    flagged_segments = quotes.loc[quotes["frozen"], "segment"].unique()
    if len(flagged_segments) == 0:
        return pd.DataFrame(columns=FROZEN_COLUMNS)

    seg = quotes.loc[quotes["segment"].isin(flagged_segments)]
    periods = seg.groupby("segment").agg(
        pair=("pair", "first"),
        venue=("venue", "first"),
        start=("timestamp", "min"),
        end=("timestamp", "max"),
        excluded_scans=("excluded", "sum"),
        frozen_out_amount=("out_amount", "first"),
        mean_vs_best_bps=("quote_vs_best_bps", "mean"),
    )
    periods["flagged_at"] = seg.loc[seg["frozen"]].groupby("segment")["timestamp"].min()
    periods["duration_s"] = (periods["end"] - periods["start"]).dt.total_seconds()
    periods[["pair", "venue"]] = periods[["pair", "venue"]].astype(str)
    periods = periods.sort_values(["duration_s", "pair", "venue"], ascending=[False, True, True])
    return periods.reset_index(drop=True)[FROZEN_COLUMNS]


# --------------------------------------------------------------------------
# 3-4. Per-scan aggregation and outlier venue
# --------------------------------------------------------------------------


def _scan_spread(quotes: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Spread in bps from max/min price per scan; undefined with < 2 venues."""
    stats = quotes.groupby(SCAN_KEYS, observed=True)["price"].agg(["max", "min", "count"])
    spread = ((stats["max"] / stats["min"] - 1.0) * 10_000).round(SPREAD_PRECISION)
    return spread.where(stats["count"] >= 2), stats["count"]


def build_scans(rows: pd.DataFrame, quotes: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Collapse quote rows to one row per (pair, timestamp) scan."""
    scans = rows.groupby(SCAN_KEYS, observed=True).agg(
        round_trip_bps=("round_trip_bps", "first"),
        logged_spread_bps=("cross_venue_spread_bps", "first"),
    )
    live = quotes.loc[~quotes["excluded"]]

    spread, venues = _scan_spread(live)
    raw_spread, _ = _scan_spread(quotes)
    scans["spread_bps"] = spread.reindex(scans.index)
    scans["venues"] = venues.reindex(scans.index).fillna(0).astype("int64")
    scans["spread_incl_frozen_bps"] = raw_spread.reindex(scans.index)
    scans["frozen_quotes"] = (
        quotes.groupby(SCAN_KEYS, observed=True)["excluded"].sum().reindex(scans.index).fillna(0).astype("int64")
    )

    # Outlier: the live single venue with the lowest vs_best_bps (ties: by name).
    outlier = (
        live.dropna(subset=["vs_best_eff"])
        .sort_values(SCAN_KEYS + ["vs_best_eff", "venue"], kind="mergesort")
        .drop_duplicates(subset=SCAN_KEYS)
        .set_index(SCAN_KEYS)["venue"]
    )
    scans["outlier_venue"] = outlier.reindex(scans.index).astype(object)
    scans["anomalous"] = scans["spread_bps"].ge(threshold)
    scans = scans.reset_index()
    scans["pair"] = scans["pair"].astype(str)  # plain strings from here on
    return scans.sort_values(SCAN_KEYS, kind="mergesort").reset_index(drop=True)


# --------------------------------------------------------------------------
# 5. Persistent anomaly runs
# --------------------------------------------------------------------------


def find_runs(scans: pd.DataFrame, params: Params) -> pd.DataFrame:
    """Group anomalous scans into runs; return the persistent ones.

    A run is a sequence of consecutive anomalous scans of one pair in which no
    gap between neighbouring scans exceeds ``max_gap`` seconds. Any scan below
    the threshold (or with an undefined spread) ends the run.
    """
    by_pair = scans.groupby("pair", sort=False, observed=True)
    gap = by_pair["timestamp"].diff().dt.total_seconds()
    prev_anomalous = by_pair["anomalous"].shift(fill_value=False).astype(bool)

    starts = scans["anomalous"] & (~prev_anomalous | gap.isna() | gap.gt(params.max_gap))
    # Assign before filtering: assigning a Series to an empty frame adopts its index.
    anomalous = scans.assign(run_id=starts.cumsum()).loc[scans["anomalous"]]
    if anomalous.empty:
        return pd.DataFrame(columns=RUN_COLUMNS)

    runs = anomalous.groupby("run_id").agg(
        pair=("pair", "first"),
        start=("timestamp", "min"),
        end=("timestamp", "max"),
        scans=("timestamp", "size"),
        mean_spread_bps=("spread_bps", "mean"),
        max_spread_bps=("spread_bps", "max"),
        mean_round_trip_bps=("round_trip_bps", "mean"),
    )
    runs["duration_s"] = (runs["end"] - runs["start"]).dt.total_seconds()

    votes = (
        anomalous.dropna(subset=["outlier_venue"])
        .groupby(["run_id", "outlier_venue"], observed=True)
        .size()
        .reset_index(name="n")
        .sort_values(["run_id", "n", "outlier_venue"], ascending=[True, False, True])
        .drop_duplicates(subset="run_id")
        .set_index("run_id")["outlier_venue"]
    )
    runs["dominant_outlier"] = votes.reindex(runs.index)

    persistent = runs.loc[runs["scans"] >= params.min_scans]
    persistent = persistent.sort_values(
        ["duration_s", "scans", "start"], ascending=[False, False, True]
    )
    return persistent.reset_index(drop=True)[RUN_COLUMNS]


# --------------------------------------------------------------------------
# 6. Per-pair statistics and outlier table
# --------------------------------------------------------------------------


def pair_statistics(
    scans: pd.DataFrame, runs: pd.DataFrame, quotes: pd.DataFrame
) -> pd.DataFrame:
    records = []
    for pair, group in scans.groupby("pair", sort=True, observed=True):
        spread = group["spread_bps"].dropna()
        round_trip = group["round_trip_bps"].dropna()
        pair_runs = runs.loc[runs["pair"] == pair]
        longest = (
            pair_runs.sort_values(["duration_s", "scans"], ascending=False).iloc[0]
            if not pair_runs.empty
            else None
        )
        records.append(
            {
                "pair": pair,
                "scans": len(group),
                "first": group["timestamp"].min(),
                "last": group["timestamp"].max(),
                "covered_s": (group["timestamp"].max() - group["timestamp"].min()).total_seconds(),
                "anomalous_scans": int(group["anomalous"].sum()),
                "pct_anomalous": 100.0 * group["anomalous"].mean(),
                "median_spread_bps": spread.median(),
                "p95_spread_bps": spread.quantile(0.95) if not spread.empty else float("nan"),
                "max_spread_bps": spread.max(),
                "persistent_runs": len(pair_runs),
                "longest_run_scans": int(longest["scans"]) if longest is not None else 0,
                "longest_run_s": float(longest["duration_s"]) if longest is not None else 0.0,
                "median_round_trip_bps": round_trip.median(),
                "pct_round_trip_positive": (
                    100.0 * round_trip.gt(0).mean() if not round_trip.empty else float("nan")
                ),
                "frozen_excluded": int(quotes.loc[quotes["pair"] == pair, "excluded"].sum()),
            }
        )
    return pd.DataFrame(records)


def outlier_table(scans: pd.DataFrame) -> pd.DataFrame:
    """How often each venue was the outlier during anomalous scans, per pair."""
    anomalous = scans.loc[scans["anomalous"]]
    if anomalous.empty:
        return pd.DataFrame(columns=["pair", "venue", "scans", "pct"])
    counts = (
        anomalous.dropna(subset=["outlier_venue"])
        .groupby(["pair", "outlier_venue"], observed=True)
        .size()
        .reset_index(name="scans")
        .rename(columns={"outlier_venue": "venue"})
    )
    totals = anomalous.groupby("pair", observed=True).size()
    counts["pct"] = 100.0 * counts["scans"] / counts["pair"].map(totals)
    return counts.sort_values(["pair", "scans", "venue"], ascending=[True, False, True]).reset_index(
        drop=True
    )


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def analyze(data: LoadedData, params: Params, source: str = "spreads.csv") -> Analysis:
    quotes = detect_frozen(data.rows, params.stale_scans, params.strict_frozen)
    scans = build_scans(data.rows, quotes, params.threshold)
    runs = find_runs(scans, params)
    analysis = Analysis(
        source=source,
        params=params,
        data=data,
        quotes=quotes,
        scans=scans,
        runs=runs,
        pair_stats=pair_statistics(scans, runs, quotes),
        outliers=outlier_table(scans),
        frozen_periods=summarize_frozen(quotes),
        stale_check=check_stale_flag(quotes, data.has_stale_column),
    )
    analysis.findings = derive_findings(analysis)
    return analysis


# --------------------------------------------------------------------------
# Findings (always derived from the analysed data)
# --------------------------------------------------------------------------


def derive_findings(a: Analysis) -> list[str]:
    if a.scans.empty:
        return ["No valid scans were found in the input file."]
    findings: list[str] = []
    thr = fmt_num(a.params.threshold)
    stats = a.pair_stats

    top = stats.sort_values(["pct_anomalous", "pair"], ascending=[False, True]).iloc[0]
    if top["anomalous_scans"] > 0:
        text = (
            f"{top['pair']}: spread ≥ {thr} bps in {fmt_num(top['pct_anomalous'])}% of scans "
            f"({top['anomalous_scans']}/{top['scans']})"
        )
        pair_outliers = a.outliers.loc[a.outliers["pair"] == top["pair"]]
        if not pair_outliers.empty:
            lead = pair_outliers.iloc[0]
            text += f"; {lead['venue']} was the outlier in {fmt_num(lead['pct'])}% of them"
        findings.append(text + ".")
    else:
        findings.append(f"No pair reached a spread of {thr} bps or more in any scan.")

    if not a.runs.empty:
        run = a.runs.iloc[0]
        outlier = run["dominant_outlier"] if pd.notna(run["dominant_outlier"]) else "no venue"
        findings.append(
            f"Longest persistent run: {run['pair']} for {fmt_duration(run['duration_s'])} "
            f"({run['scans']} scans, {fmt_ts(run['start'])} → {fmt_ts(run['end'])}), "
            f"mean spread {fmt_num(run['mean_spread_bps'])} bps, dominant outlier {outlier}."
        )
    else:
        findings.append(
            f"No persistent run (≥ {a.params.min_scans} consecutive scans ≥ {thr} bps) was found."
        )

    if not a.frozen_periods.empty:
        period = a.frozen_periods.iloc[0]
        removed = int(
            (a.scans["spread_incl_frozen_bps"].ge(a.params.threshold) & ~a.scans["anomalous"]).sum()
        )
        findings.append(
            f"{period['venue']} on {period['pair']} was frozen at "
            f"{fmt_raw(period['frozen_out_amount'])} for {fmt_duration(period['duration_s'])} "
            f"({fmt_num(period['mean_vs_best_bps'])} bps vs the best route); excluding frozen "
            f"quotes removed {removed} scan(s) that would otherwise have counted as anomalous."
        )

    with_rt = stats.dropna(subset=["pct_round_trip_positive"])
    if not with_rt.empty:
        best_rt = with_rt.sort_values(
            ["pct_round_trip_positive", "pair"], ascending=[False, True]
        ).iloc[0]
        findings.append(
            f"{best_rt['pair']} had a positive round trip in "
            f"{fmt_num(best_rt['pct_round_trip_positive'])}% of scans "
            f"(median {fmt_num(best_rt['median_round_trip_bps'])} bps)."
        )
    return findings[:4]


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def fmt_num(value: float | int | None) -> str:
    """Round to 2 decimals; missing values become an em dash."""
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.2f}"


def fmt_raw(value: float | None) -> str:
    """Raw quote amounts are shown as logged (up to 10 significant digits)."""
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.10g}"


def fmt_duration(seconds: float | None) -> str:
    """Format seconds as ``1h 05m`` (rounded to the nearest minute)."""
    if seconds is None or pd.isna(seconds):
        return "—"
    minutes = int(float(seconds) / 60 + 0.5)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_ts(ts: pd.Timestamp | None) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M:%SZ")


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[bool]) -> str:
    """Markdown table padded to fixed column widths (readable in a terminal)."""
    if not rows:
        return "_None._"
    cells = [[str(c).replace("|", "\\|") for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells), 3) for i, h in enumerate(headers)]

    def line(values: Sequence[str]) -> str:
        padded = [
            v.rjust(w) if is_right else v.ljust(w)
            for v, w, is_right in zip(values, widths, right)
        ]
        return "| " + " | ".join(padded) + " |"

    rule = "| " + " | ".join(
        ("-" * (w - 1) + ":") if is_right else "-" * w for w, is_right in zip(widths, right)
    ) + " |"
    return "\n".join([line(headers), rule, *(line(r) for r in cells)])


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def render_report(a: Analysis) -> str:
    p = a.params
    skipped = a.data.skipped
    rows = a.data.rows
    time_range = (
        f"{fmt_ts(rows['timestamp'].min())} → {fmt_ts(rows['timestamp'].max())} "
        f"({fmt_duration((rows['timestamp'].max() - rows['timestamp'].min()).total_seconds())})"
        if not rows.empty
        else "—"
    )
    file_format = (
        "13-column format with `stale`"
        if a.data.has_stale_column
        else "legacy 12-column format, no `stale`"
    )
    out: list[str] = [
        "# Spread Persistence Report",
        "",
        f"- **Input file:** `{a.source}` ({file_format})",
        f"- **Time range:** {time_range}",
        f"- **Parameters:** threshold ≥ {fmt_num(p.threshold)} bps · min-scans {p.min_scans} · "
        f"max-gap {fmt_num(p.max_gap)} s · stale-scans {p.stale_scans}",
        f"- **Rows:** {a.data.rows_read} read, {len(rows)} analysed, "
        f"**{skipped.total} skipped** (wrong field count: {skipped.bad_field_count}, "
        f"invalid values: {skipped.invalid_values}, duplicates: {skipped.duplicates})",
        f"- **Scans:** {len(a.scans)} · anomalous: {int(a.scans['anomalous'].sum()) if not a.scans.empty else 0}"
        f" · persistent runs: {len(a.runs)}",
        "",
        "Spreads are recomputed per scan as `(max price / min price − 1) × 10,000` over the "
        "non-frozen single venues; the logged `cross_venue_spread_bps` is not used.",
        "",
        "## Per-pair summary",
        "",
        md_table(
            [
                "Pair", "Scans", "Time covered", "≥ thr %", "Median bps", "P95 bps", "Max bps",
                "Runs", "Longest run", "Median RT bps", "RT > 0 %", "Frozen excl.",
            ],
            [
                [
                    s["pair"], str(s["scans"]), fmt_duration(s["covered_s"]),
                    fmt_num(s["pct_anomalous"]), fmt_num(s["median_spread_bps"]),
                    fmt_num(s["p95_spread_bps"]), fmt_num(s["max_spread_bps"]),
                    str(s["persistent_runs"]),
                    f"{s['longest_run_scans']} scans / {fmt_duration(s['longest_run_s'])}"
                    if s["persistent_runs"] else "—",
                    fmt_num(s["median_round_trip_bps"]), fmt_num(s["pct_round_trip_positive"]),
                    str(s["frozen_excluded"]),
                ]
                for _, s in a.pair_stats.iterrows()
            ],
            [False, True, True, True, True, True, True, True, True, True, True, True],
        ),
        "",
        "RT = round trip (`round_trip_bps`, taken once per scan).",
        "",
        "## Persistent runs (longest first)",
        "",
        md_table(
            [
                "Pair", "Start", "End", "Duration", "Seconds", "Scans", "Mean bps", "Max bps",
                "Dominant outlier", "Mean RT bps",
            ],
            [
                [
                    r["pair"], fmt_ts(r["start"]), fmt_ts(r["end"]), fmt_duration(r["duration_s"]),
                    fmt_num(r["duration_s"]), str(r["scans"]), fmt_num(r["mean_spread_bps"]),
                    fmt_num(r["max_spread_bps"]),
                    r["dominant_outlier"] if pd.notna(r["dominant_outlier"]) else "—",
                    fmt_num(r["mean_round_trip_bps"]),
                ]
                for _, r in a.runs.iterrows()
            ],
            [False, False, False, True, True, True, True, True, False, True],
        ),
        "",
        "## Outlier venues in anomalous scans",
        "",
        md_table(
            ["Pair", "Venue", "Times outlier", "Share of anomalous scans %"],
            [
                [o["pair"], o["venue"], str(o["scans"]), fmt_num(o["pct"])]
                for _, o in a.outliers.iterrows()
            ],
            [False, False, True, True],
        ),
        "",
        "## Frozen quotes",
        "",
        md_table(
            [
                "Pair", "Venue", "Start", "Flagged at", "End", "Duration", "Excluded scans",
                "Frozen out_amount", "vs best bps",
            ],
            [
                [
                    f["pair"], f["venue"], fmt_ts(f["start"]), fmt_ts(f["flagged_at"]),
                    fmt_ts(f["end"]), fmt_duration(f["duration_s"]), str(f["excluded_scans"]),
                    fmt_raw(f["frozen_out_amount"]), fmt_num(f["mean_vs_best_bps"]),
                ]
                for _, f in a.frozen_periods.iterrows()
            ],
            [False, False, False, False, False, True, True, True, True],
        ),
        "",
        f"A quote is flagged frozen once it has {p.stale_scans - 1} consecutive repeats "
        f"({p.stale_scans} identical scans while the best route moves). Start = first scan with "
        "the identical quote; " + (
            "only the flagged scans are excluded (`--strict-frozen`)."
            if p.strict_frozen
            else "once flagged, the whole period from Start to End is excluded."
        ),
        "",
        _stale_line(a.stale_check),
        "",
        "## Findings",
        "",
        *(f"- {finding}" for finding in a.findings),
        "",
    ]
    return "\n".join(out)


def _stale_line(check: StaleCheck) -> str:
    if not check.available:
        return "**Disagreements with the `stale` column:** n/a (legacy file without `stale`)."
    return (
        f"**Disagreements with the `stale` column:** {check.disagreements} of {check.compared} "
        f"rows with a `stale` value (stale=true but not frozen here: {check.flagged_not_frozen}; "
        f"frozen here but stale=false: {check.frozen_not_flagged})."
    )


def runs_to_csv(runs: pd.DataFrame, path: str | Path) -> None:
    export = runs.copy()
    for col in ("start", "end"):
        export[col] = export[col].map(lambda ts: ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    numeric = ["duration_s", "mean_spread_bps", "max_spread_bps", "mean_round_trip_bps"]
    export[numeric] = export[numeric].astype(float).round(2)
    export.to_csv(path, index=False, encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _bounded(kind: type, minimum: float, name: str):
    def parse(text: str) -> float | int:
        try:
            value = kind(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be a {kind.__name__}") from exc
        if value < minimum:
            raise argparse.ArgumentTypeError(f"{name} must be >= {minimum}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find cross-venue spreads that persist across consecutive scans."
    )
    parser.add_argument("csv", help="path to spreads.csv")
    parser.add_argument("--threshold", type=_bounded(float, 0, "--threshold"), default=30.0,
                        help="spread in bps at or above which a scan is anomalous (default 30)")
    parser.add_argument("--min-scans", type=_bounded(int, 1, "--min-scans"), default=3,
                        help="minimum consecutive anomalous scans for a persistent run (default 3)")
    parser.add_argument("--max-gap", type=_bounded(float, 0, "--max-gap"), default=300.0,
                        help="seconds between scans of a pair that break a run (default 300)")
    parser.add_argument("--stale-scans", type=_bounded(int, 2, "--stale-scans"), default=5,
                        help="identical quotes while the market moves => frozen (default 5)")
    parser.add_argument("--strict-frozen", action="store_true",
                        help="exclude only the scans flagged by the repeat rule, not the whole "
                             "frozen period")
    parser.add_argument("--out", help="write the Markdown report to this file (default stdout)")
    parser.add_argument("--runs-csv", help="also export the persistent runs table as CSV")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = Params(
        threshold=args.threshold,
        min_scans=args.min_scans,
        max_gap=args.max_gap,
        stale_scans=args.stale_scans,
        strict_frozen=args.strict_frozen,
    )
    try:
        data = load_spreads(args.csv)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    analysis = analyze(data, params, source=Path(args.csv).name)
    report = render_report(analysis)

    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Report written to {args.out}", file=sys.stderr)
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(report)
    if args.runs_csv:
        runs_to_csv(analysis.runs, args.runs_csv)
        print(f"Persistent runs written to {args.runs_csv}", file=sys.stderr)
    if data.skipped.total:
        print(f"Skipped {data.skipped.total} malformed row(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
