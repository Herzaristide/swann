"""Préparation d'un lot d'EPUB en vrac (bibliothèques numériques libres) en un
dossier books/ prêt pour `swann extract` (`swann prepare`).

Étapes, chacune mise en cache (cache.sqlite) et relançable :
1. inventaire : métadonnées OPF, mentions de licence, lecture du texte
   (langue détectée, longueur, extraits) ;
2. normalisation par source (Wikisource, Wolne Lektury, Gutenberg,
   textos.info, Bibliothèque électronique du Québec…) ;
3. personnes : rattachement à Wikidata, nom d'usage unique ;
4. œuvres : rattachement à Wikidata, regroupement des éditions (traductions
   comprises), langue originale, doublons ;
5. droits (`access`) ;
6. écriture : books/<auteur>/<œuvre>/work.toml + liens durs vers les EPUB,
   books/persons.toml, rapport des cas à vérifier, quarantaine.
"""
