"""Assistant N1 : propose une réponse à chaque ticket à partir de la base de connaissances.

1. Recherche : l'article le plus proche du ticket, avec un bonus pour les articles
   de la catégorie du ticket. Trois modes (--search) :
   - embed : recherche par le sens, via les embeddings d'Ollama (nomic-embed-text) ;
   - tfidf : recherche par les mots (TF-IDF en Python pur, sans dépendance) ;
   - hybrid (par défaut) : les deux méthodes votent. Si elles désignent le même
     article, la réponse peut partir seule ; sinon, l'article du TF-IDF (le plus
     précis sur nos tests) est proposé à un agent. Si Ollama ne répond pas : TF-IDF seul.
2. Rédaction : l'IA locale (Ollama) rédige une réponse personnalisée à partir des
   étapes de l'article, sans rien inventer. Sans Ollama, l'article est cité tel quel.
3. Décision :
   - article en libre-service ET score >= seuil : réponse publique envoyée au client
     (étiquette ia-reponse-auto). C'est ce qui alimente le taux de résolution sans intervention ;
   - sinon (droits à accorder, matériel à livrer, panne à traiter, score trop faible) :
     suggestion en note interne pour l'agent (étiquette ia-suggestion).

Usage :
    python suggest_replies.py --eval      # vérifie l'article trouvé pour data/tickets.csv
    python suggest_replies.py --compare   # compare embed et tfidf, tickets d'origine et reformulés
    python suggest_replies.py --dry-run   # affiche les décisions sur les vrais tickets
    python suggest_replies.py             # répond dans Jira (tickets déjà triés seulement)
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv

from setup_queues import CATEGORIES
from triage import normalize, text_of

load_dotenv()

ROOT = Path(__file__).parent
KB_DIR = ROOT / "kb"
CSV_PATH = ROOT / "data" / "tickets.csv"
REPHRASED_CSV_PATH = ROOT / "data" / "tickets_reformules.csv"
EMBED_CACHE = KB_DIR / ".embeddings.json"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
SEARCH_MODE = os.getenv("SEARCH_MODE", "hybrid")
# Les deux méthodes ne donnent pas des scores sur la même échelle : un seuil chacune.
# Calibré sur les tickets d'origine et reformulés : bons articles entre 0,72 et 0,92.
THRESHOLDS = {
    "tfidf": float(os.getenv("AUTO_REPLY_THRESHOLD", "0.40")),
    "embed": float(os.getenv("EMBED_AUTO_THRESHOLD", "0.70")),
    "consensus": float(os.getenv("EMBED_AUTO_THRESHOLD", "0.70")),
    "desaccord": float("inf"),  # les deux méthodes divergent : toujours un agent
}
CATEGORY_BONUS = {"tfidf": 0.10, "embed": 0.05}
DONE_LABEL = "ia-repondu"

STOP_WORDS = (
    "a au aux avec ce ces dans de des du elle en et il je j la le les leur lui ma mais me mes "
    "mon ne nous on ou par pas pour qu que qui sa se ses son sur ta te tes toi ton tu un une "
    "vos votre vous c d l m n s t y est suis ai a ont sont plus tout tres bien depuis alors fait"
).split()

REPLY_PROMPT = """Tu es technicien de support informatique N1. Rédige une réponse courte au client,
en français, en le vouvoyant, à partir UNIQUEMENT des étapes de l'article fourni.
N'invente aucune étape, aucun lien, aucun délai. Commence par « Bonjour {name}, »,
reprends les étapes utiles sous forme de liste numérotée, et termine par :
« Si le problème persiste, répondez à ce message et un technicien prendra le relais. »"""


def load_kb():
    articles = []
    for path in sorted(KB_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta_block, body = raw.split("---", 2)[1:]
        meta = dict(line.split(": ", 1) for line in meta_block.strip().splitlines())
        articles.append({**meta, "body": body.strip()})
    return articles


def tokens(text):
    """Mots et paires de mots consécutifs (bigrammes), sans les mots vides."""
    words = [w for w in re.findall(r"\w+", normalize(text)) if w not in STOP_WORDS and len(w) > 1]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


class TfidfIndex:
    """Index TF-IDF minimal (même principe que TfidfVectorizer de scikit-learn).

    Écrit en Python pur : scikit-learn embarque des DLL que le Contrôle intelligent
    des applications de Windows peut bloquer.
    """

    def __init__(self, articles):
        self.articles = articles
        # Titre et mots-clés répétés : ils pèsent plus que le corps de l'article.
        docs = [Counter(tokens(f"{a['titre']} {a['mots_cles']} " * 3 + a["body"])) for a in articles]
        n = len(docs)
        df = Counter(term for doc in docs for term in doc)
        self.idf = {term: math.log((1 + n) / (1 + count)) + 1 for term, count in df.items()}
        self.vectors = [self._vector(doc) for doc in docs]

    def _vector(self, counts):
        vec = {t: (1 + math.log(c)) * self.idf[t] for t, c in counts.items() if t in self.idf}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def similarities(self, text):
        query = self._vector(Counter(tokens(text)))
        return [sum(w * vec.get(t, 0.0) for t, w in query.items()) for vec in self.vectors]



def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0)


def embed(texts):
    r = requests.post(f"{OLLAMA_URL}/api/embed", json={"model": EMBED_MODEL, "input": texts}, timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]


class EmbeddingIndex:
    """Recherche par le sens : chaque article et chaque ticket devient un vecteur
    (embedding) calculé par Ollama, et on compare les vecteurs.

    Les vecteurs des articles sont mis en cache dans kb/.embeddings.json et ne sont
    recalculés que si un article change.
    """

    def __init__(self, articles):
        self.articles = articles
        docs = [f"search_document: {a['titre']}. {a['mots_cles']}. {a['body']}" for a in articles]
        key = hashlib.sha256((EMBED_MODEL + "".join(docs)).encode("utf-8")).hexdigest()
        try:
            cache = json.loads(EMBED_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        if cache.get("key") == key:
            self.vectors = cache["vectors"]
        else:
            self.vectors = embed(docs)
            EMBED_CACHE.write_text(json.dumps({"key": key, "vectors": self.vectors}), encoding="utf-8")

    def similarities(self, text):
        query = embed([f"search_query: {text}"])[0]
        return [cosine(query, vec) for vec in self.vectors]


class KnowledgeBase:
    def __init__(self, articles, mode=SEARCH_MODE):
        self.articles = articles
        self.tfidf = TfidfIndex(articles)
        self.embeddings = None
        if mode in ("embed", "hybrid"):
            try:
                self.embeddings = EmbeddingIndex(articles)
            except (requests.RequestException, KeyError) as e:
                if mode == "embed":
                    raise
                print(f"(Embeddings indisponibles, recherche TF-IDF : {e.__class__.__name__})")
        self.mode = mode if self.embeddings else "tfidf"

    def search(self, text, category_label=None, method=None):
        """Renvoie (article, score, méthode utilisée)."""
        method = method or self.mode
        if method == "hybrid":
            article_t, score_t, _ = self.search(text, category_label, "tfidf")
            article_e, score_e, used = self.search(text, category_label, "embed")
            if used != "embed":
                return article_t, score_t, "tfidf"
            if article_t["id"] == article_e["id"]:
                return article_e, score_e, "consensus"
            return article_t, score_t, "desaccord"
        if method == "embed":
            try:
                scores = self.embeddings.similarities(text)
            except (requests.RequestException, KeyError) as e:
                print(f"  (Embeddings indisponibles, repli TF-IDF : {e.__class__.__name__})")
                method, scores = "tfidf", self.tfidf.similarities(text)
        else:
            scores = self.tfidf.similarities(text)
        best, best_score = None, -1.0
        for article, score in zip(self.articles, scores):
            bonus = CATEGORY_BONUS[method] if category_label and article["categorie"] == category_label else 0.0
            if score + bonus > best_score:
                best, best_score = article, score + bonus
        return best, min(best_score, 1.0), method


def write_reply(ticket_text, article, name):
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": REPLY_PROMPT.format(name=name)},
                    {"role": "user", "content": f"Ticket :\n{ticket_text}\n\nArticle {article['id']} :\n{article['body']}"},
                ],
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=180,
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip(), f"IA ({OLLAMA_MODEL})"
    except (requests.RequestException, KeyError) as e:
        print(f"  (Ollama indisponible, article cité tel quel : {e.__class__.__name__})")
        steps = re.sub(r"\n{2,}", "\n", article["body"].split("\n", 1)[-1]).strip()
        return (
            f"Bonjour {name},\n\nVoici la procédure pour résoudre ce problème :\n{steps}\n\n"
            "Si le problème persiste, répondez à ce message et un technicien prendra le relais."
        ), "modèle de réponse"


def is_auto(article, score, method):
    return article.get("libre_service") == "oui" and score >= THRESHOLDS[method]


def evaluate(kb, path=CSV_PATH, method=None, verbose=True):
    label_by_name = {name: label for label, name in CATEGORIES.items()}
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    correct = auto = wrong_auto = 0
    for row in rows:
        article, score, used = kb.search(
            f"{row['summary']}\n{row['description']}", label_by_name[row["expected_category"]], method
        )
        ok = article["id"] == row["expected_kb"]
        correct += ok
        decision = "auto" if is_auto(article, score, used) else "agent"
        auto += ok and decision == "auto"
        wrong_auto += (not ok) and decision == "auto"
        if verbose:
            print(f"{'OK' if ok else 'KO'}  {article['id']} (attendu {row['expected_kb']})  {used} {score:.2f} -> {decision:<5}  {row['summary']}")
    if verbose:
        print(f"\nBon article : {correct}/{len(rows)} ({100 * correct / len(rows):.0f} %)")
        print(f"Réponse automatique correcte : {auto}/{len(rows)}, les autres passent par un agent.")
        print(f"Réponse automatique ERRONÉE (mauvais article envoyé au client) : {wrong_auto}/{len(rows)}")
    return correct, auto, wrong_auto, len(rows)


def compare(kb):
    if not kb.embeddings:
        raise SystemExit("Embeddings indisponibles : lance « ollama pull nomic-embed-text » puis réessaie.")
    print(f"{'Jeu de tickets':<22}{'Méthode':<9}{'Bon article':>13}{'Auto correcte':>15}{'Auto erronée':>14}")
    for label, path in (("Tickets d'origine", CSV_PATH), ("Tickets reformulés", REPHRASED_CSV_PATH)):
        for method in ("tfidf", "embed", "hybrid"):
            correct, auto, wrong, n = evaluate(kb, path, method, verbose=False)
            print(f"{label:<22}{method:<9}{f'{correct}/{n}':>13}{f'{auto}/{n}':>15}{f'{wrong}/{n}':>14}")


def answer_tickets(jira, kb, project, dry_run):
    all_labels = ", ".join(CATEGORIES)
    jql = (
        f"project = {project} AND resolution = Unresolved AND labels in ({all_labels}) "
        f"AND labels != {DONE_LABEL} ORDER BY created ASC"
    )
    issues = jira.search(jql, fields=("summary", "description", "labels", "reporter"))
    if not issues:
        print("Aucun ticket trié en attente de réponse.")
    for issue in issues:
        labels = issue["fields"].get("labels", [])
        category = next((l for l in labels if l in CATEGORIES), None)
        text = text_of(issue)
        article, score, method = kb.search(text, category)
        auto = is_auto(article, score, method)
        print(f"{issue['key']:<6} -> {article['id']} {method} {score:.2f} {'réponse auto' if auto else 'suggestion agent'}  {issue['fields']['summary']}")
        if dry_run:
            continue
        name = (issue["fields"].get("reporter") or {}).get("displayName", "")
        reply, engine = write_reply(text, article, name)
        # Étiquettes d'abord : un échec en cours de route ne provoque jamais de double réponse.
        jira.add_labels(issue["key"], DONE_LABEL, "ia-reponse-auto" if auto else "ia-suggestion")
        footer = f"\n\n[Source : {article['id']} « {article['titre']} »]"
        if auto:
            jira.public_comment(issue["key"], reply + footer)
            jira.internal_note(issue["key"], f"Réponse automatique envoyée ({article['id']}, {method} {score:.2f}, {engine}).")
        else:
            jira.internal_note(
                issue["key"],
                f"Suggestion de réponse ({article['id']}, {method} {score:.2f}, {engine}), à valider par un agent :\n\n{reply}{footer}",
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval", action="store_true", help="vérifie la recherche sur le CSV, sans Jira")
    parser.add_argument("--compare", action="store_true", help="compare embed et tfidf, sans Jira")
    parser.add_argument("--reformules", action="store_true", help="avec --eval : tickets reformulés")
    parser.add_argument("--search", choices=("hybrid", "embed", "tfidf"), default=SEARCH_MODE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    kb = KnowledgeBase(load_kb(), "hybrid" if args.compare else args.search)
    if args.compare:
        compare(kb)
        return
    if args.eval:
        evaluate(kb, REPHRASED_CSV_PATH if args.reformules else CSV_PATH)
        return

    from jira_client import Jira

    answer_tickets(Jira.from_env(), kb, os.environ["JIRA_PROJECT"], args.dry_run)


if __name__ == "__main__":
    main()
