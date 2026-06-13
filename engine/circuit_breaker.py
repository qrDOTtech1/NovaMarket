"""Circuit breakers NovaMarket — session, daily loss, cool-down."""
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional
from engine.risk import DAILY_LOSS_LIMIT_PCT

MAX_CONSECUTIVE_LOSSES = 3
COOLDOWN_AFTER_STREAK  = 1800   # 30 min après 3 pertes d'affilée
MAX_DRAWDOWN_FROM_PEAK = 0.20   # halt if drawdown from session peak exceeds 20%


@dataclass
class MarketSession:
    start_bankroll: float       = 0.0
    current_bankroll: float     = 0.0
    peak_bankroll: float        = 0.0
    session_pnl: float          = 0.0
    consecutive_losses: int     = 0
    cooldown_until: float       = 0.0
    daily_triggered: bool       = False
    drawdown_triggered: bool    = False
    total_trades: int           = 0
    wins: int                   = 0
    losses: int                 = 0
    category_exposure: Dict[str, float] = field(default_factory=dict)

    @property
    def winrate(self) -> float:
        t = self.wins + self.losses
        return round(self.wins / t * 100, 1) if t else 0.0

    @property
    def open_exposure(self) -> float:
        return sum(self.category_exposure.values())

    @property
    def drawdown_pct(self) -> float:
        if self.peak_bankroll <= 0:
            return 0.0
        return round((self.peak_bankroll - self.current_bankroll) / self.peak_bankroll, 4)


class CircuitBreaker:
    _sessions: Dict[int, MarketSession] = {}
    _lock = threading.Lock()

    @classmethod
    def init(cls, user_id: int, bankroll: float):
        with cls._lock:
            cls._sessions[user_id] = MarketSession(
                start_bankroll=bankroll,
                current_bankroll=bankroll,
                peak_bankroll=bankroll,
            )

    @classmethod
    def get(cls, user_id: int) -> Optional[MarketSession]:
        return cls._sessions.get(user_id)

    @classmethod
    def can_trade(cls, user_id: int) -> tuple:
        with cls._lock:
            s = cls._sessions.get(user_id)
            if not s:
                return False, "session non initialisée"
            if s.daily_triggered:
                return False, "circuit breaker journalier actif"
            if s.drawdown_triggered:
                return False, "drawdown from peak exceeded — arrêt"
            loss_pct = (s.start_bankroll - s.current_bankroll) / max(s.start_bankroll, 1)
            if loss_pct >= DAILY_LOSS_LIMIT_PCT:
                s.daily_triggered = True
                return False, f"daily loss {loss_pct*100:.1f}% — arrêt"
            if s.drawdown_pct >= MAX_DRAWDOWN_FROM_PEAK:
                s.drawdown_triggered = True
                return False, f"drawdown {s.drawdown_pct*100:.1f}% from peak — arrêt"
            now = time.time()
            if s.cooldown_until > now:
                return False, f"cooldown {int(s.cooldown_until - now)}s"
            return True, ""

    @classmethod
    def record_trade(cls, user_id: int, pnl: float, bankroll: float,
                     category: str = "general", size: float = 0.0):
        with cls._lock:
            s = cls._sessions.get(user_id)
            if not s:
                return
            s.current_bankroll = bankroll
            s.peak_bankroll    = max(s.peak_bankroll, bankroll)
            s.session_pnl     += pnl
            s.total_trades    += 1
            # Libérer l'exposition de ce trade
            if category in s.category_exposure:
                s.category_exposure[category] = max(0, s.category_exposure[category] - size)
            if pnl < 0:
                s.losses             += 1
                s.consecutive_losses += 1
                if s.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    s.cooldown_until    = time.time() + COOLDOWN_AFTER_STREAK
                    s.consecutive_losses = 0
            else:
                s.wins               += 1
                s.consecutive_losses  = 0

    @classmethod
    def add_exposure(cls, user_id: int, category: str, size: float):
        with cls._lock:
            s = cls._sessions.get(user_id)
            if not s:
                return
            s.category_exposure[category] = s.category_exposure.get(category, 0) + size

    @classmethod
    def reset(cls, user_id: int):
        with cls._lock:
            cls._sessions.pop(user_id, None)
