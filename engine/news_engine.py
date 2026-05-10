"""
News Engine — agrégation RSS multi-sources, dédoublonnage, scoring de pertinence.
Rafraîchissement toutes les 60 secondes. Chaque article est traité une seule fois.
"""
import hashlib
import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional
import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Sources RSS — couverture maximale ────────────────────────────────────────
RSS_FEEDS = {
    # Actualités générales
    "reuters_top":    "https://feeds.reuters.com/reuters/topNews",
    "reuters_world":  "https://feeds.reuters.com/Reuters/worldNews",
    "reuters_us":     "https://feeds.reuters.com/Reuters/domesticNews",
    "bbc_world":      "http://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_us":         "http://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "guardian_world": "https://www.theguardian.com/world/rss",
    "ap_top":         "https://feeds.apnews.com/rss/apf-topnews",
    "ap_politics":    "https://feeds.apnews.com/rss/apf-politics",
    "ap_business":    "https://feeds.apnews.com/rss/apf-business",

    # Politique US (Polymarket très actif là-dessus)
    "politico":       "https://www.politico.com/rss/politicopicks.xml",
    "axios_politics": "https://api.axios.com/feed/",
    "hill":           "https://thehill.com/rss/syndicator/19110",
    "npr_politics":   "https://feeds.npr.org/1014/rss.xml",

    # Crypto / Finance (marchés Polymarket crypto)
    "cointelegraph":  "https://cointelegraph.com/rss",
    "coindesk":       "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "decrypt":        "https://decrypt.co/feed",
    "theblock":       "https://www.theblock.co/rss.xml",

    # Science / Tech
    "techcrunch":     "https://techcrunch.com/feed/",
    "ars_technica":   "https://feeds.arstechnica.com/arstechnica/index",

    # Sports (marchés sportifs Polymarket)
    "espn":           "https://www.espn.com/espn/rss/news",
    "bbc_sport":      "http://feeds.bbci.co.uk/sport/rss.xml",
}

# Mots-clés Polymarket — augmentent le score de pertinence d'un article
POLYMARKET_KEYWORDS = [
    # Politique
    "election", "president", "congress", "senate", "vote", "poll", "approval",
    "democrat", "republican", "biden", "trump", "harris", "white house",
    "referendum", "parliament", "government", "minister",
    # Crypto
    "bitcoin", "ethereum", "crypto", "btc", "eth", "sec", "etf", "halving",
    "regulation", "stablecoin", "blockchain", "defi", "nft",
    # Finance / Macro
    "fed", "interest rate", "inflation", "gdp", "recession", "market",
    "earnings", "ipo", "merger", "acquisition", "bankruptcy",
    # Géopolitique
    "war", "conflict", "ceasefire", "sanctions", "nato", "un ", "treaty",
    "invasion", "attack", "military",
    # Science / Tech
    "ai", "artificial intelligence", "openai", "google", "microsoft", "apple",
    "fda", "drug", "vaccine", "climate",
    # Sports
    "championship", "world cup", "super bowl", "nba", "nfl", "ufc",
    "winner", "final", "playoff",
]

REFRESH_INTERVAL = 60   # secondes


class Article:
    def __init__(self, source: str, title: str, summary: str,
                 url: str, published: Optional[datetime] = None):
        self.source    = source
        self.title     = title
        self.summary   = summary
        self.url       = url
        self.published = published or datetime.now(timezone.utc)
        self.uid       = hashlib.md5(url.encode()).hexdigest()
        self.score     = self._score()
        self.full_text = ""

    def _score(self) -> int:
        text = (self.title + " " + self.summary).lower()
        return sum(1 for kw in POLYMARKET_KEYWORDS if kw in text)

    def to_dict(self) -> dict:
        return {
            "source":    self.source,
            "title":     self.title,
            "summary":   self.summary[:300],
            "url":       self.url,
            "published": self.published.isoformat(),
            "score":     self.score,
        }


class NewsEngine:
    """
    Aggrège toutes les sources RSS, dédoublonne par URL hash,
    maintient un buffer des N derniers articles.
    Thread-safe.
    """
    MAX_BUFFER  = 500   # articles en mémoire
    MIN_SCORE   = 1     # score minimum pour être analysé par l'IA

    def __init__(self):
        self._articles: dict[str, Article] = {}   # uid -> Article
        self._lock = threading.Lock()
        self._new_since: list[str] = []           # uids non encore traités
        self._last_refresh = 0.0
        self._stats = {"total_fetched": 0, "total_sources": 0}

    def refresh(self) -> int:
        """Rafraîchit toutes les sources. Retourne le nb de nouveaux articles."""
        new_count = 0
        for name, url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:20]:  # 20 derniers par source
                    title   = getattr(entry, "title", "").strip()
                    summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
                    link    = getattr(entry, "link", "")
                    if not title or not link:
                        continue
                    # Nettoyer le HTML dans le summary
                    try:
                        summary = BeautifulSoup(summary, "lxml").get_text(separator=" ")[:500]
                    except Exception:
                        summary = summary[:500]
                    # Date de publication
                    pub = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        try:
                            import calendar
                            ts  = calendar.timegm(entry.published_parsed)
                            pub = datetime.fromtimestamp(ts, tz=timezone.utc)
                        except Exception:
                            pass
                    art = Article(name, title, summary, link, pub)
                    with self._lock:
                        if art.uid not in self._articles:
                            self._articles[art.uid] = art
                            if art.score >= self.MIN_SCORE:
                                self._new_since.append(art.uid)
                            new_count += 1
            except Exception as e:
                logger.debug(f"[News] {name} erreur: {e}")
        # Purge du buffer
        with self._lock:
            if len(self._articles) > self.MAX_BUFFER:
                oldest = sorted(self._articles.values(), key=lambda a: a.published)
                for old in oldest[:len(self._articles) - self.MAX_BUFFER]:
                    self._articles.pop(old.uid, None)
        self._last_refresh = time.time()
        self._stats["total_fetched"] += new_count
        logger.info(f"[News] refresh +{new_count} articles ({len(self._articles)} total)")
        return new_count

    def pop_new(self, max_items: int = 30) -> list:
        """Retourne et vide la liste des nouveaux articles pertinents."""
        with self._lock:
            uids = self._new_since[:max_items]
            self._new_since = self._new_since[max_items:]
            return [self._articles[u] for u in uids if u in self._articles]

    def latest(self, n: int = 20, min_score: int = 1) -> list:
        with self._lock:
            arts = [a for a in self._articles.values() if a.score >= min_score]
        return sorted(arts, key=lambda a: a.published, reverse=True)[:n]

    @property
    def stats(self) -> dict:
        return {**self._stats, "buffer_size": len(self._articles),
                "pending_analysis": len(self._new_since)}
