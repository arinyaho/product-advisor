"""Prepare and build isolated library releases without changing the active service."""

import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import shutil

import numpy as np

import server
from book_library import Catalog, atomic_json, book_summary, digest, entrepreneurship, founding, monetizing
from library_index import EMBED_MODEL, FIXED_CONFIG, RETRIEVAL_CONFIG, Provider, build, close_engine, engine, preserve_source_text


def prepare(source_root):
    sources = [source_root / "source-1.pdf", source_root / "source-2.pdf", source_root / "source-3.epub"]
    books = [monetizing(sources[0], server), founding(sources[1]), entrepreneurship(sources[2])]
    verified = Catalog(books)
    for chunk in verified.chunks.values():
        verified.verify_chunk(chunk)
    catalog = {"books": books}
    version = "three-books-" + digest(json.dumps({"config": FIXED_CONFIG, "retrieval": RETRIEVAL_CONFIG, "catalog": catalog}, sort_keys=True, ensure_ascii=False).encode())[:16]
    directory = server.DATA_DIR / "libraries" / version
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sources").mkdir(exist_ok=True)
    for book, source in zip(books, sources, strict=True):
        target = directory / "sources" / (book["book_id"] + "." + book["format"])
        if not target.exists():
            shutil.copyfile(source, target)
        if digest(target.read_bytes()) != book["sha256"]:
            raise RuntimeError("Prepared source copy has the wrong hash")
    atomic_json(directory / "catalog.json", catalog)
    atomic_json(directory / "manifest.json", {"version": version, "config": FIXED_CONFIG, "catalog_sha256": digest((directory / "catalog.json").read_bytes()), "books": [book_summary(book) for book in books]})
    atomic_json(server.DATA_DIR / "library-candidate.json", {"version": version})
    print(json.dumps({"directory": str(directory), "version": version, "config": FIXED_CONFIG, "books": [book_summary(book) for book in books]}, ensure_ascii=False), flush=True)


def reuse_query_profile(source):
    """Copy a completed index only when the source, embedding, and extraction recipe match."""
    catalog = Catalog.load(source)
    old_manifest = json.loads((source / "manifest.json").read_text())
    old_ready = json.loads((source / "ready.json").read_text())
    if digest((source / "catalog.json").read_bytes()) != old_manifest["catalog_sha256"] or old_ready["config"] != old_manifest["config"]:
        raise ValueError("Source catalog or configuration differs from its manifest")
    compatible = lambda config: {key: value for key, value in config.items() if key != "rerank"}
    if compatible(old_manifest["config"]) != compatible(FIXED_CONFIG):
        raise ValueError("Cannot reuse an index with different extraction or embedding settings")
    for name, expected in old_ready["file_sha256"].items():
        if digest((source / name).read_bytes()) != expected:
            raise ValueError("The source index differs from its sealed manifest")
    raw_catalog = json.loads((source / "catalog.json").read_text())
    version = "three-books-" + digest(json.dumps({"config": FIXED_CONFIG, "retrieval": RETRIEVAL_CONFIG, "catalog": raw_catalog}, sort_keys=True, ensure_ascii=False).encode())[:16]
    target = source.parent / version
    if target.exists():
        raise ValueError("Target release already exists; inspect it rather than overwriting it")
    target.mkdir()
    shutil.copyfile(source / "catalog.json", target / "catalog.json")
    for folder in ("sources", "indices"):
        shutil.copytree(source / folder, target / folder)
    for scope in [*catalog.books, "all"]:
        base = target / "indices" / scope
        (base / (old_manifest["version"] + "-" + scope)).rename(base / (version + "-" + scope))
    manifest = {**old_manifest, "version": version, "config": FIXED_CONFIG, "derived_from": old_manifest["version"], "reuse": "identical_sources_embeddings_and_extraction; query_profile_changed"}
    ready = {**old_ready, "config": FIXED_CONFIG, "new_paid_index_calls": 0, "derived_from": old_manifest["version"]}
    ready.pop("file_sha256", None)
    atomic_json(target / "manifest.json", manifest)
    atomic_json(target / "ready.json", ready)
    atomic_json(server.DATA_DIR / "library-candidate.json", {"version": version})
    print(json.dumps({"directory": str(target), "version": version, "new_paid_index_calls": 0}), flush=True)


async def index(directory, max_chunks):
    with (directory / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        catalog = Catalog.load(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["config"] != FIXED_CONFIG:
            raise ValueError("Index recipe differs from the fixed model configuration")
        if (directory / "ready.json").exists():
            if not all((directory / "indices" / scope / "ready.json").exists() for scope in [*catalog.books, "all"]):
                raise ValueError("Completed marker has missing index scopes; inspect before rebuilding")
            from library_index import LibraryIndex
            validated = LibraryIndex(directory, server._openrouter)
            validated.loop.close()
            print(json.dumps({"status": "already_sealed", "new_paid_index_calls": 0}), flush=True)
            return
        if server.EMBED_MODEL != EMBED_MODEL:
            raise ValueError("Legacy vectors use a different embedding model; do not mix incompatible caches")
        provider = Provider(server._openrouter, directory / "build-cache", budget=5.0, max_calls=len(catalog.chunks) + 800)
        legacy = server._index()
        texts = [chunk.text for chunk in server.CHUNKS + server.EXCERPTS]
        vectors = np.concatenate(legacy)
        print(json.dumps(await build(directory, catalog, provider, texts, vectors, max_chunks=max_chunks)), flush=True)


async def restore_source(directory):
    catalog = Catalog.load(directory)
    async def no_provider(texts):
        raise AssertionError("Source restoration must not make a paid provider call")
    for scope in [*catalog.books, "all"]:
        rag = engine(directory / "indices" / scope, no_provider)
        await rag.initialize_storages()
        try:
            restored = await preserve_source_text(rag, [chunk for chunk in catalog.chunks.values() if scope == "all" or chunk["book_id"] == scope])
            print(json.dumps({"scope": scope, "restored_original_bodies": restored}), flush=True)
        finally:
            await close_engine(rag)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source-root", required=True, type=Path)
    run = sub.add_parser("build")
    run.add_argument("--directory", required=True, type=Path)
    run.add_argument("--max-chunks", type=int)
    restore = sub.add_parser("restore-source-text")
    restore.add_argument("--directory", required=True, type=Path)
    reuse = sub.add_parser("reuse-query-profile")
    reuse.add_argument("--from-directory", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.source_root)
    elif args.command == "build":
        asyncio.run(index(args.directory, args.max_chunks))
    elif args.command == "restore-source-text":
        asyncio.run(restore_source(args.directory))
    else:
        reuse_query_profile(args.from_directory)
