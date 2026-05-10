"""
Polymarket CLOB Client — wrapper complet.
Markets publics (lecture) + ordres (authentication Polygon EOA).
"""
import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
CHAIN_ID   = 137   # Polygon mainnet

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.constants import BUY, SELL
    CLOB_OK = True
except ImportError:
    CLOB_OK = False
    logger.warning("py-clob-client non installé")


# ── Catégories Polymarket ────────────────────────────────────────────────────
CATEGORIES = ["politics", "crypto", "sports", "business", "science", "pop culture"]

# ── Paramètres marché ────────────────────────────────────────────────────────
MIN_LIQUIDITY_USD   = 1_000    # ignore les marchés trop peu liquides
MIN_VOLUME_24H      = 500      # volume minimum 24h
MIN_HOURS_TO_CLOSE  = 24       # ignore les marchés qui ferment dans < 24h
MAX_HOURS_TO_CLOSE  = 720      # ignore les marchés à > 30 jours (trop incertains)


class PolyMarketClient:
    def __init__(self, private_key: str):
        self.private_key = private_key
        self._client: Optional[object] = None

    def connect(self) -> dict:
        if not CLOB_OK:
            return {"ok": False, "error": "py-clob-client non installé"}
        try:
            self._client = ClobClient(
                CLOB_API,
                key=self.private_key,
                chain_id=CHAIN_ID,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            # Récupérer le solde USDC
            bal = self._get_balance()
            return {"ok": True, "usdc": bal}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_balance(self) -> float:
        try:
            bal = self._client.get_balance()
            return float(bal) if bal else 0.0
        except Exception:
            return 0.0

    def get_balance(self) -> float:
        return self._get_balance()

    # ── Données marché (public, pas besoin d'auth) ───────────────────────────

    @staticmethod
    def get_active_markets(category: str = None, limit: int = 100) -> list:
        """Récupère les marchés actifs depuis Gamma API (endpoint public)."""
        try:
            params = {
                "active":       "true",
                "closed":       "false",
                "limit":        limit,
                "order":        "volume24hr",
                "ascending":    "false",
            }
            if category:
                params["tag"] = category
            resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[Poly] get_active_markets: {e}")
            return []

    @staticmethod
    def get_market_detail(condition_id: str) -> Optional[dict]:
        try:
            resp = requests.get(f"{GAMMA_API}/markets/{condition_id}", timeout=8)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def filter_tradeable(markets: list) -> list:
        """Filtre et enrichit les marchés tradables selon nos critères."""
        tradeable = []
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)

        for m in markets:
            try:
                # Liquidité
                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < MIN_LIQUIDITY_USD:
                    continue
                # Volume 24h
                vol24 = float(m.get("volume24hr", 0) or 0)
                if vol24 < MIN_VOLUME_24H:
                    continue
                # Temps jusqu'à fermeture
                end_date_str = m.get("endDate") or m.get("end_date_iso")
                if end_date_str:
                    try:
                        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        hours_left = (end - now).total_seconds() / 3600
                        if hours_left < MIN_HOURS_TO_CLOSE or hours_left > MAX_HOURS_TO_CLOSE:
                            continue
                        m["_hours_left"] = round(hours_left, 1)
                    except Exception:
                        pass
                # Prix YES actuel
                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    import json
                    try: outcomes = json.loads(outcomes)
                    except Exception: outcomes = []
                prices_str = m.get("outcomePrices", "[]")
                if isinstance(prices_str, str):
                    import json
                    try: prices = json.loads(prices_str)
                    except Exception: prices = []
                else:
                    prices = prices_str or []
                if len(prices) >= 1:
                    try:
                        m["_yes_price"] = float(prices[0])   # probabilité YES
                        m["_no_price"]  = float(prices[1]) if len(prices) > 1 else 1 - m["_yes_price"]
                    except Exception:
                        m["_yes_price"] = 0.5
                        m["_no_price"]  = 0.5
                else:
                    m["_yes_price"] = 0.5
                    m["_no_price"]  = 0.5

                # Ne pas trader si trop near-1 ou near-0 (résolu ou évident)
                yes = m["_yes_price"]
                if yes < 0.03 or yes > 0.97:
                    continue

                m["_liquidity"] = liquidity
                m["_vol24"]     = vol24
                tradeable.append(m)
            except Exception as e:
                logger.debug(f"[Poly] filter market error: {e}")
                continue

        return tradeable

    # ── Ordres ───────────────────────────────────────────────────────────────

    def place_order(self, token_id: str, side: str, size_usdc: float,
                    price: float) -> dict:
        """
        Place un ordre market/limit sur Polymarket.
        side : "BUY_YES" | "BUY_NO"
        price : probabilité entre 0 et 1
        size_usdc : montant en USDC
        """
        if not CLOB_OK or not self._client:
            return {"ok": False, "error": "client non initialisé"}
        try:
            clob_side = BUY   # on achète toujours le token (YES ou NO)
            order_args = OrderArgs(
                price=round(price, 4),
                size=round(size_usdc, 2),
                side=clob_side,
                token_id=token_id,
            )
            signed = self._client.create_order(order_args)
            result = self._client.post_order(signed, OrderType.GTC)
            return {"ok": True, "order_id": result.get("orderID", ""), "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_positions(self) -> list:
        """Positions ouvertes."""
        if not self._client:
            return []
        try:
            return self._client.get_positions() or []
        except Exception:
            return []

    def cancel_order(self, order_id: str) -> dict:
        try:
            return {"ok": True, "result": self._client.cancel(order_id)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def get_token_ids(market: dict) -> dict:
        """Retourne {"YES": token_id, "NO": token_id} pour un marché."""
        tokens = market.get("tokens") or market.get("clobTokenIds") or []
        if isinstance(tokens, str):
            import json
            try: tokens = json.loads(tokens)
            except Exception: tokens = []
        result = {"YES": None, "NO": None}
        if len(tokens) >= 2:
            result["YES"] = tokens[0].get("token_id") if isinstance(tokens[0], dict) else tokens[0]
            result["NO"]  = tokens[1].get("token_id") if isinstance(tokens[1], dict) else tokens[1]
        return result
