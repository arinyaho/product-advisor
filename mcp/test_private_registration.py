"""Private bibliographic metadata must not be baked into public source adapters."""

import json
import copy

import pytest

from book_library import Catalog, MI, FS, digest, registered_metadata
from test_book_library import fixture
import library_cli


def private_catalog():
    books = copy.deepcopy(list(fixture().books.values()))
    for book, identity in zip(books, ("private-source-one", "private-source-two")):
        old = book["book_id"]
        book["book_id"] = identity
        for section in book["sections"]:
            section["id"] = str(section["id"]).replace(old, identity) if isinstance(section["id"], str) else section["id"]
        for chunk in book["chunks"]:
            chunk["book_id"] = identity
            if isinstance(chunk["section_id"], str):
                chunk["section_id"] = chunk["section_id"].replace(old, identity)
    return Catalog(books)


def test_private_ids_keep_numeric_sections_and_public_aliases():
    catalog = private_catalog()
    assert catalog.section(7)[0]["book_id"] == "private-source-one"
    assert catalog.resolve_book_id(MI) == "private-source-one"
    assert catalog.resolve_book_id(FS) == "private-source-two"
    assert catalog.resolve_book_id("private-source-two") == "private-source-two"
    assert catalog.section(FS + ":s001", FS)[1]["id"] == "private-source-two:s001"
    assert catalog.list_sections(FS)["sections"][0]["book_id"] == "private-source-two"
    assert catalog.read(FS + ":s001", FS, None, "full")["section_id"] == "private-source-two:s001"
    with pytest.raises(ValueError, match="Unknown book_id"):
        catalog.resolve_book_id("unregistered-source")


def test_private_registration_supplies_metadata_and_verifies_source_hash(tmp_path):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"synthetic source bytes")
    registration = {"book_id": "private-source-one", "title": "Private title", "authors": ["Private author"], "edition": "Verified privately", "metadata_evidence": {"title_pdf_page": 1}, "sha256": digest(source.read_bytes())}
    actual = registered_metadata(source, registration)
    assert actual["book_id"] == registration["book_id"]
    assert actual["title"] == registration["title"]
    assert actual["authors"] == registration["authors"]
    assert actual["metadata_evidence"] == registration["metadata_evidence"]
    source.write_bytes(b"changed source bytes")
    with pytest.raises(ValueError, match="Source differs"):
        registered_metadata(source, registration)


def test_source_registration_rejects_duplicate_ids_before_reading_files(tmp_path):
    path = tmp_path / "private-registration.json"
    path.write_text(json.dumps({"sources": [{"book_id": "same"}] * 3}))
    with pytest.raises(ValueError, match="three distinct"):
        library_cli.source_books(tmp_path, path)
