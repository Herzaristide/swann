"""Pages ajoutées par les bibliothèques numériques (appel aux dons, licence,
contributeurs, notice de l'auteur…) : elles ne font pas partie de l'œuvre et
fausseraient le texte, la langue détectée et l'alignement. Reconnues au nom
du fichier ET à une marque de la bibliothèque, pour ne jamais écarter un
chapitre qui s'appellerait « last.xhtml »."""

from __future__ import annotations

import posixpath
import re

from lxml import etree

RULES: list[tuple[re.Pattern[str], re.Pattern[str]]] = [
    # Wolne Lektury : page de titre, collecte de dons, soutien, page éditoriale
    (re.compile(r"^(fund\d+|support|last|title)\.x?html?$"), re.compile(r"Wolne Lektur|wolnelektury\.pl")),
    # Wikisource (export WsExport) : « À propos de cette édition » et contributeurs
    (re.compile(r"^about\.x?html?$"), re.compile(r"Wsexport|Wikisource|Викитек|Wikiźród", re.I)),
    # textos.info : informations du site, notice de l'auteur, autres textes
    (
        re.compile(r"^(info|autor|otros|portada)\.x?html?$"),
        re.compile(r"textos\.info|Otros textos de|Biblioteca digital"),
    ),
]


def is_boilerplate(path: str, root: etree._Element, epub_text: str) -> bool:
    """`epub_text` : texte de quelques documents de l'EPUB, où chercher la
    marque de la bibliothèque (elle n'est pas toujours sur la page elle-même)."""
    name = posixpath.basename(path).lower()
    for name_re, mark_re in RULES:
        if name_re.match(name):
            text = " ".join(root.itertext())
            if mark_re.search(text) or mark_re.search(epub_text):
                return True
    return False
