"""Crée les tickets de démo dans un projet Jira Service Management via l'API REST.

Usage :
    python seed_tickets.py --dry-run   # affiche ce qui serait créé, sans appel API
    python seed_tickets.py             # crée les tickets

Variables d'environnement (fichier .env accepté) :
    JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT
"""

import argparse
import csv
import os
from pathlib import Path

from jira_client import Jira, adf

CSV_PATH = Path(__file__).parent / "data" / "tickets.csv"

# Les noms des types de ticket varient selon le modèle de projet et la langue du site.
# On essaie chaque nom dans l'ordre jusqu'à en trouver un qui existe dans le projet.
ISSUE_TYPE_CANDIDATES = {
    "Incident": ["[System] Incident", "Incident"],
    "Service request": [
        "[System] Service request",
        "Service request",
        "Demande de service",
        "[System] Demande de service",
    ],
}

# Jira Cloud n'a pas de priorité « Critical » par défaut.
PRIORITY_ALIASES = {"Critical": "Highest"}


def load_tickets():
    with CSV_PATH.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def resolve_issue_type(request_type, available):
    for name in ISSUE_TYPE_CANDIDATES.get(request_type, [request_type]):
        if name in available:
            return available[name]
    raise SystemExit(
        f"Aucun type de ticket trouvé pour « {request_type} ». "
        f"Types disponibles : {', '.join(available)}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickets = load_tickets()

    if args.dry_run:
        for t in tickets:
            print(f"[{t['request_type']:<15}] [{t['priority']:<8}] {t['summary']}")
        print(f"\n{len(tickets)} tickets prêts à être créés.")
        return

    jira = Jira.from_env()
    project = os.environ["JIRA_PROJECT"]
    available = jira.issue_types(project)
    # Un ticket dont le résumé existe déjà dans le projet n'est pas recréé.
    existing = {i["fields"]["summary"].strip().casefold() for i in jira.search(f"project = {project}")}

    created_count = 0
    for t in tickets:
        if t["summary"].strip().casefold() in existing:
            print(f"déjà présent  {t['summary']}")
            continue
        fields = {
            "project": {"key": project},
            "summary": t["summary"],
            "description": adf(t["description"]),
            "issuetype": {"id": resolve_issue_type(t["request_type"], available)},
        }
        priority = PRIORITY_ALIASES.get(t["priority"], t["priority"])
        try:
            created = jira.post("/rest/api/3/issue", {"fields": {**fields, "priority": {"name": priority}}})
        except RuntimeError as e:
            # Certains schémas JSM n'exposent pas le champ priorité à la création.
            if "priority" not in str(e):
                raise
            created = jira.post("/rest/api/3/issue", {"fields": fields})
        created_count += 1
        print(f"{created['key']:<12}  {t['summary']}")

    print(f"\n{created_count} tickets créés dans {project}, {len(tickets) - created_count} déjà présents.")


if __name__ == "__main__":
    main()
