"""Graph navigation must retain original sources without replacing chunk evidence."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lightrag.utils import sanitize_text_for_encoding
from lightrag.utils_pipeline import make_custom_chunk_id

from company_context.context import digest, search
from company_context.test_context import corpus, record


def native_id(value, chunk):
    return make_custom_chunk_id("doc-" + digest(value.records[chunk["source_id"]]), sanitize_text_for_encoding(value.render(chunk)))


def test_graph_connects_original_sources_outside_top_chunks_without_declaring_override():
    value = corpus([record(text="A proposed paid pilot for customer North."), record("meeting-2", text="North is now on hold. South has a separate grant.", record_kind="summary", derived_from=["https://example.com/meeting"])])
    first, second = value.chunks.values()
    raw = {"metadata": {"query_mode": "mix"}, "data": {
        "chunks": [{"content": value.render(first)}],
        "entities": [{"entity_name": "North", "entity_type": "organization", "description": "North discussed a pilot and was later placed on hold.", "source_id": native_id(value, first) + "<SEP>" + native_id(value, second)}],
        "relationships": [{"src_id": "North", "tgt_id": "Pilot", "description": "The proposal is not payment.", "source_id": native_id(value, first)}],
    }}
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value=raw), tokenizer=SimpleNamespace(encode=list))
    result = asyncio.run(search(value, rag, "Which opportunity changed?", k=1))
    assert result["results"] == [value.read(first["chunk_id"])]
    entity, relation = result["graph"]
    assert entity["name"] == "North" and relation["target"] == "Pilot"
    assert {r["source_id"] for r in entity["sources"]} == {"meeting-1", "meeting-2"}
    ref = next(r for r in entity["sources"] if r["source_id"] == "meeting-2")
    assert ref["record_kind"] == "summary" and ref["derived_from"] == ["https://example.com/meeting"]
    assert ref["occurred_at"] == second["occurred_at"]
    assert ref["read_source"] == {"chunk_id": second["chunk_id"]}
    assert value.read(**ref["read_source"])["text"] == second["text"]
    assert entity["evidence_status"] == "model_derived_requires_original_source_review"
    assert "supersedes" not in json.dumps(result)


def test_graph_budget_skips_oversized_descriptions_but_keeps_chunks_and_reports_omissions():
    value = corpus();chunk = next(iter(value.chunks.values()))
    rows = [{"entity_name": "Oversized", "description": "x" * 20000, "source_id": native_id(value, chunk)}, {"entity_name": "Small", "description": "A concise description.", "source_id": native_id(value, chunk)}]
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": value.render(chunk)}], "entities": rows}}), tokenizer=SimpleNamespace(encode=list))
    result = asyncio.run(search(value, rag, "Current pilot status"))
    assert [r["name"] for r in result["graph"]] == ["Small"]
    assert result["results"] == [value.read(chunk["chunk_id"])]
    assert result["graph_coverage"]["omitted"] == 1
    assert result["graph_coverage"]["tokens"] <= 6000


def test_graph_unknown_provenance_is_explicit_and_naive_does_not_return_graph():
    value = corpus();chunk = next(iter(value.chunks.values()))
    raw = {"metadata": {"query_mode": "mix"}, "data": {"chunks": [], "entities": [{"entity_name": "Partial", "description": "A derived claim.", "source_id": native_id(value, chunk) + "<SEP>not-in-snapshot"}, {"entity_name": "Untraceable", "description": "Unsupported", "source_id": "not-in-snapshot"}]}}
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value=raw), tokenizer=SimpleNamespace(encode=list))
    result = asyncio.run(search(value, rag, "Pilot context"))
    assert [r["name"] for r in result["graph"]] == ["Partial"]
    assert result["graph"][0]["unresolved_source_count"] == 1
    assert result["graph_coverage"]["untraceable"] == 1
    assert result["evidence_status"] == "candidates_require_source_review"
    raw["metadata"]["query_mode"] = "naive"
    result = asyncio.run(search(value, rag, "Pilot context", mode="naive"))
    assert result["graph"] == []
