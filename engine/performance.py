"""
Performance Analytics Engine — tracks historical trade outcomes to enable adaptive strategy.

Features:
  - Win rate by category, confidence bucket, edge bucket
  - Adaptive thresholds: adjusts MIN_EDGE and MIN_CONF based on where the bot actually wins
  - Time-weighted signal scoring: freshness of article + hours_to_close factor
  - Smart exit signals: take-profit and stop-loss recommendations for open positions
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Confidence buckets for performance tracking
CONF_BUCKETS = [(90, 100), (75, 89), (60, 74), (45, 59)]
EDGE_BUCKETS = [(0.25, 1.0), (0.18, 0.25), (0.12, 0.18), (0.08, 0.12)]

# Smart exit thresholds
TAKE_PROFIT_RATIO = 0.70  # exit if market moved 70%+ toward our estimate
STOP_LOSS_REVERSAL = 0.05  # cut if edge has reversed by 5%+ against us
MAX_HOLDING_HOURS = 168    # force review after 7 days


def compute_performance_stats(positions: list) -> dict:
    """
    Analyze closed positions to compute performance breakdowns.
    Returns stats by category, confidence bucket, and edge bucket.
    """
    closed = [p for p in positions if p.result in ("WIN", "LOSS")]
    if not closed:
        return {"total": 0, "by_category": {}, "by_confidence": {}, "by_edge": {}}

    by_category = {}
    by_confidence = {}
    by_edge = {}

    for p in closed:
        won = p.result == "WIN"

        cat = p.category or "general"
        if cat not in by_category:
            by_category[cat] = {"wins": 0, "losses": 0, "pnl": 0.0}
        by_category[cat]["wins" if won else "losses"] += 1
        by_category[cat]["pnl"] += p.pnl_usd or 0

        conf = p.ai_confidence or 0
        for lo, hi in CONF_BUCKETS:
            if lo <= conf <= hi:
                key = f"{lo}-{hi}"
                if key not in by_confidence:
                    by_confidence[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
                by_confidence[key]["wins" if won else "losses"] += 1
                by_confidence[key]["pnl"] += p.pnl_usd or 0
                break

        edge = p.edge_at_entry or 0
        for lo, hi in EDGE_BUCKETS:
            if lo <= edge <= hi:
                key = f"{int(lo*100)}-{int(hi*100)}%"
                if key not in by_edge:
                    by_edge[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
                by_edge[key]["wins" if won else "losses"] += 1
                by_edge[key]["pnl"] += p.pnl_usd or 0
                break

    for bucket_group in (by_category, by_confidence, by_edge):
        for stats in bucket_group.values():
            total = stats["wins"] + stats["losses"]
            stats["winrate"] = round(stats["wins"] / total * 100, 1) if total else 0
            stats["total"] = total
            stats["pnl"] = round(stats["pnl"], 2)

    return {
        "total": len(closed),
        "by_category": by_category,
        "by_confidence": by_confidence,
        "by_edge": by_edge,
    }


def get_adaptive_thresholds(positions: list) -> dict:
    """
    Compute adaptive edge/confidence thresholds based on historical performance.
    Only adjusts after 20+ closed trades to avoid premature optimization.
    Returns {"min_edge": float, "min_confidence": int, "reason": str}
    """
    closed = [p for p in positions if p.result in ("WIN", "LOSS")]

    # Default thresholds (same as current hardcoded values)
    defaults = {"min_edge": 0.12, "min_confidence": 60, "reason": "defaults (< 20 trades)"}

    if len(closed) < 20:
        return defaults

    # Find the lowest edge bucket that's still profitable
    profitable_edge = 0.12
    for lo, hi in sorted(EDGE_BUCKETS, key=lambda x: x[0]):
        bucket_trades = [p for p in closed if lo <= (p.edge_at_entry or 0) <= hi]
        if len(bucket_trades) >= 5:
            wins = sum(1 for p in bucket_trades if p.result == "WIN")
            winrate = wins / len(bucket_trades)
            pnl = sum(p.pnl_usd or 0 for p in bucket_trades)
            if winrate >= 0.55 and pnl > 0:
                profitable_edge = lo
                break

    # Find the lowest confidence that's still profitable
    profitable_conf = 60
    for lo, hi in sorted(CONF_BUCKETS, key=lambda x: x[0]):
        bucket_trades = [p for p in closed if lo <= (p.ai_confidence or 0) <= hi]
        if len(bucket_trades) >= 5:
            wins = sum(1 for p in bucket_trades if p.result == "WIN")
            winrate = wins / len(bucket_trades)
            pnl = sum(p.pnl_usd or 0 for p in bucket_trades)
            if winrate >= 0.55 and pnl > 0:
                profitable_conf = lo
                break

    # If win rate is terrible overall, tighten thresholds
    total_wins = sum(1 for p in closed if p.result == "WIN")
    overall_winrate = total_wins / len(closed)
    if overall_winrate < 0.40:
        profitable_edge = max(profitable_edge, 0.15)
        profitable_conf = max(profitable_conf, 70)
        reason = f"tightened (overall WR={overall_winrate:.0%} < 40%)"
    elif overall_winrate > 0.65:
        reason = f"relaxed (overall WR={overall_winrate:.0%} > 65%)"
    else:
        reason = f"adaptive (WR={overall_winrate:.0%}, {len(closed)} trades)"

    return {
        "min_edge": profitable_edge,
        "min_confidence": profitable_conf,
        "reason": reason,
    }


def compute_time_weight(article_age_minutes: float, hours_to_close: float) -> float:
    """
    Compute a time-weight multiplier for signal quality.
    - Fresher articles get higher weight (news decays fast)
    - Markets closer to expiry need larger edges (less time for convergence)
    Returns a multiplier between 0.5 and 1.2
    """
    # Article freshness factor: 1.0 if < 30min, decays to 0.6 at 12h
    if article_age_minutes <= 30:
        freshness = 1.2
    elif article_age_minutes <= 120:
        freshness = 1.0
    elif article_age_minutes <= 360:
        freshness = 0.85
    else:
        freshness = 0.65

    # Time-to-close factor: markets with less time need bigger edge
    if hours_to_close <= 24:
        time_factor = 0.7   # short-term: penalize (need bigger edge to justify)
    elif hours_to_close <= 72:
        time_factor = 1.0   # sweet spot
    elif hours_to_close <= 168:
        time_factor = 0.95
    else:
        time_factor = 0.85  # very long-term: slight penalty (more uncertainty)

    return round(freshness * time_factor, 3)


def check_smart_exit(position, current_price: float) -> Optional[dict]:
    """
    Evaluate whether an open position should be exited early.
    Returns {"action": "TAKE_PROFIT"|"STOP_LOSS"|"HOLD", "reason": str}
    or None if no action needed.
    """
    if not position or position.result != "OPEN":
        return None

    entry_price = position.entry_price
    estimated_prob = position.estimated_prob
    side = position.side

    if not entry_price or not estimated_prob:
        return None

    # For YES positions: profit when price goes up toward estimated_prob
    # For NO positions: profit when YES price goes down (our NO price goes up)
    if side == "YES":
        our_current = current_price
        target = estimated_prob
    else:
        our_current = 1 - current_price
        target = 1 - estimated_prob

    # Distance moved toward target
    total_distance = abs(target - entry_price)
    if total_distance < 0.01:
        return {"action": "HOLD", "reason": "target too close to entry"}

    moved = our_current - entry_price
    progress_ratio = moved / total_distance if total_distance > 0 else 0

    # Take profit: market has moved 70%+ toward our AI estimate
    if progress_ratio >= TAKE_PROFIT_RATIO:
        return {
            "action": "TAKE_PROFIT",
            "reason": (f"market moved {progress_ratio:.0%} toward target "
                       f"(entry={entry_price:.2f} current={our_current:.2f} target={target:.2f})"),
        }

    # Stop loss: edge has reversed significantly against us
    if moved < -STOP_LOSS_REVERSAL:
        return {
            "action": "STOP_LOSS",
            "reason": (f"price reversed {abs(moved):.0%} against position "
                       f"(entry={entry_price:.2f} current={our_current:.2f})"),
        }

    # Check holding time
    if position.timestamp:
        now = datetime.now(timezone.utc)
        held_hours = (now - position.timestamp.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if held_hours > MAX_HOLDING_HOURS:
            return {
                "action": "REVIEW",
                "reason": f"held for {held_hours:.0f}h (>{MAX_HOLDING_HOURS}h) — consider re-evaluating",
            }

    return {"action": "HOLD", "reason": "within normal parameters"}


def score_signal_quality(edge: float, confidence: int, article_age_minutes: float,
                         hours_to_close: float, category_winrate: float = 0.5) -> dict:
    """
    Composite signal quality score combining all factors.
    Returns {"score": 0-100, "grade": "A"|"B"|"C"|"D", "factors": dict}
    """
    # Base score from edge × confidence
    base = (edge * 100) * (confidence / 100)  # 0-25 range typically

    # Time weight
    tw = compute_time_weight(article_age_minutes, hours_to_close)

    # Historical category performance boost/penalty
    cat_factor = 0.8 + (category_winrate * 0.4)  # 0.8 to 1.2

    raw_score = base * tw * cat_factor
    # Normalize to 0-100 scale (typical range: 5-30 raw → map to 0-100)
    normalized = min(100, max(0, raw_score * 4))

    if normalized >= 80:
        grade = "A"
    elif normalized >= 60:
        grade = "B"
    elif normalized >= 40:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": round(normalized, 1),
        "grade": grade,
        "factors": {
            "base_edge_conf": round(base, 2),
            "time_weight": tw,
            "category_factor": round(cat_factor, 2),
        },
    }
