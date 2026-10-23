"""Automatisations du service desk, en Python via l'API REST Jira.

Trois règles, équivalentes à des règles Jira Automation :
  1. Accusé de réception : commentaire public sur chaque nouveau ticket.
  2. Priorité maximale : le ticket est assigné tout de suite à l'agent de garde.
  3. Relance client : commentaire après 3 jours sans réponse en « En attente du client ».

Chaque règle pose une étiquette sur le ticket traité, ce qui évite de le traiter deux fois.
L'étiquette est posée avant le commentaire : si un appel échoue en cours de route,
le ticket peut manquer un commentaire, mais n'en reçoit jamais deux.

Usage :
    python automations.py --dry-run        # liste ce qui serait fait
    python automations.py                  # exécute une fois
    python automations.py --loop 60        # exécute toutes les 60 secondes
"""

import argparse
import os
import time

from jira_client import Jira

ACK_TEXT = (
    "Bonjour {name}, votre demande {key} a bien été reçue. "
    "Un technicien la prend en charge sous 4 h ouvrées."
)
REMINDER_TEXT = "Sans retour de votre part sous 48 h, ce ticket sera clôturé."
WAITING_STATUS = "En attente du client"


def rules(project):
    base = f"project = {project} AND resolution = Unresolved"
    return [
        ("Accusé de réception", f"{base} AND (labels is EMPTY OR labels != auto-ack)", acknowledge),
        ("Priorité maximale", f"{base} AND priority = Highest AND assignee is EMPTY", assign_on_call),
        ("Relance client", f'{base} AND status = "{WAITING_STATUS}" AND updated <= -3d AND (labels is EMPTY OR labels != relance-1)', remind),
    ]


def acknowledge(jira, issue, ctx):
    reporter = (issue["fields"].get("reporter") or {}).get("displayName", "")
    jira.add_labels(issue["key"], "auto-ack")
    jira.public_comment(issue["key"], ACK_TEXT.format(name=reporter, key=issue["key"]))


def assign_on_call(jira, issue, ctx):
    jira.put(f"/rest/api/3/issue/{issue['key']}/assignee", {"accountId": ctx["on_call"]})
    jira.add_labels(issue["key"], "auto-escalade")


def remind(jira, issue, ctx):
    jira.add_labels(issue["key"], "relance-1")
    jira.public_comment(issue["key"], REMINDER_TEXT)


def run_once(jira, project, dry_run, ctx):
    for name, jql, action in rules(project):
        issues = jira.search(jql, fields=("summary", "reporter"))
        for issue in issues:
            print(f"[{name}] {issue['key']}  {issue['fields']['summary']}")
            if not dry_run:
                action(jira, issue, ctx)
        if not issues:
            print(f"[{name}] rien à faire")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--loop", type=int, metavar="SECONDES", help="relance en boucle")
    args = parser.parse_args()

    jira = Jira.from_env()
    project = os.environ["JIRA_PROJECT"]
    # L'agent de garde est le compte du token, sauf si JIRA_ON_CALL_ACCOUNT_ID est défini.
    ctx = {"on_call": os.getenv("JIRA_ON_CALL_ACCOUNT_ID") or jira.get("/rest/api/3/myself")["accountId"]}

    while True:
        run_once(jira, project, args.dry_run, ctx)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
