---
title: Authentification
group: services
summary: Keycloak (realm swann), clients, rôles corpus:*, flux OAuth de la liseuse, de la console et des comptes de service, vérification des jetons par l'API.
links: [architecture, corpus-api, liseuse, console, deploiement]
---

# Authentification et droits

L'identité est entièrement déléguée à **Keycloak** (realm `swann`). Aucune
brique du corpus ne stocke de compte : l'API ne connaît que le `sub` du
jeton, et chaque application clé ses données sur ce `sub`.

## Realm `swann`

```mermaid
flowchart LR
  subgraph realm["Realm swann"]
    capi["client corpus-api<br/>(audience, porte les rôles)"]
    cr["client swann-reader<br/>confidentiel, PKCE<br/>+ compte de service corpus:read"]
    cc["client swann-console<br/>confidentiel"]
    def["rôle par défaut<br/>corpus:read"]
    dev["utilisateur lecteur (dev)<br/>tous les rôles corpus:*"]
  end
  capi --- r1["corpus:read"]
  capi --- r2["corpus:review"]
  capi --- r3["corpus:admin"]
```

| Rôle | Autorise |
| --- | --- |
| `corpus:read` | catalogue, texte des éditions accessibles, recherche, alignements |
| `corpus:review` | verdicts et corrections d'alignement, atelier de la console (**jeton utilisateur** obligatoire) |
| `corpus:admin` | éditions `restricted`, supervision, dépôts, fiches, index |

Le realm est importé au premier démarrage depuis
`keycloak/import/swann-realm.json` (URLs et secrets injectés par variables
d'environnement), puis ignoré s'il existe déjà. Le thème de connexion
`keycloak/themes/swann` reprend les couleurs de la liseuse.

## Les trois façons d'appeler l'API

| Appelant | Flux OAuth | Exemple |
| --- | --- | --- |
| Lecteur connecté via une app | code + PKCE, jeton utilisateur relayé par le BFF | lire, enregistrer sa progression |
| Visiteur anonyme | `client_credentials` du compte de service de l'app | parcourir le catalogue des éditions `open` |
| Script ou worker | `client_credentials` | `reader-worker`, notebook d'analyse |

## Connexion à la liseuse

```mermaid
sequenceDiagram
  autonumber
  participant N as Navigateur
  participant R as reader (Better Auth)
  participant K as Keycloak
  participant DB as Postgres reader
  N->>R: /library (pas de session)
  R-->>N: redirection vers Keycloak (PKCE)
  N->>K: page de connexion (thème swann)
  K-->>N: redirection /api/auth/callback?code=…
  N->>R: callback
  R->>K: échange code → jetons (backchannel http://keycloak:8080)
  K-->>R: access + refresh + id token
  R->>DB: session + jetons (schéma auth), sub copié dans auth.user
  R-->>N: cookie de session httpOnly
```

La console suit exactement le même schéma avec le client `swann-console`, et
refuse l'accès sans `corpus:admin` ou `corpus:review`.

## Vérification côté API

```mermaid
flowchart TB
  t["Authorization: Bearer JWT"] --> sig{"signature valide<br/>(JWKS du realm, mis en cache)"}
  sig -- "non" --> e401["401"]
  sig -- "oui" --> iss{"émetteur = OIDC_ISSUER<br/>audience = corpus-api"}
  iss -- "non" --> e401
  iss -- "oui" --> roles["rôles : client corpus-api + realm"]
  roles --> need{"rôle requis ?"}
  need -- "non" --> e403["403"]
  need -- "oui" --> svc{"corpus:review et<br/>compte de service ?"}
  svc -- "oui" --> e403
  svc -- "non" --> ok["requête traitée<br/>(sub disponible)"]
```

- En conteneur, les clés sont lues par le réseau interne
  (`OIDC_DISCOVERY_URL = http://keycloak:8080/realms/swann`) tandis que
  l'émetteur attendu reste l'URL publique (`http://localhost:8080/realms/swann`) :
  Keycloak tourne avec `KC_HOSTNAME_BACKCHANNEL_DYNAMIC`.
- Le client doit ajouter l'audience `corpus-api` aux jetons (mapper
  « Audience »).
- En développement, `AUTH_DISABLED=true` accorde tous les droits sans jeton.
