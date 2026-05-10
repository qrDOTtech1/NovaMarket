"""
Risk Engine NovaMarket — Kelly AGRESSIF pour Polymarket.

Philosophie Polymarket :
  • Chaque marché se résout à 0 ou 1 (binaire) — pas de sortie partielle
  • Avec une vraie edge, le Kelly plein maximise la croissance exponentielle
  • L'objectif est +200% — cela nécessite des positions significatives
  • La protection vient de la QUALITÉ des signaux, pas de la taille des mises

Kelly dynamique selon la confiance :
  conf >= 85% → Kelly × 1.0  (plein Kelly)
  conf >= 70% → Kelly × 0.75
  conf >= 55% → Kelly × 0.50
  conf <  55% → Kelly × 0.25 (conservateur)
"""

# ── Limites ───────────────────────────────────────────────────────────────────
MAX_BANKROLL_PCT_PER_TRADE  = 0.25   # max 25% du bankroll par trade
MAX_BANKROLL_PCT_TOTAL      = 0.90   # peut exposer jusqu'à 90% simultanément
MAX_BANKROLL_PCT_CATEGORY   = 0.45   # max 45% par catégorie
DAILY_LOSS_LIMIT_PCT        = 0.40   # stop seulement si -40% sur la journée
MIN_EDGE_TO_TRADE           = 0.08   # edge minimum 8%
MIN_CONFIDENCE_TO_TRADE     = 45     # confiance minimum 45%
MAX_ACTIVE_POSITIONS        = 999    # pas de limite artificielle — le bankroll est la seule limite

# ── Kelly fractions dynamiques ────────────────────────────────────────────────
def _kelly_fraction(confidence: int) -> float:
    if confidence >= 85: return 1.00   # plein Kelly — signal béton
    if confidence >= 70: return 0.75   # 3/4 Kelly
    if confidence >= 55: return 0.50   # demi Kelly
    return 0.25                        # quart Kelly — signal faible


def kelly_size(prob_ai: float, prob_market: float, bankroll: float,
               confidence: int = 60) -> float:
    """
    Kelly fractionnel dynamique.
    Plus la confiance est haute, plus on mise fort.
    """
    if prob_market <= 0 or prob_market >= 1:
        return 0.0
    b = (1.0 / prob_market) - 1.0   # payout net si WIN
    p = prob_ai
    q = 1.0 - prob_ai
    if b <= 0:
        return 0.0
    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0
    fraction = _kelly_fraction(confidence)
    kelly_frac = kelly_full * fraction
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
        return 0.0, "exposition totale max atteinte (90%)"
    if category_exposure / max(bankroll, 1) >= MAX_BANKROLL_PCT_CATEGORY:
        return 0.0, "exposition catégorie max atteinte (45%)"

    # Kelly dynamique basé sur la confiance
    size = kelly_size(prob_ai, prob_market, bankroll, confidence)

    # Capital disponible (ne pas dépasser ce qui reste libre)
    available = bankroll - open_exposure
    size = min(size, available)
    size = min(size, bankroll * MAX_BANKROLL_PCT_PER_TRADE)
    size = max(size, 1.0)   # minimum $1

    return round(size, 2), ""


def expected_value(prob_ai: float, prob_market: float, size: float) -> float:
    """EV attendue en USD."""
    if prob_market <= 0:
        return 0.0
    payout  = size / prob_market
    profit  = payout - size
    loss    = size
    ev      = prob_ai * profit - (1 - prob_ai) * loss
    return round(ev, 4)


def roi_if_win(prob_market: float) -> float:
    """ROI % si le marché se résout en notre faveur."""
    if prob_market <= 0:
        return 0.0
    return round((1.0 / prob_market - 1.0) * 100, 1)
