"""Découpage v1 : segments -> chunks (fenêtres de segments entiers).

Règles :
  - seules les sections du corps sont découpées, chacune séparément (un chunk
    ne déborde jamais de sa section) ; les notes ne sont jamais incluses ;
  - on accumule des segments jusqu'à ~TARGET tokens, sans dépasser MAX_TOKENS
    (un segment seul plus long reste un chunk à lui seul) ;
  - le chunk suivant reprend les derniers segments du précédent (~OVERLAP
    tokens de chevauchement) ;
  - un dernier chunk trop petit est fusionné avec le précédent ;
  - une section qui ne contient que des titres n'est pas découpée.
"""

from __future__ import annotations

VERSION = "v1"
TARGET_TOKENS = 400
MAX_TOKENS = 512
OVERLAP_TOKENS = 60
MIN_TOKENS = 100


def chunk_spans(tokens: list[int]) -> list[tuple[int, int]]:
    """tokens[i] = nombre de tokens du i-ème segment de la section.
    Retourne des intervalles inclusifs (début, fin) d'indices de segments."""
    n = len(tokens)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j, total = i, 0
        while j < n and (j == i or total + tokens[j] <= TARGET_TOKENS):
            total += tokens[j]
            j += 1
        spans.append((i, j - 1))
        if j >= n:
            break
        # Chevauchement : on recule tant que les segments repris tiennent
        # dans OVERLAP_TOKENS, en avançant d'au moins un segment.
        k, overlap = j, 0
        while k - 1 > i and overlap + tokens[k - 1] <= OVERLAP_TOKENS:
            k -= 1
            overlap += tokens[k]
        i = k

    if len(spans) >= 2:
        last_a, last_b = spans[-1]
        prev_a, _ = spans[-2]
        last_total = sum(tokens[last_a : last_b + 1])
        merged_total = sum(tokens[prev_a : last_b + 1])
        if last_total < MIN_TOKENS and merged_total <= MAX_TOKENS:
            spans[-2:] = [(prev_a, last_b)]
    return spans


def plan_chunks(
    segments: list[dict], token_counts: list[int], body_sections: set
) -> list[tuple[int, int, int]]:
    """segments : dicts {seq, section_id, kind} dans l'ordre de lecture.
    Retourne (seq de début, seq de fin, nombre de tokens) pour chaque chunk."""
    chunks: list[tuple[int, int, int]] = []
    i = 0
    while i < len(segments):
        section = segments[i]["section_id"]
        j = i
        while j < len(segments) and segments[j]["section_id"] == section:
            j += 1
        group = [k for k in range(i, j) if section in body_sections and segments[k]["kind"] != "note"]
        if group and any(segments[k]["kind"] != "heading" for k in group):
            counts = [token_counts[k] for k in group]
            for a, b in chunk_spans(counts):
                chunks.append(
                    (
                        segments[group[a]]["seq"],
                        segments[group[b]]["seq"],
                        sum(counts[a : b + 1]),
                    )
                )
        i = j
    return chunks
