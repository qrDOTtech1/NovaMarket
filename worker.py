"""
NovaMarket Worker — boucle principale.
News (RSS) → IA (Perplexity + Ollama) → Signaux → Risk → Exécution Polymarket
Cycle news : 60s | Cycle positions : 120s

L'IA est OBLIGATOIRE — le bot ne démarre pas sans au moins un backend disponible.
"""
import time
import logging
import threading
from datetime import datetime

from models import db, BotSession, Position, NewsLog, BotActivity, PolyCredential, OllamaConfig
from engine.polymarket_client import PolyMarketClient, CATEGORIES
from engine.news_engine import NewsEngine
from engine.ai_analyst import (batch_analyze, check_ai_available,
                                AIUnavailableError, set_runtime_config)
from engine.risk import get_size, expected_value, roi_if_win, MAX_ACTIVE_POSITIONS
from engine.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# Cache partagé marchés (user_id → liste) — lisible depuis app.py
MARKETS_CACHE: dict = {}

NEWS_INTERVAL     = 60    # refresh RSS toutes les 60s
POSITION_INTERVAL = 120   # check positions ouvertes toutes les 2min
MARKET_INTERVAL   = 300   # refresh liste marchés toutes les 5min


SIM_BANKROLL = 50.0   # bankroll virtuel en mode simulation

class MarketWorker(threading.Thread):
    def __init__(self, app, user_id: int, session_id: int, simulate: bool = False):
        super().__init__(daemon=True, name=f"nm-{user_id}")
        self.app        = app
        self.user_id    = user_id
        self.session_id = session_id
        self.simulate   = simulate
        self._stop      = threading.Event()
        self._news      = NewsEngine()
        self._markets   = []
        self._last_markets_refresh = 0.0
        self._last_news_refresh    = 0.0
        self._last_pos_check       = 0.0

    def stop(self):
        self._stop.set()

    def run(self):
        with self.app.app_context():
            self._loop()

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log(self, level: str, emoji: str, msg: str):
        try:
            entry = BotActivity(user_id=self.user_id, level=level,
                                emoji=emoji, message=msg)
            db.session.add(entry)
            db.session.commit()
            # Garder max 300 lignes
            old = (db.session.query(BotActivity.id)
                   .filter_by(user_id=self.user_id)
                   .order_by(BotActivity.id.desc())
                   .offset(300).all())
            if old:
                db.session.query(BotActivity).filter(
                    BotActivity.id.in_([r[0] for r in old])
                ).delete(synchronize_session=False)
                db.session.commit()
        except Exception as e:
            logger.warning(f"[NM] _log error: {e}")
            db.session.rollback()

    # ── Boucle principale ─────────────────────────────────────────────────────

    def _loop(self):
        # ── 1. Injection config Ollama depuis DB ──────────────────────────────
        ollama_cfg = OllamaConfig.query.filter_by(user_id=self.user_id).first()
        if ollama_cfg and ollama_cfg.ollama_url:
            set_runtime_config(
                url=ollama_cfg.ollama_url,
                api_key=ollama_cfg.get_api_key(),
                model_fast=ollama_cfg.model_fast or "",
                model_smart=ollama_cfg.model_smart or "",
            )
            self._log("info", "⚙️",
                      f"Config Ollama Cloud chargée : "
                      f"fast={ollama_cfg.model_fast or 'défaut'} | "
                      f"smart={ollama_cfg.model_smart or 'défaut'}")
        else:
            self._log("info", "⚙️", "Ollama Cloud : config env vars (pas de config DB)")

        # ── 2. Vérification IA (obligatoire) ─────────────────────────────────
        self._log("info", "🤖", "Vérification des backends IA cloud…")
        ai_status = check_ai_available()
        if not ai_status["ok"]:
            self._set_error(
                "❌ Aucun backend IA cloud disponible — "
                "Vérifie PERPLEXITY_API_KEY et OLLAMA_URL (instance cloud)."
            )
            self._log("error", "🚫",
                      "Bot stoppé : aucune IA cloud disponible "
                      f"(OllamaCloud={ai_status['ollama']} Perplexity={ai_status['perplexity']})")
            return

        ai_backends = []
        if ai_status["perplexity"]: ai_backends.append("Perplexity 🌐")
        if ai_status["ollama"]:     ai_backends.append("Ollama Cloud 🌐")
        self._log("success", "🧠", f"IA cloud connectée : {' | '.join(ai_backends)}")

        # ── 3. Credentials Polymarket ─────────────────────────────────────────
        cred = PolyCredential.query.filter_by(user_id=self.user_id).first()
        if not cred:
            self._set_error("Credentials Polymarket manquants")
            return

        client = PolyMarketClient(cred.get_key())
        conn   = client.connect()

        if self.simulate:
            # Simulation : bankroll virtuel, la connexion est optionnelle
            bankroll = SIM_BANKROLL
            real_bal = conn.get("usdc", 0) if conn.get("ok") else 0
            self._log("info", "📊",
                      f"[SIM] Mode SIMULATION activé — bankroll virtuel {SIM_BANKROLL:.0f}$ "
                      f"(solde réel : {real_bal:.2f} USDC) | "
                      f"Les ordres ne seront PAS exécutés sur Polymarket")
        else:
            if not conn.get("ok"):
                self._set_error(f"Connexion Polymarket échouée : {conn.get('error')}")
                return
            bankroll = conn["usdc"]
            self._log("success", "🚀",
                      f"NovaMarket démarré — bankroll {bankroll:.2f} USDC | "
                      f"{len(CATEGORIES)} catégories | {len(self._news._articles)} articles en cache")

        CircuitBreaker.init(self.user_id, bankroll)

        while not self._stop.is_set():
            now = time.time()
            try:
                if self.simulate:
                    invested = sum(
                        p.size_usd for p in
                        Position.query.filter_by(user_id=self.user_id, result="OPEN").all()
                    )
                    bankroll = max(SIM_BANKROLL - invested, 1.0)
                else:
                    bankroll = client.get_balance()
                cb = CircuitBreaker.get(self.user_id)

                if cb and cb.daily_triggered:
                    self._log("error", "🚫", "Circuit breaker journalier — pause jusqu'au reset")
                    self._stop.wait(NEWS_INTERVAL)
                    continue

                # Refresh marchés
                if now - self._last_markets_refresh > MARKET_INTERVAL:
                    self._refresh_markets(client)
                    self._last_markets_refresh = now

                # Refresh news + analyse
                if now - self._last_news_refresh > NEWS_INTERVAL:
                    self._news_cycle(client, bankroll, cb)
                    self._last_news_refresh = now

                # Check positions ouvertes
                if now - self._last_pos_check > POSITION_INTERVAL:
                    self._check_positions(client, bankroll)
                    self._last_pos_check = now

            except Exception as e:
                logger.error(f"[NM {self.user_id}] Erreur boucle: {e}", exc_info=True)
                self._log("error", "❌", f"Erreur interne: {str(e)[:120]}")

            self._stop.wait(15)   # tick toutes les 15s

        self._log("info", "🔴", "NovaMarket arrêté.")
        self._set_stopped()

    # ── Refresh marchés ───────────────────────────────────────────────────────

    def _refresh_markets(self, client: PolyMarketClient):
        all_markets = []
        for cat in CATEGORIES:
            all_markets.extend(client.get_active_markets(category=cat, limit=50))
        self._markets = client.filter_tradeable(all_markets)
        # Mettre à jour le cache global (lisible depuis les routes Flask)
        MARKETS_CACHE[self.user_id] = [
            {
                "question":   m.get("question", m.get("title", ""))[:120],
                "category":   m.get("category", ""),
                "yes_price":  round(m.get("_yes_price", 0.5) * 100),
                "no_price":   round(m.get("_no_price",  0.5) * 100),
                "liquidity":  round(m.get("_liquidity", 0)),
                "vol24":      round(m.get("_vol24", 0)),
                "hours_left": round(m.get("_hours_left", 0), 1),
                "url":        f"https://polymarket.com/event/{m.get('slug', m.get('conditionId',''))}",
            }
            for m in self._markets[:60]
        ]
        self._log("info", "🔄",
                  f"Marchés refreshés — {len(self._markets)} tradables "
                  f"({len(all_markets)} scannés)")

    # ── Cycle news ────────────────────────────────────────────────────────────

    def _news_cycle(self, client: PolyMarketClient, bankroll: float, cb):
        new_count = self._news.refresh()
        if new_count == 0:
            return

        self._log("info", "📰",
                  f"+{new_count} nouveaux articles | "
                  f"Buffer : {self._news.stats['buffer_size']} | "
                  f"En attente analyse : {self._news.stats['pending_analysis']}")

        if not self._markets:
            return

        # Articles pertinents non encore traités
        articles = self._news.pop_new(max_items=15)
        if not articles:
            return

        self._log("info", "🤖",
                  f"Analyse IA de {len(articles)} articles vs {len(self._markets)} marchés… "
                  f"(Perplexity web search + Ollama)")

        # Analyse batch IA — lève AIUnavailableError si IA tombe pendant la session
        try:
            signals = batch_analyze(articles, self._markets)
        except AIUnavailableError as e:
            self._log("error", "🚫",
                      f"IA indisponible en cours de session : {e} — "
                      "cycle ignoré, prochain essai dans 60s")
            return  # on ne stoppe pas le bot, juste le cycle courant

        # Log articles dans la DB
        for art in articles:
            try:
                sig_count = sum(1 for s in signals if s["article_title"] == art.title)
                nl = NewsLog(user_id=self.user_id, source=art.source, title=art.title,
                             url=art.url, relevance=art.score, signals_gen=sig_count)
                db.session.add(nl)
            except Exception as e:
                logger.warning(f"[NM] NewsLog insert error: {e}")
        try:
            db.session.commit()
        except Exception as e:
            logger.error(f"[NM] NewsLog commit error: {e}")
            db.session.rollback()

        if not signals:
            self._log("info", "🔍", "Aucun signal exploitable dans ce batch")
            return

        self._log("info", "💡",
                  f"{len(signals)} signal(s) généré(s) — traitement par risk engine…")

        # Exécuter les signaux
        active_count = Position.query.filter_by(
            user_id=self.user_id, result="OPEN"
        ).count()

        for sig in signals:
            if active_count >= MAX_ACTIVE_POSITIONS:
                self._log("warning", "⚠️", "Max positions actives atteint")
                break
            ok, cb_reason = CircuitBreaker.can_trade(self.user_id)
            if not ok:
                self._log("warning", "🚫", f"CB bloque: {cb_reason}")
                break
            self._execute_signal(sig, client, bankroll, cb, active_count)
            active_count += 1

    # ── Exécution d'un signal ─────────────────────────────────────────────────

    def _execute_signal(self, sig: dict, client: PolyMarketClient,
                        bankroll: float, cb, active_count: int):
        market   = sig["market"]
        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", market.get("title", ""))[:120]
        category = market.get("category", "general")
        side     = sig["side"]
        edge     = sig["edge"]
        conf     = sig["confidence"]

        # ── VÉRIF : UNE SEULE position OPEN par marché ───────────────────────
        existing = Position.query.filter_by(
            user_id=self.user_id, market_id=market_id, result="OPEN"
        ).first()
        if existing:
            self._log("info", "🔁",
                      f"Position déjà ouverte sur [{question[:50]}…] — signal ignoré")
            return

        cb_session = CircuitBreaker.get(self.user_id)
        open_exp   = cb_session.open_exposure if cb_session else 0
        cat_exp    = (cb_session.category_exposure.get(category, 0)
                      if cb_session else 0)

        size, reject_reason = get_size(
            edge=edge, confidence=conf,
            prob_ai=sig["estimated_prob"],
            prob_market=sig["entry_price"],
            bankroll=bankroll,
            open_exposure=open_exp,
            category_exposure=cat_exp,
        )

        if size <= 0:
            self._log("info", "🔎",
                      f"Refusé [{side}] {question[:60]}… — {reject_reason}")
            return

        ev = expected_value(sig["estimated_prob"], sig["entry_price"], size)

        self._log("info", "📊",
                  f"Signal [{side} {question[:50]}…] | "
                  f"mkt={sig['current_prob']:.0%} → IA={sig['estimated_prob']:.0%} "
                  f"edge={edge:.0%} conf={conf}% | "
                  f"taille={size:.2f}$ EV={ev:+.2f}$")

        if self.simulate:
            # Simulation — pas de vrai ordre, juste un ID fictif
            order = {"ok": True, "order_id": f"SIM-{int(time.time())}"}
            self._log("info", "📊",
                      f"[SIM] Ordre simulé : {side} [{question[:50]}…] | "
                      f"{size:.2f}$ virtuel | edge {edge:.0%} | EV {ev:+.2f}$")
        else:
            # Récupérer les token IDs
            token_ids = client.get_token_ids(market)
            token_id  = token_ids.get(side)
            if not token_id:
                self._log("warning", "⚠️", f"Token ID introuvable pour {side} sur ce marché")
                return

            # Placement ordre réel
            order = client.place_order(token_id, side, size, sig["entry_price"])
            if not order.get("ok"):
                self._log("warning", "⚠️",
                          f"Ordre refusé: {str(order.get('error',''))[:80]}")
                return

        # Enregistrement position
        try:
            roi = roi_if_win(sig["entry_price"])
            pos = Position(
                session_id=self.session_id, user_id=self.user_id,
                market_id=market.get("conditionId", market.get("condition_id", "")),
                market_question=question,
                category=category,
                side=side,
                size_usd=size,
                entry_price=sig["entry_price"],
                estimated_prob=sig["estimated_prob"],
                edge_at_entry=edge,
                ai_confidence=conf,
                ai_reasoning=sig.get("reasoning", "")[:400],
                thesis=sig.get("thesis", "")[:200],
                exit_trigger=sig.get("exit_trigger", "")[:200],
                article_title=sig.get("article_title", "")[:200],
                article_source=sig.get("article_source", ""),
                ev_usd=ev,
                result="OPEN",
                order_id=order.get("order_id", ""),
                hours_to_close=market.get("_hours_left"),
            )
            db.session.add(pos)
            db.session.commit()
            CircuitBreaker.add_exposure(self.user_id, category, size)
        except Exception as e:
            logger.error(f"[NM] record position error: {e}")
            db.session.rollback()

        pfx = "[SIM] " if self.simulate else ""
        self._log("success", "✅",
                  f"{pfx}Position ouverte : {side} [{question[:50]}…] | "
                  f"{size:.2f}$ | edge {edge:.0%} | EV {ev:+.2f}$ | "
                  f"conf {conf}%")

    # ── Check positions ouvertes ──────────────────────────────────────────────

    def _check_positions(self, client: PolyMarketClient, bankroll: float):
        positions = Position.query.filter_by(
            user_id=self.user_id, result="OPEN"
        ).all()
        if not positions:
            return

        self._log("info", "🔄",
                  f"Vérification {len(positions)} position(s) ouverte(s)…")

        poly_positions = {p.get("conditionId", ""): p
                         for p in client.get_positions()}

        for pos in positions:
            try:
                # Récupérer le prix actuel
                market = PolyMarketClient.get_market_detail(pos.market_id)
                if not market:
                    continue

                prices_str = market.get("outcomePrices", "[]")
                if isinstance(prices_str, str):
                    import json
                    try: prices = json.loads(prices_str)
                    except Exception: prices = []
                else:
                    prices = prices_str or []

                if prices:
                    current_yes = float(prices[0]) if prices else 0.5
                    current_price = current_yes if pos.side == "YES" else (1 - current_yes)
                    pos.current_price = current_price

                # Vérifier si résolu
                resolved   = market.get("resolved", False)
                resolution = market.get("resolution", "")
                if resolved and resolution:
                    won = (
                        (pos.side == "YES" and resolution.lower() in ("yes", "1", "true")) or
                        (pos.side == "NO"  and resolution.lower() in ("no",  "0", "false"))
                    )
                    if won:
                        pnl = pos.size_usd * (1 / pos.entry_price - 1)
                        pos.pnl_usd    = round(pnl, 4)
                        pos.result     = "WIN"
                        pos.exit_price = 1.0
                        self._update_session(pnl, "WIN")
                        CircuitBreaker.record_trade(
                            self.user_id, pnl, bankroll, pos.category, pos.size_usd
                        )
                        self._log("success", "🎯",
                                  f"WIN [{pos.side}] {pos.market_question[:60]}… | "
                                  f"+{pnl:.2f}$")
                    else:
                        pnl = -pos.size_usd
                        pos.pnl_usd    = round(pnl, 4)
                        pos.result     = "LOSS"
                        pos.exit_price = 0.0
                        self._update_session(pnl, "LOSS")
                        CircuitBreaker.record_trade(
                            self.user_id, pnl, bankroll, pos.category, pos.size_usd
                        )
                        self._log("warning", "🛡️",
                                  f"LOSS [{pos.side}] {pos.market_question[:60]}… | "
                                  f"{pnl:.2f}$")
                db.session.commit()
            except Exception as e:
                logger.error(f"[NM] check_positions error: {e}")
                db.session.rollback()

    # ── Utils ─────────────────────────────────────────────────────────────────

    def _update_session(self, pnl: float, result: str):
        try:
            s = BotSession.query.get(self.session_id)
            if s:
                s.pnl_usd      += pnl
                s.total_trades += 1
                if result == "WIN": s.wins   += 1
                else:               s.losses += 1
                db.session.commit()
        except Exception as e:
            logger.error(f"[NM] update_session: {e}")
            db.session.rollback()

    def _set_error(self, msg: str):
        try:
            s = BotSession.query.get(self.session_id)
            if s:
                s.status = "error"; s.error_msg = msg
                s.stopped_at = datetime.utcnow()
                db.session.commit()
        except Exception:
            pass
        logger.error(f"[NM {self.user_id}] {msg}")

    def _set_stopped(self):
        try:
            s = BotSession.query.get(self.session_id)
            if s:
                s.status = "stopped"; s.stopped_at = datetime.utcnow()
                db.session.commit()
        except Exception:
            pass
        CircuitBreaker.reset(self.user_id)


class BotManager:
    _workers: dict = {}
    _lock = threading.Lock()

    @classmethod
    def start(cls, app, user_id: int, session_id: int, simulate: bool = False) -> bool:
        with cls._lock:
            w = cls._workers.get(user_id)
            if w and w.is_alive():
                return False
            w = MarketWorker(app, user_id, session_id, simulate=simulate)
            w.start()
            cls._workers[user_id] = w
            return True

    @classmethod
    def stop(cls, user_id: int) -> bool:
        with cls._lock:
            w = cls._workers.pop(user_id, None)
            if w: w.stop(); return True
            return False

    @classmethod
    def is_running(cls, user_id: int) -> bool:
        w = cls._workers.get(user_id)
        return bool(w and w.is_alive())
