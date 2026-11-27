"""Offline checks of the MCP client's HTTP response identity hook."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("reported", [None, "three-books-0123456789abcdef-aaaaaaaaaaaa"])
def test_wrong_runtime_cannot_issue_acceptance(tmp_path, monkeypatch, reported):
    spec = importlib.util.spec_from_file_location("verify_library", Path(__file__).with_name("verify-live-library.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    version = "three-books-0123456789abcdef"
    deployment = version + "-bbbbbbbbbbbb"
    (tmp_path / "manifest.json").write_text(json.dumps({"version": version}))
    (tmp_path / "ready.json").write_text('{"retrieval_version":"test"}')
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(
        '[mcp_servers.textbooks]\nurl="https://test.invalid/mcp"\n'
        '[mcp_servers.textbooks.http_headers]\nAuthorization="Bearer test"\n'
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(verifier.Catalog, "load", lambda release: None)

    @asynccontextmanager
    async def transport(url, http_client):
        hook, = http_client.event_hooks["response"]
        await hook(verifier.httpx2.Response(200, headers={"x-book-library-deployment": deployment}))
        await hook(verifier.httpx2.Response(200, headers={"x-book-library-deployment": reported} if reported else {}))
        pytest.fail("Mixed runtime responses reached the MCP checks")
        yield

    monkeypatch.setattr(verifier, "streamable_http_client", transport)
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="deployment mismatch"):
        asyncio.run(verifier.verify(tmp_path, output, "mock-host", tmp_path / "ready.json", deployment))
    assert not output.exists()
