"""
Smart Exit Engine — profit-taking, edge erosion, and stale position management.

Exit conditions (checked every position cycle):
  1. PROFIT_TARGET  — unrealized ROI exceeds threshold based on confidence
  2. EDGE_ERODED    — market moved toward our estimate (edge consumed), lock in gains
  3. EDGE_REVERSED  — market moved PAST our estimate (we're now wrong-side), cut loss
  4. STALE_POSITION — position open > max hours with no resolution in sight
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ExitReason(Enum):
    HOLD           = "HOLD"
    PROFIT_TARGET  = "PROFIT_TARGET"
    EDGE_ERODED    = "EDGE_ERODED"
    EDGE_REVERSED  = "EDGE_REVERSED"
    STALE_POSITION = "STALE_POSITION"


@dataclass
class ExitSignal:
    should_exit: bool
    reason: ExitReason
    detail: str
    unrealized_pnl_pct: float


# Profit targets by confidence band
PROFIT_TARGETS = {
    85: 0.40,   # conf >= 85% → take profit at +40% ROI
    70: 0.55,   # conf >= 70% → take profit at +55% ROI
    55: 0.70,   # conf >= 55% → take profit at +70% ROI
    0:  0.90,   # conf <  55% → take profit at +90% ROI (let high-uncertainty run)
}

EDGE_EROSION_THRESHOLD = 0.03    # if remaining edge < 3%, exit
EDGE_REVERSAL_THRESHOLD = -0.05  # if edge flipped by > 5%, cut
MAX_STALE_HOURS = 168            # 7 days without resolution → flag for review


def _get_profit_target(confidence: int) -> float:
    for threshold in sorted(PROFIT_TARGETS.keys(), reverse=True):
        if confidence >= threshold:
            return PROFIT_TARGETS[threshold]
    return 0.90


def evaluate_exit(
    side: str,
    entry_price: float,
    current_price: float,
    estimated_prob: float,
    ai_confidence: int,
    hours_open: Optional[float] = None,
) -> ExitSignal:
    """
    Evaluate whether a position should be exited.
    Returns ExitSignal with recommendation.
    """
    if not current_price or current_price <= 0 or entry_price <= 0:
        return ExitSignal(False, ExitReason.HOLD, "insufficient price data", 0.0)

    # Unrealized PnL as percentage
    unrealized_pct = (current_price - entry_price) / entry_price

    # 1. Profit target
    target = _get_profit_target(ai_confidence)
    if unrealized_pct >= target:
        return ExitSignal(
            True, ExitReason.PROFIT_TARGET,
            f"ROI {unrealized_pct:.0%} >= target {target:.0%} (conf={ai_confidence}%)",
            unrealized_pct,
        )

    # 2. Edge erosion — market converged toward our estimate
    if side == "YES":
        remaining_edge = estimated_prob - current_price
    else:
        remaining_edge = (1 - estimated_prob) - current_price

    if 0 <= remaining_edge < EDGE_EROSION_THRESHOLD and unrealized_pct > 0.05:
        return ExitSignal(
            True, ExitReason.EDGE_ERODED,
            f"edge eroded to {remaining_edge:.1%}, locking {unrealized_pct:.0%} gain",
            unrealized_pct,
        )

    # 3. Edge reversal — we're now on the wrong side
    if remaining_edge < EDGE_REVERSAL_THRESHOLD:
        return ExitSignal(
            True, ExitReason.EDGE_REVERSED,
            f"edge reversed to {remaining_edge:.1%}, cutting loss",
            unrealized_pct,
        )

    # 4. Stale position
    if hours_open and hours_open > MAX_STALE_HOURS:
        return ExitSignal(
            True, ExitReason.STALE_POSITION,
            f"open {hours_open:.0f}h (>{MAX_STALE_HOURS}h limit)",
            unrealized_pct,
        )

    return ExitSignal(False, ExitReason.HOLD, "", unrealized_pct)
