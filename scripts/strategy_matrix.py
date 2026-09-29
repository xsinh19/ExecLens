"""Print a markdown matrix of strategy results across all six scenarios.

    python scripts/strategy_matrix.py [--trials 30] [--seed 7] [--quantity 20000] [--horizon 1800]

The README's strategy table is produced by this script, so the numbers can
be regenerated and checked; nothing in it is typed by hand.
"""

from __future__ import annotations

import argparse

from execlens.data.synthetic import SCENARIOS
from execlens.simulation.comparison import (
    IMPLEMENTABLE_STRATEGIES,
    MIN_COMPLETION,
    ComparisonRequest,
    best_strategy,
    compare_strategies,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--quantity", type=int, default=20_000)
    parser.add_argument("--horizon", type=int, default=1_800)
    args = parser.parse_args()

    header = (
        "| Scenario | "
        + " | ".join(IMPLEMENTABLE_STRATEGIES)
        + f" | Lowest mean cost (>= {MIN_COMPLETION:.0%} filled) |"
    )
    print(
        f"Mean arrival cost, bps (+/- 95% CI half-width); BUY {args.quantity:,} shares over "
        f"{args.horizon // 60} min; {args.trials} trials per scenario; seed {args.seed}. "
        "Positive = worse. Completion shown when below 100%.\n"
    )
    print(header)
    print("|" + "---|" * (len(IMPLEMENTABLE_STRATEGIES) + 2))
    for scenario in SCENARIOS:
        result = compare_strategies(
            ComparisonRequest(
                scenario=scenario,
                quantity=args.quantity,
                horizon_seconds=args.horizon,
                trials=args.trials,
                seed=args.seed,
            )
        )
        rows = {row["strategy"]: row for row in result.summary.iter_rows(named=True)}
        cells = []
        for strategy in IMPLEMENTABLE_STRATEGIES:
            row = rows[strategy]
            cell = f"{row['mean_arrival_cost_bps']:.1f} (+/-{row['arrival_cost_ci95_bps']:.1f})"
            if row["mean_completion_ratio"] < 0.999:
                cell += f", {row['mean_completion_ratio']:.0%} filled"
            cells.append(cell)
        best = best_strategy(result.summary)
        print(f"| `{scenario}` | " + " | ".join(cells) + f" | {best} |")


if __name__ == "__main__":
    main()
