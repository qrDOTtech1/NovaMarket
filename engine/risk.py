"""
Risk engine NovaMarket — Kelly criterion adapté + limites d'exposition.
Philosophie : maximiser la croissance long terme, protéger le capital.
"""

# ── Limites globales ──────────────────────────────────────────────────────────
MAX_BANKROLL_PCT_PER_TRADE  = 0.03   # max 3% du bankroll par trade
MAX_BANKROLL_PCT_TOTAL      = 0.60   # max 60% exposé simultanément
MAX_BANKROLL_PCT_CATEGORY   = 0.20   # max 20% par catégorie (politics, crypto, etc.)
DAILY_LOSS_LIMIT_PCT        = 0.15   # stop si -15% du bankroll sur la journée
MIN_EDGE_TO_TRADE           = 0.10   # edge minimum 10% (prob AI vs prob marché)
MIN_CONFIDENCE_TO_TRADE     = 40     # confiance IA minimum
MAX_ACTIVE_POSITIONS        = 15     # positions simultanées max

# ── Kelly fraction ───────────────────────────────────────────────────────────
KELLY_FRACTION = 0.25   # Kelly × 0.25 = Kelly fractionnel (plus conservateur)


def kelly_size(prob_ai: float, prob_market: float, bankroll: float,
               max_pct: float = MAX_BANKROLL_PCT_PER_TRADE) -> float:
    """
    Kelly fractionnel — taille optimale pour maximiser la croissance long terme.
    prob_ai     : probabilité estimée par l'IA
    prob_market : prix actuel (= odds implicites)
    bankroll    : capital disponible

    Formule Kelly standard : f = (b*p - q) / b
    où b = (1/prob_market - 1), p = prob_ai, q = 1 - prob_ai
    """
    if prob_market <= 0 or prob_market >= 1:
        return 0.0
    b = (1.0 / prob_market) - 1.0   # odds décimaux - 1
    p = prob_ai
    q = 1.0 - prob_ai
    if b <= 0:
        return 0.0
    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0
    # Kelly fractionnel
    kelly_frac = kelly_full * KELLY_FRACTION
    # Cap par MAX_BANKROLL_PCT_PER_TRADE
    capped = min(kelly_frac, max_pct)
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
        return 0.0, "exposition totale max atteinte"
    if category_exposure / max(bankroll, 1) >= MAX_BANKROLL_PCT_CATEGORY:
        return 0.0, "exposition catégorie max atteinte"

    # Kelly de base
    size = kelly_size(prob_ai, prob_market, bankroll)

    # Bonus confiance : confiance > 70 → +20%, > 85 → +40%
    if confidence >= 85:
        size *= 1.4
    elif confidence >= 70:
        size *= 1.2

    # Cap final
    max_allowed = bankroll * MAX_BANKROLL_PCT_PER_TRADE
    size = min(size, max_allowed)
    size = min(size, bankroll - open_exposure)  # ne pas dépasser le capital dispo
    size = max(size, 1.0)   # minimum $1

    return round(size, 2), ""


def expected_value(prob_ai: float, prob_market: float, size: float) -> float:
    """EV attendue du trade en USD."""
    if prob_market <= 0:
        return 0.0
    payout   = size / prob_market   # payout si WIN
    profit   = payout - size
    loss     = size
    ev       = prob_ai * profit - (1 - prob_ai) * loss
    return round(ev, 4)
