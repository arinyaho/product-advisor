"""Check experiment packing and graph-to-original-source identity mapping."""

from types import SimpleNamespace
import asyncio
import json

import httpx
import pytest

from lightrag.utils import sanitize_text_for_encoding
from lightrag.utils_pipeline import make_custom_chunk_id

from company_context.compare_context import evidence, generate, pack
from company_context.context import Models, digest
from company_context.test_context import corpus


def test_graph_provenance_resolves_to_original_chunk_and_keeps_unknown_reference():
    value = corpus()
    chunk = next(iter(value.chunks.values()))
    text = sanitize_text_for_encoding(value.render(chunk))
    native_id = make_custom_chunk_id("doc-" + digest(value.records[chunk["source_id"]]), text)
    chunks, graph = evidence(value, {
        "chunks": [{"content": text}, {"content": text}],
        "entities": [{"entity_name": "Example", "source_id": native_id}],
        "relationships": [{"src_id": "Example", "tgt_id": "Other", "source_id": native_id + "<SEP>missing"}],
    })
    assert [row["chunk_id"] for row in chunks] == [chunk["chunk_id"]]
    assert chunks[0]["text"] == chunk["text"]
    assert [row["source_id"] for row in graph[0]["sources"]] == [chunk["source_id"]]
    assert [row["chunk_id"] for row in graph[1]["sources"]] == [chunk["chunk_id"]]
    assert graph[1]["unresolved_source_references"] == ["missing"]
    assert all(row["evidence_status"] == "model_extracted_not_verified_fact" for row in graph)


def test_packing_preserves_ranked_prefix_without_splitting_evidence():
    records = [{"x": "123"}, {"x": "456"}]
    tokenizer = SimpleNamespace(encode=list)
    assert pack(records, tokenizer, 15) == [records[0]]
    assert pack(records, tokenizer, 1) == []
    assert pack(records, tokenizer, 25) == records


def test_truncated_answer_is_preserved_before_failure_and_stops_replay(tmp_path, monkeypatch):
    from company_context import compare_context

    key = tmp_path / "key"
    key.write_text("synthetic-test-credential")
    models = Models({"chat_url": "http://127.0.0.1", "embedding_url": "http://127.0.0.1", "chat_key_file": str(key), "chat_model": "synthetic", "embedding_model": "synthetic", "dimensions": 2}, max_calls=2, budget=1., receipts=tmp_path / "receipts")
    raw = {"choices": [{"finish_reason": "length", "message": {"content": '{"partial"'}}], "usage": {"cost": .01}}
    original_client = httpx.AsyncClient
    monkeypatch.setattr(compare_context.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)), **kwargs))
    output = tmp_path / "provider.json"
    with pytest.raises(RuntimeError, match="generation failed"):
        asyncio.run(generate(models, {}, output))
    assert json.loads(output.read_text()) == raw
    receipts = [json.loads(line) for line in models.receipts.read_text().splitlines()]
    assert [r["status"] for r in receipts] == ["pending_cost_unknown", "invalid_response"]
    assert [r["call"] for r in receipts] == [1, 1]
    assert models.failed and models.calls == 1
    with pytest.raises(RuntimeError, match="previous request failed"):
        asyncio.run(generate(models, {}, output))
    assert models.calls == 1
