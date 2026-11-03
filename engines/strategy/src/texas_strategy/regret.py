"""Regret matching only; this is not a CFR traversal or a poker strategy."""

from collections.abc import Sequence
from math import fsum, isfinite


def regret_matching(regrets: Sequence[float]) -> tuple[float, ...]:
    """Normalize positive regrets over the caller's already-legal action set.

    With no positive regret, use a uniform policy. Scale before summing to
    avoid overflow when individual finite regrets are very large.
    """
    values = tuple(float(value) for value in regrets)
    if not values:
        raise ValueError("At least one legal action is required")
    if not all(isfinite(value) for value in values):
        raise ValueError("Regrets must be finite")
    positive = tuple(max(value, 0.0) for value in values)
    maximum = max(positive)
    if maximum == 0:
        return (1.0 / len(values),) * len(values)
    scaled = tuple(value / maximum for value in positive)
    total = fsum(scaled)
    return tuple(value / total for value in scaled)
