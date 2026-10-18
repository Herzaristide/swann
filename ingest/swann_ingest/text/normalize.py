"""Normalisation du texte : uniquement des corrections invisibles.

On ne touche jamais à la typographie (« », —, ’) ni aux espaces insécables
(U+00A0, U+202F) : le texte stocké doit rester fidèle à l'édition.
"""

import re
import unicodedata

# Césure conditionnelle, espaces de largeur nulle, BOM, word joiner.
# (U+200C/U+200D sont conservés : ils sont signifiants en persan, en hindi...)
_INVISIBLE = dict.fromkeys(map(ord, "­​⁠﻿"), None)

# Blancs "ordinaires" à fusionner (pas les insécables).
_COLLAPSIBLE = re.compile(r"[ \t\r\n\f\v  ]+")


def clean_piece(text: str) -> str:
    """NFC + suppression des invisibles + fusion des blancs en une espace."""
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    return _COLLAPSIBLE.sub(" ", text)
