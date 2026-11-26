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
        assert [row["status"] for row in receipts] == ["pending_cost_unknown", "failed_cost_unknown"]
        assert [row["call"] for row in receipts] == [1, 1]
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


def test_reviewed_failure_retains_its_reserved_cost_and_original_receipt(tmp_path):
    path = tmp_path / "receipts.jsonl"
    failed = {"call": 1, "status": "failed_cost_unknown", "charged_or_estimated_usd": .02}
    path.write_text(json.dumps(failed) + "\n")
    assert Models(config(), receipts=path).failed
    reviewed = {**failed, "status": "reviewed_failure_cost_reserved", "operator_review": "Independent diagnostic succeeded; authorize one new query attempt."}
    with path.open("a") as stream:
        stream.write(json.dumps(reviewed) + "\n")
    reopened = Models(config(), receipts=path, budget=.03)
    assert not reopened.failed
    assert reopened.cost == pytest.approx(.02)
    assert reopened.calls == 1
    with pytest.raises(RuntimeError, match="limit"):
        reopened.reserve("synthetic", "/embeddings", .02)
    assert json.loads(path.read_text().splitlines()[0]) == failed


def test_chat_fallback_keeps_model_price_ceiling_and_actual_usage(monkeypatch, tmp_path):
    value = config()
    ceiling = {"model": "synthetic", "context_tokens": 1_000_000, "max_price": {"prompt": .5, "completion": 2.5, "request": 0}, "cache_multiplier": 2}
    value["cost_ceiling"] = {"chat": ceiling}
    payloads = []

    async def post(self, url, **kwargs):
        payloads.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"finish_reason": "stop", "message": {"content": "complete"}}], "usage": {"cost": .001}})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    provider = Models(value, receipts=tmp_path / "receipts")
    assert asyncio.run(provider.extract("synthetic")) == "complete"
    assert len(payloads) == 1
    assert payloads[0]["model"] == "synthetic"
    assert payloads[0]["provider"] == {"max_price": ceiling["max_price"], "allow_fallbacks": True, "require_parameters": True}
    assert payloads[0]["max_tokens"] == 8192
    restored = Models(value, receipts=tmp_path / "receipts")
    assert restored.cost == pytest.approx(.001) and not restored.failed


def test_http_error_retains_only_safe_diagnostics_and_still_stops_replay(monkeypatch, tmp_path):
    calls = []

    async def post(self, url, **kwargs):
        calls.append(True)
        return httpx.Response(429, request=httpx.Request("POST", url), headers={"Retry-After": "60", "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795708800000", "X-Private": "private-header"}, json={"error": {"message": "private-message", "metadata": {"error_type": "rate_limit_exceeded", "limit_source": "openrouter_in_flight_budget", "reason": "in_flight_budget_exhausted", "provider_code": "private-code", "flagged_input": "private-input"}}})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    provider = Models(config(), receipts=tmp_path / "receipts")
    with pytest.raises(RuntimeError, match="Configured model request failed"):
        asyncio.run(provider.extract("synthetic"))
    rows = [json.loads(line) for line in provider.receipts.read_text().splitlines()]
    assert [row["status"] for row in rows] == ["pending_cost_unknown", "failed_cost_unknown"]
    assert rows[-1]["http_status"] == 429 and rows[-1]["error_type"] == "HTTPStatusError"
    assert rows[-1]["failure_metadata"] == {"retry-after": 60, "x-ratelimit-limit": 100, "x-ratelimit-remaining": 0, "x-ratelimit-reset": 1795708800000, "error_type": "rate_limit_exceeded", "limit_source": "openrouter_in_flight_budget", "reason": "in_flight_budget_exhausted", "provider_code_present": True}
    assert "private-" not in provider.receipts.read_text()
    restored = Models(config(), receipts=provider.receipts)
    assert restored.failed and restored.cost == .1
    with pytest.raises(RuntimeError, match="no automatic replay"):
        asyncio.run(restored.extract("do not replay"))
    assert calls == [True]


@pytest.mark.parametrize("body, expected", [(b"not json", {}), (b"[]", {}), (b'{"error": null}', {}), (b'{"error": {"metadata": []}}', {}), (b'{"error": {"metadata": {"provider_code": 42}}}', {"provider_code_present": True}), (b'{"error": {"metadata": {"error_type": ["private"], "limit_source": "private-limit", "reason": "private-reason", "provider_code": {"private": true}}}}', {"provider_code_present": True})])
def test_malformed_error_diagnostics_do_not_mask_failure_or_leak(monkeypatch, tmp_path, body, expected):
    async def post(self, url, **kwargs):
        return httpx.Response(429, request=httpx.Request("POST", url), headers={"Retry-After": "private-header", "X-RateLimit-Limit": "9" * 100, "X-RateLimit-Remaining": "-1", "X-RateLimit-Reset": "NaN"}, content=body)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    provider = Models(config(), receipts=tmp_path / "receipts")
    with pytest.raises(RuntimeError, match="Configured model request failed"):
        asyncio.run(provider.extract("synthetic"))
    row = json.loads(provider.receipts.read_text().splitlines()[-1])
    assert row["status"] == "failed_cost_unknown" and row["error_type"] == "HTTPStatusError"
    assert row["charged_or_estimated_usd"] == .1
    if expected:
        assert row["failure_metadata"] == expected
    else:
        assert "failure_metadata" not in row
    assert "private" not in provider.receipts.read_text()
