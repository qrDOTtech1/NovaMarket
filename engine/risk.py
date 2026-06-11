"""
Risk Engine NovaMarket — Kelly adaptatif pour Polymarket.

Kelly dynamique — fraction scales with confidence AND edge quality:
  conf >= 85% AND edge >= 20% → Kelly × 0.60 (high-conviction)
  conf >= 70% → Kelly × 0.40
  conf >= 55% → Kelly × 0.25
  conf <  55% → Kelly × 0.15 (low-conviction, exploratory)

Drawdown guard: position sizing shrinks proportionally as session PnL drops,
providing automatic de-risking without triggering the circuit breaker.
"""
import math

# ── Limites ───────────────────────────────────────────────────────────────────
MAX_BANKROLL_PCT_PER_TRADE  = 0.12   # max 12% per trade (was 25%)
MAX_BANKROLL_PCT_TOTAL      = 0.65   # max 65% total exposure (was 90%)
MAX_BANKROLL_PCT_CATEGORY   = 0.30   # max 30% per category (was 45%)
DAILY_LOSS_LIMIT_PCT        = 0.25   # stop at -25% daily (was 40%)
MIN_EDGE_TO_TRADE           = 0.10   # min 10% edge (was 8%)
MIN_CONFIDENCE_TO_TRADE     = 55     # min 55% confidence (was 45%)
MAX_ACTIVE_POSITIONS        = 15     # hard cap prevents over-diversification

# ── Kelly fractions dynamiques ────────────────────────────────────────────────
def _kelly_fraction(confidence: int, edge: float = 0.0) -> float:
    if confidence >= 85 and edge >= 0.20: return 0.60
    if confidence >= 85: return 0.50
    if confidence >= 70: return 0.40
    if confidence >= 55: return 0.25
    return 0.15


def _drawdown_scalar(bankroll: float, start_bankroll: float) -> float:
    """
    Scale down position sizes as drawdown deepens.
    At 0% drawdown → 1.0, at 15% drawdown → 0.5, at 25% → near 0.
    """
    if start_bankroll <= 0 or bankroll >= start_bankroll:
        return 1.0
    dd = (start_bankroll - bankroll) / start_bankroll
    if dd >= DAILY_LOSS_LIMIT_PCT:
        return 0.0
    return max(0.1, 1.0 - (dd / DAILY_LOSS_LIMIT_PCT) ** 0.8)


def kelly_size(prob_ai: float, prob_market: float, bankroll: float,
               confidence: int = 60, edge: float = 0.0,
               start_bankroll: float = 0.0) -> float:
    if prob_market <= 0 or prob_market >= 1:
        return 0.0
    b = (1.0 / prob_market) - 1.0
    p = prob_ai
    q = 1.0 - prob_ai
    if b <= 0:
        return 0.0
    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0
    fraction = _kelly_fraction(confidence, edge)
    kelly_frac = kelly_full * fraction

    dd_scalar = _drawdown_scalar(bankroll, start_bankroll) if start_bankroll > 0 else 1.0
    kelly_frac *= dd_scalar

    capped = min(kelly_frac, MAX_BANKROLL_PCT_PER_TRADE)
    return round(bankroll * capped, 2)


def get_size(
    edge: float,
    confidence: int,
    prob_ai: float,
    prob_market: float,
    bankroll: float,
    open_exposure: float = 0.0,
    category_exposure: float = 0.0,
    start_bankroll: float = 0.0,
) -> tuple:
    """
    Calcule la taille du trade.
    Retourne (size_usdc, reason_if_rejected)
    """
    if edge < MIN_EDGE_TO_TRADE:
        return 0.0, f"edge {edge:.0%} < min {MIN_EDGE_TO_TRADE:.0%}"
    if confidence < MIN_CONFIDENCE_TO_TRADE:
        return 0.0, f"confiance {confidence}% < min {MIN_CONFIDENCE_TO_TRADE}%"
    if open_exposure / max(bankroll, 1) >= MAX_BANKROLL_PCT_TOTAL:
        return 0.0, f"exposition totale max atteinte ({MAX_BANKROLL_PCT_TOTAL:.0%})"
    if category_exposure / max(bankroll, 1) >= MAX_BANKROLL_PCT_CATEGORY:
        return 0.0, f"exposition catégorie max atteinte ({MAX_BANKROLL_PCT_CATEGORY:.0%})"

    size = kelly_size(prob_ai, prob_market, bankroll, confidence,
                      edge=edge, start_bankroll=start_bankroll)

    available = bankroll - open_exposure
    size = min(size, available)
    size = min(size, bankroll * MAX_BANKROLL_PCT_PER_TRADE)
    size = max(size, 1.0)

    return round(size, 2), ""


def expected_value(prob_ai: float, prob_market: float, size: float) -> float:
    if prob_market <= 0:
        return 0.0
    payout  = size / prob_market
    profit  = payout - size
    loss    = size
    ev      = prob_ai * profit - (1 - prob_ai) * loss
    return round(ev, 4)


def roi_if_win(prob_market: float) -> float:
    if prob_market <= 0:
        return 0.0
    return round((1.0 / prob_market - 1.0) * 100, 1)
