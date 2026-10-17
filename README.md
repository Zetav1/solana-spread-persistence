# Spread Persistence Analyzer

`analyze_spreads.py` reads the `spreads.csv` log of a read-only Solana quote scanner
(Jupiter aggregator plus single-venue quotes for SOL/USDC, BONK/SOL and JUP/USDC). It
finds the cross-venue spreads that **persist** across consecutive scans and filters out
**frozen quotes** (for example a stuck Phoenix book returning `89.018192` USDC for 1 SOL
while the market is near 103).

- One file, Python 3.10+, **pandas** only (pytest for the tests). Tested with pandas 2.3.3 and 3.0.3.
- Analysis only: it reads one local CSV file. It makes **no network calls** and has no API keys,
  wallets or blockchain access.
- Fast: about 500,000 rows in roughly 5 s on a laptop (see [Performance](#performance)).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python analyze_spreads.py spreads.csv [--threshold 30] [--min-scans 3] [--max-gap 300] \
    [--stale-scans 5] [--out report.md] [--runs-csv runs.csv]
```

| Option            | Default | Meaning |
|-------------------|---------|---------|
| `--threshold`     | 30      | Spread in bps at or above which a scan is anomalous (`>=`). |
| `--min-scans`     | 3       | Minimum consecutive anomalous scans for a persistent run. |
| `--max-gap`       | 300     | Seconds between two scans of a pair after which a run is broken (a gap equal to it does not break the run). |
| `--stale-scans`   | 5       | Identical quotes from a venue, while the market moves, after which the quote is frozen. |
| `--out`           | stdout  | Write the Markdown report to this file. |
| `--runs-csv`      | none    | Also export the persistent runs table as CSV. |
| `--strict-frozen` | off     | Optional extra: exclude only the scans flagged by the repeat rule, not the whole frozen period (see [Design decisions](#design-decisions)). |

Examples:

```bash
python analyze_spreads.py examples/sample_spreads.csv
python analyze_spreads.py examples/demo_spreads.csv --out examples/demo_report.md --runs-csv examples/demo_runs.csv
python analyze_spreads.py spreads.csv --threshold 25 --min-scans 5
```

Exit codes: `0` on success; `2` when the file is missing or empty, a required column is missing, or
a parameter is invalid. In each of these cases a clear message is printed to stderr. Malformed rows
never stop the run: they are skipped, counted and reported.

## Input format

The input has one row per venue quote per scan. Rows with the same `timestamp` and `pair` form one scan.
The required columns are:

`timestamp, slot, pair, amount, venue, out_amount, price, vs_best_bps, impact_pct, route, cross_venue_spread_bps, round_trip_bps`

The `stale` column is **optional**. Both the legacy 12-column format and the current 13-column format are accepted.

Handled explicitly:

- Timestamps are ISO 8601 and parsed as UTC.
- Prices in scientific notation are read correctly (BONK `2.86421177e-8`).
- `vs_best_bps` and `round_trip_bps` may be empty.
- `route` values may contain the UTF-8 character `→` and quoted commas. A UTF-8 BOM is accepted.
- Some venues may be missing from some scans.

A row is **skipped and counted** if any of these is true:

- it has the wrong number of fields;
- the timestamp cannot be parsed;
- `pair` or `venue` is empty;
- a numeric cell is non-numeric or infinite;
- `price` or `out_amount` is not positive;
- it duplicates a (pair, timestamp, venue) row already seen.

## How it works

The script follows the steps of the specification. Each step has its own function:

| Step | Function | What it does |
|---|---|---|
| 1. Load & validate | `load_spreads` | Parses the columns with pandas' C parser, checks that the required columns exist, and skips and counts malformed rows. |
| 2. Frozen quotes | `detect_frozen`, `summarize_frozen`, `check_stale_flag` | Applies the repeat/reset rule per (pair, venue) without using `stale`. It lists each frozen period and counts disagreements with `stale`. |
| 3. Collapse per scan | `build_scans` | Produces one row per (pair, timestamp). The spread is recomputed as `(max price / min price − 1) × 10,000` over the non-frozen single venues; with fewer than 2 venues it is undefined and the scan is not anomalous. `round_trip_bps` is taken once per scan, never summed or averaged across rows. |
| 4. Outlier venue | `build_scans` | The outlier is the non-frozen single venue (never `Jupiter (best)`) with the lowest `vs_best_bps`. |
| 5. Persistent runs | `find_runs` | Works per pair, vectorized. A run is consecutive anomalous scans with no gap greater than `--max-gap`; it is persistent with at least `--min-scans` scans. Recorded per run: start, end, duration, scans, mean/max spread, dominant outlier and mean round trip. |
| 6. Per-pair stats | `pair_statistics`, `outlier_table` | Scans, time covered, % ≥ threshold, median/p95/max spread, runs, longest run, median round trip, % positive round trip and excluded frozen quotes. |
| Report | `derive_findings`, `render_report`, `runs_to_csv` | Writes the six report sections. The findings are built from the computed tables and are never hardcoded. |

### Frozen-quote rule

The rule is applied per (pair, venue), in time order, comparing each quote with that venue's previous quote:

| Venue `out_amount` | `Jupiter (best)` `out_amount` | Repeat counter |
|---|---|---|
| changed | any | reset to 0 |
| identical | changed (the market moved) | +1 |
| identical | identical (flat market) | unchanged |

A quote is **frozen** once the counter reaches `--stale-scans − 1`. With the defaults that is 4 repeats, i.e. the 5th identical scan.

## Design decisions

Some points are open to interpretation in the specification. Each choice below is deliberate and covered by tests:

1. **The whole frozen period is excluded, not only the flagged scans.**
   - The repeat rule only confirms a frozen quote on its 5th identical scan. The first four identical
     quotes would otherwise stay in the data and still create a fake ~1,500 bps spread, which is often
     enough on its own to look like a persistent run.
   - So once a quote is confirmed frozen, the whole run of identical quotes is excluded, from the
     first identical scan to the last.
   - The frozen flag itself, which is what gets compared with `stale`, still follows the rule exactly.
   - The frozen-quotes table shows both `Start` (first identical quote) and `Flagged at` (the scan
     where the rule fires).
   - `--strict-frozen` switches to excluding only the flagged scans. In the demo data, strict mode
     keeps a fake Phoenix "run" of 4 scans at about 2,020 bps. The default mode removes it.
2. **A venue missing from a scan** does not reset its counter. The next quote is compared with the
   venue's previous quote, and with the best route at that time.
3. **A missing `Jupiter (best)` row** counts as "market did not move". The counter neither increases nor resets.
4. **`stale` disagreements** are counted per single-venue row that has a `stale` value. Empty values (legacy rows)
   are not compared. The two kinds are reported separately: `stale=true` but not frozen here, and frozen here but `stale=false`.
5. **The threshold comparison** uses the spread rounded to 1e-6 bps. This avoids float artefacts such as
   `29.999999999997` for an exact 30 bps spread, so `== 30` counts as specified.
6. **A scan with an undefined spread** (fewer than 2 non-frozen venues) is not anomalous and ends a run.
7. **Durations** are shown as `Xh MMm`, rounded to the nearest minute (`1h 05m`, `0h 03m`). The runs table
   also shows the exact duration in seconds.
8. **Rounding**: computed numbers are shown with 2 decimals. The frozen `out_amount` is a raw quote, so it is shown as
   logged (`89.018192`). Rounding it would hide the value that identifies the stuck book.
9. **The "time covered"** by a pair is the span from its first scan to its last. **"% of scans with a positive round trip"**
   counts only scans that have a round-trip value.
10. **Ties** are broken by venue name, so the output is deterministic. This applies to the outlier of a scan and the dominant outlier of a run.

## Report

The Markdown report has the six required sections:

1. Header: input file, time range, parameters, rows skipped.
2. Per-pair summary.
3. Persistent runs, longest first.
4. Outlier venues in anomalous scans.
5. Frozen quotes, with the count of disagreements with `stale`.
6. Findings.

Tables are padded to fixed column widths, so they read well in a terminal and also render as Markdown.

Example files in [`examples/`](examples):

| File | Content |
|---|---|
| `sample_spreads.csv` → [`sample_report.md`](examples/sample_report.md) | The two-scan sample from the specification. |
| `demo_spreads.csv` → [`demo_report.md`](examples/demo_report.md), [`demo_runs.csv`](examples/demo_runs.csv) | A 7-hour synthetic day made by `examples/make_demo_csv.py` (fixed seed). It includes the stuck Phoenix book at `89.018192`, an older frozen period in legacy rows (empty `stale`), a persistently bad Raydium quote on JUP/USDC, a 25-minute scanner stop, missing venues and two malformed rows. |

Excerpt of `examples/demo_report.md`:

```markdown
## Persistent runs (longest first)

| Pair     | Start                | End                  | Duration | Seconds | Scans | Mean bps | Max bps | Dominant outlier | Mean RT bps |
| -------- | -------------------- | -------------------- | -------: | ------: | ----: | -------: | ------: | ---------------- | ----------: |
| JUP/USDC | 2026-10-17 15:02:59Z | 2026-10-17 17:04:51Z |   2h 02m | 7311.57 |    74 |    43.32 |   55.50 | Raydium          |      -13.82 |
| JUP/USDC | 2026-10-17 11:42:31Z | 2026-10-17 13:19:20Z |   1h 37m | 5809.34 |    59 |    42.41 |   55.79 | Raydium          |      -13.06 |
...
| SOL/USDC | 2026-10-17 11:07:06Z | 2026-10-17 11:20:29Z |   0h 13m |  803.70 |     9 |    35.31 |   39.33 | Raydium          |        0.74 |

## Frozen quotes

| Pair     | Venue   | Start                | Flagged at           | End                  | Duration | Excluded scans | Frozen out_amount | vs best bps |
| -------- | ------- | -------------------- | -------------------- | -------------------- | -------: | -------------: | ----------------: | ----------: |
| SOL/USDC | Phoenix | 2026-10-17 14:35:17Z | 2026-10-17 14:41:50Z | 2026-10-17 14:59:00Z |   0h 24m |             14 |         89.018192 |    -1687.93 |
| SOL/USDC | Phoenix | 2026-10-17 11:42:02Z | 2026-10-17 11:48:28Z | 2026-10-17 11:53:33Z |   0h 12m |              8 |        103.524467 |      -39.72 |

**Disagreements with the `stale` column:** 1 of 1807 rows with a `stale` value (stale=true but not frozen here: 1; frozen here but stale=false: 0).

## Findings

- JUP/USDC: spread ≥ 30.00 bps in 97.92% of scans (235/240); Raydium was the outlier in 100.00% of them.
- Longest persistent run: JUP/USDC for 2h 02m (74 scans, 2026-10-17 15:02:59Z → 2026-10-17 17:04:51Z), mean spread 43.32 bps, dominant outlier Raydium.
- Phoenix on SOL/USDC was frozen at 89.018192 for 0h 24m (-1687.93 bps vs the best route); excluding frozen quotes removed 20 scan(s) that would otherwise have counted as anomalous.
- SOL/USDC had a positive round trip in 75.66% of scans (median 1.74 bps).
```

Without frozen-quote exclusion, the SOL/USDC maximum spread in this file would be about 2,028 bps instead of 39.33.

## Tests

```bash
python -m pytest
```

There are 75 tests. All fixtures are **synthetic CSV files built inside the tests** (`tests/conftest.py`).
No network is used.

| Required case | Test |
|---|---|
| Run of exactly `min-scans` (persistent) | `test_runs.py::test_run_of_exactly_min_scans_is_persistent` |
| Run of `min-scans − 1` (not persistent) | `test_runs.py::test_run_of_min_scans_minus_one_is_not_persistent` |
| Run broken by a gap > `max-gap` | `test_runs.py::test_run_broken_by_gap_greater_than_max_gap` (+ `test_gap_equal_to_max_gap_does_not_break_run`) |
| Run broken by one scan below the threshold | `test_runs.py::test_run_broken_by_one_scan_below_threshold` |
| Threshold boundary (`== 30` counts) | `test_runs.py::test_threshold_boundary_equal_counts` (+ `test_just_below_threshold_does_not_count`) |
| Several pairs mixed in one file | `test_runs.py::test_several_pairs_mixed_in_one_file` |
| Missing venues | `test_runs.py::test_missing_venues_and_single_venue_scan`, `test_frozen.py::test_venue_missing_from_a_scan_does_not_reset_the_counter` |
| Empty `round_trip_bps` | `test_parsing.py::test_empty_vs_best_and_round_trip`, `test_runs.py::test_run_fields` |
| Malformed row | `test_parsing.py::test_malformed_rows_are_skipped_and_counted` |
| `route` containing a comma | `test_parsing.py::test_utf8_arrow_and_quoted_comma_in_route` |
| Frozen: identical for exactly `--stale-scans` scans while the best route moves | `test_frozen.py::test_identical_for_exactly_stale_scans_while_market_moves_is_frozen` |
| Frozen: one scan fewer (not frozen) | `test_frozen.py::test_one_scan_fewer_is_not_frozen` |
| Frozen: identical while the best route is also unchanged (not frozen) | `test_frozen.py::test_identical_quotes_in_a_flat_market_are_not_frozen` |
| Frozen venue creating a fake spread ≥ 30 bps (disappears) | `test_frozen.py::test_frozen_venue_fake_spread_disappears_after_exclusion` |
| File without `stale` (12 columns) | `test_frozen.py::test_old_format_without_stale_column`, `test_parsing.py::test_legacy_12_column_file` |
| `stale` disagreeing with the logic | `test_frozen.py::test_stale_column_disagreements_are_counted_not_fatal` |

Other tests cover:

- the reset and flat-market branches of the counter, and custom `--stale-scans` values;
- every CLI option and its default, invalid parameters and a missing column (exit code 2);
- the run fields, which are checked by hand;
- per-pair statistics, including p95;
- the six report sections, valid Markdown tables and 2-decimal rounding;
- findings that change with the data;
- the runs CSV export;
- a **network-blocking test**, where `socket` is patched to fail and the analysis still runs;
- a **500,000-row performance test** under 10 s.

```text
$ python -m pytest
============================= test session starts =============================
platform win32 -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0
rootdir: job255
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.13.0, dash-4.3.0, langsmith-0.8.16, cov-7.1.0
collected 75 items

tests\test_frozen.py ...............                                     [ 20%]
tests\test_parsing.py ...............                                    [ 40%]
tests\test_report.py ........................                            [ 72%]
tests\test_runs.py .....................                                 [100%]

============================= 75 passed in 29.50s =============================
```

## Performance

These measurements were taken on a Windows laptop with Python 3.14 and pandas 3.0, using a
509,094-row file (`python examples/make_demo_csv.py big.csv --scans 32000`):

| Stage | Time |
|---|---|
| Loading and validating the CSV | ~2.6 s |
| Analysis | ~1.9 s |
| Rendering the report | ~0.2 s |
| End-to-end (`python analyze_spreads.py big.csv --out report.md`, including the Python and pandas start-up) | ~6.2 s |

The script stays this fast because it avoids per-row Python code entirely:

- numeric columns are parsed by pandas' C parser;
- frozen detection and run detection are vectorized (`groupby().shift()`, cumulative sums);
- `pair` and `venue` are categorical for the large groupbys.

## Project layout

```
analyze_spreads.py         # the script (single file)
requirements.txt           # pandas, pytest
pytest.ini
tests/
  conftest.py              # synthetic CSV builders
  test_parsing.py          # step 1: layout, malformed rows, 12/13 columns
  test_frozen.py           # step 2: frozen-quote rule and stale disagreements
  test_runs.py             # steps 3-6: per-scan aggregation, runs, statistics
  test_report.py           # report, CLI, offline guarantee, performance
examples/
  sample_spreads.csv       # sample from the specification
  sample_report.md
  make_demo_csv.py         # deterministic synthetic data generator
  demo_spreads.csv
  demo_report.md
  demo_runs.csv
```
