"""Fragments SQL partagés : libellés selon la langue d'affichage, droits.

Paramètres nommés attendus : %(lang)s (langue d'affichage ou NULL) et
%(access)s (valeurs d'`editions.access` visibles pour l'appelant).

Correspondance de langue : exacte d'abord, puis sur la langue de base
("fr-CA" accepte "fr").
"""


def lang_match(column: str) -> str:
    return (
        f"%(lang)s::text IS NOT NULL AND ({column} = %(lang)s "
        f"OR split_part({column}, '-', 1) = split_part(%(lang)s::text, '-', 1))"
    )


def lang_order(column: str) -> str:
    return f"({column} = %(lang)s) DESC"


# Titre de l'œuvre `w` dans la langue d'affichage : à joindre avec
#   LEFT JOIN LATERAL (WORK_TITLE) wt ON true
# puis coalesce(wt.title, w.title), coalesce(wt.language, w.original_language).
WORK_TITLE = f"""
    SELECT t.title, t.language FROM work_titles t
    WHERE t.work_id = w.id AND {lang_match("t.language")}
    ORDER BY {lang_order("t.language")}, t.title LIMIT 1
"""


def person_name(alias: str = "p") -> str:
    return f"""coalesce(
        (SELECT pn.name FROM person_names pn
         WHERE pn.person_id = {alias}.id AND {lang_match("pn.language")}
         ORDER BY {lang_order("pn.language")}, pn.name LIMIT 1),
        {alias}.display_name)"""


def movement_label(alias: str = "m") -> str:
    return f"""coalesce(
        (SELECT ml.label FROM movement_labels ml
         WHERE ml.movement_id = {alias}.id AND {lang_match("ml.language")}
         ORDER BY {lang_order("ml.language")} LIMIT 1),
        {alias}.slug)"""


def authors_json(work: str = "w") -> str:
    """Auteurs de l'œuvre, dans l'ordre, en JSON."""
    return f"""(SELECT coalesce(json_agg(json_build_object(
                    'id', p.id, 'name', {person_name()},
                    'birth_year', p.birth_year, 'death_year', p.death_year) ORDER BY wa.position), '[]')
                FROM work_authors wa JOIN persons p ON p.id = wa.person_id
                WHERE wa.work_id = {work}.id)"""


def movements_json(work: str = "w") -> str:
    return f"""(SELECT coalesce(json_agg(json_build_object(
                    'id', m.id, 'slug', m.slug, 'label', {movement_label()}) ORDER BY m.slug), '[]')
                FROM work_movements wm JOIN movements m ON m.id = wm.movement_id
                WHERE wm.work_id = {work}.id)"""


def translators_json(edition: str = "e") -> str:
    return f"""(SELECT coalesce(json_agg(json_build_object('id', p.id, 'name', {person_name()})
                    ORDER BY c.position), '[]')
                FROM edition_contributors c JOIN persons p ON p.id = c.person_id
                WHERE c.edition_id = {edition}.id AND c.role = 'translator')"""


def visible(edition: str = "e") -> str:
    """L'édition est visible par l'appelant (droits `access`) et n'est pas à
    la corbeille (une œuvre à la corbeille y met toutes ses éditions)."""
    return f"({edition}.access::text = ANY(%(access)s) AND {edition}.deleted_at IS NULL)"


def work_visible(work: str = "w") -> str:
    return f"EXISTS (SELECT 1 FROM editions ve WHERE ve.work_id = {work}.id AND {visible('ve')})"


# Fiche résumée d'une édition (EditionSummary), pour l'alias `e`.
EDITION_SUMMARY = f"""
    e.id, e.title, e.language, e.is_original, {translators_json()} AS translators,
    e.publisher, e.year, e.access::text AS access, e.revision,
    (SELECT json_build_object('status', ea.status::text, 'aligned_ratio', ea.aligned_ratio)
     FROM edition_alignments ea WHERE ea.edition_id = e.id) AS alignment
"""
