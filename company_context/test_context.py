"""Synthetic checks only; never put company records or credentials in this suite."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from company_context.context import Corpus, search, validate_model_url


def record(source_id="meeting-1", **changes):
    return {
        "source_id": source_id,
        "source_type": "slack",
        "url": "https://workspace.slack.com/archives/C001/p123",
        "title": "Pilot review",
        "author": "Test representative",
        "occurred_at": "2026-10-01T09:00:00+09:00",
        "updated_at": "2026-10-02T09:00:00+09:00",
        "collected_at": "2026-10-03T09:00:00+09:00",
        "record_kind": "source",
        "text": "The pilot was completed. The customer declined the purchase. The reason is unknown.",
        **changes,
    }


def corpus(records=None):
    return Corpus({"principal": "synthetic-reader", "records": records or [record()]})


def test_source_identity_dates_and_original_text_survive_chunking():
    original = record(text="가" * 8000 + "\nSecond paragraph.")
    value = corpus([original, record("meeting-2", author="Another representative")])
    chunks = list(value.chunks.values())
    assert {c["source_id"] for c in chunks} == {"meeting-1", "meeting-2"}
    for chunk in chunks:
        source = value.records[chunk["source_id"]]
        assert chunk["text"] == source["text"][chunk["start"]:chunk["end"]]
        assert chunk["occurred_at"] == source["occurred_at"]
        assert chunk["updated_at"] == source["updated_at"]
        assert chunk["record_kind"] == "source"
    assert "".join(c["text"] for c in chunks if c["source_id"] == "meeting-1") == original["text"]
    current = value.read(chunks[0]["chunk_id"])
    assert current["previous_chunk_id"] is None
    reconstructed = []
    while True:
        assert current["source_id"] == "meeting-1"
        reconstructed.append(current["text"])
        if current["next_chunk_id"] is None:
            break
        following = value.read(current["next_chunk_id"])
        assert following["previous_chunk_id"] == current["chunk_id"]
        current = following
    assert "".join(reconstructed) == original["text"]
    isolated = value.read(chunks[-1]["chunk_id"])
    assert isolated["previous_chunk_id"] is None and isolated["next_chunk_id"] is None


def test_revisions_and_removal_produce_distinct_snapshots():
    first = corpus()
    updated = corpus([record(text="A revised record.")])
    removed = corpus([record("meeting-2")])
    assert len({first.version, updated.version, removed.version}) == 3
    assert set(first.chunks).isdisjoint(updated.chunks)
    assert set(removed.records) == {"meeting-2"}


@pytest.mark.parametrize("changes", [
    {"source_id": ""}, {"record_kind": "verified_fact"},
    {"url": "file:///private/record"}, {"text": ""},
    {"updated_at": "yesterday"}, {"occurred_at": "2026-10-01"},
])
def test_invalid_provenance_is_rejected(changes):
    with pytest.raises(ValueError):
        corpus([record(**changes)])


def test_duplicate_sources_and_summary_without_attribution_are_rejected():
    with pytest.raises(ValueError):
        corpus([record(), record()])
    with pytest.raises(ValueError):
        corpus([record(record_kind="summary")])
    value = corpus([record(record_kind="summary", derived_from=["https://example.com/source"])])
    assert next(iter(value.chunks.values()))["record_kind"] == "summary"


def test_public_model_endpoints_are_rejected(monkeypatch):
    monkeypatch.setattr("company_context.context.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    with pytest.raises(ValueError, match="private"):
        validate_model_url("https://public.example/v1")
    assert validate_model_url("https://openrouter.ai/api/v1", "openrouter") == "https://openrouter.ai/api/v1"
    with pytest.raises(ValueError, match="private"):
        validate_model_url("https://public.example/v1", "openrouter")
    monkeypatch.setattr("company_context.context.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("10.1.2.3", 8000))])
    assert validate_model_url("http://models.internal:8000/v1")
    with pytest.raises(ValueError):
        validate_model_url("https://user:password@models.internal/v1")


def test_query_returns_exact_sources_and_never_claims_truth():
    value = corpus([record(), record("meeting-2", text="The operator liked the pilot. No budget was discussed.")])
    chosen = list(value.chunks.values())[1]
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": value.render(chosen)}]}}))
    result = asyncio.run(search(value, rag, "Who discussed the budget?", "mix", 5))
    assert [hit["source_id"] for hit in result["results"]] == ["meeting-2"]
    assert result["results"][0]["text"] == chosen["text"]
    assert result["evidence_status"] == "candidates_require_source_review"
    assert rag.aquery_data.await_args.args[0] == "Who discussed the budget?"
    param = rag.aquery_data.await_args.kwargs["param"]
    assert param.ll_keywords == ["Who discussed the budget?"]


def test_unknown_chunk_and_wrong_mode_fail_closed():
    value = corpus()
    for payload in (
        {"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": "Fabricated evidence"}]}},
        {"metadata": {"query_mode": "naive"}, "data": {"chunks": []}},
    ):
        rag = SimpleNamespace(aquery_data=AsyncMock(return_value=payload))
        result = asyncio.run(search(value, rag, "Pilot outcome", "mix", 5))
        assert result["retrieval"]["status"] == "unavailable"
        assert result["results"] == []


def test_empty_evidence_differs_from_provider_failure():
    value = corpus()
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "naive"}, "data": {"chunks": []}}))
    result = asyncio.run(search(value, rag, "Unknown purchase reason", "naive", 5))
    assert result["evidence_status"] == "no_matches"
    rag.aquery_data.side_effect = RuntimeError("sensitive provider response")
    result = asyncio.run(search(value, rag, "Unknown purchase reason", "naive", 5))
    assert result["retrieval"]["status"] == "unavailable"
    assert "sensitive" not in json.dumps(result)
