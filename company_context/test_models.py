"""Provider failures, billing bounds and explicit external processing authorization."""

import asyncio
import json

import httpx
import pytest

from company_context.context import Models


def config():
    return {"external_provider": "openrouter", "chat_url": "https://openrouter.ai/api/v1", "embedding_url": "https://openrouter.ai/api/v1", "chat_model": "synthetic", "embedding_model": "synthetic", "dimensions": 2}


def test_budget_refuses_dispatch_and_failed_request_is_not_replayed(monkeypatch, tmp_path):
    calls = []
    async def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("private provider response must not escape")
    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    async def run():
        provider = Models(config(), budget=.01)
        with pytest.raises(RuntimeError):
            await provider.embed(["synthetic text"])
        assert calls == []
        provider = Models(config(), receipts=tmp_path / "receipts.jsonl")
        for _ in range(2):
            with pytest.raises(RuntimeError) as error:
                await provider.embed(["synthetic text"])
            assert "private provider response" not in str(error.value)
        assert calls == [True]
        receipts = [json.loads(line) for line in (tmp_path / "receipts.jsonl").read_text().splitlines()]
        assert [row["status"] for row in receipts] == ["failed_cost_unknown"]
        assert "synthetic text" not in json.dumps(receipts)
        restarted = Models(config(), receipts=tmp_path / "receipts.jsonl")
        assert restarted.failed and restarted.calls == 1
        with pytest.raises(RuntimeError):
            await restarted.embed(["do not replay"])
        assert calls == [True]
    asyncio.run(run())


def test_embedding_response_membership_and_actual_usage(monkeypatch, tmp_path):
    payloads = []
    async def post(self, url, **kwargs):
        payloads.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}], "usage": {"cost": .001}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    async def run():
        provider = Models(config(), receipts=tmp_path / "receipts.jsonl")
        result = await provider.embed(["first source", "second source"])
        assert result.tolist() == [[1., 0.], [0., 1.]]
        assert payloads[0]["input"] == ["first source", "second source"]
        assert provider.cost == pytest.approx(.001)
    asyncio.run(run())


def test_truncated_extraction_is_rejected(monkeypatch, tmp_path):
    async def post(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"finish_reason": "length", "message": {"content": "partial graph"}}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    async def run():
        provider = Models(config(), receipts=tmp_path / "receipts.jsonl")
        with pytest.raises(RuntimeError, match="truncated"):
            await provider.extract("synthetic input")
        assert provider.failed
        assert provider.calls == 1
        restarted = Models(config(), receipts=tmp_path / "receipts.jsonl")
        assert restarted.failed
        with pytest.raises(RuntimeError, match="no automatic replay"):
            await restarted.extract("do not replay")
    asyncio.run(run())
