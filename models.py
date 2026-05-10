import os
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    plan          = db.Column(db.String(20), default="trial")
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class PolyCredential(db.Model):
    """Clé privée Polygon pour Polymarket."""
    __tablename__ = "poly_credentials"
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    private_key  = db.Column(db.Text, nullable=False)   # stocké chiffré via env FERNET_KEY
    wallet_addr  = db.Column(db.String(42), nullable=True)
    verified_at  = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("poly_credential", uselist=False))

    def set_key(self, key: str):
        fernet_key = os.environ.get("FERNET_KEY", "")
        if fernet_key:
            from cryptography.fernet import Fernet
            self.private_key = Fernet(fernet_key.encode()).encrypt(key.encode()).decode()
        else:
            self.private_key = key

    def get_key(self) -> str:
        fernet_key = os.environ.get("FERNET_KEY", "")
        if fernet_key and not self.private_key.startswith("0x"):
            try:
                from cryptography.fernet import Fernet
                return Fernet(fernet_key.encode()).decrypt(self.private_key.encode()).decode()
            except Exception:
                pass
        return self.private_key


class BotSession(db.Model):
    __tablename__ = "bot_sessions"
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status       = db.Column(db.String(20), default="stopped")
    started_at   = db.Column(db.DateTime, nullable=True)
    stopped_at   = db.Column(db.DateTime, nullable=True)
    pnl_usd      = db.Column(db.Float, default=0.0)
    total_trades = db.Column(db.Integer, default=0)
    wins         = db.Column(db.Integer, default=0)
    losses       = db.Column(db.Integer, default=0)
    error_msg    = db.Column(db.Text, nullable=True)


class Position(db.Model):
    """Position ouverte sur Polymarket."""
    __tablename__ = "positions"
    id             = db.Column(db.Integer, primary_key=True)
    session_id     = db.Column(db.Integer, db.ForeignKey("bot_sessions.id"), nullable=False)
    user_id        = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    timestamp      = db.Column(db.DateTime, default=datetime.utcnow)
    market_id      = db.Column(db.String(100), nullable=False)
    market_question= db.Column(db.Text, nullable=False)
    category       = db.Column(db.String(30), default="general")
    side           = db.Column(db.String(5),  nullable=False)   # YES | NO
    size_usd       = db.Column(db.Float, nullable=False)
    entry_price    = db.Column(db.Float, nullable=False)   # probabilité à l'entrée
    current_price  = db.Column(db.Float, nullable=True)
    exit_price     = db.Column(db.Float, nullable=True)
    estimated_prob = db.Column(db.Float, nullable=True)    # estimation IA
    edge_at_entry  = db.Column(db.Float, nullable=True)
    ai_confidence  = db.Column(db.Integer, default=0)
    ai_reasoning   = db.Column(db.Text, nullable=True)
    article_title  = db.Column(db.Text, nullable=True)
    article_source = db.Column(db.String(50), nullable=True)
    ev_usd         = db.Column(db.Float, nullable=True)
    pnl_usd        = db.Column(db.Float, nullable=True)
    result         = db.Column(db.String(20), default="OPEN")  # OPEN|WIN|LOSS|CANCELLED
    order_id       = db.Column(db.String(100), nullable=True)
    hours_to_close = db.Column(db.Float, nullable=True)

    def to_dict(self):
        return {
            "id":               self.id,
            "timestamp":        self.timestamp.isoformat(),
            "market_question":  self.market_question[:80],
            "category":         self.category,
            "side":             self.side,
            "size_usd":         self.size_usd,
            "entry_price":      self.entry_price,
            "estimated_prob":   self.estimated_prob,
            "edge_at_entry":    self.edge_at_entry,
            "ai_confidence":    self.ai_confidence,
            "ev_usd":           self.ev_usd,
            "pnl_usd":          self.pnl_usd,
            "result":           self.result,
        }


class NewsLog(db.Model):
    """Articles analysés récemment."""
    __tablename__ = "news_logs"
    id          = db.Column(db.Integer, primary_key=True)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)
    source      = db.Column(db.String(50), nullable=False)
    title       = db.Column(db.Text, nullable=False)
    url         = db.Column(db.Text, nullable=True)
    relevance   = db.Column(db.Integer, default=0)
    signals_gen = db.Column(db.Integer, default=0)   # nb de signaux générés

    def to_dict(self):
        return {
            "timestamp":   self.timestamp.strftime("%H:%M:%S"),
            "source":      self.source,
            "title":       self.title[:100],
            "relevance":   self.relevance,
            "signals_gen": self.signals_gen,
        }


class BotActivity(db.Model):
    """Flux d'activité temps réel."""
    __tablename__ = "bot_activity"
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    level     = db.Column(db.String(10), default="info")
    emoji     = db.Column(db.String(10), nullable=True)
    message   = db.Column(db.Text, nullable=False)

    def to_dict(self):
        return {
            "timestamp": self.timestamp.strftime("%H:%M:%S"),
            "level":     self.level,
            "emoji":     self.emoji or "📡",
            "message":   self.message,
        }
