#!/usr/bin/env python3
"""Generate a deterministic synthetic ``spreads.csv`` for demos and benchmarks.

It mimics the real scanner: a scan of SOL/USDC, BONK/SOL and JUP/USDC every
~90-110 s, one row per venue quote, and these scripted episodes:

* JUP/USDC: Raydium sits ~45 bps below the best route almost all the time.
* SOL/USDC: Raydium widens to ~35-40 bps for a while (a persistent run).
* SOL/USDC: Phoenix freezes twice. The first time happens in the legacy part
  of the file (empty ``stale``, logged spread includes it); the second time
  it is stuck at 89.018192 USDC for over 20 minutes while the market is ~103.
* The scanner is stopped for 25 minutes (a gap larger than ``--max-gap``).
* Two malformed rows and routes with ``→`` and quoted commas.

Usage::

    python examples/make_demo_csv.py examples/demo_spreads.csv [--scans 240]
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta, timezone

HEADER = [
    "timestamp", "slot", "pair", "amount", "venue", "out_amount", "price", "vs_best_bps",
    "impact_pct", "route", "cross_venue_spread_bps", "round_trip_bps", "stale",
]
# pair -> (amount, start price, venue offsets in bps (mean, sd), round trip (mean, sd))
PAIRS: dict[str, tuple[float, float, dict[str, tuple[float, float]], tuple[float, float]]] = {
    "SOL/USDC": (1, 103.4, {"Raydium": (-8, 4), "Raydium CLMM": (-3, 1.5), "Whirlpool": (-4, 1.5),
                           "Meteora DLMM": (-3, 1.5), "Phoenix": (-9, 4)}, (1.5, 2.5)),
    "BONK/SOL": (10_000_000, 2.864e-8, {"Raydium": (-24, 6), "Raydium CLMM": (-5, 2),
                                        "Whirlpool": (-1, 1), "Meteora DLMM": (-16, 4)}, (-11, 4)),
    "JUP/USDC": (100, 0.4215, {"Raydium": (-45, 6), "Raydium CLMM": (-4, 2),
                               "Whirlpool": (-6, 2), "Meteora DLMM": (-5, 2)}, (-14, 5)),
}
LEGACY_SCANS = 100            # rows before this scan index have an empty `stale`
SCANNER_STOP = (120, 25 * 60)  # (scan index, seconds stopped)
SOL_RAYDIUM_WIDE = range(40, 49)
# window -> frozen out_amount (None: stuck 45 bps below the market at the start)
PHOENIX_FROZEN = {range(60, 69): None, range(150, 165): 89.018192}


def fmt(x: float) -> str:
    return f"{x:.9g}"


def generate(n_scans: int, seed: int = 7) -> list[list[str]]:
    rng = random.Random(seed)
    t = datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc)
    slot = 458_000_000
    mid = {pair: spec[1] for pair, spec in PAIRS.items()}
    frozen_out: dict[range, float] = {}
    rows: list[list[str]] = []

    for i in range(n_scans):
        t += timedelta(seconds=rng.uniform(90, 110))
        if i == SCANNER_STOP[0]:
            t += timedelta(seconds=SCANNER_STOP[1])
        for k, (pair, (amount, _, venues, rt)) in enumerate(PAIRS.items()):
            ts = t + timedelta(seconds=14 * k + rng.uniform(0, 1))
            slot = 458_000_000 + int((ts - datetime(2026, 10, 17, tzinfo=timezone.utc)).total_seconds() * 2.5)
            mid[pair] *= 1 + rng.gauss(0, 0.0012)
            best_out = mid[pair] * amount

            quotes: dict[str, float] = {}
            stale: dict[str, str] = {}
            for venue, (mu, sd) in venues.items():
                if venue == "Raydium" and pair == "SOL/USDC" and i in SOL_RAYDIUM_WIDE:
                    mu, sd = -38, 3
                bps = min(0.0, rng.gauss(mu, sd))
                out = best_out * (1 + bps / 10_000)
                if pair == "SOL/USDC" and venue == "Phoenix":
                    for window, value in PHOENIX_FROZEN.items():
                        if i in window:
                            stuck = value if value else round(best_out * 0.9955, 6)
                            frozen_out.setdefault(window, stuck)
                            out = frozen_out[window]
                            # the scanner's own flag, from the 5th identical quote on;
                            # it flags one scan too early on purpose (a disagreement).
                            if i >= LEGACY_SCANS:
                                stale[venue] = "true" if i - window.start >= 3 else "false"
                quotes[venue] = round(out, 9) if pair == "BONK/SOL" else round(out, 6)
                stale.setdefault(venue, "false" if i >= LEGACY_SCANS else "")
            if pair == "SOL/USDC" and i % 11 == 5:
                quotes.pop("Phoenix")  # venue missing from some scans

            best = max(max(quotes.values()), best_out)
            live = [q for v, q in quotes.items() if stale.get(v) != "true"]
            logged_spread = (max(live) / min(live) - 1) * 10_000
            round_trip = "" if rng.random() < 0.05 else f"{rng.gauss(*rt):.2f}"
            common = [round_trip]

            def row(venue: str, out: float, vs_best: str, route: str, flag: str) -> list[str]:
                return [
                    ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
                    str(slot), pair, f"{amount:g}", venue, fmt(out), fmt(out / amount), vs_best,
                    f"{rng.uniform(0, 0.5):.4f}", route, f"{logged_spread:.2f}", *common, flag,
                ]

            best_route = rng.choice(["BisonFi", "Whirlpool", "Raydium CLMM → Whirlpool",
                                     "Meteora DLMM, Whirlpool (split 60/40)"])
            rows.append(row("Jupiter (best)", best, "", best_route, ""))
            for venue, out in quotes.items():
                route = f"{venue} → {venue}" if venue == "Raydium CLMM" else venue
                rows.append(row(venue, out, f"{(out / best - 1) * 10_000:.2f}", route, stale[venue]))

    # two malformed rows: an extra field and a broken timestamp
    bad = list(rows[10])
    rows.insert(11, bad[:5] + ["oops"] + bad[5:])
    bad = list(rows[20])
    bad[0] = "not-a-timestamp"
    rows.insert(21, bad)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("out", help="path of the CSV to write")
    parser.add_argument("--scans", type=int, default=240, help="scans per pair (default 240)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(generate(args.scans, args.seed))


if __name__ == "__main__":
    main()
