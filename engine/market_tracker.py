"""
MarketTracker — CSV persistant pour garantir qu'aucun marché n'est analysé deux fois.

Fonctionnement :
  • Chaque marché analysé est écrit dans un CSV avec timestamp + résultat
  • Au démarrage du worker, les entrées non-expirées sont rechargées en mémoire
  • TTL configurable (défaut : 12h) — après expiration le marché peut être ré-analysé
    (les marchés évoluent, les prix bougent)
  • Thread-safe

Colonnes CSV :
  market_key | conditionId | question | analyzed_at | signal | edge | confidence | side | source
"""
import csv
import os
import threading
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Répertoire de données — /tmp/novamarket sur Railway (éphémère mais survit aux
# redémarrages du worker dans la même session container)
_DEFAULT_DATA_DIR = os.environ.get("NOVAMARKET_DATA_DIR", "/tmp/novamarket")

CSV_COLUMNS = [
    "market_key",    # clé de dédup (conditionId ou "q:question")
    "conditionId",   # raw conditionId (peut être vide)
    "question",      # texte complet de la question
    "analyzed_at",   # ISO 8601 UTC
    "signal",        # "YES" | "NO" | "NONE"  (signal généré ?)
    "edge",          # float 0-1 (0 si pas de signal)
    "confidence",    # int 0-100
    "side",          # "YES" | "NO" | "" (côté du trade)
    "source",        # source de l'article corrélé
]

# TTL par défaut : 12h. Après ça, le marché peut être ré-analysé (prix ont bougé)
DEFAULT_TTL_HOURS = 12


class MarketTracker:
    """
    Tracker CSV persistant des marchés analysés.
    Thread-safe.
    """

    def __init__(self, user_id: int, data_dir: str = _DEFAULT_DATA_DIR,
                 ttl_hours: int = DEFAULT_TTL_HOURS):
        self.user_id   = user_id
        self.ttl_hours = ttl_hours
        self._lock     = threading.Lock()

        # Chemin CSV : un fichier par user_id
        data_path = Path(data_dir)
        data_path.mkdir(parents=True, exist_ok=True)
        self.csv_path = data_path / f"markets_u{user_id}.csv"

        # Set en mémoire pour les lookups O(1)
        self._analyzed: set = set()   # market_keys non-expirés
        self._rows: list    = []      # toutes les lignes (pour récrire)

        self._load()

    # ── Chargement ────────────────────────────────────────────────────────────

    def _load(self):
        """Charge le CSV et filtre les entrées expirées."""
        if not self.csv_path.exists():
            logger.info(f"[Tracker] Nouveau CSV : {self.csv_path}")
            return

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.ttl_hours)
        valid_rows = []
        loaded = 0
        expired = 0

        try:
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        analyzed_at = datetime.fromisoformat(row["analyzed_at"])
                        if analyzed_at.tzinfo is None:
                            analyzed_at = analyzed_at.replace(tzinfo=timezone.utc)
                        if analyzed_at >= cutoff:
                            valid_rows.append(row)
                            self._analyzed.add(row["market_key"])
                            loaded += 1
                        else:
                            expired += 1
                    except Exception:
                        pass  # ligne corrompue, on ignore
        except Exception as e:
            logger.warning(f"[Tracker] Erreur lecture CSV: {e}")
            return

        self._rows = valid_rows
        logger.info(
            f"[Tracker] Chargé {loaded} marchés (TTL {self.ttl_hours}h) "
            f"| {expired} expirés purgés | {self.csv_path.name}"
        )

        # Récrire le CSV sans les entrées expirées
        if expired > 0:
            self._write_all()

    # ── API publique ──────────────────────────────────────────────────────────

    def is_analyzed(self, market_key: str) -> bool:
        """Retourne True si ce marché a déjà été analysé et n'est pas expiré."""
        with self._lock:
            return market_key in self._analyzed

    def mark(self, market_key: str, conditionId: str, question: str,
             signal: bool = False, edge: float = 0.0, confidence: int = 0,
             side: str = "", source: str = ""):
        """
        Enregistre un marché comme analysé.
        Appelé après chaque estimate_probability(), signal ou non.
        """
        row = {
            "market_key":  market_key,
            "conditionId": conditionId or "",
            "question":    question[:200],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "signal":      "YES" if signal else "NO",
            "edge":        f"{edge:.4f}",
            "confidence":  str(confidence),
            "side":        side,
            "source":      source,
        }
        with self._lock:
            self._analyzed.add(market_key)
            self._rows.append(row)
            self._append_row(row)

    def load_into_set(self, target_set: set):
        """
        Injecte toutes les clés non-expirées dans un set existant.
        Utilisé au démarrage du worker pour alimenter _analyzed_this_session.
        """
        with self._lock:
            target_set.update(self._analyzed)
        logger.info(
            f"[Tracker] {len(self._analyzed)} marchés pré-bloqués "
            f"depuis CSV → _analyzed_this_session"
        )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._analyzed)

    def stats(self) -> dict:
        """Résumé pour l'API /debug/status."""
        with self._lock:
            total = len(self._rows)
            with_signal = sum(1 for r in self._rows if r.get("signal") == "YES")
        return {
            "csv_path":    str(self.csv_path),
            "total":       total,
            "with_signal": with_signal,
            "ttl_hours":   self.ttl_hours,
        }

    def export_csv_path(self) -> str:
        """Retourne le chemin absolu du CSV (pour téléchargement depuis Flask)."""
        return str(self.csv_path)

    # ── I/O ───────────────────────────────────────────────────────────────────

    def _append_row(self, row: dict):
        """Ajoute une ligne au CSV (append). Crée le header si nouveau fichier."""
        try:
            write_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as e:
            logger.warning(f"[Tracker] Erreur écriture CSV: {e}")

    def _write_all(self):
        """Réécrit tout le CSV (utilisé après purge des entrées expirées)."""
        try:
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(self._rows)
        except Exception as e:
            logger.warning(f"[Tracker] Erreur réécriture CSV: {e}")
