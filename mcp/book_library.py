"""Source-preserving PDF/EPUB catalog; runtime content is kept outside the image."""

from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

import pymupdf

from advisor_graph import footer_only_chunks


PARSER_VERSION = "three-books-1"
MI = "book-a"
FS = "book-b"
DE = "book-c"
SOURCE_PREFIX = "@product-advisor-source "


def digest(value):
    return hashlib.sha256(value).hexdigest()


def clean(value):
    return unicodedata.normalize("NFKC", value).strip()


def words(value):
    return " ".join(clean(value).split())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def rendered_chunk(chunk):
    return SOURCE_PREFIX + json.dumps({"chunk_id": chunk["chunk_id"], "book_id": chunk["book_id"]}, separators=(",", ":")) + "\n" + chunk["text"]


def chunk_identity(content):
    if not content.startswith(SOURCE_PREFIX):
        raise ValueError("Retrieved chunk has no source identity")
    return json.loads(content.split("\n", 1)[0][len(SOURCE_PREFIX):])["chunk_id"]


def embedding_text(content):
    return content.split("\n", 1)[1] if content.startswith(SOURCE_PREFIX) else content


def evidence_exclusion(chunk):
    """Keep navigation readable, but do not present it as source evidence."""
    normalize = lambda text: re.sub(r"[\W_]+", " ", clean(text or "").casefold()).strip()
    headings = {normalize(chunk.get(key)) for key in ("section", "chapter")} - {""}
    if headings & {"index", "contents", "table of contents"}:
        return "navigation"
    text = normalize(chunk["text"])
    if not text:
        return "empty"
    if text in headings:
        return "heading_only"
    return None


def _chunk(book, section, text, location):
    identity = f"{book['book_id']}:{book['sha256']}:{PARSER_VERSION}:{section['id']}:{json.dumps(location, sort_keys=True)}:{digest(text.encode())}"
    return {"chunk_id": "src-" + digest(identity.encode())[:24], "book_id": book["book_id"], "section_id": section["id"], "section": section["title"], "chapter": section.get("chapter", section["title"]), "text": text, "location": location}


def _metadata(path, book_id, title, authors, edition, evidence):
    return {"book_id": book_id, "title": title, "authors": authors, "edition": edition, "format": path.suffix.lower()[1:], "sha256": digest(path.read_bytes()), "source_filename": path.name, "parser_version": PARSER_VERSION, "metadata_evidence": evidence, "sections": [], "units": [], "chunks": []}


def monetizing(path, legacy):
    if digest(path.read_bytes()) != digest(Path(legacy.BOOK_PDF).read_bytes()):
        raise ValueError("Provided Reference A file differs from the legacy source; compatibility must be reviewed")
    copyright_page = legacy.PAGES[8]
    if "Authorized India Edition" not in copyright_page or "2016" not in copyright_page:
        raise ValueError("Reference A copyright evidence changed")
    book = _metadata(path, MI, "Reference A: Private reference material", ["Author A1", "Author A2"], "Authorized India Edition; copyright 2016; publication year not verified", {"title_pdf_page": 7, "copyright_pdf_page": 8, "isbn": ["private-isbn", "private-isbn"], "publication_year_verified": False})
    book["sections"] = [{**section, "book_id": MI, "chapter": next((s["title"] for s in reversed(legacy.SECTIONS) if s["level"] == 2 and s["start"] <= section["start"]), section["title"])} for section in legacy.SECTIONS]
    sections = {section["id"]: section for section in book["sections"]}
    for number, text in legacy.PAGES.items():
        book["units"].append({"unit_id": f"pdf:{number}", "text": text, "pdf_page": number, "printed_page": None})
    offsets = Counter()
    for index, chunk in enumerate(legacy.CHUNKS):
        if (chunk.page, chunk.text) in legacy.FOOTER_ONLY_CHUNKS:
            continue
        page_text = words(legacy.PAGES[chunk.page])
        text = words(chunk.text)
        at = page_text.find(text, offsets[chunk.page])
        if at < 0:
            raise ValueError(f"Original chunk cannot be located: {index}")
        offsets[chunk.page] = at + len(text)
        location = {"kind": "pdf", "pdf_page": chunk.page, "printed_page": None, "normalized_char_start": at, "normalized_char_end": at + len(text)}
        book["chunks"].append(_chunk(book, sections[chunk.section], chunk.text, location))
    book["excluded_footer_chunks"] = len(legacy.CHUNKS) - len(book["chunks"])
    return book


def founding(path):
    document = pymupdf.open(path)
    title = words(document[0].get_text())
    if not title.startswith("Reference B:"):
        raise ValueError("Reference B title page did not match")
    book = _metadata(path, FS, title, ["Author B"], "Unnumbered PDF text edition; publication year and edition not verified", {"title_pdf_page": 1, "author_source": "user supplied; no author credit found inside PDF", "publication_year_verified": False, "pdf_creation_date_is_not_publication_date": True})
    toc_text = "\n".join(document[index].get_text() for index in range(1, 5))
    entries = []
    pending = ""
    for line in toc_text.splitlines():
        line = words(line)
        if not line or line == "Table of Contents":
            continue
        match = re.match(r"(.+?)\s*\.{3,}\s*(\d+)\s*$", line)
        if match:
            heading = words(pending + " " + match[1]); pending = ""
            number = int(match[2])
            if number >= 6:
                entries.append((heading, number))
        elif not re.fullmatch(r"\d+", line):
            pending += " " + line
    if len(entries) < 40:
        raise ValueError("Reference B printed contents could not be parsed")
    chapter = "Introduction"
    for index, (heading, number) in enumerate(entries):
        level = 1 if re.match(r"Chapter \d+:|Introduction:|Part \d+:|Further Reading", heading) else 2
        if level == 1:
            chapter = heading
        book["sections"].append({"id": f"{FS}:s{index:03d}", "book_id": FS, "level": level, "title": heading, "chapter": chapter, "start": number, "end": entries[index + 1][1] if index + 1 < len(entries) else len(document)})
    raw_units = []
    for page in document:
        tables = page.find_tables().tables
        blocks = []
        for block in page.get_text("blocks", sort=True):
            if clean(block[4]) and not any(pymupdf.Rect(block[:4]).intersects(pymupdf.Rect(table.bbox)) for table in tables):
                blocks.append({"text": clean(block[4]), "bbox": list(block[:4]), "atomic": False})
        for table in tables:
            rows = [" | ".join(words(cell or "") for cell in row) for row in table.extract()]
            blocks.append({"text": "\n".join(rows), "bbox": list(table.bbox), "atomic": True})
        blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
        raw_units.append({"unit_id": f"pdf:{page.number + 1}", "pdf_page": page.number + 1, "printed_page": None, "text": "\n\n".join(block["text"] for block in blocks), "blocks": blocks})
    # Repeated margin blocks are removed only when the whole block is a running header/footer.
    from collections import namedtuple
    MarginChunk = namedtuple("MarginChunk", "page text")
    margin_candidates = [MarginChunk(page.number + 1, clean(block[4])) for page in document for block in page.get_text("blocks") if clean(block[4])]
    excluded = footer_only_chunks(document, margin_candidates)
    for unit in raw_units:
        unit["blocks"] = [block for block in unit["blocks"] if (unit["pdf_page"], block["text"]) not in excluded]
        unit["text"] = "\n\n".join(block["text"] for block in unit["blocks"])
    book["units"] = raw_units
    for section in book["sections"]:
        number = section["start"]
        normalized = words(raw_units[number - 1]["text"])
        heading = words(section["title"])
        raw = raw_units[number - 1]["text"]
        pattern = r"(?im)^[^\S\n]*" + r"\s+".join(re.escape(word) for word in heading.split()) + r"[^\S\n]*$"
        match = re.search(pattern, raw)
        if match is None:
            raise ValueError(f"Contents heading not found on PDF page {number}: {heading}")
        prefix = words(raw[:match.start()])
        at = len(prefix) + bool(prefix)
        if normalized[at:at + len(heading)].casefold() != heading.casefold():
            raise ValueError("PDF heading normalization did not preserve its source position")
        section["start_char"] = at
    for index, section in enumerate(book["sections"]):
        next_section = book["sections"][index + 1] if index + 1 < len(book["sections"]) else None
        for number in range(section["start"], section["end"] + 1):
            text = words(raw_units[number - 1]["text"])
            lo = section["start_char"] if number == section["start"] else 0
            hi = next_section["start_char"] if next_section and number == next_section["start"] else len(text)
            if hi <= lo:
                continue
            pieces = []
            cursor = 0
            for block in raw_units[number - 1]["blocks"]:
                value = words(block["text"])
                left, right = max(lo, cursor), min(hi, cursor + len(value))
                if right > left:
                    pieces.append({"text": text[left:right], "start": left, "end": right, "atomic": block["atomic"]})
                cursor += len(value) + 1
            pending = []
            def flush():
                if pending:
                    left, right = pending[0]["start"], pending[-1]["end"]
                    _append_text_chunks(book, section, text[left:right], {"kind": "pdf", "pdf_page": number, "printed_page": None, "normalized_char_start": left}, atomic=len(pending) == 1 and pending[0]["atomic"])
                    pending.clear()
            for piece in pieces:
                if pending and (piece["atomic"] or sum(len(item["text"].split()) for item in pending) + len(piece["text"].split()) > 200):
                    flush()
                pending.append(piece)
                if piece["atomic"]:
                    flush()
            flush()
    book["excluded_footer_chunks"] = len(excluded)
    return book


def _append_text_chunks(book, section, text, location, atomic=False):
    matches = list(re.finditer(r"\S+", text))
    if not matches:
        return
    size = len(matches) if atomic else 200
    for index in range(0, len(matches), size):
        start = matches[index].start()
        end = matches[min(index + size, len(matches)) - 1].end()
        position = {**location, "word_start": index, "word_end": min(index + size, len(matches))}
        if location["kind"] == "pdf":
            position["normalized_char_start"] = location["normalized_char_start"] + start
            position["normalized_char_end"] = location["normalized_char_start"] + end
        book["chunks"].append(_chunk(book, section, text[start:end], position))


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _html_text(element):
    if _tag(element) in ("script", "style"):
        return ""
    if _tag(element) == "img":
        return " [Image: " + (element.get("alt") or "no text description supplied") + "; source=" + element.get("src", "") + "] "
    output = element.text or ""
    for child in element:
        output += _html_text(child) + (child.tail or "")
        if _tag(child) in ("p", "li", "tr", "br"):
            output += "\n"
    return clean(output)


def entrepreneurship(path):
    with zipfile.ZipFile(path) as archive:
        text_members = [item for item in archive.infolist() if PurePosixPath(item.filename).suffix.lower() in (".xhtml", ".html", ".xml", ".opf", ".ncx")]
        if sum(item.file_size for item in text_members) > 32_000_000 or any(item.file_size > 8_000_000 for item in text_members):
            raise ValueError("EPUB text exceeds the 32 MB total or 8 MB per-document limit")
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        opf = next(element.get("full-path") for element in container.iter() if _tag(element) == "rootfile")
        package = ET.fromstring(archive.read(opf)); base = posixpath.dirname(opf)
        metadata = next(element for element in package if _tag(element) == "metadata")
        titles = [element.text for element in metadata if _tag(element) == "title"]
        authors = [element.text for element in metadata if _tag(element) == "creator"]
        if "Author C" not in authors or "Expanded" not in titles[0]:
            raise ValueError("EPUB title/author evidence did not match")
        manifest = {element.get("id"): element.attrib for element in package.iter() if _tag(element) == "item"}
        nav = next(item for item in manifest.values() if "nav" in item.get("properties", "").split())
        navigation = ET.fromstring(archive.read(posixpath.join(base, nav["href"])))
        book = _metadata(path, DE, ": ".join(titles), authors, "Expanded & Updated; 10th-anniversary edition; 2024", {"package": opf, "publication_date": next((element.text for element in metadata if _tag(element) == "date"), None), "isbn": "private-isbn", "copyright_document": posixpath.join(base, "f02.xhtml"), "edition_evidence": "Navigation/preface: Expanded and Enhanced 10th-Anniversary Edition"})
        nav_root = next(element for element in navigation.iter() if _tag(element) == "nav" and "toc" in " ".join(element.attrib.values()))
        def walk_nav(element, level=0):
            for child in element:
                if _tag(child) == "ol":
                    walk_nav(child, level + 1)
                elif _tag(child) == "li":
                    link = next((node for node in child if _tag(node) == "a"), None)
                    if link is not None:
                        parsed = urlsplit(link.get("href", ""))
                        book["sections"].append({"id": f"{DE}:s{len(book['sections']):03d}", "book_id": DE, "level": level, "title": words(_html_text(link)), "href": posixpath.normpath(posixpath.join(base, unquote(parsed.path))), "anchor": unquote(parsed.fragment)})
                    walk_nav(child, level)
        walk_nav(nav_root)
        skip = {"Cover", "Table of Contents", "Title Page", "Copyright", "Dedication", "Index", "About the Author", "End User License Agreement"}
        all_blocks = []
        for itemref in (element for element in package.iter() if _tag(element) == "itemref"):
            item = manifest[itemref.get("idref")]
            if item.get("media-type") != "application/xhtml+xml" or "nav" in item.get("properties", ""):
                continue
            href = posixpath.normpath(posixpath.join(base, item["href"]))
            root = ET.fromstring(archive.read(href))
            body = next(element for element in root.iter() if _tag(element) == "body")
            blocks = []
            def visit(element):
                tag = _tag(element)
                if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "figure", "blockquote", "pre", "img"):
                    value = _html_text(element)
                    if tag == "table":
                        rows = [" | ".join(words(_html_text(cell)) for cell in row if _tag(cell) in ("td", "th")) for row in element.iter() if _tag(row) == "tr"]
                        value = "\n".join(row for row in rows if row)
                    if value:
                        anchors = [node.get("id") for node in element.iter() if node.get("id")]
                        blocks.append({"text": value, "anchors": anchors, "block_type": tag})
                else:
                    before = len(blocks)
                    for child in element:
                        visit(child)
                    if element.get("id") and len(blocks) > before:
                        blocks[before]["anchors"].insert(0, element.get("id"))
            visit(body)
            for index, block in enumerate(blocks):
                all_blocks.append({**block, "href": href, "block": index, "unit_id": f"epub:{href}:block:{index}", "printed_page": None})
        book["sections"] = [section for section in book["sections"] if section["title"] not in skip]
        for section in book["sections"]:
            candidates = [index for index, block in enumerate(all_blocks) if block["href"] == section["href"] and (section["anchor"] in block["anchors"] if section["anchor"] else True)]
            if not candidates:
                raise ValueError(f"EPUB navigation target is missing: {section['href']}#{section['anchor']}")
            section["start_block"] = candidates[0]
        book["sections"].sort(key=lambda section: (section["start_block"], section["level"]))
        book["units"] = all_blocks
        for index, section in enumerate(book["sections"]):
            next_section = book["sections"][index + 1] if index + 1 < len(book["sections"]) else None
            section["end_block"] = next_section["start_block"] if next_section else len(all_blocks)
            section["chapter"] = next((s["title"] for s in reversed(book["sections"][:index + 1]) if s["level"] == 1), section["title"])
            if section["title"] in skip:
                continue
            pending = []
            def flush():
                if not pending:
                    return
                first, last = pending[0], pending[-1]
                location = {"kind": "epub", "href": first["href"], "anchor": first["anchors"][0] if first["anchors"] else None, "block": first["block"], "block_end": last["block"], "printed_page": None}
                _append_text_chunks(book, section, "\n\n".join(block["text"] for block in pending), location, atomic=len(pending) == 1 and first["block_type"] in ("table", "figure", "blockquote"))
                pending.clear()
            for block in all_blocks[section["start_block"]:section["end_block"]]:
                atomic = block["block_type"] in ("table", "figure", "blockquote")
                if pending and (block["href"] != pending[0]["href"] or atomic or sum(len(item["text"].split()) for item in pending) + len(block["text"].split()) > 200):
                    flush()
                pending.append(block)
                if atomic:
                    flush()
            flush()
        book["image_policy"] = "Preserve image references and supplied alt text; image-only content is not OCRed or invented"
        return book


def book_summary(book):
    return {key: value for key, value in book.items() if key not in ("units", "chunks", "sections")} | {"section_count": len(book["sections"]), "chunk_count": len(book["chunks"])}


class Catalog:
    def __init__(self, books):
        self.books = {book["book_id"]: book for book in books}
        self.chunks = {chunk["chunk_id"]: chunk for book in books for chunk in book["chunks"]}
        if len(self.chunks) != sum(len(book["chunks"]) for book in books):
            raise ValueError("Chunk identity collision")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        catalog = cls(json.loads((directory / "catalog.json").read_text())["books"])
        for book in catalog.books.values():
            source = directory / "sources" / (book["book_id"] + "." + book["format"])
            if digest(source.read_bytes()) != book["sha256"]:
                raise ValueError(f"Source hash changed: {book['book_id']}")
        return catalog

    def section(self, identifier, book_id=None):
        selected = book_id or (MI if isinstance(identifier, int) else str(identifier).split(":", 1)[0])
        if selected not in self.books:
            raise ValueError("Unknown book_id")
        section = next((section for section in self.books[selected]["sections"] if section["id"] == identifier), None)
        if section is None:
            raise ValueError("Unknown section id for this book")
        return self.books[selected], section

    def verify_chunk(self, chunk):
        book = self.books[chunk["book_id"]]
        location = chunk["location"]
        if location["kind"] == "pdf":
            unit = next(unit for unit in book["units"] if unit.get("pdf_page") == location["pdf_page"])
            actual = words(unit["text"])[location["normalized_char_start"]:location["normalized_char_end"]]
        else:
            blocks = [unit for unit in book["units"] if unit["href"] == location["href"] and location["block"] <= unit["block"] <= location["block_end"]]
            text = " ".join(unit["text"] for unit in blocks).split()
            actual = " ".join(text[location["word_start"]:location["word_end"]])
        if words(actual) != words(chunk["text"]):
            raise ValueError(f"Chunk source location does not reproduce its text: {chunk['chunk_id']}")
        return True

    def list_sections(self, book_id=None, offset=0, limit=100):
        if book_id is not None and book_id not in self.books:
            raise ValueError("Unknown book_id")
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Use offset >= 0 and limit 1..100")
        sections = [{**section, "book_id": book["book_id"], "book_title": book["title"], "edition": book["edition"]} for book in self.books.values() if not book_id or book["book_id"] == book_id for section in book["sections"]]
        return {"sections": sections[offset:offset + limit], "total": len(sections), "next_offset": offset + limit if offset + limit < len(sections) else None}

    def read(self, identifier, book_id, start_page, scope):
        book, section = self.section(identifier, book_id)
        position = book["sections"].index(section)
        selected = {section["id"]}
        for child in book["sections"][position + 1:]:
            if child["level"] <= section["level"]:
                break
            selected.add(child["id"])
        chunks = [chunk for chunk in book["chunks"] if chunk["section_id"] in selected]
        start, word_offset = 0, 0
        if isinstance(start_page, int):
            if book["format"] != "pdf":
                raise ValueError("EPUB has no PDF page numbers; use the returned continuation cursor")
            start = next((index for index, chunk in enumerate(chunks) if chunk["location"]["pdf_page"] >= start_page), len(chunks))
        elif isinstance(start_page, str):
            match = re.fullmatch(r"chunk:(src-[a-f0-9]+):word:(\d+)", start_page)
            if not match:
                raise ValueError("Invalid source continuation cursor")
            start = next((index for index, chunk in enumerate(chunks) if chunk["chunk_id"] == match[1]), len(chunks))
            word_offset = int(match[2])
            if start == len(chunks) or word_offset >= len(chunks[start]["text"].split()):
                raise ValueError("Continuation cursor is outside this section")
        limit = 2000 if scope == "full" else 60
        text, locations, pages = [], [], set()
        remaining, continuation = limit, None
        for index in range(start, len(chunks)):
            chunk = chunks[index]
            page = chunk["location"].get("pdf_page")
            if page is not None:
                pages.add(page)
            if len(pages) > 10:
                continuation = {"id": identifier, "book_id": book["book_id"], "start_page": f"chunk:{chunk['chunk_id']}:word:0"}
                break
            offset = word_offset if index == start else 0
            tokens = chunk["text"].split()
            take = min(remaining, len(tokens) - offset)
            text.append(chunk["text"] if offset == 0 and take == len(tokens) else " ".join(tokens[offset:offset + take]))
            locations.append({"chunk_id": chunk["chunk_id"], **chunk["location"], "excerpt_word_start": offset, "excerpt_word_end": offset + take})
            remaining -= take
            if take < len(tokens) - offset:
                continuation = {"id": identifier, "book_id": book["book_id"], "start_page": f"chunk:{chunk['chunk_id']}:word:{offset + take}"}
                break
            if remaining == 0:
                if index + 1 < len(chunks):
                    continuation = {"id": identifier, "book_id": book["book_id"], "start_page": f"chunk:{chunks[index + 1]['chunk_id']}:word:0"}
                break
        return {"book_id": book["book_id"], "title": book["title"], "edition": book["edition"], "section_id": identifier, "section": section["title"], "scope": scope, "text": "\n\n".join(text), "locations": locations, "continuation": continuation, "source_sha256": book["sha256"]}
