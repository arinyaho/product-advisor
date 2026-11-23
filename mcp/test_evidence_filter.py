"""Navigation stays readable but never consumes the evidence or reranking budget."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from book_library import Catalog, MI, evidence_exclusion, rendered_chunk
from library_index import LibraryIndex
from test_book_library import fixture


@pytest.mark.parametrize("section,chapter,text,reason", [
    ("INDEX", "INDEX", "Pricing, 80, 120; customer value, 140", "navigation"),
    ("Pricing", "Table of Contents", "Pricing .... 120", "navigation"),
    ("STEP 6: Full Life Cycle Use Case", "STEP 6: Full Life Cycle Use Case", "STEP 6\nFull Life Cycle Use Case", "heading_only"),
    ("Budget", "Discovery", "Budget", "heading_only"),
    ("Budget", "Discovery", "", "empty"),
    ("Budget", "Discovery", "No budget.", None),
    ("Budget", "Discovery", "Budget\nAsk who can approve this purchase.", None),
    ("Notes", "Discovery", "A veto holder can block the purchase.", None),
    ("Index funds", "Pricing", "An index fund is a product example.", None),
])
def test_classifies_navigation_without_a_minimum_word_count(section, chapter, text, reason):
    assert evidence_exclusion({"section": section, "chapter": chapter, "text": text}) == reason


@pytest.mark.parametrize("mode", ["mix", "naive"])
def test_only_body_evidence_reaches_the_reranker_and_k_limit(mode):
    catalog = fixture()
    book = catalog.books[MI]
    body = book["chunks"][0]
    index_chunk = {**body, "chunk_id": "src-" + "a" * 24, "section_id": 99, "section": "INDEX", "chapter": "INDEX", "text": "Customer needs, 60; buyer, 70"}
    heading = {**body, "chunk_id": "src-" + "b" * 24, "text": body["section"]}
    book["chunks"] = [index_chunk, heading, body]
    book["sections"].append({"id": 99, "title": "INDEX", "level": 1})
    catalog = Catalog(list(catalog.books.values()))
    before = copy.deepcopy(book)
    rag = SimpleNamespace(
        aquery_data=AsyncMock(return_value={"metadata": {"query_mode": mode}, "data": {"chunks": [{"content": rendered_chunk(c)} for c in book["chunks"]]}}),
        chunks_vdb=SimpleNamespace(query=AsyncMock(return_value=[{"content": rendered_chunk(index_chunk)}])),
        tokenizer=SimpleNamespace(encode=lambda text: text.split()),
    )
    rerank = AsyncMock(return_value=[{"index": 0, "relevance_score": .99}])
    index = object.__new__(LibraryIndex)
    index.catalog, index.engines, index.version = catalog, {MI: rag}, "synthetic"
    index.provider = SimpleNamespace(rerank=rerank)
    result = asyncio.run(index._search("customer needs", 1, MI, mode, "full"))
    assert [h["chunk_id"] for h in result["results"]] == [body["chunk_id"]]
    assert rerank.await_args.kwargs["documents"] == [body["text"]]
    assert result["retrieval"]["excluded_non_evidence_chunks"] == 2
    assert book == before
    assert 99 in [s["id"] for s in catalog.list_sections(MI)["sections"]]
    assert catalog.read(99, MI, None, "full")["text"] == index_chunk["text"]


def test_navigation_only_candidates_return_no_evidence_without_reranking():
    catalog = fixture()
    chunk = catalog.books[MI]["chunks"][0]
    chunk["text"] = chunk["section"]
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "naive"}, "data": {"chunks": [{"content": rendered_chunk(chunk)}]}}), tokenizer=SimpleNamespace(encode=lambda text: text.split()))
    index = object.__new__(LibraryIndex)
    index.catalog, index.engines, index.version = catalog, {MI: rag}, "synthetic"
    index.provider = SimpleNamespace(rerank=AsyncMock())
    result = asyncio.run(index._search("customer needs", 5, MI, "naive", "full"))
    assert result["retrieval"]["status"] == "ready"
    assert result["retrieval"]["actual_mode"] == "naive"
    assert result["results"] == []
    index.provider.rerank.assert_not_awaited()
