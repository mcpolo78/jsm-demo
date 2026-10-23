"""Crée les files d'attente du service desk (une par catégorie, plus « À trier »).

Usage :
    python setup_queues.py --dry-run
    python setup_queues.py

Les files déjà présentes (même nom) sont ignorées, donc le script peut être relancé.

Attention : Jira Cloud n'a pas d'API publique pour créer une file d'attente. Le script
utilise l'API interne de JSM (/rest/servicedesk/1/...), celle qu'appelle l'interface web.
Elle fonctionne aujourd'hui, mais Atlassian peut la modifier sans préavis.
"""

import argparse
import os

from jira_client import Jira

CATEGORIES = {
    "cat-acces": "Accès et comptes",
    "cat-messagerie": "Messagerie",
    "cat-reseau": "Réseau",
    "cat-impression": "Impression",
    "cat-materiel": "Matériel",
    "cat-logiciels": "Logiciels",
}

COLUMNS = ["issuetype", "issuekey", "summary", "reporter", "assignee", "status", "priority", "created"]


def build_queues(project):
    open_issues = f"project = {project} AND resolution = Unresolved"
    all_labels = ", ".join(CATEGORIES)
    queues = [("À trier", f"{open_issues} AND (labels is EMPTY OR labels not in ({all_labels})) ORDER BY priority DESC, created ASC")]
    for label, name in CATEGORIES.items():
        queues.append((name, f"{open_issues} AND labels = {label} ORDER BY priority DESC, created ASC"))
    return queues


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project = os.getenv("JIRA_PROJECT", "IT")
    queues = build_queues(project)

    if args.dry_run:
        for name, jql in queues:
            print(f"{name:<18} {jql}")
        return

    jira = Jira.from_env()
    desk_id = jira.service_desk_id(project)
    existing = {q["name"] for q in jira.get(f"/rest/servicedeskapi/servicedesk/{desk_id}/queue").get("values", [])}

    for name, jql in queues:
        if name in existing:
            print(f"déjà présente  {name}")
            continue
        jira.post(f"/rest/servicedesk/1/servicedesk/{project}/queues", {"name": name, "jql": jql, "columns": COLUMNS})
        print(f"créée          {name}")


if __name__ == "__main__":
    main()
