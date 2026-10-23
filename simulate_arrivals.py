"""Simule l'arrivée de tickets clients, un à la fois, pour mesurer la chaîne en conditions réelles.

À lancer pendant que pipeline.py tourne en boucle, puis lire les délais avec
kpis.py --depuis "<heure de début>".

Usage :
    python simulate_arrivals.py --dry-run
    python simulate_arrivals.py --interval 30            # tickets reformulés, un toutes les 30 s
    python simulate_arrivals.py --csv data/tickets.csv --limite 5
"""

import argparse
import csv
import os
import time
from datetime import datetime
from pathlib import Path

from jira_client import Jira, adf
from seed_tickets import resolve_issue_type

DEFAULT_CSV = Path(__file__).parent / "data" / "tickets_reformules.csv"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--interval", type=int, default=30, metavar="SECONDES")
    parser.add_argument("--limite", type=int, help="nombre maximum de tickets")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with args.csv.open(encoding="utf-8") as f:
        tickets = list(csv.DictReader(f))[: args.limite]
    if args.dry_run:
        for t in tickets:
            print(t["summary"])
        print(f"\n{len(tickets)} tickets, un toutes les {args.interval} s.")
        return

    jira = Jira.from_env()
    project = os.environ["JIRA_PROJECT"]
    available = jira.issue_types(project)
    # Comme seed_tickets.py : un résumé déjà présent dans le projet n'est pas recréé.
    existing = {i["fields"]["summary"].strip().casefold() for i in jira.search(f"project = {project}")}
    print(f"Début : {datetime.now():%Y-%m-%d %H:%M}  (à passer à kpis.py --depuis)")

    for n, t in enumerate(tickets):
        if t["summary"].strip().casefold() in existing:
            print(f"déjà présent  {t['summary']}")
            continue
        if n:
            time.sleep(args.interval)
        created = jira.post(
            "/rest/api/3/issue",
            {
                "fields": {
                    "project": {"key": project},
                    "summary": t["summary"],
                    "description": adf(t["description"]),
                    "issuetype": {"id": resolve_issue_type(t.get("request_type", "Incident"), available)},
                }
            },
        )
        print(f"{datetime.now():%H:%M:%S}  {created['key']:<6}  {t['summary']}")


if __name__ == "__main__":
    main()
