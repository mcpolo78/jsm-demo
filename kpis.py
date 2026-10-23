"""Indicateurs du service desk, calculés depuis Jira, et tableau de bord HTML.

Indicateurs suivis (ceux de l'offre : taux de résolution sans intervention et délais) :
  - tickets reçus, triés par l'IA, résolus sans intervention, suggestions pour un agent ;
  - délai médian de l'accusé de réception et de la réponse de l'IA ;
  - répartition par catégorie et articles de la base de connaissances les plus utilisés.

Usage :
    python kpis.py                 # affiche les indicateurs et écrit dashboard.html
    python kpis.py --open          # idem, puis ouvre le tableau de bord dans le navigateur
    python kpis.py --depuis "2026-10-23 17:00"   # seulement les tickets créés depuis cette date
    python kpis.py --public        # écrit docs/index.html (GitHub Pages), sans lien vers le site Jira
"""

import argparse
import html
import os
import re
import statistics
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path

from setup_queues import CATEGORIES
from suggest_replies import load_kb

OUTPUT = Path(__file__).parent / "dashboard.html"
PUBLIC_OUTPUT = Path(__file__).parent / "docs" / "index.html"
ACK_MARKER = "a bien été reçue"
SOURCE_RE = re.compile(r"\[Source : (KB-\d+)")


def adf_text(node):
    """Texte brut d'un champ au format Atlassian Document Format."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(adf_text(child) for child in node.get("content", []))


def parse_date(value):
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=datetime.strptime(value[-5:], "%z").tzinfo
    )


def minutes_between(start, end):
    return (parse_date(end) - parse_date(start)).total_seconds() / 60


def compute(issues):
    k = {
        "total": len(issues),
        "ouverts": 0,
        "tries": 0,
        "auto": 0,
        "suggestions": 0,
        "delais_ack": [],
        "delais_ia": [],
        "categories": {name: {"total": 0, "auto": 0, "agent": 0} for name in CATEGORIES.values()},
        "articles": Counter(),
    }
    for issue in issues:
        f = issue["fields"]
        labels = set(f.get("labels") or [])
        if (f.get("status") or {}).get("statusCategory", {}).get("key") != "done":
            k["ouverts"] += 1
        k["tries"] += "ia-triee" in labels
        k["auto"] += "ia-reponse-auto" in labels
        k["suggestions"] += "ia-suggestion" in labels
        for label, name in CATEGORIES.items():
            if label in labels:
                cat = k["categories"][name]
                cat["total"] += 1
                cat["auto"] += "ia-reponse-auto" in labels
                cat["agent"] += "ia-suggestion" in labels

        ack_done = ia_done = False
        for c in sorted((f.get("comment") or {}).get("comments", []), key=lambda c: c["created"]):
            text = adf_text(c.get("body"))
            source = SOURCE_RE.search(text)
            if source:
                k["articles"][source.group(1)] += 1
            if not c.get("jsdPublic", True):
                continue
            if not ack_done and ACK_MARKER in text:
                k["delais_ack"].append(minutes_between(f["created"], c["created"]))
                ack_done = True
            elif not ia_done and source:
                k["delais_ia"].append(minutes_between(f["created"], c["created"]))
                ia_done = True
    return k


def pct(part, total):
    return f"{100 * part / total:.0f} %" if total else "—"


def fmt_minutes(values):
    if not values:
        return "—"
    m = statistics.median(values)
    if m < 1:
        return f"{m * 60:.0f} s"
    if m < 120:
        return f"{m:.0f} min"
    return f"{m / 60:.1f} h"


def print_summary(k):
    print(f"Tickets reçus                      : {k['total']} (dont {k['ouverts']} ouverts)")
    print(f"Triés par l'IA                     : {k['tries']} ({pct(k['tries'], k['total'])})")
    print(f"Résolus sans intervention          : {k['auto']} ({pct(k['auto'], k['total'])})")
    print(f"Suggestions transmises à un agent  : {k['suggestions']}")
    print(f"Délai médian accusé de réception   : {fmt_minutes(k['delais_ack'])}")
    print(f"Délai médian réponse de l'IA       : {fmt_minutes(k['delais_ia'])}")
    print("\nPar catégorie (total / auto / agent) :")
    for name, c in k["categories"].items():
        print(f"  {name:<18} {c['total']:>3} / {c['auto']:>3} / {c['agent']:>3}")


def render_html(k, site_url, project, depuis=None):
    cats = k["categories"]
    max_cat = max([c["total"] for c in cats.values()] + [1])
    bars = "".join(
        f"""<div class="row"><span class="lbl">{html.escape(name)}</span>
        <span class="track"><span class="seg auto" style="width:{100 * c['auto'] / max_cat:.1f}%"></span><span class="seg agent" style="width:{100 * c['agent'] / max_cat:.1f}%"></span><span class="seg other" style="width:{100 * (c['total'] - c['auto'] - c['agent']) / max_cat:.1f}%"></span></span>
        <span class="num">{c['total']}</span></div>"""
        for name, c in cats.items()
    )
    titles = {a["id"]: a["titre"] for a in load_kb()}
    top = k["articles"].most_common(6)
    max_art = max([n for _, n in top] + [1])
    articles = "".join(
        f"""<div class="row art"><span class="lbl" title="{html.escape(titles.get(kb, ''))}">{kb} · {html.escape(titles.get(kb, ''))}</span><span class="track"><span class="seg auto" style="width:{100 * n / max_art:.1f}%"></span></span><span class="num">{n}</span></div>"""
        for kb, n in top
    ) or "<p class='muted'>Aucune réponse de la base de connaissances pour l'instant.</p>"
    tiles = [
        ("Tickets reçus", k["total"], f"{k['ouverts']} ouverts"),
        ("Triés par l'IA", pct(k["tries"], k["total"]), f"{k['tries']} tickets"),
        ("Résolus sans intervention", pct(k["auto"], k["total"]), f"{k['auto']} réponses automatiques"),
        ("Suggestions agent", k["suggestions"], "à valider par un technicien"),
        ("Délai médian accusé", fmt_minutes(k["delais_ack"]), "création → 1er message"),
        ("Délai médian réponse IA", fmt_minutes(k["delais_ia"]), "création → procédure envoyée"),
    ]
    tiles_html = "".join(
        f"<div class='tile'><div class='t'>{t}</div><div class='v'>{v}</div><div class='s'>{s}</div></div>"
        for t, v, s in tiles
    )
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    meta = [f"Projet {html.escape(project)}"]
    if depuis:
        meta.append(f"tickets créés depuis le {html.escape(depuis)}")
    meta.append(f"mis à jour le {now}")
    if site_url:
        queue_url = f"{site_url.rstrip('/')}/jira/servicedesk/projects/{project}/queues"
        meta.append(f'<a href="{html.escape(queue_url)}">ouvrir les files d\'attente</a>')
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Service desk IT – indicateurs</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --text:#1f2328; --muted:#6b7078; --line:#e4e4e0;
  --auto:#2f7d5b; --agent:#c7882b; --other:#b9bcc2; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16181b; --card:#1f2226; --text:#e8e8e6;
  --muted:#9aa0a8; --line:#30343a; --auto:#4fb487; --agent:#e0a64a; --other:#5a5f67; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1040px; margin:0 auto; padding:28px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:0 0 14px; }}
.muted, .s, .t {{ color:var(--muted); }} .muted {{ margin:0 0 24px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }}
.tile, .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.t {{ font-size:13px; }} .v {{ font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; }} .s {{ font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; }}
.row {{ display:grid; grid-template-columns:130px 1fr 32px; align-items:center; gap:10px; margin:8px 0; font-size:14px; }}
.track {{ display:flex; height:14px; border-radius:4px; overflow:hidden; }}
.art {{ grid-template-columns:minmax(0,1.4fr) 1fr 32px; }} .art .lbl {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.seg {{ display:block; height:100%; }} .auto {{ background:var(--auto); }} .agent {{ background:var(--agent); }} .other {{ background:var(--other); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.legend {{ display:flex; gap:16px; font-size:12px; color:var(--muted); margin-top:10px; flex-wrap:wrap; }}
.legend i {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px; }}
a {{ color:inherit; }} footer {{ margin-top:24px; font-size:12px; color:var(--muted); }}
</style></head><body><main>
<h1>Service desk IT : indicateurs</h1>
<p class="muted">{" · ".join(meta)}</p>
<section class="tiles">{tiles_html}</section>
<section class="grid">
  <div class="card"><h2>Tickets par catégorie</h2>{bars}
    <div class="legend"><span><i class="auto"></i>Résolu par l'IA</span><span><i class="agent"></i>Suggestion agent</span><span><i class="other"></i>Pas encore traité</span></div></div>
  <div class="card"><h2>Articles les plus utilisés</h2>{articles}</div>
</section>
<footer>Généré par kpis.py depuis l'API Jira. Tri : Ollama (qwen2.5:7b). Recherche : TF-IDF + embeddings (nomic-embed-text).</footer>
</main></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--open", action="store_true", help="ouvre le tableau de bord dans le navigateur")
    parser.add_argument("--depuis", metavar="'AAAA-MM-JJ HH:MM'", help="ne compte que les tickets créés depuis cette date")
    parser.add_argument("--public", action="store_true", help="écrit docs/index.html, sans lien vers le site Jira")
    args = parser.parse_args()

    from jira_client import Jira

    jira = Jira.from_env()
    project = os.environ["JIRA_PROJECT"]
    jql = f'project = {project} AND created >= "{args.depuis}"' if args.depuis else f"project = {project}"
    issues = jira.search(jql, fields=("created", "labels", "comment", "status"))
    k = compute(issues)
    print_summary(k)
    output = PUBLIC_OUTPUT if args.public else OUTPUT
    output.parent.mkdir(exist_ok=True)
    site_url = None if args.public else os.environ["JIRA_URL"]
    output.write_text(render_html(k, site_url, project, args.depuis), encoding="utf-8")
    print(f"\nTableau de bord : {output}")
    if args.open:
        webbrowser.open(output.as_uri())


if __name__ == "__main__":
    main()
