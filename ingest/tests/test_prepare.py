"""`swann prepare` : normalisation des métadonnées des bibliothèques numériques
(sans réseau)."""

from __future__ import annotations

from swann_ingest.prepare.layout import access, slugify
from swann_ingest.prepare.persons import Person, initials_match
from swann_ingest.prepare.sources import Draft, Name, filename_author, non_literary, split_name
from swann_ingest.prepare.works import Edition, Work


def test_split_name_wikisource():
    n = split_name("автор Константин Дмитриевич Бальмонт (1867—1942)")
    assert (n.name, n.birth, n.death) == ("Константин Дмитриевич Бальмонт", 1867, 1942)
    assert split_name("Гай Валерий Катулл (ок. 87 до н. э.— ок. 54 до н. э.").name == "Гай Валерий Катулл"
    assert split_name("Dostoevsky, Fyodor").name == "Fyodor Dostoevsky"


def test_filename_author():
    assert filename_author("Alexandre-Dumas-Lhoroscope-fr", "L'horoscope") == "Alexandre Dumas"
    assert filename_author("Kipling-Capitaines-courageux-fr-(2)", "Capitaines courageux") == "Kipling"
    assert filename_author("Quelquechose-fr", "Autre titre") is None


def test_initials():
    assert initials_match("M. A. Kuzmin", "Mikhail Kuzmin")
    assert not initials_match("M. A. Kuzmin", "J.A. Kuzmin")
    assert initials_match("Herbert Spencer", "Herbert Spencer")


def test_non_literary():
    draft = Draft("f.epub", "x", "wikisource", None, "Об утверждении Положения", "ru", [], [])
    assert non_literary(draft)
    draft.title = "Евгений Онегин"
    assert non_literary(draft) is None
    # Titre officiel mais texte signé : il reste dans l'œuvre de l'auteur
    signed = Draft("f.epub", "x", "wikisource", None, "Резолюция", "ru", [Name("Гавриил Державин")], [])
    assert non_literary(signed) is None


def test_slugify():
    assert slugify("Dostoïevski, Fiodor") == "dostoievski-fiodor"
    assert slugify("Ślizgawka łódź") == "slizgawka-lodz"
    assert slugify("Преступление и наказание") == "prestuplenie-i-nakazanie"


def _edition(source: str, license: str, language: str, authors=(), translators=()) -> Edition:
    draft = Draft("f.epub", "x", source, None, "T", language, [], [], license=license)
    record = {"text": {"body_chars": 1000}}
    return Edition(draft, record, language, list(authors), list(translators))


def test_access_rules():
    persons = {
        "old": Person("old", display="Ancien", death=1900),
        "new": Person("new", display="Récent", death=1990),
        "nodate": Person("nodate", display="Inconnu"),
    }
    work = Work([], original_language="ru")
    # Licence libre déclarée : ouvert
    assert access(_edition("wolnelektury", "free", "pl"), work, persons)[0] == "open"
    # Traduction sans traducteur nommé : prudence
    assert access(_edition("textos", "unknown", "es", ["old"]), work, persons)[0] == "excerpt"
    # Original, auteur mort depuis plus de 70 ans
    assert access(_edition("gutenberg", "pd-us", "ru", ["old"]), work, persons)[0] == "open"
    # Traducteur mort trop récemment, ou date inconnue
    assert access(_edition("gutenberg", "pd-us", "fr", ["old"], ["new"]), work, persons)[0] == "excerpt"
    assert access(_edition("beq", "unknown", "ru", ["nodate"]), work, persons)[0] == "excerpt"
    assert access(_edition("gutenberg", "copyrighted", "ru", ["old"]), work, persons)[0] == "excerpt"


def test_match_content_merges_translation_of_same_author():
    import numpy as np

    from swann_ingest.prepare.works import match_content

    def edition(file, language, author, paragraphs, chars=5000):
        draft = Draft(file, file, "x", None, file, language, [], [])
        record = {"text": {"body_chars": chars, "head": paragraphs, "sample": []}}
        return Edition(draft, record, language, [author], [])

    story = [f"Paragraphe numéro {i} de la nouvelle, assez long pour compter." for i in range(6)]
    other = [f"Un tout autre récit, paragraphe {i}, sans rapport avec la nouvelle." for i in range(6)]
    ru = Work([edition("ru.epub", "ru", "chekhov-ru", story)], qid="Q1")
    es = Work([edition("es.epub", "es", "chekhov-es", story)])
    es_other = Work([edition("es2.epub", "es", "chekhov-es", other)])
    works = [ru, es, es_other]
    identity = {"chekhov-ru": "Q5", "chekhov-es": "Q5"}

    # Faux encodeur : un même paragraphe (traduit) donne le même vecteur
    vocab = {t: i for i, t in enumerate(story + other)}

    def encode(texts):
        out = np.zeros((len(texts), len(vocab)), dtype=np.float32)
        for row, t in enumerate(texts):
            out[row, vocab[t]] = 1.0
        return out

    merged = match_content(works, identity, encode, lambda _: None)
    assert len(merged) == 1
    assert works == [ru, es_other]
    assert {e.language for e in ru.editions} == {"ru", "es"}


def test_split_name_strips_note_calls_and_qualifiers():
    assert split_name("Александр Львович Парвус (1867–1924)[1]").name == "Александр Львович Парвус"
    assert split_name("Фридрих Давид Гретер[ВТ 1]").name == "Фридрих Давид Гретер"
    assert split_name("Aleksander Dumas (ojciec)").name == "Aleksander Dumas"
    assert split_name("graf Leo Tolstoy").name == "Leo Tolstoy"
    assert split_name("К. Р.").name == "К. Р."
