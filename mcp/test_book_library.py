"""Source identity, scope isolation, and compatibility checks for the book library."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from book_library import Catalog, MI, FS, chunk_identity, digest, rendered_chunk
from library_index import LibraryIndex, close_engine, engine, preserve_source_text
from library_index import Provider
import server


def test_multi_book_tools_are_available():
    assert callable(getattr(server, "list_books", None))


def test_failed_provider_stops_queued_paid_requests(tmp_path):
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("provider rejected payload")
    provider = Provider(fail, tmp_path)
    for _ in range(2):
        try:
            provider.request("embeddings", {"model": "test"})
        except RuntimeError:
            pass
    assert len(calls) == 1


def test_korean_query_translation_is_bounded_literal_and_not_cached():
    calls = []
    def transport(endpoint, payload, **kwargs):
        calls.append((endpoint, payload, kwargs))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"query": "Who controls the customer's budget?"})}}]}
    provider = Provider(transport)
    assert asyncio.run(provider.normalize_query("고객 예산을 누가 결정하나?")) == "Who controls the customer's budget?"
    assert asyncio.run(provider.normalize_query("willingness to pay")) == "willingness to pay"
    assert len(calls) == 1 and calls[0][0] == "chat/completions"
    assert calls[0][1]["max_tokens"] == 512 and calls[0][1]["temperature"] == 0
    assert calls[0][1]["response_format"]["json_schema"]["strict"] is True
    assert "Do not answer or add concepts" in calls[0][1]["messages"][0]["content"]
    assert calls[0][2]["attempts"] == 1 and provider.directory is None
    assert all("고객" not in json.dumps(event, ensure_ascii=False) for event in provider.events)


def test_serving_busy_response_retries_once_and_records_both_attempts(monkeypatch):
    import io
    import urllib.error
    calls = []
    def transport(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://provider.invalid", 429, "Model busy", {"Retry-After": "1"}, io.BytesIO(b'{"error":{"message":"Model busy"}}'))
        return {"usage": {"cost": .001}, "ok": True}
    monkeypatch.setattr("library_index.time.sleep", lambda seconds: None)
    provider = Provider(transport)
    provider.retry_busy = True
    assert provider.request("embeddings", {"model": "test"})["ok"]
    assert len(calls) == 2
    assert [event["status"] for event in provider.events] == ["failed_cost_unknown", "completed"]


def test_busy_retries_are_bounded_and_indexing_remains_fail_fast(monkeypatch):
    import io
    import urllib.error
    monkeypatch.setattr("library_index.time.sleep", lambda seconds: None)
    for status, enabled, expected in ((429, True, 2), (404, True, 1), (429, False, 1)):
        calls = []
        def transport(*args, **kwargs):
            calls.append(True)
            raise urllib.error.HTTPError("https://provider.invalid", status, "Unavailable", {"Retry-After": "1"}, io.BytesIO(b'{"error":{"message":"Unavailable"}}'))
        provider = Provider(transport, retry_busy=enabled)
        try:
            provider.request("embeddings", {"model": "test"})
        except RuntimeError:
            pass
        else:
            raise AssertionError("Repeated provider failures must remain failures")
        assert len(calls) == len(provider.events) == expected


def fixture():
    books = []
    for book_id in (MI, FS):
        chunk = {"chunk_id": "src-" + digest(book_id.encode())[:24], "book_id": book_id, "section_id": 7 if book_id == MI else FS + ":s001", "section": "Customer needs", "chapter": "Discovery", "text": "Identify the buyer and confirm customer needs before changing the price.", "location": {"kind": "pdf", "pdf_page": 60, "printed_page": None}}
        books.append({"book_id": book_id, "title": book_id, "edition": "test edition", "format": "pdf", "authors": ["Test Author"], "metadata_evidence": {}, "sha256": "test-hash", "sections": [{"id": chunk["section_id"], "title": chunk["section"], "level": 1}], "chunks": [chunk], "units": []})
    return Catalog(books)


def test_section_identifiers_preserve_legacy_numeric_identity():
    catalog = fixture()
    assert catalog.section(7)[0]["book_id"] == MI
    assert catalog.section(FS + ":s001")[0]["book_id"] == FS


def test_rendered_identity_does_not_collide_for_identical_text_in_two_books():
    catalog = fixture()
    chunks = list(catalog.chunks.values())
    assert chunks[0]["text"] == chunks[1]["text"]
    assert rendered_chunk(chunks[0]) != rendered_chunk(chunks[1])
    assert [chunk_identity(rendered_chunk(chunk)) for chunk in chunks] == [chunk["chunk_id"] for chunk in chunks]


def test_filter_selects_scoped_graph_before_retrieval_and_rejects_a_leak():
    catalog = fixture()
    first, second = catalog.chunks.values()
    good = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": rendered_chunk(first)}]}}), chunks_vdb=SimpleNamespace(query=AsyncMock(return_value=[])), tokenizer=SimpleNamespace(encode=lambda value: value.split()))
    other = SimpleNamespace(aquery_data=AsyncMock())
    index = object.__new__(LibraryIndex)
    index.catalog = catalog
    index.engines = {MI: good, FS: other, "all": other}
    index.provider = SimpleNamespace(rerank=AsyncMock(return_value=[{"index": 0, "relevance_score": .9}]))
    index.version = "test-release"
    result = asyncio.run(index._search("customer needs", 5, MI, "mix", "excerpt"))
    assert [row["chunk_id"] for row in result["results"]] == [first["chunk_id"]]
    other.aquery_data.assert_not_awaited()
    good.aquery_data.return_value = {"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": rendered_chunk(second)}]}}
    result = asyncio.run(index._search("customer needs", 5, MI, "mix", "excerpt"))
    assert result["retrieval"]["status"] == "unavailable"
    assert result["retrieval"]["actual_mode"] is None
    assert result["results"] == []


def test_mix_preserves_native_vector_candidates_before_reranking():
    catalog = fixture()
    strong = catalog.books[MI]["chunks"][0]
    weak = {**strong, "chunk_id": "src-" + "f" * 24, "text": "Unrelated navigation overview"}
    catalog.books[MI]["chunks"].append(weak)
    catalog = Catalog(list(catalog.books.values()))
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": rendered_chunk(weak)}]}}), chunks_vdb=SimpleNamespace(query=AsyncMock(return_value=[{"id": "native-vector-key", "content": rendered_chunk(strong)}])), tokenizer=SimpleNamespace(encode=lambda value: value.split()))
    index = object.__new__(LibraryIndex)
    index.catalog, index.engines, index.version = catalog, {MI: rag}, "test"
    async def rerank(query, documents, top_n=None):
        return [{"index": position, "relevance_score": .9 if text == strong["text"] else .01} for position, text in enumerate(documents)]
    index.provider = SimpleNamespace(rerank=rerank)
    result = asyncio.run(index._search("customer budget", 5, MI, "mix", "full"))
    assert [hit["chunk_id"] for hit in result["results"]] == [strong["chunk_id"]]
    assert result["retrieval"]["actual_mode"] == "mix"
    rag.chunks_vdb.query.assert_awaited_once_with("customer budget", top_k=40)


def test_real_lightrag_scopes_do_not_share_kv_memory(tmp_path):
    async def check():
        async def embed(texts):
            raise AssertionError("No provider call is needed for scope isolation")
        first = engine(tmp_path / "release" / "indices" / MI, embed)
        second = engine(tmp_path / "release" / "indices" / FS, embed)
        await first.initialize_storages()
        await second.initialize_storages()
        try:
            await first.text_chunks.upsert({"only-in-first-book": {"content": "private to first graph"}})
            assert await second.text_chunks.get_by_id("only-in-first-book") is None
        finally:
            await first.finalize_storages()
            await second.finalize_storages()
    asyncio.run(check())


def test_library_mcp_schema_excerpt_permissions_and_continuation(monkeypatch):
    from starlette.testclient import TestClient
    catalog = fixture()
    chunk = catalog.books[FS]["chunks"][0]
    chunk["text"] = " ".join(f"word{index}" for index in range(150))
    library = SimpleNamespace(catalog=catalog, version="test", error=None, ready=SimpleNamespace(is_set=lambda: True), thread=SimpleNamespace(is_alive=lambda: False))
    monkeypatch.setattr(server, "LIBRARY", library)
    monkeypatch.setattr(server, "_bearer", lambda headers: "excerpt" if headers.get("authorization") == "Bearer fixture" else None)
    with TestClient(server.app()) as client:
        headers = {"Authorization": "Bearer fixture", "Accept": "application/json, text/event-stream"}
        def rpc(method, parameters):
            response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters})
            response.raise_for_status()
            if response.headers.get("mcp-session-id"):
                headers["mcp-session-id"] = response.headers["mcp-session-id"]
            payload = next((line[6:] for line in response.text.splitlines() if line.startswith("data: ")), response.text)
            return json.loads(payload)["result"]
        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "library-test", "version": "1"}})
        def call(name, arguments):
            envelope = rpc("tools/call", {"name": name, "arguments": arguments})
            assert not envelope.get("isError")
            return json.loads(envelope["content"][0]["text"])
        books = rpc("tools/call", {"name": "list_books", "arguments": {}})["structuredContent"]["result"]
        assert [book["book_id"] for book in books] == [MI, FS]
        toc = call("list_sections", {"book_id": FS})
        assert [section["id"] for section in toc["sections"]] == [FS + ":s001"]
        first = call("get_section", {"id": FS + ":s001", "book_id": FS})
        assert first["text"].split() == chunk["text"].split()[:60]
        second = call("get_section", first["continuation"])
        assert second["text"].split() == chunk["text"].split()[60:120]
        assert client.post("/mcp", json={}).status_code == 401


def test_native_storage_retains_original_control_characters_after_insertion(tmp_path):
    import numpy as np
    from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding
    async def check():
        async def embed(texts):
            result = np.zeros((len(texts), 2560), dtype=np.float32)
            result[:, 0] = 1
            return result
        rag = engine(tmp_path / "release" / "indices" / MI, embed)
        await rag.initialize_storages()
        chunk = next(iter(fixture().chunks.values()))
        chunk["text"] = "Original PDF extraction contains a control character: revenue \x01 cost."
        content = rendered_chunk(chunk)
        try:
            await rag.ainsert_custom_kg({"chunks": [{"content": content, "source_id": chunk["chunk_id"]}], "entities": [], "relationships": []})
            identity = compute_mdhash_id(sanitize_text_for_encoding(content), prefix="chunk-")
            assert (await rag.text_chunks.get_by_id(identity))["content"] != content
            assert await preserve_source_text(rag, [chunk]) == 1
            assert (await rag.text_chunks.get_by_id(identity))["content"] == content
            assert await preserve_source_text(rag, [chunk]) == 0
        finally:
            await close_engine(rag)
    asyncio.run(check())
