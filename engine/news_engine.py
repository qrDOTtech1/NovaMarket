"""
News Engine — agrégation RSS multi-sources, dédoublonnage, scoring de pertinence.
Rafraîchissement toutes les 60 secondes. Chaque article est traité une seule fois.
"""
import hashlib
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        self.published = published or datetime.now(timezone.utc)
        self.uid       = hashlib.md5(url.encode()).hexdigest()
        self.score     = self._score()
        self.full_text = ""
        self.processed = False  # Marqueur pour éviter les réanalyses

    def _score(self) -> int:
        text = (self.title + " " + self.summary).lower()
        keyword_score = sum(1 for kw in POLYMARKET_KEYWORDS if kw in text)
        # Recency bonus: articles < 1h get +3, < 3h get +2, < 6h get +1
        if self.published:
            age_hours = (datetime.now(timezone.utc) - self.published).total_seconds() / 3600
            if age_hours < 1:
                keyword_score += 3
            elif age_hours < 3:
                keyword_score += 2
            elif age_hours < 6:
                keyword_score += 1
        return keyword_score

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
    Aggrège toutes les sources RSS, dédoublonne par URL hash et titre,
    maintient un buffer des N derniers articles.
    Thread-safe.
    """
    MAX_BUFFER  = 500   # articles en mémoire
    MIN_SCORE   = 1     # score minimum pour être analysé par l'IA

    def __init__(self):
        self._articles: dict[str, Article] = {}   # uid -> Article
        self._titles_seen: set[str] = set()       # pour déduplier par titre
        self._lock = threading.Lock()
        self._new_since: list[str] = []           # uids non encore traités
        self._last_refresh = 0.0
        self._stats = {"total_fetched": 0, "total_sources": 0}

    def _fetch_single_feed(self, name: str, url: str) -> list:
        """Fetch a single RSS feed. Returns list of (name, entry) tuples."""
        results = []
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                results.append((name, entry))
        except Exception as e:
            logger.debug(f"[News] {name} erreur: {e}")
        return results

    def refresh(self) -> int:
        """Rafraîchit toutes les sources en parallèle. Retourne le nb de nouveaux articles."""
        import re
        import calendar

        all_entries = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(self._fetch_single_feed, name, url): name
                for name, url in RSS_FEEDS.items()
            }
            for future in as_completed(futures, timeout=30):
                try:
                    all_entries.extend(future.result())
                except Exception as e:
                    logger.debug(f"[News] feed future error: {e}")

        new_count = 0
        for name, entry in all_entries:
            title   = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            link    = getattr(entry, "link", "")
            if not title or not link:
                continue
            try:
                summary = BeautifulSoup(summary, "lxml").get_text(separator=" ")[:500]
            except Exception:
                summary = summary[:500]
            pub = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    ts  = calendar.timegm(entry.published_parsed)
                    pub = datetime.fromtimestamp(ts, tz=timezone.utc)
                except Exception:
                    pass
            art = Article(name, title, summary, link, pub)
            with self._lock:
                title_norm = re.sub(r'\s+', ' ', title.lower().strip())
                title_keywords = ' '.join(re.findall(r'\w+', title_norm))

                is_duplicate = (art.uid in self._articles or
                               title_norm in self._titles_seen or
                               title_keywords in self._titles_seen)

                if not is_duplicate:
                    self._articles[art.uid] = art
                    self._titles_seen.add(title_norm)
                    self._titles_seen.add(title_keywords)
                    if art.score >= self.MIN_SCORE:
                        self._new_since.append(art.uid)
                    new_count += 1

        # Purge buffer + fix _titles_seen leak
        with self._lock:
            if len(self._articles) > self.MAX_BUFFER:
                oldest = sorted(self._articles.values(), key=lambda a: a.published)
                to_remove = oldest[:len(self._articles) - self.MAX_BUFFER]
                for old in to_remove:
                    self._articles.pop(old.uid, None)
                    norm = re.sub(r'\s+', ' ', old.title.lower().strip())
                    self._titles_seen.discard(norm)
                    self._titles_seen.discard(' '.join(re.findall(r'\w+', norm)))
            # Cap _titles_seen to 2x buffer to prevent unbounded growth
            if len(self._titles_seen) > self.MAX_BUFFER * 3:
                self._titles_seen = {
                    re.sub(r'\s+', ' ', a.title.lower().strip())
                    for a in self._articles.values()
                } | {
                    ' '.join(re.findall(r'\w+', re.sub(r'\s+', ' ', a.title.lower().strip())))
                    for a in self._articles.values()
                }
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
                        art.processed = True  # Marquer IMMÉDIATEMENT
                    else:
                        # Garder les articles processed ou excédentaires
                        if not art.processed:
                            remaining.append(uid)
                else:
                    # UID orphelin, enlever de la liste
                    pass

            logger.debug(
                f"[News] pop_new: {len(unprocessed)} articles retournés, "
                f"{len(remaining)} en attente, {len(self._articles)} total"
            )

            # Garder seulement les articles non-traités pour le prochain appel
            self._new_since = remaining
            return unprocessed

    def find_articles_for_market(self, market_question: str, top_n: int = 3) -> list:
        """
        Cherche les articles RSS les plus corrélés à une question de marché.
        Matching par mots-clés communs entre la question et titre/résumé.
        Retourne les top_n articles les plus pertinents.
        """
        import re
        # Tokens significatifs de la question (>3 lettres, sans mots vides)
        STOP = {"will","the","and","for","that","this","with","from","are",
                "have","has","was","were","been","into","than","then","its",
                "can","not","but","all","over","when","what","who","how","why",
                "des","les","une","est","sur","par","dans","qui","que","pour",
                "pas","plus","avec","même","tout","fait","mais","si","ou","où"}
        q_tokens = {w for w in re.findall(r'[a-z]{4,}', market_question.lower())
                    if w not in STOP}
        if not q_tokens:
            return []

        scored = []
        with self._lock:
            for art in self._articles.values():
                text = (art.title + " " + art.summary).lower()
                overlap = sum(1 for t in q_tokens if t in text)
                if overlap > 0:
                    # Bonus si le titre seul contient des tokens (plus précis)
                    title_bonus = sum(1 for t in q_tokens if t in art.title.lower())
                    score = overlap + title_bonus * 0.5
                    scored.append((score, art))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [art for _, art in scored[:top_n]]

    def latest(self, n: int = 20, min_score: int = 1) -> list:
        with self._lock:
            arts = [a for a in self._articles.values() if a.score >= min_score]
        return sorted(arts, key=lambda a: a.published, reverse=True)[:n]

    @property
    def stats(self) -> dict:
        return {**self._stats, "buffer_size": len(self._articles),
                "pending_analysis": len(self._new_since)}
