"""
AI Analyst — Pipeline hybride Ollama Cloud + Perplexity pour Polymarket.

Architecture :
  • Ollama Cloud  → classify + match marchés (rapide, via OLLAMA_URL cloud)
  • Perplexity    → estimate_probability (sonar-online = accès web temps réel !)
  • Fallback      → si Ollama Cloud down, Perplexity prend tout
                    si Perplexity down, Ollama prend tout
  • Si AUCUN disponible → AIUnavailableError → bot refuse de démarrer

Variables Railway requises :
  OLLAMA_URL        → URL de ton instance Ollama cloud (ex: https://xxx.railway.app)
  OLLAMA_API_KEY    → clé API Ollama cloud (optionnel selon config)
  OLLAMA_FAST       → modèle rapide  (défaut: llama3.1:8b)
  OLLAMA_SMART      → modèle smart   (défaut: llama3.1:8b)
  PERPLEXITY_API_KEY → clé Perplexity (hardcodée en fallback)
"""
import os
import json
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config Ollama Cloud ───────────────────────────────────────────────────────
# URL testée : https://api.ollama.com
# Fast recommandé  : rnj-1:8b      (0.7s, JSON propre)
# Smart recommandé : qwen3-next:80b (10s, JSON parfait + raisonnement)
OLLAMA_URL     = os.environ.get("OLLAMA_URL",     "https://api.ollama.com")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_FAST    = os.environ.get("OLLAMA_FAST",    "rnj-1:8b")
OLLAMA_SMART   = os.environ.get("OLLAMA_SMART",   "qwen3-next:80b")
OLLAMA_TIMEOUT = 20

# ── Config Perplexity ─────────────────────────────────────────────────────────
PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL  = "https://api.perplexity.ai/chat/completions"
PPLX_FAST_MODEL = "sonar"       # classify + match marchés (rapide, économique)
PPLX_SMART_MODEL= "sonar-pro"   # estimate_probability (web search temps réel ✅)
PPLX_TIMEOUT    = 30


# ── Runtime config (surchargé par worker depuis DB) ───────────────────────────
_runtime: dict = {}

def set_runtime_config(url: str, api_key: str, model_fast: str, model_smart: str):
    """Appelé par le worker au démarrage pour injecter la config DB."""
    global _runtime
    _runtime = {
        "url":         url or OLLAMA_URL,
        "api_key":     api_key or OLLAMA_API_KEY,
        "model_fast":  model_fast or OLLAMA_FAST or "llama3.1:8b",
        "model_smart": model_smart or OLLAMA_SMART or "llama3.1:8b",
    }
    logger.info(
        f"[AI] Config Ollama Cloud → {_runtime['url']} | "
        f"fast={_runtime['model_fast']} smart={_runtime['model_smart']}"
    )

def _get_ollama_url()     -> str: return _runtime.get("url",         OLLAMA_URL)
def _get_ollama_key()     -> str: return _runtime.get("api_key",     OLLAMA_API_KEY)
def _get_model_fast()     -> str: return _runtime.get("model_fast",  OLLAMA_FAST or "llama3.1:8b")
def _get_model_smart()    -> str: return _runtime.get("model_smart", OLLAMA_SMART or "llama3.1:8b")


# ── Helpers pour lister les modèles cloud ─────────────────────────────────────

MODEL_RECOMMENDATIONS = {
    # Modèles dont on connaît le rôle optimal (testés sur api.ollama.com)
    "fast":  ["8b", "3b", "small", "mini", "tiny", "fast", "nano",
              "rnj-1", "ministral-3:8b", "ministral-3:3b", "gemma3:4b"],
    "smart": ["70b", "80b", "120b", "large", "pro", "405b", "ultra",
              "qwen3-next", "cogito", "devstral-2", "gpt-oss:120b",
              "minimax", "nemotron-3-super"],
}

def _score_model(name: str) -> dict:
    """Retourne un dict de métadonnées pour affichage dans l'UI."""
    n = name.lower()
    is_cloud = "cloud" in n

    # Recommandation
    rec_fast  = any(k in n for k in MODEL_RECOMMENDATIONS["fast"])
    rec_smart = any(k in n for k in MODEL_RECOMMENDATIONS["smart"])

    if rec_smart:
        badge = "smart"
        hint  = "Idéal pour analyse approfondie (estimation probabilité)"
    elif rec_fast:
        badge = "fast"
        hint  = "Idéal pour classification rapide et matching de marchés"
    else:
        badge = "general"
        hint  = "Usage général"

    return {
        "name":     name,
        "is_cloud": is_cloud,
        "badge":    badge,
        "hint":     hint,
    }


def fetch_ollama_models(url: str, api_key: str) -> dict:
    """
    Récupère la liste des modèles disponibles sur l'instance Ollama Cloud.
    Retourne {"ok": bool, "models": list, "error": str}
    """
    if not url:
        return {"ok": False, "models": [], "error": "OLLAMA_URL non renseigné"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(
            f"{url.rstrip('/')}/api/tags",
            headers=headers,
            timeout=8,
        )
        if resp.status_code != 200:
            return {"ok": False, "models": [],
                    "error": f"HTTP {resp.status_code} — {resp.text[:200]}"}
        data   = resp.json()
        raw    = data.get("models", [])
        models = [_score_model(m["name"] if isinstance(m, dict) else m) for m in raw]
        # Cloud models en premier, puis par badge smart > fast > general
        order = {"smart": 0, "fast": 1, "general": 2}
        models.sort(key=lambda m: (not m["is_cloud"], order.get(m["badge"], 3)))
        return {"ok": True, "models": models, "error": ""}
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}


class AIUnavailableError(Exception):
    """Levée quand aucun backend IA n'est joignable."""
    pass


# ── Backends ──────────────────────────────────────────────────────────────────

def _call_ollama(model: str, prompt: str, max_tokens: int = 200) -> Optional[str]:
    """Appel Ollama Cloud /api/generate — retourne None si indisponible."""
    url = _get_ollama_url()
    key = _get_ollama_key()
    if not url:
        return None  # URL cloud non configurée
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/api/generate",
            headers=headers,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.05, "num_predict": max_tokens},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("response", "")
        logger.debug(f"[AI/Ollama Cloud] HTTP {resp.status_code}")
        return None
    except Exception as e:
        logger.debug(f"[AI/Ollama Cloud] indisponible: {e}")
        return None


def _call_perplexity(model: str, prompt: str, max_tokens: int = 300,
                     system: str = "") -> Optional[str]:
    """Appel Perplexity API (OpenAI-compatible) — retourne None si erreur."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = requests.post(
            PERPLEXITY_URL,
            headers={
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": max_tokens,
            },
            timeout=PPLX_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        logger.warning(f"[AI/Perplexity] HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        logger.warning(f"[AI/Perplexity] erreur: {e}")
        return None


def _call_fast(prompt: str, max_tokens: int = 150) -> str:
    """
    Appel rapide (classify, match marchés) :
      1. Ollama Cloud  → modèle fast sélectionné par l'utilisateur
      2. Perplexity    → fallback si Ollama cloud down
    Lève AIUnavailableError si les deux échouent.
    """
    raw = _call_ollama(_get_model_fast(), prompt, max_tokens)
    if raw is not None:
        return raw
    logger.info("[AI] Ollama Cloud indisponible — fallback Perplexity fast")
    raw = _call_perplexity(PPLX_FAST_MODEL, prompt, max_tokens)
    if raw is not None:
        return raw
    raise AIUnavailableError("Ollama Cloud et Perplexity indisponibles")


def _call_smart(prompt: str, system: str = "", max_tokens: int = 400) -> str:
    """
    Appel analytique profond (estimation probabilité) :
      1. Perplexity sonar-large-online → priorité absolue (accès web temps réel)
      2. Ollama Cloud                  → fallback avec modèle smart sélectionné
    Lève AIUnavailableError si les deux échouent.
    """
    raw = _call_perplexity(PPLX_SMART_MODEL, prompt, max_tokens, system)
    if raw is not None:
        return raw
    logger.info("[AI] Perplexity indisponible — fallback Ollama Cloud smart")
    raw = _call_ollama(_get_model_smart(), prompt, max_tokens)
    if raw is not None:
        return raw
    raise AIUnavailableError("Perplexity et Ollama Cloud indisponibles")


def _parse_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    s = raw.find("{")
    e = raw.rfind("}") + 1
    if s == -1 or e == 0:
        return None
    try:
        return json.loads(raw[s:e])
    except Exception:
        return None


# ── Health check ──────────────────────────────────────────────────────────────

def check_ai_available() -> dict:
    """
    Vérifie la disponibilité des deux backends cloud.
    Retourne {"ok": bool, "ollama": bool, "perplexity": bool, "error": str}
    """
    ollama_ok = False
    pplx_ok   = False

    # Test Ollama Cloud (ping /api/tags)
    url = _get_ollama_url()
    key = _get_ollama_key()
    if url:
        try:
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = requests.get(f"{url.rstrip('/')}/api/tags", headers=headers, timeout=6)
            ollama_ok = r.status_code == 200
        except Exception as e:
            logger.debug(f"[AI] Ollama Cloud ping failed: {e}")
    else:
        logger.warning("[AI] OLLAMA_URL non configuré — Ollama Cloud désactivé")

    # Test Perplexity (mini appel réel)
    try:
        test = _call_perplexity(PPLX_FAST_MODEL, "Reply with: ok", max_tokens=5)
        pplx_ok = test is not None and len(test.strip()) > 0
    except Exception as e:
        logger.debug(f"[AI] Perplexity ping failed: {e}")

    ok    = ollama_ok or pplx_ok
    error = "" if ok else "Aucun backend IA cloud disponible (Ollama Cloud + Perplexity)"

    logger.info(f"[AI] Health check — OllamaCloud:{ollama_ok} Perplexity:{pplx_ok}")
    return {"ok": ok, "ollama": ollama_ok, "perplexity": pplx_ok, "error": error}


# ── Pipeline ──────────────────────────────────────────────────────────────────

def classify_article(title: str, summary: str) -> dict:
    """
    Classifie un article pour Polymarket.
    Retourne {"category": str, "keywords": list, "relevance": 0-10}
    Lève AIUnavailableError si IA indisponible.
    """
    prompt = (
        "Classifie cet article pour les marchés de prédiction Polymarket.\n"
        f"Titre: {title[:200]}\nRésumé: {summary[:300]}\n\n"
        "Retourne UNIQUEMENT ce JSON (rien d'autre):\n"
        '{"category":"politics","keywords":["mot1","mot2"],"relevance":7}\n'
        "category: politics | elections | crypto | economics | business | "
        "sports | nfl | nba | soccer | mma | world | science | tech | health | "
        "pop culture | entertainment | trump\n"
        "relevance: 0=hors-sujet, 10=directement pertinent pour un marché de prédiction\n"
        "IMPORTANT: marchés originaux (célébrités, box-office, reality TV, people) = pop culture"
    )
    raw    = _call_fast(prompt, max_tokens=120)
    result = _parse_json(raw)
    if not result:
        return {"category": "general", "keywords": [], "relevance": 3}
    return result


def find_relevant_markets(article_title: str, article_summary: str,
                           markets: list, top_n: int = 5) -> list:
    """
    Identifie les marchés les plus impactés par un article.
    Retourne une liste de marchés.
    Lève AIUnavailableError si IA indisponible.
    """
    if not markets:
        return []

    market_list = ""
    for i, m in enumerate(markets[:30]):
        q = m.get("question", m.get("title", ""))[:120]
        p = m.get("_yes_price", 0.5)
        market_list += f"{i}: [{p:.0%} YES] {q}\n"

    prompt = (
        f"Article: {article_title[:200]}\n"
        f"Résumé: {article_summary[:300]}\n\n"
        f"Marchés:\n{market_list}\n"
        f"Donne les indices (max {top_n}) des marchés directement impactés.\n"
        f"Réponds UNIQUEMENT avec ce JSON (indices entre crochets): "
        f'{{\"relevant\": [0, 1]}}'
    )
    raw    = _call_fast(prompt, max_tokens=50)
    result = _parse_json(raw)
    if not result:
        return []
    indices = result.get("relevant", [])
    # Sécurité : filtrer les indices non-entiers ou hors bornes
    return [markets[i] for i in indices if isinstance(i, int) and 0 <= i < len(markets)]


def estimate_probability(article_title: str, article_summary: str,
                          market_question: str, current_prob: float) -> dict:
    """
    Estime la vraie probabilité d'un marché via Perplexity (accès web temps réel).
    Retourne {"estimated_prob", "confidence", "reasoning", "direction"}
    Lève AIUnavailableError si IA indisponible.
    """
    system = (
        "Tu es un analyste expert en marchés de prédiction Polymarket. "
        "Tu as accès à Internet — utilise-le pour vérifier les faits actuels. "
        "Réponds UNIQUEMENT en JSON valide. Pas d'explication, juste le JSON."
    )
    prompt = (
        f"MARCHÉ POLYMARKET: {market_question[:250]}\n"
        f"Probabilité actuelle du marché: {current_prob:.1%}\n\n"
        f"ACTUALITÉ:\nTitre: {article_title[:200]}\n"
        f"Résumé: {article_summary[:400]}\n\n"
        "Recherche les dernières infos et analyse l'impact sur ce marché.\n\n"
        "Réponds avec EXACTEMENT ce JSON (rien d'autre):\n"
        "{\n"
        "  \"estimated_prob\": 0.72,\n"
        "  \"confidence\": 75,\n"
        "  \"reasoning\": \"Pourquoi cette probabilité en 1-2 phrases\",\n"
        "  \"direction\": \"UP\",\n"
        "  \"exit_trigger\": \"Quel événement/date résoudra ce marché\",\n"
        "  \"thesis\": \"La thèse d'investissement en 1 phrase courte\"\n"
        "}\n\n"
        "estimated_prob: 0.01-0.99 | confidence: 0-100 | direction: UP/DOWN/NEUTRAL\n"
        "exit_trigger: l'événement précis qui résoudra le marché (ex: 'Résultats élection nov 2025')\n"
        "thesis: la raison principale du trade (ex: 'Momentum post-halving Bitcoin')\n"
        "Si edge < 8% vs marché, mets confidence < 40."
    )
    raw    = _call_smart(prompt, system=system, max_tokens=350)
    result = _parse_json(raw)
    if not result:
        # Si parse échoue mais raw non-null → essaye d'extraire manuellement
        logger.debug(f"[AI] parse_json échoué sur: {raw[:200] if raw else 'None'}")
        raise AIUnavailableError("Réponse IA non parseable")

    ep = float(result.get("estimated_prob", current_prob))
    ep = max(0.01, min(0.99, ep))
    cf = int(result.get("confidence", 0))
    cf = max(0, min(100, cf))

    logger.info(
        f"[AI/Perplexity] {market_question[:60]}… "
        f"mkt={current_prob:.0%} → est={ep:.0%} conf={cf}% dir={result.get('direction','?')}"
    )

    return {
        "estimated_prob": ep,
        "confidence":     cf,
        "reasoning":      str(result.get("reasoning", ""))[:300],
        "direction":      result.get("direction", "NEUTRAL"),
    }


def batch_analyze(articles: list, markets: list) -> list:
    """
    Analyse batch : pour chaque article → marchés impactés → signaux.
    Retourne liste triée de signal dicts.
    Lève AIUnavailableError si IA indisponible dès le premier appel.
    """
    candidates = []

    for art in articles:
        # 1. Classification
        cls = classify_article(art.title, art.summary)
        if cls.get("relevance", 0) < 3:
            continue

        # 2. Marchés pertinents
        relevant = find_relevant_markets(art.title, art.summary, markets, top_n=4)
        if not relevant:
            continue

        # 3. Estimation probabilité via Perplexity (web search)
        for market in relevant:
            current_prob = market.get("_yes_price", 0.5)
            question     = market.get("question", market.get("title", ""))

            try:
                analysis = estimate_probability(
                    art.title, art.summary, question, current_prob
                )
            except AIUnavailableError:
                raise  # propage — le worker gère
            except Exception as e:
                logger.warning(f"[AI] estimate_probability erreur: {e}")
                continue

            estimated = analysis["estimated_prob"]
            edge      = abs(estimated - current_prob)
            conf      = analysis["confidence"]

            if edge < 0.10 or conf < 40:
                continue

            side       = "YES" if estimated > current_prob else "NO"
            side_price = current_prob if side == "YES" else (1 - current_prob)

            candidates.append({
                "market":         market,
                "article_title":  art.title,
                "article_source": art.source,
                "current_prob":   current_prob,
                "estimated_prob": estimated,
                "edge":           round(edge, 4),
                "confidence":     conf,
                "direction":      analysis["direction"],
                "reasoning":      analysis.get("reasoning", ""),
                "exit_trigger":   analysis.get("exit_trigger", ""),
                "thesis":         analysis.get("thesis", ""),
                "side":           side,
                "entry_price":    side_price,
            })

            logger.info(
                f"[AI] ✅ Signal {side} | {question[:55]}… "
                f"mkt={current_prob:.0%} → IA={estimated:.0%} "
                f"edge={edge:.0%} conf={conf}%"
            )

    # Tri par edge × confidence (meilleurs signaux en premier)
    return sorted(candidates, key=lambda x: x["edge"] * x["confidence"], reverse=True)
