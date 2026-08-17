"""Scoreboard aggregation for the advisor outcome ledger (ADR-035, P4.3).

Pure functions from ledger rows to report-ready aggregates. The
rendering (a tools log-table today, a web card later) sits elsewhere;
both consume this module so the framing rules can't drift between
surfaces:

- **Hit-rate is ``better / (better + worse)``**, with ties reported
  BESIDE the rate, never folded into either side — a tie is "the
  counterfactual didn't distinguish them," not half a win.
- **A rate prints only at or above :data:`MIN_SAMPLE_FOR_RATE`
  decisive rows** (ADR-035 decision 6: aggregate before believing).
  Below the floor the counts still show; the rate is withheld.
- **Roles are reported against their OWN counterfactuals, never ranked
  against each other** (decision 7 — the cascade's branches see
  different populations). The one legitimate head-to-head is the
  paired quant-vs-hold comparison on the escalated subset, whose
  compare rule lives here as :func:`pair_quant_vs_hold`.
- **Nothing here touches a dollar figure** (decision 3). Counts and
  rates only; the arm JSONs' internal replay numbers stay in the
  ledger.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from wobblebot.ports.advisor import RecommendationOutcome

# Decisive rows (better + worse) required before a hit-rate is shown as
# a rate at all. Below this, a percentage invites exactly the
# over-reading ADR-035 decision 6 forbids.
MIN_SAMPLE_FOR_RATE = 30


@dataclass(frozen=True)
class ScoreboardCell:
    """One (role, granularity) cell of scored outcomes."""

    role: str
    granularity_minutes: int | None
    better: int
    worse: int
    tie: int

    @property
    def scored(self) -> int:
        """All scored rows in the cell."""
        return self.better + self.worse + self.tie

    @property
    def decisive(self) -> int:
        """Rows where the counterfactual distinguished the arms."""
        return self.better + self.worse

    @property
    def hit_rate(self) -> float | None:
        """``better / decisive``, or ``None`` below the sample floor."""
        if self.decisive < MIN_SAMPLE_FOR_RATE:
            return None
        return self.better / self.decisive


def aggregate_scored(
    rows: Iterable[tuple[str, RecommendationOutcome]],
) -> list[ScoreboardCell]:
    """Fold ``(role, outcome)`` pairs into per-(role, granularity) cells.

    Only rows with an outcome sign count; anything else in the input is
    ignored (the caller filters, this just refuses to miscount). Cells
    come back sorted by role, then granularity (finest first, ``None``
    last) for stable rendering.
    """
    tallies: dict[tuple[str, int | None], Counter[str]] = {}
    for role, outcome in rows:
        if outcome.outcome is None:
            continue
        key = (role, outcome.granularity_minutes)
        tallies.setdefault(key, Counter())[outcome.outcome] += 1
    cells = [
        ScoreboardCell(
            role=role,
            granularity_minutes=granularity,
            better=counts["better"],
            worse=counts["worse"],
            tie=counts["tie"],
        )
        for (role, granularity), counts in tallies.items()
    ]
    return sorted(
        cells,
        key=lambda c: (
            c.role,
            c.granularity_minutes is None,
            c.granularity_minutes if c.granularity_minutes is not None else 0,
        ),
    )


def unscoreable_taxonomy(outcomes: Iterable[RecommendationOutcome]) -> Counter[str]:
    """Bucket unscoreable rows by collapsed reason.

    Same collapsing rule as the evaluator driver's run summary — the
    reason up to the first ``:`` — so the two surfaces bucket
    identically (``"insufficient bars: 3/168 at 60m"`` and
    ``"insufficient bars: 9/168 at 60m"`` are one bucket).
    """
    buckets: Counter[str] = Counter()
    for outcome in outcomes:
        if outcome.scoreable:
            continue
        reason = outcome.unscoreable_reason or "unknown"
        buckets[reason.split(":", 1)[0]] += 1
    return buckets


def pair_quant_vs_hold(
    outcome_sign: Literal["better", "worse", "tie"],
) -> Literal["quant", "heuristic", "even"]:
    """One escalated input's paired winner, given the heuristic held.

    The load-bearing observation (ADR-035 decision 7's pairing, made
    concrete): on an input the cascade ESCALATED, the deterministic
    guard layer's would-have-said is HOLD by construction — that is
    what escalation means. A hold keeps the in-force config, which is
    exactly the counterfactual's in-force arm. So the paired
    quant-vs-heuristic result on such an input IS the quant outcome's
    sign, re-labeled: quant ``better`` beats the hold, quant ``worse``
    loses to it. No extra replay exists to run — the two comparisons
    are the same comparison.

    (The caller must verify the held premise by actually re-running
    the guard layer on the stored input, and route guard-fired rows
    elsewhere — this function is only the hold branch.)
    """
    if outcome_sign == "better":
        return "quant"
    if outcome_sign == "worse":
        return "heuristic"
    return "even"
