---
title: Alignement
group: traitements
summary: Comment Swann relie les paragraphes des traductions d'une même œuvre — édition de référence, unités pivots, programmation dynamique LaBSE, qualité et relecture humaine.
links: [ingestion, donnees, recherche, liseuse, console]
---

# Alignement des traductions

L'alignement permet de **passer d'un passage à sa traduction** : changer
d'édition sans perdre sa place, lire côte à côte, joindre les traductions
aux résultats de recherche.

## Le pivot : les unités de l'œuvre

Plutôt que d'aligner chaque paire d'éditions (n² paires), toutes les
éditions sont alignées sur une **édition de référence**, dont chaque segment
du corps devient une **unité** (`work_units`) de l'œuvre.

```mermaid
flowchart LR
  subgraph ref["Référence (ru, originale)"]
    r1["seq 120"]
    r2["seq 121"]
  end
  subgraph units["work_units"]
    u1(("u120"))
    u2(("u121"))
  end
  subgraph fr["Traduction fr"]
    f1["seq 118"]
    f2["seq 119"]
    f3["seq 120"]
  end
  subgraph en["Traduction en"]
    e1["seq 125"]
    e2["seq 126"]
  end
  r1 --- u1
  r2 --- u2
  f1 --- u1
  f2 --- u1
  f3 --- u2
  e1 --- u1
  e2 --- u2
```

Les liens sont **n-n** : un paragraphe original peut correspondre à deux
paragraphes traduits (1-2), ou l'inverse (2-1). fr → en passe par les unités,
sans alignement direct entre les deux traductions.

## Algorithme (`swann-labse-v1`)

```mermaid
flowchart TB
  s1["1 · Choisir la référence<br/>l'originale, sinon la plus longue"] --> s2["2 · Créer les work_units<br/>une par segment du corps"]
  s2 --> s3["3 · Apparier les chapitres<br/>sens des paragraphes + numéro, dans l'ordre<br/>seuil 0,30"]
  s3 --> s4["4 · Aligner les paragraphes de chaque paire<br/>programmation dynamique, LaBSE<br/>seuil 0,40"]
  s4 --> s5["5 · Qualité par édition<br/>part alignée, score moyen, part de scores faibles"]
  s5 --> st{"statut"}
  st --> rel["reliable"]
  st --> dou["doubtful"]
  st --> rej["rejected"]
```

L'alignement de paragraphes est **monotone** (une traduction suit l'ordre de
l'original) : à chaque pas, la programmation dynamique choisit le meilleur
regroupement parmi 1-1, 1-2, 2-1, 1-0, 0-1 (et 1-3, 3-1), selon le cosinus
entre vecteurs **LaBSE** des éléments regroupés, avec une légère pénalité
pour les fusions.

- Travailler **chapitre par chapitre** borne le coût et empêche une erreur de
  se propager à tout le livre.
- Les liens **manuels** (`method = 'manual'`) ne sont jamais écrasés : une
  œuvre qui en contient n'est réalignée qu'avec `--force` (ou à partir d'une
  section depuis la console).
- Le statut `doubtful` écarte les adaptations et traductions abrégées des
  analyses ; la liseuse retombe alors sur le chapitre correspondant.

## Utilisation par l'API

| Endpoint | Rôle |
| --- | --- |
| `GET /v1/works/{id}/alignment` | qualité de chaque édition |
| `GET /v1/editions/{id}/counterpart?seq=&lang=` | position équivalente ; `via: segment` ou repli `via: section` |
| `GET /v1/editions/{id}/parallel?target=&from_seq=&to_seq=` | paires de groupes de segments pour la lecture côte à côte |

## Relecture humaine

```mermaid
sequenceDiagram
  autonumber
  participant R as Relecteur (corpus:review)
  participant A as Corpus API
  participant P as Postgres
  R->>A: GET /v1/alignment/sample
  A->>P: tire un lien au hasard
  A-->>R: segment source, unité, segments cibles
  alt lien correct
    R->>A: POST /v1/alignment/reviews {verdict: correct, is_sample: true}
  else lien faux
    R->>A: POST /v1/alignment/reviews {verdict: incorrect}
    R->>A: DELETE /v1/alignment/links + POST /v1/alignment/links (manual)
  end
  A->>P: verdict + reviewer_sub (sub Keycloak)
```

Les verdicts tirés au hasard (`is_sample`) mesurent la **précision** de
l'aligneur par méthode, avec un intervalle de Wilson : c'est le chiffre qui
permet de filtrer les analyses par confiance. La console ajoute un atelier
complet (carte par section, vue côte à côte éditable, réalignement) — voir
[Console](#console).
