"""
NovaMarket — Application Flask principale.
Bot de trading Polymarket : RSS + IA + Kelly + Circuit Breaker
"""
import os
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify)
from flask_migrate import Migrate

from models import db, User, PolyCredential, BotSession, Position, NewsLog, BotActivity, OllamaConfig
from worker import BotManager, MARKETS_CACHE
from engine.polymarket_client import PolyMarketClient
from engine.circuit_breaker import CircuitBreaker
from engine.ai_analyst import check_ai_available, fetch_ollama_models

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger(__name__)

# ── App factory ───────────────────────────────────────────────────────────────

def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "novamarket-dev-secret-change-me")

    # DB
    db_url = os.environ.get("DATABASE_URL", "sqlite:///novamarket.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"]        = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    Migrate(app, db)

    with app.app_context():
        db.create_all()

        # Migration manuelle : ajouter colonne user_id à NewsLog si elle n'existe pas
        # (fallback si Alembic n'est pas disponible)
        try:
            from sqlalchemy import inspect, text
            inspector = inspect(db.engine)
            news_cols = [c["name"] for c in inspector.get_columns("news_logs")]
            if "user_id" not in news_cols:
                logger.info("[DB] Migration : ajout colonne user_id à news_logs…")
                with db.engine.connect() as conn:
                    if "postgresql" in db_url:
                        conn.execute(text(
                            "ALTER TABLE news_logs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1 "
                            "CONSTRAINT news_logs_user_id_fkey REFERENCES users(id) ON DELETE CASCADE"
                        ))
                    elif "sqlite" in db_url:
                        conn.execute(text(
                            "ALTER TABLE news_logs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
                        ))
                    conn.commit()
                    logger.info("[DB] Migration OK : colonne user_id ajoutée")
        except Exception as e:
            logger.warning(f"[DB] Migration user_id échouée (peut-être déjà migrée): {e}")

        # Avertir si SQLite (éphémère sur Railway → données perdues au redeploy)
        if "sqlite" in db_url:
            logger.warning(
                "⚠️  SQLite détecté — les données (clés, sessions, positions) "
                "seront PERDUES à chaque redéploiement. "
                "Ajoute PostgreSQL dans Railway et configure DATABASE_URL."
            )

    # Routes
    _register_routes(app)
    return app


# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────────

def _register_routes(app):

    # ── Landing ───────────────────────────────────────────────────────────────

    @app.route("/")
    def landing():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        return render_template("landing.html")

    # ── Auth ──────────────────────────────────────────────────────────────────

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email    = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            if not username or not email or not password:
                flash("Tous les champs sont requis.", "error")
                return render_template("register.html")
            if len(password) < 8:
                flash("Mot de passe trop court (8 caractères min).", "error")
                return render_template("register.html")
            if User.query.filter((User.username == username) | (User.email == email)).first():
                flash("Nom d'utilisateur ou email déjà utilisé.", "error")
                return render_template("register.html")

            u = User(username=username, email=email)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            session["user_id"] = u.id
            flash("Compte créé ! Configure ta clé Polymarket pour démarrer.", "success")
            return redirect(url_for("onboarding"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            identifier = request.form.get("identifier", "").strip()
            password   = request.form.get("password", "")
            u = User.query.filter(
                (User.username == identifier) | (User.email == identifier)
            ).first()
            if u and u.check_password(password):
                session["user_id"] = u.id
                return redirect(url_for("dashboard"))
            flash("Identifiants incorrects.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        uid = session.pop("user_id", None)
        if uid:
            BotManager.stop(uid)
        return redirect(url_for("landing"))

    # ── Onboarding ────────────────────────────────────────────────────────────

    @app.route("/onboarding", methods=["GET", "POST"])
    @login_required
    def onboarding():
        uid  = session["user_id"]
        user = User.query.get(uid)

        # Vérifier que l'utilisateur existe (intégrité DB)
        if not user:
            flash("Compte invalide — réenregistre-toi.", "error")
            session.pop("user_id", None)
            return redirect(url_for("register"))

        cred = PolyCredential.query.filter_by(user_id=uid).first()

        if request.method == "POST":
            raw_key = request.form.get("private_key", "").strip()
            if not raw_key:
                flash("Clé privée requise.", "error")
                return render_template("onboarding.html", cred=cred)

            # Test de connexion
            client = PolyMarketClient(raw_key)
            conn   = client.connect()
            if not conn.get("ok"):
                flash(f"Connexion échouée : {conn.get('error', 'Erreur inconnue')}", "error")
                return render_template("onboarding.html", cred=cred)

            try:
                if not cred:
                    cred = PolyCredential(user_id=uid)
                    db.session.add(cred)
                cred.set_key(raw_key)
                cred.wallet_addr = conn.get("address", "")
                cred.verified_at = datetime.utcnow()
                db.session.commit()

                flash(f"Wallet connecté ✅  Solde : {conn['usdc']:.2f} USDC", "success")
                return redirect(url_for("dashboard"))
            except Exception as e:
                db.session.rollback()
                logger.error(f"[onboarding] DB error: {e}")
                flash(f"Erreur DB — réenregistre-toi svp. ({str(e)[:60]})", "error")
                session.pop("user_id", None)
                return redirect(url_for("register"))

        return render_template("onboarding.html", cred=cred)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.route("/dashboard")
    @login_required
    def dashboard():
        uid  = session["user_id"]
        user = User.query.get(uid)
        cred = PolyCredential.query.filter_by(user_id=uid).first()

        # Session active
        active_session = (BotSession.query
                          .filter_by(user_id=uid, status="running")
                          .order_by(BotSession.id.desc())
                          .first())

        # Historique sessions (10 dernières)
        past_sessions = (BotSession.query
                         .filter_by(user_id=uid)
                         .order_by(BotSession.id.desc())
                         .limit(10).all())

        # Positions ouvertes
        open_positions = (Position.query
                          .filter_by(user_id=uid, result="OPEN")
                          .order_by(Position.timestamp.desc())
                          .all())

        # Dernières positions fermées (20)
        closed_positions = (Position.query
                            .filter(Position.user_id == uid,
                                    Position.result.in_(["WIN", "LOSS"]))
                            .order_by(Position.timestamp.desc())
                            .limit(20).all())

        # Activité récente
        activities = (BotActivity.query
                      .filter_by(user_id=uid)
                      .order_by(BotActivity.id.desc())
                      .limit(50).all())

        # News récentes
        recent_news = (NewsLog.query
                       .order_by(NewsLog.timestamp.desc())
                       .limit(20).all())

        # Stats globales
        all_pos = Position.query.filter_by(user_id=uid).all()
        total   = len(all_pos)
        wins    = sum(1 for p in all_pos if p.result == "WIN")
        losses  = sum(1 for p in all_pos if p.result == "LOSS")
        total_pnl = sum((p.pnl_usd or 0) for p in all_pos)
        winrate   = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0

        cb = CircuitBreaker.get(uid)

        # is_running : thread en mémoire (même processus) OU session DB active
        # Nécessaire car Gunicorn multi-process — chaque worker a sa propre mémoire
        is_running = BotManager.is_running(uid) or (
            active_session is not None and active_session.stopped_at is None
        )
        sim_mode = (active_session.mode == "simulation") if active_session else False

        return render_template("dashboard.html",
            user=user,
            cred=cred,
            is_running=is_running,
            sim_mode=sim_mode,
            active_session=active_session,
            past_sessions=past_sessions,
            open_positions=open_positions,
            closed_positions=closed_positions,
            activities=activities,
            recent_news=recent_news,
            total_trades=total,
            wins=wins,
            losses=losses,
            total_pnl=total_pnl,
            winrate=winrate,
            cb=cb,
        )

    # ── Bot control ───────────────────────────────────────────────────────────

    @app.route("/bot/start", methods=["POST"])
    @login_required
    def bot_start():
        uid  = session["user_id"]
        cred = PolyCredential.query.filter_by(user_id=uid).first()

        if not cred or not cred.verified_at:
            return jsonify({"ok": False, "error": "Configure ta clé Polymarket d'abord"})

        # Vérifier en mémoire ET en DB (multi-process Gunicorn)
        already_running = BotManager.is_running(uid) or bool(
            BotSession.query.filter_by(user_id=uid, status="running").filter(
                BotSession.stopped_at.is_(None)
            ).first()
        )
        if already_running:
            return jsonify({"ok": False, "error": "Bot déjà en cours"})

        # ── Déterminer le mode : simulation si solde < 10$ ────────────────────
        simulate = False
        try:
            client = PolyMarketClient(cred.get_key())
            conn   = client.connect()
            if conn.get("ok"):
                balance  = conn.get("usdc", 0)
                simulate = balance < 10.0
            else:
                simulate = True   # connexion impossible → simulation
        except Exception:
            simulate = True

        s = BotSession(
            user_id=uid,
            status="running",
            mode="simulation" if simulate else "real",
            started_at=datetime.utcnow(),
        )
        db.session.add(s)
        db.session.commit()

        launched = BotManager.start(app, uid, s.id, simulate=simulate)
        if not launched:
            s.status = "error"; s.error_msg = "Échec démarrage thread"
            db.session.commit()
            return jsonify({"ok": False, "error": "Impossible de démarrer le worker"})

        return jsonify({
            "ok":        True,
            "session_id": s.id,
            "mode":      "simulation" if simulate else "real",
        })

    @app.route("/bot/stop", methods=["POST"])
    @login_required
    def bot_stop():
        uid = session["user_id"]
        BotManager.stop(uid)
        # Forcer la mise à jour en DB même si le thread est dans un autre worker
        stale = BotSession.query.filter_by(user_id=uid, status="running").filter(
            BotSession.stopped_at.is_(None)
        ).all()
        for s in stale:
            s.status     = "stopped"
            s.stopped_at = datetime.utcnow()
        if stale:
            db.session.commit()
        return jsonify({"ok": True})

    # ── API ───────────────────────────────────────────────────────────────────

    @app.route("/api/status")
    @login_required
    def api_status():
        uid = session["user_id"]
        cb  = CircuitBreaker.get(uid)

        active_session = (BotSession.query
                          .filter_by(user_id=uid, status="running")
                          .order_by(BotSession.id.desc())
                          .first())

        open_count = Position.query.filter_by(user_id=uid, result="OPEN").count()

        return jsonify({
            "running":        BotManager.is_running(uid),
            "open_positions": open_count,
            "session_pnl":    round(active_session.pnl_usd, 2) if active_session else 0,
            "total_trades":   active_session.total_trades if active_session else 0,
            "cb": {
                "daily_triggered":    cb.daily_triggered if cb else False,
                "consecutive_losses": cb.consecutive_losses if cb else 0,
                "open_exposure":      round(cb.open_exposure, 2) if cb else 0,
                "winrate":            cb.winrate if cb else 0,
            } if cb else None,
        })

    @app.route("/api/activity")
    @login_required
    def api_activity():
        uid    = session["user_id"]
        since  = request.args.get("since", 0, type=int)
        q = (BotActivity.query
             .filter(BotActivity.user_id == uid, BotActivity.id > since)
             .order_by(BotActivity.id.asc())
             .limit(50))
        entries = [e.to_dict() | {"id": e.id} for e in q]
        return jsonify({"entries": entries})

    @app.route("/api/positions")
    @login_required
    def api_positions():
        uid    = session["user_id"]
        result = request.args.get("result", "OPEN")
        pos    = (Position.query
                  .filter_by(user_id=uid, result=result)
                  .order_by(Position.timestamp.desc())
                  .limit(30).all())
        return jsonify({"positions": [p.to_dict() for p in pos]})

    @app.route("/api/positions/<int:pos_id>/close", methods=["POST"])
    @login_required
    def api_close_position(pos_id):
        """Fermer / annuler manuellement une position."""
        uid = session["user_id"]
        pos = Position.query.filter_by(id=pos_id, user_id=uid, result="OPEN").first()
        if not pos:
            return jsonify({"ok": False, "error": "Position introuvable ou déjà fermée"})

        pos.result = "CANCELLED"
        pos.pnl_usd = 0.0
        pos.exit_price = pos.current_price or pos.entry_price
        try:
            db.session.commit()
            logger.info(f"[NM] Position #{pos_id} annulée manuellement par user {uid}")
        except Exception as e:
            db.session.rollback()
            return jsonify({"ok": False, "error": str(e)[:80]})
        return jsonify({"ok": True, "message": f"Position #{pos_id} annulée"})

    @app.route("/api/positions/rebalance", methods=["POST"])
    @login_required
    def api_rebalance_positions():
        """
        Rééquilibrage IA des positions ouvertes :
        1. Fusionne les doublons (même market_id) → garde le meilleur, annule le reste
        2. Re-analyse chaque position unique avec Perplexity
        3. Recalcule la mise optimale Kelly selon le bankroll actuel
        4. Marque les positions confirmées comme bloquées pour les prochains cycles
        """
        from engine.ai_analyst import estimate_probability, AIUnavailableError
        from engine.risk import kelly_size, get_size

        uid = session["user_id"]
        positions = Position.query.filter_by(user_id=uid, result="OPEN").all()
        if not positions:
            return jsonify({"ok": False, "error": "Aucune position ouverte"})

        cred = PolyCredential.query.filter_by(user_id=uid).first()
        balance = 0.0
        try:
            client = PolyMarketClient(cred.get_key()) if cred else None
            balance = client.get_balance() if client else 0.0
        except Exception:
            pass

        # Mode simulation : bankroll virtuel
        active_session = BotSession.query.filter_by(user_id=uid, status="running").first()
        if active_session and active_session.mode == "simulation":
            from worker import SIM_BANKROLL
            invested = sum(p.size_usd for p in positions)
            balance = max(SIM_BANKROLL - invested, 1.0)
        bankroll = balance if balance > 0 else 50.0

        results = {"merged": [], "updated": [], "closed": [], "errors": []}

        # ── 1. Fusionner les doublons ──────────────────────────────────────────
        by_market: dict = {}
        for pos in positions:
            mid = pos.market_id or ""
            if mid not in by_market:
                by_market[mid] = []
            by_market[mid].append(pos)

        to_analyze = []
        for mid, group in by_market.items():
            if len(group) > 1:
                # Garder la position avec la meilleure confiance IA
                best = max(group, key=lambda p: (p.ai_confidence or 0))
                for dup in group:
                    if dup.id != best.id:
                        dup.result = "CANCELLED"
                        dup.pnl_usd = 0.0
                        results["merged"].append(f"#{dup.id} annulé (doublon de #{best.id})")
                to_analyze.append(best)
            else:
                to_analyze.append(group[0])

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({"ok": False, "error": f"Erreur merge: {str(e)[:80]}"})

        # ── 2. Re-analyser chaque position unique + ajuster Kelly ─────────────
        open_exposure = sum(p.size_usd for p in to_analyze)

        for pos in to_analyze:
            try:
                analysis = estimate_probability(
                    pos.article_title or pos.market_question[:100],
                    pos.ai_reasoning or "",
                    pos.market_question,
                    pos.entry_price,
                )
                ep   = analysis["estimated_prob"]
                conf = analysis["confidence"]
                edge = abs(ep - pos.entry_price)

                # Recalcul Kelly avec bankroll actuel
                new_size, reason = get_size(
                    edge=edge, confidence=conf,
                    prob_ai=ep, prob_market=pos.entry_price,
                    bankroll=bankroll,
                    open_exposure=max(open_exposure - pos.size_usd, 0),
                )

                old_size = pos.size_usd
                if new_size <= 0:
                    # Signal plus valide : annuler
                    pos.result = "CANCELLED"
                    pos.pnl_usd = 0.0
                    results["closed"].append(
                        f"#{pos.id} fermé — signal invalide ({reason or 'edge trop faible'})"
                    )
                else:
                    pos.estimated_prob = ep
                    pos.ai_confidence  = conf
                    pos.ai_reasoning   = analysis.get("reasoning", pos.ai_reasoning)[:400]
                    pos.exit_trigger   = analysis.get("exit_trigger", pos.exit_trigger or "")[:200]
                    pos.thesis         = analysis.get("thesis", pos.thesis or "")[:200]
                    pos.size_usd       = new_size
                    pos.ev_usd         = (ep - pos.entry_price) * new_size
                    open_exposure     += new_size - old_size
                    results["updated"].append(
                        f"#{pos.id} {pos.market_question[:50]}… "
                        f"{old_size:.1f}$→{new_size:.1f}$ conf={conf}% edge={edge:.0%}"
                    )

                    # Ajouter au set bloqué du worker si actif (par market_id ET question)
                    worker = BotManager._workers.get(uid)
                    if worker:
                        if pos.market_id:
                            worker._analyzed_this_session.add(pos.market_id)
                        if pos.market_question:
                            worker._analyzed_this_session.add(
                                "q:" + str(pos.market_question)[:100].lower().strip()
                            )

            except AIUnavailableError as e:
                results["errors"].append(f"IA indisponible: {str(e)[:60]}")
                break
            except Exception as e:
                results["errors"].append(f"#{pos.id}: {str(e)[:60]}")

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            results["errors"].append(f"Commit: {str(e)[:60]}")

        total_changes = len(results["merged"]) + len(results["updated"]) + len(results["closed"])
        logger.info(f"[NM] Rebalance user {uid}: {total_changes} changements")
        return jsonify({"ok": True, "results": results,
                        "summary": f"{len(results['updated'])} mis à jour, "
                                   f"{len(results['merged'])} doublons fusionnés, "
                                   f"{len(results['closed'])} fermés"})

    @app.route("/api/positions/close-all", methods=["POST"])
    @login_required
    def api_close_all_positions():
        """Fermer toutes les positions ouvertes."""
        uid = session["user_id"]
        positions = Position.query.filter_by(user_id=uid, result="OPEN").all()
        closed = 0
        for pos in positions:
            pos.result = "CANCELLED"
            pos.pnl_usd = 0.0
            pos.exit_price = pos.current_price or pos.entry_price
            closed += 1
        try:
            db.session.commit()
            logger.info(f"[NM] {closed} positions annulées par user {uid}")
        except Exception as e:
            db.session.rollback()
            return jsonify({"ok": False, "error": str(e)[:80]})
        return jsonify({"ok": True, "message": f"{closed} position(s) annulée(s)"})

    @app.route("/api/news")
    @login_required
    def api_news():
        uid = session["user_id"]
        news = (NewsLog.query
                .filter_by(user_id=uid)
                .order_by(NewsLog.timestamp.desc())
                .limit(50).all())
        return jsonify({"news": [n.to_dict() for n in news]})

    @app.route("/api/markets")
    @login_required
    def api_markets():
        uid     = session["user_id"]
        markets = MARKETS_CACHE.get(uid, [])
        # Si cache vide (bot pas encore démarré), on tente un fetch live
        if not markets:
            try:
                from engine.polymarket_client import PolyMarketClient, CATEGORIES
                all_m = []
                for cat in CATEGORIES[:6]:   # top 6 catégories pour rester rapide
                    all_m.extend(PolyMarketClient.get_active_markets(category=cat, limit=30))
                filtered = PolyMarketClient.filter_tradeable(all_m)
                markets = [
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
                    for m in filtered[:40]
                ]
            except Exception as e:
                logger.error(f"[markets] live fetch: {e}")
        return jsonify({"markets": markets, "count": len(markets)})

    @app.route("/api/balance")
    @login_required
    def api_balance():
        uid  = session["user_id"]
        cred = PolyCredential.query.filter_by(user_id=uid).first()
        if not cred:
            return jsonify({"ok": False, "usdc": 0})
        try:
            client  = PolyMarketClient(cred.get_key())
            balance = client.get_balance()
            return jsonify({"ok": True, "usdc": round(balance, 2)})
        except Exception as e:
            return jsonify({"ok": False, "usdc": 0, "error": str(e)})

    # ── Settings Ollama ───────────────────────────────────────────────────────

    @app.route("/settings/ollama", methods=["GET", "POST"])
    @login_required
    def settings_ollama():
        uid = session["user_id"]
        cfg = OllamaConfig.query.filter_by(user_id=uid).first()

        if request.method == "POST":
            ollama_url = request.form.get("ollama_url", "").strip().rstrip("/")
            api_key    = request.form.get("api_key", "").strip()
            model_fast = request.form.get("model_fast", "").strip()
            model_smart= request.form.get("model_smart", "").strip()

            if not ollama_url:
                ollama_url = "https://api.ollama.com"  # hardcode si absent

            if not cfg:
                cfg = OllamaConfig(user_id=uid)
                db.session.add(cfg)

            cfg.ollama_url = ollama_url
            if api_key:
                cfg.set_api_key(api_key)
            cfg.model_fast  = model_fast  or cfg.model_fast
            cfg.model_smart = model_smart or cfg.model_smart
            cfg.verified_at = datetime.utcnow()
            db.session.commit()
            flash("Configuration Ollama Cloud sauvegardée ✅", "success")
            return redirect(url_for("settings_ollama"))

        return render_template("settings_ollama.html", cfg=cfg, models=[])

    @app.route("/api/ollama/models")
    @login_required
    def api_ollama_models():
        """Récupère la liste des modèles depuis l'instance Ollama Cloud."""
        url     = request.args.get("url", "").strip()
        api_key = request.args.get("api_key", "").strip()

        # Si pas passé en param, tenter depuis la DB
        if not url:
            uid = session["user_id"]
            cfg = OllamaConfig.query.filter_by(user_id=uid).first()
            if cfg:
                url     = cfg.ollama_url or ""
                api_key = api_key or cfg.get_api_key()

        result = fetch_ollama_models(url, api_key)
        return jsonify(result)

    @app.route("/api/ai-status")
    @login_required
    def api_ai_status():
        """Vérifie les backends IA disponibles (Ollama + Perplexity)."""
        status = check_ai_available()
        return jsonify(status)

    @app.route("/api/debug/status")
    @login_required
    def api_debug_status():
        """Debug endpoint — state du bot et vérifications."""
        uid = session["user_id"]
        active_session = BotSession.query.filter_by(user_id=uid, status="running").first()
        articles_count = NewsLog.query.filter_by(user_id=uid).count()
        positions_count = Position.query.filter_by(user_id=uid, result="OPEN").count()

        # Tracker CSV stats
        worker = BotManager._workers.get(uid)
        tracker_stats = worker._tracker.stats() if worker else {}

        return jsonify({
            "user_id": uid,
            "bot_running": BotManager.is_running(uid),
            "session_mode": active_session.mode if active_session else None,
            "articles_in_db": articles_count,
            "open_positions": positions_count,
            "ai_status": check_ai_available(),
            "markets_cached": len(MARKETS_CACHE.get(uid, [])),
            "tracker": tracker_stats,
        })

    @app.route("/api/markets/tracker")
    @login_required
    def api_markets_tracker():
        """
        Retourne les marchés analysés (JSON) pour affichage dans le dashboard.
        Tri : plus récents en premier.
        """
        import csv as csv_mod, os
        from engine.market_tracker import MarketTracker, _DEFAULT_DATA_DIR
        from pathlib import Path
        uid = session["user_id"]

        csv_path = Path(_DEFAULT_DATA_DIR) / f"markets_u{uid}.csv"
        if not csv_path.exists():
            return jsonify({"rows": [], "stats": {"total": 0, "with_signal": 0}})

        rows = []
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    rows.append({
                        "market_key":  row.get("market_key", ""),
                        "question":    row.get("question", "")[:140],
                        "analyzed_at": row.get("analyzed_at", ""),
                        "signal":      row.get("signal", "NO"),
                        "edge":        float(row.get("edge", 0) or 0),
                        "confidence":  int(row.get("confidence", 0) or 0),
                        "side":        row.get("side", ""),
                        "source":      row.get("source", ""),
                    })
        except Exception as e:
            return jsonify({"rows": [], "stats": {}, "error": str(e)})

        # Trier par analyzed_at décroissant
        rows.sort(key=lambda r: r["analyzed_at"], reverse=True)
        with_signal = sum(1 for r in rows if r["signal"] == "YES")
        return jsonify({
            "rows":  rows[:200],  # max 200 lignes côté UI
            "stats": {
                "total":       len(rows),
                "with_signal": with_signal,
                "no_signal":   len(rows) - with_signal,
            },
        })

    @app.route("/api/markets/tracker/csv")
    @login_required
    def api_markets_tracker_csv():
        """
        Télécharger le CSV des marchés analysés.
        Retourne le fichier CSV ou un JSON d'erreur si le tracker n'est pas dispo.
        """
        from flask import send_file
        uid = session["user_id"]
        worker = BotManager._workers.get(uid)
        if not worker:
            # Bot stoppé — créer un tracker temporaire pour lire le CSV existant
            from engine.market_tracker import MarketTracker
            tmp_tracker = MarketTracker(user_id=uid)
            csv_path = tmp_tracker.export_csv_path()
        else:
            csv_path = worker._tracker.export_csv_path()

        import os
        if not os.path.exists(csv_path):
            return jsonify({"error": "Aucun CSV disponible — démarrez le bot d'abord"}), 404

        return send_file(
            csv_path,
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"novamarket_markets_u{uid}.csv",
        )

    @app.route("/api/markets/tracker/reset", methods=["POST"])
    @login_required
    def api_markets_tracker_reset():
        """
        Remet à zéro le CSV des marchés analysés.
        Utile pour forcer une ré-analyse complète (ex: après un long arrêt).
        """
        import os
        from engine.market_tracker import MarketTracker, _DEFAULT_DATA_DIR
        from pathlib import Path
        uid = session["user_id"]
        csv_path = Path(_DEFAULT_DATA_DIR) / f"markets_u{uid}.csv"
        deleted = False
        if csv_path.exists():
            csv_path.unlink()
            deleted = True

        # Vider aussi le set en mémoire si worker actif
        worker = BotManager._workers.get(uid)
        if worker:
            worker._analyzed_this_session.clear()
            worker._tracker._analyzed.clear()
            worker._tracker._rows.clear()

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "message": "Tracker réinitialisé — tous les marchés seront ré-analysés",
        })

    # ── Performance Analytics ────────────────────────────────────────────────

    @app.route("/api/analytics")
    @login_required
    def api_analytics():
        """
        Performance analytics: PnL over time, category breakdown, edge accuracy,
        signal quality metrics. Powers the Analytics dashboard tab.
        """
        from collections import defaultdict
        uid = session["user_id"]

        all_pos = (Position.query
                   .filter(Position.user_id == uid,
                           Position.result.in_(["WIN", "LOSS"]))
                   .order_by(Position.timestamp.asc())
                   .all())

        if not all_pos:
            return jsonify({
                "ok": True,
                "cumulative_pnl": [],
                "by_category": {},
                "edge_accuracy": [],
                "summary": {
                    "total_trades": 0, "wins": 0, "losses": 0,
                    "winrate": 0, "total_pnl": 0, "avg_edge": 0,
                    "avg_confidence": 0, "best_category": "",
                    "worst_category": "", "profit_factor": 0,
                    "avg_win": 0, "avg_loss": 0,
                    "expectancy_per_trade": 0,
                },
                "daily_pnl": {},
                "by_side": {"YES": {"trades": 0, "pnl": 0, "winrate": 0},
                            "NO":  {"trades": 0, "pnl": 0, "winrate": 0}},
            })

        # Cumulative PnL over time
        cumulative = []
        running_pnl = 0.0
        for p in all_pos:
            running_pnl += (p.pnl_usd or 0)
            cumulative.append({
                "timestamp": p.timestamp.isoformat(),
                "pnl": round(running_pnl, 2),
                "trade_id": p.id,
                "result": p.result,
            })

        # Category breakdown
        by_cat = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0,
                                       "pnl": 0.0, "total_edge": 0.0,
                                       "total_confidence": 0})
        for p in all_pos:
            cat = p.category or "general"
            by_cat[cat]["trades"] += 1
            by_cat[cat]["pnl"] += (p.pnl_usd or 0)
            by_cat[cat]["total_edge"] += (p.edge_at_entry or 0)
            by_cat[cat]["total_confidence"] += (p.ai_confidence or 0)
            if p.result == "WIN":
                by_cat[cat]["wins"] += 1
            else:
                by_cat[cat]["losses"] += 1

        for cat, data in by_cat.items():
            t = data["wins"] + data["losses"]
            data["winrate"] = round(data["wins"] / t * 100, 1) if t else 0
            data["avg_edge"] = round(data["total_edge"] / t, 4) if t else 0
            data["avg_confidence"] = round(data["total_confidence"] / t) if t else 0
            data["pnl"] = round(data["pnl"], 2)
            del data["total_edge"]
            del data["total_confidence"]

        # Edge accuracy: compare AI estimated_prob vs actual outcome
        edge_data = []
        for p in all_pos:
            if p.estimated_prob and p.entry_price:
                actual = 1.0 if p.result == "WIN" else 0.0
                edge_data.append({
                    "estimated_prob": round(p.estimated_prob, 3),
                    "entry_price": round(p.entry_price, 3),
                    "edge": round(p.edge_at_entry or 0, 4),
                    "confidence": p.ai_confidence or 0,
                    "actual": actual,
                    "correct": (p.estimated_prob > p.entry_price) == (actual == 1.0),
                    "category": p.category or "general",
                })

        # Daily PnL
        daily = defaultdict(float)
        for p in all_pos:
            day_key = p.timestamp.strftime("%Y-%m-%d")
            daily[day_key] += (p.pnl_usd or 0)
        daily_pnl = {k: round(v, 2) for k, v in sorted(daily.items())}

        # Side breakdown (YES vs NO)
        by_side = {"YES": {"trades": 0, "wins": 0, "pnl": 0.0},
                   "NO":  {"trades": 0, "wins": 0, "pnl": 0.0}}
        for p in all_pos:
            s = p.side if p.side in ("YES", "NO") else "YES"
            by_side[s]["trades"] += 1
            by_side[s]["pnl"] += (p.pnl_usd or 0)
            if p.result == "WIN":
                by_side[s]["wins"] += 1
        for s in by_side:
            t = by_side[s]["trades"]
            by_side[s]["winrate"] = round(by_side[s]["wins"] / t * 100, 1) if t else 0
            by_side[s]["pnl"] = round(by_side[s]["pnl"], 2)

        # Summary stats
        wins = sum(1 for p in all_pos if p.result == "WIN")
        losses = sum(1 for p in all_pos if p.result == "LOSS")
        total = wins + losses
        total_pnl = sum(p.pnl_usd or 0 for p in all_pos)
        win_pnls = [p.pnl_usd for p in all_pos if p.result == "WIN" and p.pnl_usd]
        loss_pnls = [abs(p.pnl_usd) for p in all_pos if p.result == "LOSS" and p.pnl_usd]
        gross_profit = sum(win_pnls) if win_pnls else 0
        gross_loss = sum(loss_pnls) if loss_pnls else 0

        cat_pnls = {cat: data["pnl"] for cat, data in by_cat.items()}
        best_cat = max(cat_pnls, key=cat_pnls.get) if cat_pnls else ""
        worst_cat = min(cat_pnls, key=cat_pnls.get) if cat_pnls else ""

        summary = {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "winrate": round(wins / total * 100, 1) if total else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_edge": round(sum(p.edge_at_entry or 0 for p in all_pos) / total, 4) if total else 0,
            "avg_confidence": round(sum(p.ai_confidence or 0 for p in all_pos) / total) if total else 0,
            "best_category": best_cat,
            "worst_category": worst_cat,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
            "avg_win": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0,
            "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0,
            "expectancy_per_trade": round(total_pnl / total, 2) if total else 0,
        }

        return jsonify({
            "ok": True,
            "cumulative_pnl": cumulative,
            "by_category": dict(by_cat),
            "edge_accuracy": edge_data,
            "summary": summary,
            "daily_pnl": daily_pnl,
            "by_side": by_side,
        })

    # ── Healthcheck Railway ───────────────────────────────────────────────────

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "service": "NovaMarket"})


# ── Entrypoint ────────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
