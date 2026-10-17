# Spread Persistence Report

- **Input file:** `demo_spreads.csv` (13-column format with `stale`)
- **Time range:** 2026-10-17 10:01:36Z → 2026-10-17 17:04:51Z (7h 03m)
- **Parameters:** threshold ≥ 30.00 bps · min-scans 3 · max-gap 300.00 s · stale-scans 5
- **Rows:** 3820 read, 3818 analysed, **2 skipped** (wrong field count: 1, invalid values: 1, duplicates: 0)
- **Scans:** 720 · anomalous: 274 · persistent runs: 8

Spreads are recomputed per scan as `(max price / min price − 1) × 10,000` over the non-frozen single venues; the logged `cross_venue_spread_bps` is not used.

## Per-pair summary

| Pair     | Scans | Time covered | ≥ thr % | Median bps | P95 bps | Max bps | Runs |       Longest run | Median RT bps | RT > 0 % | Frozen excl. |
| -------- | ----: | -----------: | ------: | ---------: | ------: | ------: | ---: | ----------------: | ------------: | -------: | -----------: |
| BONK/SOL |   240 |       7h 03m |   12.50 |      23.27 |   33.22 |   38.94 |    0 |                 — |        -10.87 |     0.45 |            0 |
| JUP/USDC |   240 |       7h 03m |   97.92 |      42.26 |   53.36 |   55.79 |    7 | 74 scans / 2h 02m |        -13.23 |     0.00 |            0 |
| SOL/USDC |   240 |       7h 03m |    3.75 |       8.74 |   16.95 |   39.33 |    1 |  9 scans / 0h 13m |          1.74 |    75.66 |           22 |

RT = round trip (`round_trip_bps`, taken once per scan).

## Persistent runs (longest first)

| Pair     | Start                | End                  | Duration | Seconds | Scans | Mean bps | Max bps | Dominant outlier | Mean RT bps |
| -------- | -------------------- | -------------------- | -------: | ------: | ----: | -------: | ------: | ---------------- | ----------: |
| JUP/USDC | 2026-10-17 15:02:59Z | 2026-10-17 17:04:51Z |   2h 02m | 7311.57 |    74 |    43.32 |   55.50 | Raydium          |      -13.82 |
| JUP/USDC | 2026-10-17 11:42:31Z | 2026-10-17 13:19:20Z |   1h 37m | 5809.34 |    59 |    42.41 |   55.79 | Raydium          |      -13.06 |
| JUP/USDC | 2026-10-17 10:02:05Z | 2026-10-17 11:04:22Z |   1h 02m | 3737.75 |    39 |    43.07 |   53.91 | Raydium          |      -13.67 |
| JUP/USDC | 2026-10-17 14:06:20Z | 2026-10-17 14:49:21Z |   0h 43m | 2581.89 |    27 |    43.23 |   55.11 | Raydium          |      -14.77 |
| JUP/USDC | 2026-10-17 11:07:33Z | 2026-10-17 11:39:13Z |   0h 32m | 1900.16 |    20 |    40.81 |   49.01 | Raydium          |      -13.62 |
| JUP/USDC | 2026-10-17 13:45:55Z | 2026-10-17 14:02:46Z |   0h 17m | 1010.29 |    11 |    42.45 |   48.94 | Raydium          |      -13.18 |
| SOL/USDC | 2026-10-17 11:07:06Z | 2026-10-17 11:20:29Z |   0h 13m |  803.70 |     9 |    35.31 |   39.33 | Raydium          |        0.74 |
| JUP/USDC | 2026-10-17 14:52:48Z | 2026-10-17 14:59:28Z |   0h 07m |  399.97 |     5 |    43.85 |   53.12 | Raydium          |      -17.21 |

## Outlier venues in anomalous scans

| Pair     | Venue   | Times outlier | Share of anomalous scans % |
| -------- | ------- | ------------: | -------------------------: |
| BONK/SOL | Raydium |            30 |                     100.00 |
| JUP/USDC | Raydium |           235 |                     100.00 |
| SOL/USDC | Raydium |             9 |                     100.00 |

## Frozen quotes

| Pair     | Venue   | Start                | Flagged at           | End                  | Duration | Excluded scans | Frozen out_amount | vs best bps |
| -------- | ------- | -------------------- | -------------------- | -------------------- | -------: | -------------: | ----------------: | ----------: |
| SOL/USDC | Phoenix | 2026-10-17 14:35:17Z | 2026-10-17 14:41:50Z | 2026-10-17 14:59:00Z |   0h 24m |             14 |         89.018192 |    -1687.93 |
| SOL/USDC | Phoenix | 2026-10-17 11:42:02Z | 2026-10-17 11:48:28Z | 2026-10-17 11:53:33Z |   0h 12m |              8 |        103.524467 |      -39.72 |

A quote is flagged frozen once it has 4 consecutive repeats (5 identical scans while the best route moves). Start = first scan with the identical quote; once flagged, the whole period from Start to End is excluded.

**Disagreements with the `stale` column:** 1 of 1807 rows with a `stale` value (stale=true but not frozen here: 1; frozen here but stale=false: 0).

## Findings

- JUP/USDC: spread ≥ 30.00 bps in 97.92% of scans (235/240); Raydium was the outlier in 100.00% of them.
- Longest persistent run: JUP/USDC for 2h 02m (74 scans, 2026-10-17 15:02:59Z → 2026-10-17 17:04:51Z), mean spread 43.32 bps, dominant outlier Raydium.
- Phoenix on SOL/USDC was frozen at 89.018192 for 0h 24m (-1687.93 bps vs the best route); excluding frozen quotes removed 20 scan(s) that would otherwise have counted as anomalous.
- SOL/USDC had a positive round trip in 75.66% of scans (median 1.74 bps).
