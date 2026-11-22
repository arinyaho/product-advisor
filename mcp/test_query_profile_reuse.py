"""Query-only model changes reuse identical index bytes and reject embedding changes."""

import json
import asyncio
from types import SimpleNamespace

import pytest

from book_library import MI, FS, atomic_json, digest
import library_cli
from library_index import FIXED_CONFIG


def source_release(tmp_path, monkeypatch, incompatible=False):
    source = tmp_path / "old"
    source.mkdir()
    (source / "sources").mkdir()
    (source / "sources/private-source.pdf").write_bytes(b"synthetic source bytes")
    catalog = {"books": [{"book_id": MI}, {"book_id": FS}]}
    atomic_json(source / "catalog.json", catalog)
    config = {**FIXED_CONFIG, "rerank": "previous-query-only-model"}
    if incompatible:
        config["embedding"] = "incompatible-vector-space"
    version = "three-books-0123456789abcdef"
    hashes = {}
    for scope in (MI, FS, "all"):
        path = source / "indices" / scope / (version + "-" + scope) / "store.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(("unchanged native index bytes: " + scope).encode())
        hashes[str(path.relative_to(source))] = digest(path.read_bytes())
    atomic_json(source / "manifest.json", {"version": version, "config": config, "catalog_sha256": digest((source / "catalog.json").read_bytes())})
    atomic_json(source / "ready.json", {"config": config, "file_sha256": hashes})
    monkeypatch.setattr(library_cli.Catalog, "load", lambda path: SimpleNamespace(books={MI: {}, FS: {}}))
    monkeypatch.setattr(library_cli.server, "DATA_DIR", tmp_path / "data")
    def no_provider(*args, **kwargs):
        raise AssertionError("Copying a compatible index must not call the provider")
    monkeypatch.setattr(library_cli.server, "_openrouter", no_provider)
    return source, version


def test_query_only_profile_preserves_sources_and_native_index_bytes(tmp_path, monkeypatch):
    source, old_version = source_release(tmp_path, monkeypatch)
    library_cli.reuse_query_profile(source)
    version = json.loads((tmp_path / "data/library-candidate.json").read_text())["version"]
    target = tmp_path / version
    assert (target / "catalog.json").read_bytes() == (source / "catalog.json").read_bytes()
    assert (target / "sources/private-source.pdf").read_bytes() == b"synthetic source bytes"
    assert json.loads((target / "ready.json").read_text())["new_paid_index_calls"] == 0
    assert "file_sha256" not in json.loads((target / "ready.json").read_text())
    for scope in (MI, FS, "all"):
        original = source / "indices" / scope / (old_version + "-" + scope) / "store.json"
        copied = target / "indices" / scope / (version + "-" + scope) / "store.json"
        assert original.read_bytes() == copied.read_bytes()


def test_incompatible_embedding_profile_is_rejected_without_writes(tmp_path, monkeypatch):
    source, _ = source_release(tmp_path, monkeypatch, incompatible=True)
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="different extraction or embedding"):
        library_cli.reuse_query_profile(source)
    assert set(tmp_path.iterdir()) == before


def test_completed_index_is_validated_without_starting_paid_build(tmp_path, monkeypatch):
    atomic_json(tmp_path / "manifest.json", {"config": FIXED_CONFIG})
    atomic_json(tmp_path / "ready.json", {})
    atomic_json(tmp_path / "indices/all/ready.json", {})
    monkeypatch.setattr(library_cli.Catalog, "load", lambda path: SimpleNamespace(chunks={}, books={}))
    checked, closed = [], []
    class ValidatedIndex:
        def __init__(self, directory, transport):
            checked.append(directory)
            self.loop = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr("library_index.LibraryIndex", ValidatedIndex)
    def no_paid_build(*args, **kwargs):
        raise AssertionError("A completed index must not initialize a paid build")
    monkeypatch.setattr(library_cli, "Provider", no_paid_build)
    asyncio.run(library_cli.index(tmp_path, None))
    assert checked == [tmp_path] and closed == [True]
