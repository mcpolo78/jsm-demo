# Comparatif des méthodes de recherche dans la base de connaissances

Mesuré le 23/10/2026 sur le PC de démo (RTX 4060, Ollama : nomic-embed-text + qwen2.5:7b), avec `python suggest_replies.py --compare`.

| Jeu de tickets | Méthode | Bon article | Réponse auto correcte | Réponse auto **erronée** |
|---|---|---|---|---|
| Tickets d'origine (20) | TF-IDF | 20/20 | 12/20 | 0/20 |
| Tickets d'origine (20) | Embeddings | 20/20 | 13/20 | 0/20 |
| Tickets d'origine (20) | **Hybride (consensus)** | **20/20** | **13/20** | **0/20** |
| Tickets reformulés (12) | TF-IDF | 11/12 | 0/12 | 0/12 |
| Tickets reformulés (12) | Embeddings | 8/12 | 6/12 | 2/12 |
| Tickets reformulés (12) | **Hybride (consensus)** | **11/12** | **5/12** | **0/12** |

## Lecture

- **TF-IDF** (recherche par les mots) trouve presque toujours le bon article, mais quand l'utilisateur emploie d'autres mots que l'article, ses scores s'effondrent : aucune réponse automatique.
- **Embeddings** (recherche par le sens) sont confiants même sur des formulations libres, mais se trompent plus souvent. Sur les tickets reformulés, 2 clients auraient reçu une mauvaise procédure.
- **Hybride** : la réponse part seule seulement si les deux méthodes désignent le même article, avec un score d'embedding d'au moins 0,70. En cas de désaccord, l'article du TF-IDF est proposé à un agent. Résultat : la confiance des embeddings, la précision du TF-IDF, et **zéro réponse erronée envoyée au client**.

Garde-fou supplémentaire : les articles qui demandent une action du support (droits, création de compte, matériel, panne, licence) ne partent jamais en automatique, quel que soit le score.
