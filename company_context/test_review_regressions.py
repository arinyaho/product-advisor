"""Regressions for durable requests, private evidence, and snapshot metadata."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient

from company_context import compare_context, context, evaluate, proxy
from company_context.context import Models, search
from company_context.server import create_app
from company_context.test_context import corpus, record
from company_context.test_graph_context import native_id
from company_context.test_native import parse_rpc, tool_result


def model(tmp_path, with_key=True):
    config = {"chat_url": "http://127.0.0.1:8000/v1", "embedding_url": "http://127.0.0.1:8000/v1", "chat_model": "synthetic", "embedding_model": "synthetic", "dimensions": 2}
    if with_key:
        key = tmp_path / "key"
        key.write_text("synthetic-test-credential")
        config["chat_key_file"] = str(key)
    return Models(config, receipts=tmp_path / "receipts.jsonl")


async def request(provider, kind, tmp_path):
    if kind == "service":
        return await provider.extract("synthetic input")
    return await compare_context.generate(provider, {"model": "synthetic"}, tmp_path / "response.json")


@pytest.mark.parametrize("kind", ["service", "experiment"])
def test_dispatched_request_already_has_a_durable_reservation(tmp_path, monkeypatch, kind):
    provider = model(tmp_path)
    async def post(self, url, **kwargs):
        restored = Models(provider.config, receipts=provider.receipts)
        assert restored.calls == 1 and restored.cost == pytest.approx(.1)
        assert restored.failed
        with pytest.raises(RuntimeError):
            await restored.extract("must not replay")
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"finish_reason": "stop", "message": {"content": "complete"}}], "usage": {"cost": .001}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert asyncio.run(request(provider, kind, tmp_path)) == "complete"
    restored = Models(provider.config, receipts=provider.receipts)
    assert restored.calls == 1 and restored.cost == pytest.approx(.001)
    assert not restored.failed


@pytest.mark.parametrize("kind", ["service", "experiment"])
def test_cancelled_request_stops_same_process_and_restart(tmp_path, monkeypatch, kind):
    provider = model(tmp_path)
    dispatched = []
    async def cancel(*args, **kwargs):
        dispatched.append(True)
        raise asyncio.CancelledError()
    monkeypatch.setattr(httpx.AsyncClient, "post", cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(request(provider, kind, tmp_path))
    assert provider.failed
    with pytest.raises(RuntimeError):
        asyncio.run(request(provider, kind, tmp_path))
    restored = Models(provider.config, receipts=provider.receipts)
    assert restored.failed and restored.calls == 1
    assert dispatched == [True]


def test_experiment_supports_internal_models_without_key(tmp_path, monkeypatch):
    provider = model(tmp_path, with_key=False)
    async def post(self, url, **kwargs):
        assert not kwargs.get("headers", {}).get("Authorization")
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"finish_reason": "stop", "message": {"content": "complete"}}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert asyncio.run(request(provider, "experiment", tmp_path)) == "complete"


def test_existing_receipts_and_new_reservations_restore_once_per_call(tmp_path):
    provider = model(tmp_path)
    rows = [
        {"call": 1, "status": "completed", "charged_or_estimated_usd": .01},
        {"call": 2, "status": "pending_cost_unknown", "charged_or_estimated_usd": .1},
        {"call": 2, "status": "completed", "charged_or_estimated_usd": .02},
        {"call": 3, "status": "pending_cost_unknown", "charged_or_estimated_usd": .1},
    ]
    provider.receipts.write_text("".join(json.dumps(row) + "\n" for row in rows))
    restored = Models(provider.config, receipts=provider.receipts)
    assert restored.calls == 3 and restored.cost == pytest.approx(.13)
    assert restored.failed


def test_evaluation_evidence_is_private_even_with_permissive_umask(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-test-token-not-secret-1234")
    token.chmod(0o600)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"frozen_before_indexing": True, "cases": []}))
    output = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(evaluate, "rpc", lambda *args: {})
    old = os.umask(0o022)
    try:
        evaluate.evaluate("http://127.0.0.1:8766/mcp", token, cases, output)
    finally:
        os.umask(old)
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        evaluate.evaluate("http://127.0.0.1:8766/mcp", token, cases, output)


def test_graph_search_does_not_rehash_source_records(tmp_path, monkeypatch):
    value = corpus([record(text="x" * 3200), record("other")])
    chunk = next(iter(value.chunks.values()))
    raw = {"metadata": {"query_mode": "mix"}, "data": {"chunks": [], "entities": [{"entity_name": "Pilot", "description": "A derived pilot description.", "source_id": native_id(value, chunk)}]}}
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value=raw), tokenizer=SimpleNamespace(encode=list))
    original_digest = context.digest
    hashed = []
    def digest(item):
        if any(item is source for source in value.records.values()):
            hashed.append(item["source_id"])
        return original_digest(item)
    monkeypatch.setattr(context, "digest", digest)
    for _ in range(2):
        result = asyncio.run(search(value, rag, "Pilot context"))
        assert result["graph"][0]["sources"][0]["read_source"] == {"chunk_id": chunk["chunk_id"]}
    assert hashed == []


def test_status_orders_collection_times_as_instants(tmp_path):
    earliest = "2026-10-03T09:00:00+09:00"
    latest = "2026-10-03T01:00:00+00:00"
    value = corpus([record(collected_at=earliest), record("other", collected_at=latest)])
    token = tmp_path / "token"
    token.write_text("synthetic-test-token-not-secret-1234")
    token.chmod(0o600)
    with TestClient(create_app(value, None, token)) as client:
        response = client.post("/mcp", headers={"Authorization": "Bearer " + token.read_text(), "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "company_index_status", "arguments": {}}})
        status = tool_result(parse_rpc(response))
    assert (status["collected_from"], status["collected_through"]) == (earliest, latest)


def test_proxy_uses_configured_loopback_port(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-test-token-not-secret-1234")
    token.chmod(0o600)
    registered = {}
    class FakeServer:
        def __init__(self, *args, **kwargs): pass
        def tool(self):
            def register(fn):
                registered[fn.__name__] = fn
                return fn
            return register
        def run(self):
            assert registered["company_index_status"]() == {"url": "http://127.0.0.1:8877/mcp"}
    monkeypatch.setattr(proxy, "MCPServer", FakeServer)
    monkeypatch.setattr(proxy, "rpc", lambda client, *args: {"url": str(client.base_url).rstrip("/")})
    monkeypatch.setattr(sys, "argv", ["proxy", "--token-file", str(token), "--port", "8877"])
    proxy.main()
