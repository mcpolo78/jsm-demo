"""Toute la chaîne du service desk, en boucle : idéal pour une démo en direct.

À chaque tour :
  1. automations : accusé de réception, escalade des priorités maximales, relances ;
  2. triage      : catégorie choisie par l'IA, ticket routé vers sa file ;
  3. réponses    : article de la base de connaissances, réponse auto ou suggestion agent ;
  4. indicateurs : dashboard.html mis à jour.

Usage :
    python pipeline.py              # un seul tour
    python pipeline.py --loop 30    # un tour toutes les 30 secondes (Ctrl+C pour arrêter)
"""

import argparse
import os
import time

import automations
import kpis
import suggest_replies
import triage
from jira_client import Jira


def run(jira, kb, project, ctx):
    print("── Automatisations")
    automations.run_once(jira, project, False, ctx)
    print("── Tri par l'IA")
    triage.triage(jira, project, False, True)
    print("── Réponses depuis la base de connaissances")
    suggest_replies.answer_tickets(jira, kb, project, False)
    issues = jira.search(f"project = {project}", fields=("created", "labels", "comment", "status"))
    k = kpis.compute(issues)
    kpis.OUTPUT.write_text(kpis.render_html(k, os.environ["JIRA_URL"], project), encoding="utf-8")
    print(f"── Indicateurs : {k['auto']}/{k['total']} résolus sans intervention ({kpis.pct(k['auto'], k['total'])})\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loop", type=int, metavar="SECONDES")
    args = parser.parse_args()

    jira = Jira.from_env()
    project = os.environ["JIRA_PROJECT"]
    ctx = {"on_call": os.getenv("JIRA_ON_CALL_ACCOUNT_ID") or jira.get("/rest/api/3/myself")["accountId"]}
    kb = suggest_replies.KnowledgeBase(suggest_replies.load_kb())

    while True:
        run(jira, kb, project, ctx)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
