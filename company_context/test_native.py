"""Exercise native graph/vector stores and real MCP protocol with synthetic model replies."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from starlette.testclient import TestClient

from company_context.context import build, close_engine, engine, open_index, search
from company_context.server import create_app
from company_context.test_context import corpus, record


class SyntheticModels:
    config = {"chat_model": "synthetic", "embedding_model": "synthetic", "dimensions": 4}
    calls = 0

    async def embed(self, texts):
        self.calls += 1
        return np.asarray([[1., .1, .1, .1] for _ in texts], dtype=np.float32)

    async def extract(self, prompt, system_prompt=None, **kwargs):
        self.calls += 1
        return "entity<|#|>Example Organization<|#|>Organization<|#|>An organization evaluating a pilot.\nentity<|#|>Pilot<|#|>Event<|#|>A completed trial.\nrelation<|#|>Example Organization<|#|>Pilot<|#|>evaluation<|#|>The organization evaluated the pilot.\n<|COMPLETE|>"


def test_default_entity_types_and_extraction_prompt_are_not_overridden(tmp_path):
    async def run():
        rag = engine(tmp_path, SyntheticModels(), building=True)
        await rag.initialize_storages()
        try:
            assert rag.entity_extract_max_gleaning == 1
            assert rag.entity_extract_max_records == 100
            config = rag._build_global_config()
            assert not config.get("addon_params", {}).get("entity_types")
        finally:
            await close_engine(rag)
    asyncio.run(run())


def test_native_build_reopen_and_retrieve_preserve_two_source_identities(tmp_path):
    async def run():
        models = SyntheticModels()
        original = "  The pilot &amp; evaluation were completed.\n\n"
        value = corpus([record(text=original), record("meeting-2", author="Second author", text=original)])
        directory = tmp_path / "snapshot"
        ready = await build(value.snapshot, directory, models)
        assert ready["source_count"] == 2
        reopened, rag = await open_index(directory, models)
        try:
            for mode in ("naive", "mix"):
                result = await search(reopened, rag, "Pilot evaluation", mode, 20)
                assert result["retrieval"]["status"] == "ready", result
                assert {r["source_id"] for r in result["results"]} == {"meeting-1", "meeting-2"}, result
                assert {r["author"] for r in result["results"]} == {"Test representative", "Second author"}
                assert all(r["text"] == original for r in result["results"])
                if mode == "mix":
                    assert {r["name"] for r in result["graph"] if r["kind"] == "entity"} == {"Example Organization", "Pilot"}
                    assert {s["source_id"] for r in result["graph"] for s in r["sources"]} == {"meeting-1", "meeting-2"}
                    for row in result["graph"]:
                        for source in row["sources"]:
                            assert reopened.read(**source["read_source"])["text"] == original
                else:
                    assert result["graph"] == []
        finally:
            await close_engine(rag)
        modified = json.loads((directory / "snapshot.json").read_text())
        modified["records"][0]["text"] = "tampered"
        (directory / "snapshot.json").write_text(json.dumps(modified))
        with pytest.raises(ValueError, match="differs"):
            await open_index(directory, models)
    asyncio.run(run())


def parse_rpc(response):
    response.raise_for_status()
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]


def tool_result(response):
    result = response["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))


def test_real_mcp_calls_authentication_rotation_and_source_read(tmp_path):
    from company_context.test_graph_context import native_id

    token = "synthetic-test-token-not-a-secret-1234"
    token_file = tmp_path / "token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    value = corpus()
    chunk = next(iter(value.chunks.values()))
    rag = SimpleNamespace(aquery_data=AsyncMock(return_value={"metadata": {"query_mode": "mix"}, "data": {"chunks": [{"content": value.render(chunk)}], "entities": [{"entity_name": "Pilot", "description": "A pilot, not a purchase.", "source_id": native_id(value, chunk)}]}}), tokenizer=SimpleNamespace(encode=list))
    with TestClient(create_app(value, rag, token_file)) as client:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post("/mcp", json=payload).status_code == 401
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
        response = parse_rpc(client.post("/mcp", headers=headers, json=payload))
        assert {t["name"] for t in response["result"]["tools"]} == {"company_index_status", "search_company_context", "get_company_source"}
        payload.update(method="tools/call", params={"name": "search_company_context", "arguments": {"query": "Pilot purchase outcome"}})
        response = parse_rpc(client.post("/mcp", headers=headers, json=payload))
        assert not response["result"].get("isError")
        content = tool_result(response)
        assert content["results"][0]["source_id"] == "meeting-1"
        assert content["graph"][0]["description"] == "A pilot, not a purchase."
        handle = content["graph"][0]["sources"][0]["read_source"]
        assert handle == {"chunk_id": chunk["chunk_id"]}
        payload["params"] = {"name": "get_company_source", "arguments": handle}
        response = parse_rpc(client.post("/mcp", headers=headers, json=payload))
        assert tool_result(response)["text"] == record()["text"]
        token_file.write_text("rotated-synthetic-token-not-a-secret-1234")
        assert client.post("/mcp", headers=headers, json=payload).status_code == 401
