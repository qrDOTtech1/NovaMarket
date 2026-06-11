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
            try:
                from cryptography.fernet import Fernet
                self.private_key = Fernet(fernet_key.encode()).encrypt(key.encode()).decode()
                return
            except Exception:
                pass  # clé Fernet invalide → stockage en clair
        self.private_key = key

    def get_key(self) -> str:
        fernet_key = os.environ.get("FERNET_KEY", "")
        if fernet_key and self.private_key and not self.private_key.startswith("0x"):
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
    mode         = db.Column(db.String(20), default="real")   # 'real' | 'simulation'
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
    exit_trigger   = db.Column(db.Text, nullable=True)   # événement qui résoudra le marché
    thesis         = db.Column(db.Text, nullable=True)   # thèse d'investissement

    __table_args__ = (
        db.Index("ix_positions_user_result", "user_id", "result"),
        db.Index("ix_positions_user_market", "user_id", "market_id"),
        db.Index("ix_positions_timestamp", "timestamp"),
    )

    def roi_if_win(self) -> float:
        if self.entry_price and self.entry_price > 0:
            return round((1.0 / self.entry_price - 1.0) * 100, 1)
        return 0.0

    @property
    def age_hours(self) -> float:
        if not self.timestamp:
            return 0.0
        delta = datetime.utcnow() - self.timestamp
        return round(delta.total_seconds() / 3600, 1)

    @property
    def is_stale(self) -> bool:
        if self.hours_to_close and self.age_hours > self.hours_to_close * 0.9:
            return True
        return self.age_hours > 168  # >7 days with no resolution

    @property
    def unrealized_pnl(self) -> float:
        if self.current_price is None or self.entry_price is None:
            return 0.0
        if self.side == "YES":
            return round((self.current_price - self.entry_price) * self.size_usd / self.entry_price, 2)
        else:
            return round((self.entry_price - self.current_price) * self.size_usd / self.entry_price, 2)

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
            "ai_reasoning":     self.ai_reasoning,
            "thesis":           self.thesis,
            "exit_trigger":     self.exit_trigger,
            "ev_usd":           self.ev_usd,
            "pnl_usd":          self.pnl_usd,
            "result":           self.result,
            "current_price":    self.current_price,
            "roi_if_win":       self.roi_if_win(),
            "age_hours":        self.age_hours,
            "is_stale":         self.is_stale,
            "unrealized_pnl":   self.unrealized_pnl,
        }


class OllamaConfig(db.Model):
    """Configuration Ollama Cloud par utilisateur."""
    __tablename__ = "ollama_configs"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    ollama_url    = db.Column(db.Text, nullable=True)
    _api_key      = db.Column("api_key", db.Text, nullable=True)
    model_fast    = db.Column(db.String(100), nullable=True)   # classify + match
    model_smart   = db.Column(db.String(100), nullable=True)   # estimate_prob
    verified_at   = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("ollama_config", uselist=False))

    def set_api_key(self, key: str):
        fernet_key = os.environ.get("FERNET_KEY", "")
        if fernet_key and key:
            try:
                from cryptography.fernet import Fernet
                self._api_key = Fernet(fernet_key.encode()).encrypt(key.encode()).decode()
                return
            except Exception:
                pass  # clé Fernet invalide → stockage en clair
        self._api_key = key

    def get_api_key(self) -> str:
        fernet_key = os.environ.get("FERNET_KEY", "")
        if fernet_key and self._api_key:
            try:
                from cryptography.fernet import Fernet
                return Fernet(fernet_key.encode()).decrypt(self._api_key.encode()).decode()
            except Exception:
                pass
        return self._api_key or ""

    def to_dict(self):
        return {
            "ollama_url":  self.ollama_url,
            "model_fast":  self.model_fast,
            "model_smart": self.model_smart,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }


class NewsLog(db.Model):
    __tablename__ = "news_logs"
    __table_args__ = (
        db.Index("ix_news_user_ts", "user_id", "timestamp"),
    )
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)
    source      = db.Column(db.String(50), nullable=False)
    title       = db.Column(db.Text, nullable=False)
    url         = db.Column(db.Text, nullable=True)
    relevance   = db.Column(db.Integer, default=0)
    signals_gen = db.Column(db.Integer, default=0)   # nb de signaux générés

    user = db.relationship("User", backref=db.backref("news_logs"))

    def to_dict(self):
        return {
            "timestamp":   self.timestamp.strftime("%H:%M:%S"),
            "source":      self.source,
            "title":       self.title[:100],
            "url":         self.url or "",
            "relevance":   self.relevance,
            "signals_gen": self.signals_gen,
        }


class BotActivity(db.Model):
    __tablename__ = "bot_activity"
    __table_args__ = (
        db.Index("ix_activity_user_id", "user_id", "id"),
    )
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
