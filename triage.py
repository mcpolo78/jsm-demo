"""Tri automatique des tickets par IA : catégorie, puis routage vers la bonne file.

Pour chaque ticket de la file « À trier », un modèle local (Ollama) choisit la
catégorie. Le script pose l'étiquette correspondante, ce qui envoie le ticket dans
la bonne file d'attente, et ajoute une note interne avec la justification.
Si Ollama ne répond pas, un classifieur par mots-clés prend le relais.

Usage :
    python triage.py --eval        # précision sur data/tickets.csv, sans toucher à Jira
    python triage.py --dry-run     # affiche les décisions sur les vrais tickets
    python triage.py               # trie les tickets dans Jira
    python triage.py --loop 60     # trie en continu, toutes les 60 secondes

Variables optionnelles : OLLAMA_URL (http://localhost:11434), OLLAMA_MODEL (qwen2.5:7b).
"""

import argparse
import csv
import json
import os
import time
import unicodedata
from pathlib import Path

import requests
from dotenv import load_dotenv

from setup_queues import CATEGORIES

load_dotenv()  # avant de lire OLLAMA_MODEL, pour que le .env soit pris en compte

CSV_PATH = Path(__file__).parent / "data" / "tickets.csv"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
TRIAGED_LABEL = "ia-triee"

LABEL_BY_NAME = {name: label for label, name in CATEGORIES.items()}

SYSTEM_PROMPT = """Tu es un technicien de support informatique N1 qui trie les tickets.
Classe le ticket dans UNE seule de ces catégories :
- Accès et comptes : mot de passe, compte verrouillé, création de compte, droits sur un dossier.
- Messagerie : Outlook, boîte mail, signature, calendrier.
- Réseau : Wi-Fi, VPN, lenteur internet, connexion.
- Impression : imprimante, scanner, bourrage.
- Matériel : PC, écran, clavier, casque, station d'accueil, casse.
- Logiciels : installation ou plantage d'une application, licence.
Réponds en JSON avec "categorie", "confiance" (nombre entre 0 et 1) et "justification" (une phrase)."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "categorie": {"type": "string", "enum": list(LABEL_BY_NAME)},
        "confiance": {"type": "number"},
        "justification": {"type": "string"},
    },
    "required": ["categorie", "confiance", "justification"],
}

# Repli sans IA : premier mot-clé trouvé (texte sans accents, en minuscules).
KEYWORDS = {
    "Impression": ["imprim", "scanner", "bourrage", "toner"],
    "Réseau": ["wi-fi", "wifi", "vpn", "internet", "reseau", "connexion"],
    "Messagerie": ["outlook", "mail", "boite aux lettres", "signature", "calendrier"],
    "Accès et comptes": ["mot de passe", "verrouill", "compte", "acces", "droits", "session"],
    "Logiciels": ["install", "licence", "excel", "teams", "word", "logiciel", "adobe", "power bi", "plante"],
    "Matériel": ["ecran", "portable", "laptop", "pc", "casque", "clavier", "souris", "station"],
}


def normalize(text):
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def classify_keywords(text):
    t = normalize(text)
    for category, words in KEYWORDS.items():
        for w in words:
            if w in t:
                return {"categorie": category, "confiance": 0.5, "justification": f"mot-clé « {w} »", "moteur": "mots-clés"}
    return {"categorie": "Logiciels", "confiance": 0.1, "justification": "aucun mot-clé reconnu", "moteur": "mots-clés"}


def classify_llm(text):
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
            "format": RESPONSE_SCHEMA,
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=120,
    )
    r.raise_for_status()
    result = json.loads(r.json()["message"]["content"])
    if result.get("categorie") not in LABEL_BY_NAME:
        raise ValueError(f"catégorie inconnue : {result.get('categorie')}")
    result["moteur"] = f"IA ({OLLAMA_MODEL})"
    return result


def classify(text, use_llm=True):
    if use_llm:
        try:
            return classify_llm(text)
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
            print(f"  (Ollama indisponible, repli sur les mots-clés : {e.__class__.__name__})")
    return classify_keywords(text)


def evaluate(use_llm):
    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    correct = 0
    for row in rows:
        res = classify(f"{row['summary']}\n{row['description']}", use_llm)
        ok = res["categorie"] == row["expected_category"]
        correct += ok
        mark = "OK " if ok else "KO "
        print(f"{mark} {res['categorie']:<17} (attendu : {row['expected_category']:<17}) {row['summary']}")
    print(f"\nPrécision : {correct}/{len(rows)} ({100 * correct / len(rows):.0f} %)")


def text_of(issue):
    desc = issue["fields"].get("description") or {}
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)

    walk(desc)
    return f"{issue['fields']['summary']}\n{' '.join(parts)}"


def triage(jira, project, dry_run, use_llm):
    all_labels = ", ".join(CATEGORIES)
    jql = (
        f"project = {project} AND resolution = Unresolved "
        f"AND (labels is EMPTY OR labels not in ({all_labels})) ORDER BY created ASC"
    )
    issues = jira.search(jql, fields=("summary", "description"))
    if not issues:
        print("File « À trier » vide.")
    for issue in issues:
        res = classify(text_of(issue), use_llm)
        label = LABEL_BY_NAME[res["categorie"]]
        print(f"{issue['key']:<6} -> {res['categorie']:<17} ({res['confiance']:.0%}, {res['moteur']})  {issue['fields']['summary']}")
        if dry_run:
            continue
        jira.add_labels(issue["key"], label, TRIAGED_LABEL)
        jira.internal_note(
            issue["key"],
            f"Tri automatique : {res['categorie']} (confiance {res['confiance']:.0%}, {res['moteur']}). "
            f"Justification : {res['justification']}",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval", action="store_true", help="mesure la précision sur le CSV, sans Jira")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="mots-clés uniquement")
    parser.add_argument("--loop", type=int, metavar="SECONDES")
    args = parser.parse_args()
    use_llm = not args.no_llm

    if args.eval:
        evaluate(use_llm)
        return

    from jira_client import Jira

    jira = Jira.from_env()
    project = os.environ["JIRA_PROJECT"]
    while True:
        triage(jira, project, args.dry_run, use_llm)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
