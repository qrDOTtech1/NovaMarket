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

    @app.route("/api/news")
    @login_required
    def api_news():
        news = (NewsLog.query
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

    # ── Healthcheck Railway ───────────────────────────────────────────────────

    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "service": "NovaMarket"})


# ── Entrypoint ────────────────────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
