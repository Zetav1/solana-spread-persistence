# Spread Persistence Report

- **Input file:** `sample_spreads.csv` (13-column format with `stale`)
- **Time range:** 2026-10-17 16:03:44Z → 2026-10-17 16:03:58Z (0h 00m)
- **Parameters:** threshold ≥ 30.00 bps · min-scans 3 · max-gap 300.00 s · stale-scans 5
- **Rows:** 11 read, 11 analysed, **0 skipped** (wrong field count: 0, invalid values: 0, duplicates: 0)
- **Scans:** 2 · anomalous: 0 · persistent runs: 0

Spreads are recomputed per scan as `(max price / min price − 1) × 10,000` over the non-frozen single venues; the logged `cross_venue_spread_bps` is not used.

## Per-pair summary

| Pair     | Scans | Time covered | ≥ thr % | Median bps | P95 bps | Max bps | Runs | Longest run | Median RT bps | RT > 0 % | Frozen excl. |
| -------- | ----: | -----------: | ------: | ---------: | ------: | ------: | ---: | ----------: | ------------: | -------: | -----------: |
| BONK/SOL |     1 |       0h 00m |    0.00 |      27.24 |   27.24 |   27.24 |    0 |           — |        -11.08 |     0.00 |            0 |
| SOL/USDC |     1 |       0h 00m |    0.00 |      29.01 |   29.01 |   29.01 |    0 |           — |          1.89 |   100.00 |            0 |

RT = round trip (`round_trip_bps`, taken once per scan).

## Persistent runs (longest first)

_None._

## Outlier venues in anomalous scans

_None._

## Frozen quotes

_None._

A quote is flagged frozen once it has 4 consecutive repeats (5 identical scans while the best route moves). Start = first scan with the identical quote; once flagged, the whole period from Start to End is excluded.

**Disagreements with the `stale` column:** 0 of 0 rows with a `stale` value (stale=true but not frozen here: 0; frozen here but stale=false: 0).

## Findings

- No pair reached a spread of 30.00 bps or more in any scan.
- No persistent run (≥ 3 consecutive scans ≥ 30.00 bps) was found.
- SOL/USDC had a positive round trip in 100.00% of scans (median 1.89 bps).
