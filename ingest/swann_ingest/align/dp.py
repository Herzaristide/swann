"""Alignement monotone de deux suites par programmation dynamique.

Les deux suites sont parcourues dans l'ordre (une traduction suit l'ordre de
l'original). À chaque pas, on choisit le meilleur regroupement parmi 1-1,
1-2, 2-1, 1-0, 0-1 (et 1-3, 3-1 pour les paragraphes) selon la similarité de
sens des éléments regroupés (vecteurs normalisés, produit scalaire = cosinus).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Group:
    a: list[int]  # indices dans la suite de référence
    b: list[int]  # indices dans la suite cible
    score: float  # cosinus entre les deux regroupements (0 si un côté est vide)


def align(
    ea: np.ndarray,
    eb: np.ndarray,
    *,
    threshold: float,
    moves: tuple[tuple[int, int], ...] = ((1, 1), (1, 2), (2, 1), (1, 0), (0, 1)),
    merge_penalty: float = 0.05,
    bonus: np.ndarray | None = None,
    band: float | None = None,
) -> list[Group]:
    """ea, eb : vecteurs normalisés (une ligne par élément).

    Valeur d'un regroupement (na, nb) = (cos - threshold - pénalité) × (na+nb)/2 :
    un appariement n'est retenu que si son cosinus dépasse `threshold`, et les
    fusions sont légèrement pénalisées pour préférer le 1-1 à qualité égale.
    bonus[i, j] (optionnel) s'ajoute au cosinus d'un 1-1.
    band : si donné, n'explore que les cases proches de la diagonale (fraction
    de la longueur), pour les longs chapitres.
    """
    n, m = len(ea), len(eb)
    if n == 0 or m == 0:
        return [Group([i], [], 0.0) for i in range(n)] + [Group([], [j], 0.0) for j in range(m)]

    # Similarités et normes des sommes de 1 à 3 éléments voisins, précalculées
    # en listes Python (la boucle ci-dessous est en Python pur).
    sim = (ea @ eb.T).tolist()
    max_n = max(max(na for na, _ in moves), max(nb for _, nb in moves))
    norm_a = _group_norms(ea, max_n)
    norm_b = _group_norms(eb, max_n)

    def cos(i: int, na: int, j: int, nb: int) -> float:
        s = sum(sim[i + x][j + y] for x in range(na) for y in range(nb))
        return s / (norm_a[na][i] * norm_b[nb][j])

    neg = -1e18
    best = np.full((n + 1, m + 1), neg)
    back = np.zeros((n + 1, m + 1, 2), dtype=np.int16)
    best[0, 0] = 0.0
    width = None if band is None else max(10, int(band * max(n, m)))

    for i in range(n + 1):
        center = i * m / n
        j_lo = 0 if width is None else max(0, int(center) - width)
        j_hi = m if width is None else min(m, int(center) + width)
        for j in range(j_lo, j_hi + 1):
            if i == 0 and j == 0:
                continue
            value, arg = neg, (0, 0)
            for na, nb in moves:
                pi, pj = i - na, j - nb
                if pi < 0 or pj < 0 or best[pi, pj] == neg:
                    continue
                if na == 0 or nb == 0:
                    gain = 0.0
                else:
                    c = cos(pi, na, pj, nb)
                    if na == 1 and nb == 1 and bonus is not None:
                        c += float(bonus[pi, pj])
                    penalty = merge_penalty * (na + nb - 2)
                    gain = (c - threshold - penalty) * (na + nb) / 2
                total = best[pi, pj] + gain
                if total > value:
                    value, arg = total, (na, nb)
            best[i, j] = value
            back[i, j] = arg

    groups: list[Group] = []
    i, j = n, m
    if best[n, m] == neg:
        raise RuntimeError("alignement impossible (bande trop étroite)")
    while i > 0 or j > 0:
        na, nb = (int(x) for x in back[i, j])
        pi, pj = i - na, j - nb
        score = cos(pi, na, pj, nb) if na and nb else 0.0
        groups.append(Group(list(range(pi, i)), list(range(pj, j)), score))
        i, j = pi, pj
    groups.reverse()
    return groups


def _group_norms(e: np.ndarray, max_n: int) -> dict[int, list[float]]:
    """norms[k][i] = norme de e[i] + ... + e[i+k-1] (vecteurs normalisés)."""
    gram = e @ e.T
    n = len(e)
    norms: dict[int, list[float]] = {}
    for k in range(1, max_n + 1):
        norms[k] = [
            float(np.sqrt(max(gram[i : i + k, i : i + k].sum(), 1e-9))) if i + k <= n else 1.0
            for i in range(n)
        ]
    return norms
