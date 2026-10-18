"""Ouverture d'un EPUB : conteneur zip, OPF (métadonnées, manifeste, spine),
détection des DRM."""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urldefrag

from lxml import etree
from lxml import html as lxml_html

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
}

# Obfuscation des polices : autorisée, ce n'est pas un DRM sur le texte.
FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

XML_PARSER = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True, no_network=True)
HTML_PARSER = lxml_html.HTMLParser(recover=True, no_network=True, huge_tree=True)
TAG_RE = re.compile(rb"<[^>]*>")


class EpubError(Exception):
    pass


class DrmError(EpubError):
    pass


@dataclass
class ManifestItem:
    id: str
    path: str  # chemin dans le zip
    media_type: str
    properties: set[str]


@dataclass
class OpfMetadata:
    titles: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    creators: list[tuple[str, str | None]] = field(default_factory=list)  # (nom, rôle)
    publisher: str | None = None
    date: str | None = None


@dataclass
class Epub:
    path: Path
    sha256: str
    version: str
    opf_path: str
    metadata: OpfMetadata
    manifest: dict[str, ManifestItem]  # par id
    spine: list[ManifestItem]
    nav_path: str | None
    ncx_path: str | None
    guide: list[tuple[str, str]]  # (type, chemin#fragment)
    _zip: zipfile.ZipFile

    def read(self, path: str) -> bytes:
        try:
            return self._zip.read(path)
        except KeyError as e:
            raise EpubError(f"fichier absent de l'EPUB : {path}") from e

    def parse_xml(self, path: str) -> etree._Element:
        data = self.read(path)
        root = etree.fromstring(data, XML_PARSER)
        if root is None or _lost_text(root, data):
            # XHTML mal formé (balises fermantes orphelines après <body>, vu
            # sur des EPUB Gutenberg retouchés) : le mode recover du parseur
            # XML ferme le document trop tôt, le parseur HTML le lit en entier.
            html_root = lxml_html.fromstring(data, parser=HTML_PARSER)
            if html_root is not None and (root is None or _text_len(html_root) > _text_len(root)):
                root = html_root
        if root is None:
            raise EpubError(f"XML illisible : {path}")
        return root

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> Epub:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _text_len(root: etree._Element) -> int:
    return sum(len(t.strip()) for t in root.itertext())


def _lost_text(root: etree._Element, data: bytes) -> bool:
    """Le texte lu est-il bien plus court que le texte brut du fichier ?"""
    body = data.split(b"<body", 1)[-1]
    raw = len(b" ".join(TAG_RE.sub(b" ", body).split()))
    return raw > 500 and _text_len(root) < raw * 0.5


def resolve_href(base_path: str, href: str) -> tuple[str, str | None]:
    """Résout un lien relatif à un fichier du zip -> (chemin, fragment)."""
    url, frag = urldefrag(href)
    url = unquote(url)
    if not url:
        return base_path, frag or None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base_path), url))
    return path, frag or None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_drm(zf: zipfile.ZipFile) -> None:
    names = set(zf.namelist())
    if "META-INF/rights.xml" in names:
        raise DrmError("EPUB protégé par DRM (META-INF/rights.xml)")
    if "META-INF/encryption.xml" not in names:
        return
    root = etree.fromstring(zf.read("META-INF/encryption.xml"), XML_PARSER)
    if root is None:
        return
    for data in root.iter(f"{{{NS['enc']}}}EncryptedData"):
        method = data.find("enc:EncryptionMethod", NS)
        algorithm = method.get("Algorithm") if method is not None else None
        if algorithm in FONT_OBFUSCATION:
            continue
        ref = data.find(".//enc:CipherReference", NS)
        uri = ref.get("URI", "") if ref is not None else ""
        raise DrmError(f"EPUB protégé par DRM (fichier chiffré : {uri or '?'})")


def open_epub(path: Path) -> Epub:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise EpubError(f"archive zip invalide : {e}") from e
    try:
        _check_drm(zf)
        container = etree.fromstring(zf.read("META-INF/container.xml"), XML_PARSER)
        rootfile = container.find(".//container:rootfile", NS)
        if rootfile is None or not rootfile.get("full-path"):
            raise EpubError("META-INF/container.xml sans rootfile")
        opf_path = rootfile.get("full-path")
        opf = etree.fromstring(zf.read(opf_path), XML_PARSER)
    except KeyError as e:
        zf.close()
        raise EpubError(f"fichier obligatoire absent : {e}") from e
    except Exception:
        zf.close()
        raise

    version = opf.get("version", "2.0")
    metadata = _parse_metadata(opf)

    manifest: dict[str, ManifestItem] = {}
    for item in opf.iterfind("opf:manifest/opf:item", NS):
        item_id, href = item.get("id"), item.get("href")
        if not item_id or not href:
            continue
        item_path, _ = resolve_href(opf_path, href)
        manifest[item_id] = ManifestItem(
            id=item_id,
            path=item_path,
            media_type=item.get("media-type", ""),
            properties=set((item.get("properties") or "").split()),
        )

    spine_el = opf.find("opf:spine", NS)
    spine: list[ManifestItem] = []
    ncx_path = None
    if spine_el is not None:
        for ref in spine_el.iterfind("opf:itemref", NS):
            item = manifest.get(ref.get("idref", ""))
            if item and "html" in item.media_type:
                spine.append(item)
        toc_id = spine_el.get("toc")
        if toc_id and toc_id in manifest:
            ncx_path = manifest[toc_id].path
    if ncx_path is None:
        ncx_path = next(
            (i.path for i in manifest.values() if i.media_type == "application/x-dtbncx+xml"), None
        )
    nav_path = next((i.path for i in manifest.values() if "nav" in i.properties), None)

    guide = []
    for ref in opf.iterfind("opf:guide/opf:reference", NS):
        if ref.get("href") and ref.get("type"):
            p, frag = resolve_href(opf_path, ref.get("href"))
            guide.append((ref.get("type").lower(), p + (f"#{frag}" if frag else "")))

    if not spine:
        zf.close()
        raise EpubError("spine vide : aucun contenu à lire")

    return Epub(
        path=path,
        sha256=sha256_file(path),
        version=version,
        opf_path=opf_path,
        metadata=metadata,
        manifest=manifest,
        spine=spine,
        nav_path=nav_path,
        ncx_path=ncx_path,
        guide=guide,
        _zip=zf,
    )


def _text(el: etree._Element | None) -> str | None:
    if el is None:
        return None
    text = " ".join("".join(el.itertext()).split())
    return text or None


def _parse_metadata(opf: etree._Element) -> OpfMetadata:
    md = OpfMetadata()
    meta = opf.find("opf:metadata", NS)
    if meta is None:
        return md
    # EPUB 3 : rôles via <meta refines="#id" property="role">
    roles = {
        m.get("refines", "").lstrip("#"): (m.text or "").strip()
        for m in meta.iterfind("opf:meta", NS)
        if m.get("property") == "role"
    }
    for el in meta.iterfind("dc:title", NS):
        if t := _text(el):
            md.titles.append(t)
    for el in meta.iterfind("dc:language", NS):
        if t := _text(el):
            md.languages.append(t)
    for tag in ("creator", "contributor"):
        for el in meta.iterfind(f"dc:{tag}", NS):
            name = _text(el)
            if not name:
                continue
            role = el.get(f"{{{NS['opf']}}}role") or roles.get(el.get("id", ""))
            md.creators.append((name, role or ("aut" if tag == "creator" else None)))
    md.publisher = _text(meta.find("dc:publisher", NS))
    md.date = _text(meta.find("dc:date", NS))
    return md
