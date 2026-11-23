"""Verify source membership and graph provenance before sealing an immutable release."""

import argparse
import json
from pathlib import Path

import networkx as nx

from book_library import Catalog, atomic_json, chunk_identity, digest, rendered_chunk
from library_index import DIMENSION, FIXED_CONFIG, RETRIEVAL_CONFIG, RETRIEVAL_VERSION


def seal(directory):
    directory = Path(directory)
    catalog = Catalog.load(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    ready = json.loads((directory / "ready.json").read_text())
    assert manifest["config"] == ready["config"] == FIXED_CONFIG
    assert manifest["catalog_sha256"] == digest((directory / "catalog.json").read_bytes())
    for chunk in catalog.chunks.values():
        catalog.verify_chunk(chunk)
    hashes, reports = {}, {}
    for scope in [*catalog.books, "all"]:
        base = directory / "indices" / scope / (manifest["version"] + "-" + scope)
        chunks = json.loads((base / "kv_store_text_chunks.json").read_text())
        expected = {key for key, chunk in catalog.chunks.items() if scope == "all" or chunk["book_id"] == scope}
        actual = {chunk_identity(row["content"]) for row in chunks.values()}
        assert actual == expected, (scope, "Source membership differs")
        for row in chunks.values():
            assert rendered_chunk(catalog.chunks[chunk_identity(row["content"])]) == row["content"]
        graph = nx.read_graphml(base / "graph_chunk_entity_relation.graphml")
        assert graph.number_of_nodes() > 0 and graph.number_of_edges() > 0
        for name, attributes in [*graph.nodes(data=True), *((source + " -> " + target, data) for source, target, data in graph.edges(data=True))]:
            assert set(attributes["source_id"].split("<SEP>")) <= set(chunks), (scope, "Graph source missing")
            if scope != "all":
                assert name.startswith(scope + "::"), (scope, "Graph book scope differs")
        shared = [name for name in graph if name.startswith("Shared concept label:")]
        for name in shared:
            assert len({neighbor.split("::", 1)[0] for neighbor in graph.neighbors(name)}) >= 2
        for name in ("vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"):
            vectors = json.loads((base / name).read_text())
            assert vectors["embedding_dim"] == DIMENSION
            if name == "vdb_chunks.json":
                assert {row["__id__"] for row in vectors["data"]} == set(chunks)
        for name in ("kv_store_text_chunks.json", "graph_chunk_entity_relation.graphml", "vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"):
            path = base / name
            hashes[str(path.relative_to(directory))] = digest(path.read_bytes())
        reports[scope] = {"chunks": len(actual), "entities": graph.number_of_nodes(), "relationships": graph.number_of_edges(), "shared_labels": len(shared)}
    ready["file_sha256"] = hashes
    ready["retrieval_config"] = RETRIEVAL_CONFIG
    ready["retrieval_version"] = RETRIEVAL_VERSION
    ready["source_verification"] = reports
    atomic_json(directory / "ready.json", ready)
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    print(json.dumps(seal(parser.parse_args().directory)))
