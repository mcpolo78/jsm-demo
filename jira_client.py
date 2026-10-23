"""Petit client pour l'API REST Jira Cloud, partagé par les scripts du projet."""

import os
import sys

import requests
from dotenv import load_dotenv

REQUIRED_VARS = ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT")


def adf(text):
    """Convertit du texte brut au format Atlassian Document Format (API v3)."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class Jira:
    def __init__(self, url, email, token):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({"Accept": "application/json"})

    @classmethod
    def from_env(cls):
        load_dotenv()
        missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
        if missing:
            sys.exit(f"Variables manquantes : {', '.join(missing)} (voir .env.example)")
        return cls(os.environ["JIRA_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

    def request(self, method, path, payload=None, **params):
        r = self.session.request(method, f"{self.base}{path}", json=payload, params=params, timeout=30)
        if not r.ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text}")
        return r.json() if r.content else None

    def get(self, path, **params):
        return self.request("GET", path, **params)

    def post(self, path, payload):
        return self.request("POST", path, payload)

    def put(self, path, payload):
        return self.request("PUT", path, payload)

    def search(self, jql, fields=("summary",)):
        """Renvoie tous les tickets correspondant à la requête JQL."""
        issues, token = [], None
        while True:
            body = {"jql": jql, "fields": list(fields), "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            data = self.post("/rest/api/3/search/jql", body)
            issues += data.get("issues", [])
            token = data.get("nextPageToken")
            if not token:
                return issues

    def issue_types(self, project_key):
        data = self.get(f"/rest/api/3/issue/createmeta/{project_key}/issuetypes")
        return {t["name"]: t["id"] for t in data.get("issueTypes", data.get("values", []))}

    def service_desk_id(self, project_key):
        data = self.get("/rest/servicedeskapi/servicedesk")
        for desk in data.get("values", []):
            if desk["projectKey"] == project_key:
                return desk["id"]
        sys.exit(f"Aucun service desk trouvé pour le projet {project_key}")

    def add_labels(self, key, *labels):
        self.put(f"/rest/api/3/issue/{key}", {"update": {"labels": [{"add": l} for l in labels]}})

    def public_comment(self, key, text):
        """Commentaire visible par le client sur le portail JSM.

        On passe par l'API Jira générique plutôt que par /rest/servicedeskapi : celle-ci
        refuse (404) les tickets créés sans type de demande du portail, comme ceux
        importés par l'API.
        """
        self._comment(key, text, internal=False)

    def internal_note(self, key, text):
        """Note interne : visible des agents uniquement, jamais du client."""
        self._comment(key, text, internal=True)

    def _comment(self, key, text, internal):
        self.post(
            f"/rest/api/3/issue/{key}/comment",
            {"body": adf(text), "properties": [{"key": "sd.public.comment", "value": {"internal": internal}}]},
        )
