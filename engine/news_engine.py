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

# ── Sources RSS — testées et validées ───────────────────────────────────────
# Dernière vérification : 2026-05-10
RSS_FEEDS = {
    # ── Actualités mondiales ──────────────────────────────────────────────────
    "bbc_world":      "https://feeds.bbci.co.uk/news/world/rss.xml",       # ✅ 33 art
    "bbc_politics":   "https://feeds.bbci.co.uk/news/politics/rss.xml",    # ✅ 60 art
    "bbc_sport":      "https://feeds.bbci.co.uk/sport/rss.xml",            # ✅
    "guardian_world": "https://www.theguardian.com/world/rss",             # ✅ 45 art
    "al_jazeera":     "https://www.aljazeera.com/xml/rss/all.xml",         # ✅ 25 art
    "sky_news":       "https://feeds.skynews.com/feeds/rss/world.xml",     # ✅ 10 art
    "nyt_world":      "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",     # ✅ 55 art
    "nyt_politics":   "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",  # ✅ 20 art

    # ── Politique US (Polymarket très actif) ─────────────────────────────────
    "politico":       "https://rss.politico.com/politics-news.xml",        # ✅ 30 art
    "axios":          "https://api.axios.com/feed/",                       # ✅ 100 art
    "the_hill":       "https://thehill.com/feed/",                         # ✅ 100 art
    "npr_news":       "https://feeds.npr.org/1001/rss.xml",                # ✅ 10 art
    "fivethirtyeight":"https://fivethirtyeight.com/features/feed/",        # ✅ 20 art

    # ── Crypto / Web3 ────────────────────────────────────────────────────────
    "cointelegraph":  "https://cointelegraph.com/rss",                     # ✅ 30 art
    "coindesk":       "https://www.coindesk.com/arc/outboundfeeds/rss/",   # ✅ 25 art
    "decrypt":        "https://decrypt.co/feed",                           # ✅ 38 art
    "theblock":       "https://www.theblock.co/rss.xml",                   # ✅ 20 art
    "cryptoslate":    "https://cryptoslate.com/feed/",                     # ✅ 10 art
    "cryptonews":     "https://cryptonews.com/news/feed/",                 # ✅ 20 art
    "beincrypto":     "https://beincrypto.com/feed/",                      # ✅ 12 art

    # ── Finance / Marchés ────────────────────────────────────────────────────
    "bloomberg":      "https://feeds.bloomberg.com/markets/news.rss",      # ✅ 30 art
    "yahoo_finance":  "https://finance.yahoo.com/news/rssindex",           # ✅ 50 art
    "cnbc":           "https://www.cnbc.com/id/100003114/device/rss/rss.html", # ✅ 30 art
    "marketwatch":    "https://feeds.marketwatch.com/marketwatch/topstories/", # ✅ 10 art
    "ft":             "https://www.ft.com/rss/home",                       # ✅  9 art

    # ── Sports ───────────────────────────────────────────────────────────────
    "cbs_sports":     "https://www.cbssports.com/rss/headlines/",          # ✅ 36 art
    "sporting_news":  "https://www.sportingnews.com/us/rss",               # ✅ 20 art

    # ── Pop culture / Entertainment (marchés originaux Polymarket) ───────────
    "variety":        "https://variety.com/feed/",                         # films/célébrités
    "tmz":            "https://www.tmz.com/rss.xml",                       # people/célébrités
    "deadline":       "https://deadline.com/feed/",                        # box-office/TV
    "techcrunch":     "https://techcrunch.com/feed/",                      # tech/startups
}

# Mots-clés Polymarket — couvrent TOUTES les catégories du site
POLYMARKET_KEYWORDS = [
    # ── Politique US & Monde ──────────────────────────────────────────────────
    "election", "president", "congress", "senate", "vote", "poll", "approval",
    "democrat", "republican", "trump", "harris", "biden", "white house",
    "referendum", "parliament", "government", "minister", "prime minister",
    "impeach", "indictment", "conviction", "pardon", "executive order",
    "supreme court", "nominee",

    # ── Crypto & Web3 ─────────────────────────────────────────────────────────
    "bitcoin", "ethereum", "crypto", "btc", "eth", "sol", "xrp", "bnb",
    "sec", "etf", "halving", "regulation", "stablecoin", "blockchain",
    "defi", "nft", "coinbase", "binance", "altcoin", "memecoin",

    # ── Finance / Macro ───────────────────────────────────────────────────────
    "fed", "federal reserve", "interest rate", "inflation", "gdp", "recession",
    "stock market", "s&p", "nasdaq", "earnings", "ipo", "merger", "acquisition",
    "bankruptcy", "tariff", "trade war", "dollar", "oil price", "gold",

    # ── Géopolitique ──────────────────────────────────────────────────────────
    "war", "conflict", "ceasefire", "sanctions", "nato", "treaty",
    "invasion", "attack", "military", "nuclear", "ukraine", "russia",
    "china", "taiwan", "israel", "gaza", "iran",

    # ── Tech & IA ─────────────────────────────────────────────────────────────
    "artificial intelligence", "openai", "chatgpt", "google", "microsoft",
    "apple", "meta", "tesla", "spacex", "elon musk", "nvidia",
    "fda", "drug approval", "vaccine", "climate", "energy",

    # ── Sports ───────────────────────────────────────────────────────────────
    "championship", "world cup", "super bowl", "nba", "nfl", "ufc", "mma",
    "winner", "final", "playoff", "mvp", "transfer", "signing",
    "formula 1", "f1", "wimbledon", "grand slam", "oscar",

    # ── Pop culture / Marchés originaux ───────────────────────────────────────
    "oscar", "grammy", "emmy", "box office", "album", "tour",
    "celebrity", "kardashian", "taylor swift", "drake",
    "netflix", "disney", "streaming", "movie", "sequel",
    "reality tv", "survivor", "game show",
]

REFRESH_INTERVAL = 60   # secondes


class Article:
    def __init__(self, source: str, title: str, summary: str,
                 url: str, published: Optional[datetime] = None):
        self.source    = source
        self.title     = title
        self.summary   = summary
        self.url       = url
        self.published = published or datetime.now(timezone.utcnow())
        self.uid       = hashlib.md5(url.encode()).hexdigest()
        self.score     = self._score()
        self.full_text = ""
        self.processed = False  # Marqueur pour éviter les réanalyses

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
                for entry in feed.entries[:8]:  # Réduit : 20 → 8 par source (évite surcharge)
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
        """
        Retourne les nouveaux articles pertinents (non encore traités).
        Les marque comme processed pour éviter les réanalyses.
        """
        with self._lock:
            # Récupère les articles non-traités parmi _new_since
            unprocessed = []
            remaining = []
            for uid in self._new_since:
                if uid in self._articles:
                    art = self._articles[uid]
                    if not art.processed and len(unprocessed) < max_items:
                        unprocessed.append(art)
                    else:
                        remaining.append(uid)
                else:
                    remaining.append(uid)

            # Marquer les articles retournés comme processed
            for art in unprocessed:
                art.processed = True

            # Garder les uids non-traités pour le prochain appel
            self._new_since = remaining
            return unprocessed

    def latest(self, n: int = 20, min_score: int = 1) -> list:
        with self._lock:
            arts = [a for a in self._articles.values() if a.score >= min_score]
        return sorted(arts, key=lambda a: a.published, reverse=True)[:n]

    @property
    def stats(self) -> dict:
        return {**self._stats, "buffer_size": len(self._articles),
                "pending_analysis": len(self._new_since)}
