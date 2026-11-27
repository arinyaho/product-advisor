"""Completed-provider export, per-source authorization and atomic batch publication."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from company_context import document_sources, document_sync, sync
from company_context.collect_notion import open_store as notion_store, collect as collect_notion
from company_context.collect_drive import open_store as drive_store, collect as collect_drive
from company_context.context import Corpus, build, close_engine, open_index, search
from company_context.document_sources import IncompleteCapture, export_capture, merge_records
from company_context.test_collect_notion import API as NotionAPI
from company_context.test_collect_drive import API as DriveAPI
from company_context.test_context import record
from company_context.test_native import SyntheticModels


READER = "slack:T1:U1"


def doc(provider="notion", identity="p1"):
    return record(identity, source_type="notion" if provider == "notion" else "docs", url="https://www.notion.so/" + identity if provider == "notion" else "https://docs.google.com/document/d/" + identity + "/edit", text="The customer needs a paid pilot with measurable acceptance.", provider_scope={"provider": provider, "connection": "bot1" if provider == "notion" else "reader", "resource_id": identity})


def cache(root, ids=None):
    now = datetime.now(timezone.utc).isoformat()
    sync.write_json(root / "access.json", {"principal": READER, "checked_at": now, "channel_ids": ["C001"]})
    sync.write_json(root / "document-access.json", {"principal": READER, "providers": {p: {"connection": "bot1" if p == "notion" else "reader", "checked_at": now, "resource_ids": ids or ["p1", "p2", "p3", "f1", "f2", "f3"]} for p in ("notion", "drive")}})


def test_export_requires_completed_capture_and_preserves_nested_text(tmp_path):
    path = tmp_path / "notion.sqlite"
    settings = {"database": str(path), "workspace_id": "W", "connection_id": "bot1"}
    db = notion_store(path, {"workspace_id": "W", "connection_id": "bot1"})
    with pytest.raises(IncompleteCapture): export_capture("notion", settings, READER)
    collect_notion(NotionAPI(), db); db.close()
    snapshot = export_capture("notion", settings, READER)
    assert {r["source_id"] for r in snapshot["records"]} == {"notion:W:p1", "notion:W:p2"}
    first = next(r for r in snapshot["records"] if r["source_id"] == "notion:W:p1")
    assert first["text"].index("Nested text") < first["text"].index("Second")
    assert first["provider_scope"] == {"provider": "notion", "connection": "bot1", "resource_id": "p1", "dependencies": []}
    Corpus(snapshot)
    with pytest.raises(ValueError, match="identity"): export_capture("notion", {**settings, "connection_id": "other"}, READER)


def test_drive_export_only_contains_verified_text_and_correct_reader(tmp_path):
    api = DriveAPI(); path = tmp_path / "drive.sqlite"
    db = drive_store(path, api.identity); collect_drive(api, db); db.close()
    settings = {"database": str(path), "email": "reader@example.com", "permission_id": "reader"}
    snapshot = export_capture("drive", settings, READER)
    assert {r["source_id"]: r["text"] for r in snapshot["records"]} == {"drive:reader:a": "Original a", "drive:reader:b": "Original b"}
    Corpus(snapshot)
    with pytest.raises(ValueError, match="identity"): export_capture("drive", {**settings, "permission_id": "other"}, READER)


def test_merge_preserves_other_providers_and_replaces_same_resource_alias():
    old_drive = doc("drive", "f1"); old_drive.pop("provider_scope"); old_drive["source_id"] = "legacy-drive"
    old_notion = doc("notion", "p1")
    new_drive = doc("drive", "f1")
    result = merge_records([record("slack"), old_notion, old_drive], [new_drive], "drive")
    assert {r["source_id"] for r in result} == {"slack", "p1", "f1"}
    assert next(r for r in result if r["source_id"] == "f1") == new_drive


def test_provider_access_and_synced_dependency_are_required(tmp_path):
    row = doc(); row["provider_scope"]["dependencies"] = ["block:original"]
    corpus = Corpus({"principal": READER, "records": [row]})
    cache(tmp_path, ["p1"])
    assert sync.availability(corpus, tmp_path / "access.json") == "blocked_document_access_changed"
    cache(tmp_path, ["p1", "block:original"])
    assert sync.availability(corpus, tmp_path / "access.json") == "ready"
    path = tmp_path / "document-access.json"; data = json.loads(path.read_text())
    data["providers"]["notion"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(); sync.write_json(path, data)
    assert sync.availability(corpus, tmp_path / "access.json") == "blocked_document_access_expired"
    assert sync.availability(corpus) == "blocked_document_access_missing"


def test_confirmed_revocation_persists_even_when_later_provider_refresh_fails(tmp_path, monkeypatch):
    cache(tmp_path, ["p1", "f1"])
    class Notion:
        client = SimpleNamespace(close=lambda: None)
        def __init__(self, *a): pass
        def request(self, method, path, **kwargs):
            if path == "users/me": return {"id": "bot1", "bot": {"workspace_id": "W"}}
            if path == "search": return {"results": []}
            raise httpx.HTTPStatusError("denied", request=httpx.Request("GET", "https://api.notion.com"), response=httpx.Response(404))
    def broken(*a): raise RuntimeError("transient provider failure")
    monkeypatch.setattr(document_sources, "Notion", Notion); monkeypatch.setattr(document_sources, "Drive", broken)
    settings = {"principal": READER, "providers": {"notion": {"token_file": "synthetic", "workspace_id": "W", "connection_id": "bot1"}, "drive": {"credentials_file": "synthetic", "email": "reader@example.com"}}}
    with pytest.raises(RuntimeError): document_sources.refresh_document_access(tmp_path, settings, [{"records": [doc(), doc("drive", "f1")]}])
    assert "p1" not in json.loads((tmp_path / "document-access.json").read_text())["providers"]["notion"]["resource_ids"]


def setup_publication(root, monkeypatch):
    token = root / "token"; token.write_text("synthetic-not-a-secret-token-123456789"); token.chmod(0o600)
    config = root / "config.json"; config.write_text("{}")
    settings = {"principal": READER, "providers": {"notion": {}, "drive": {}}, "slack_token_file": "synthetic", "model_config": str(config), "mcp_token_file": str(token), "budget_usd": 200, "batch_sources": 1}
    sync.write_json(root / "document-sources.json", settings); cache(root)
    monkeypatch.setattr(document_sync, "Slack", lambda _: SimpleNamespace(client=SimpleNamespace(close=lambda: None)))
    def refresh(*a): cache(root); return {"principal": READER}
    monkeypatch.setattr(document_sync, "refresh_access", refresh)
    monkeypatch.setattr(document_sync, "refresh_document_access", refresh)
    monkeypatch.setattr(document_sources, "refresh_document_access", refresh)
    calls = []
    def models(config, limit, receipts, budget, **kw):
        calls.append((receipts, budget)); model = SyntheticModels(); model.cost = 0; return model
    monkeypatch.setattr(sync, "Models", models)
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **kw: None)
    from company_context.evaluate import rpc as original
    def rpc(client, method, params):
        if isinstance(client, TestClient): return original(client, method, params)
        corpus = Corpus(json.loads((root / "current/snapshot.json").read_text()))
        return {"snapshot": corpus.version, "availability": "ready"} if params["name"] == "company_index_status" else corpus.read(params["arguments"]["chunk_id"])
    monkeypatch.setattr("company_context.evaluate.rpc", rpc)
    return settings, calls


def test_completed_sources_publish_independently_in_fair_batches_and_are_searchable(tmp_path, monkeypatch):
    settings, calls = setup_publication(tmp_path, monkeypatch)
    phase = ["notion_only"]
    def exported(provider, config, reader):
        if provider == "drive" and phase[0] == "notion_only": raise IncompleteCapture("capture_in_progress")
        ids = ["p1", "p2"] if provider == "notion" else ["f1", "f2"]
        return {"principal": reader, "records": [doc(provider, identity) for identity in ids], "collection": {"completed_at": "2026-11-26T00:00:00+00:00"}}
    monkeypatch.setattr(document_sync, "export_capture", exported)
    async def run():
        await document_sync.synchronize(tmp_path, settings)
        first = json.loads((tmp_path / "current/snapshot.json").read_text())
        assert {r["source_id"] for r in first["records"]} == {"p2"}
        assert first["collection"]["managed_sources"]["notion"]["index_complete"] is False
        phase[0] = "both"
        await document_sync.synchronize(tmp_path, settings)
        second = json.loads((tmp_path / "current/snapshot.json").read_text())
        assert {r["source_id"] for r in second["records"]} == {"p2", "f2"}
        await document_sync.synchronize(tmp_path, settings)
        await document_sync.synchronize(tmp_path, settings)
        final = json.loads((tmp_path / "current/snapshot.json").read_text())
        assert {r["source_id"] for r in final["records"]} == {"p1", "p2", "f1", "f2"}
        assert all(v["index_complete"] for v in final["collection"]["managed_sources"].values())
        count = len(calls); await document_sync.synchronize(tmp_path, settings); assert len(calls) == count
        corpus, rag = await open_index(tmp_path / "current", SyntheticModels())
        try:
            result = await search(corpus, rag, "customer paid pilot", "mix", 10)
            assert result["retrieval"]["actual_mode"] == "mix"
            assert {r["source_id"] for r in result["results"]} == {"p1", "p2", "f1", "f2"}
            assert all(corpus.read(r["chunk_id"])["text"] == r["text"] for r in result["results"])
        finally: await close_engine(rag)
    asyncio.run(run())
    assert calls and all(path == tmp_path / "sync-model-receipts.jsonl" and budget == 200 for path, budget in calls)


def test_slack_refresh_preserves_published_document_records(tmp_path, monkeypatch):
    from company_context.test_ingestion_review import setup_sync
    old = {"principal": READER, "records": [doc()]}
    args = setup_sync(tmp_path, monkeypatch, old=old, messages=[{"ts": "12", "text": "New Slack evidence"}])
    cache(tmp_path)
    observed = []
    async def publish(args, snapshot, *a): observed.extend(snapshot["records"])
    monkeypatch.setattr(sync, "publish_snapshot", publish)
    asyncio.run(sync.synchronize(args))
    assert {r["source_id"] for r in observed} == {"p1", "slack:T1:C001:timeline:12"}
    assert next(r for r in observed if r["source_id"] == "p1") == doc()


def test_combined_cycle_publishes_slack_and_each_document_provider(tmp_path, monkeypatch):
    from company_context.test_context import record
    settings, _ = setup_publication(tmp_path, monkeypatch)
    settings["providers"]["jira"] = {}
    sample = doc("notion", "p1")
    jira = {**sample, "source_id": "jira:example.atlassian.net:101", "source_type": "jira",
            "url": "https://example.atlassian.net/browse/PROJ-1",
            "provider_scope": {"provider": "jira", "connection": "c" * 64, "resource_id": "101"}}
    records = {"notion": sample, "drive": doc("drive", "f1"), "jira": jira}
    monkeypatch.setattr(document_sync, "export_capture", lambda provider, *_: {
        "records": [records[provider]], "collection": {"capture_id": "pilot"}})
    access = {"principal": READER, "providers": {
        provider: {"connection": row["provider_scope"]["connection"],
                   "resource_ids": [row["provider_scope"]["resource_id"]]}
        for provider, row in records.items()}}
    monkeypatch.setattr(document_sync, "refresh_document_access",
                        lambda *_: sync.write_json(tmp_path / "document-access.json", access))
    monkeypatch.setattr(document_sync, "require_access", lambda *_: None)
    observed = []

    async def publish(args, candidate, state, previous, refresh, updates):
        observed.append((args.index_parallelism, candidate, updates))

    monkeypatch.setattr(document_sync, "publish_snapshot", publish)
    settings["index_parallelism"] = 4
    slack = record("slack")
    slack_batch = ({"principal": READER, "records": [slack], "collection": {}}, {"last_database": "capture"})
    asyncio.run(document_sync.synchronize(tmp_path, settings, slack_batch=slack_batch))
    [(parallelism, candidate, updates)] = observed
    assert parallelism == 4
    assert {r["source_type"] for r in candidate["records"]} == {"slack", "notion", "docs", "jira"}
    assert updates["last_database"] == "capture"


def test_combined_noop_completes_pending_slack_collection(tmp_path, monkeypatch):
    from company_context.test_context import record
    settings, _ = setup_publication(tmp_path, monkeypatch)
    settings["providers"] = {}
    old = {"principal": READER, "records": [record()], "collection": {"completed_at": "2026-11-26T00:00:00+00:00"}}
    release = tmp_path / "release"
    release.mkdir()
    sync.write_json(release / "snapshot.json", old)
    (tmp_path / "current").symlink_to(release)
    sync.write_json(tmp_path / "sync.json", {"phase": "collecting", "pending_database": "capture", "pending_until": 12})
    asyncio.run(document_sync.synchronize(tmp_path, settings, slack_batch=(old, {"last_database": "capture"})))
    state = json.loads((tmp_path / "sync.json").read_text())
    assert state["phase"] == "unchanged"
    assert state["last_collected_at"] == "2026-11-26T00:00:00+00:00"
    assert state["last_database"] == "capture"
    assert "pending_database" not in state and "pending_until" not in state


def test_combined_sync_rejects_negative_document_batch_limit_before_export(tmp_path, monkeypatch):
    settings, _ = setup_publication(tmp_path, monkeypatch)
    settings["batch_sources"] = -1
    settings["index_parallelism"] = 4
    monkeypatch.setattr(document_sync, "export_capture", lambda *_: pytest.fail("Invalid batch attempted an export"))
    batch = ({"principal": READER, "records": [], "collection": {}}, {})
    with pytest.raises(ValueError, match="document batch count"):
        asyncio.run(document_sync.synchronize(tmp_path, settings, slack_batch=batch))


def test_document_fallback_keeps_previous_slack_completion_timestamp(tmp_path, monkeypatch):
    from company_context.test_context import record
    settings, _ = setup_publication(tmp_path, monkeypatch)
    settings["providers"] = {"notion": {}}
    completed = "2026-11-26T00:00:00+00:00"
    old = {"principal": READER, "records": [record()],
           "collection": {"slack_capture_started_at": completed, "completed_at": completed}}
    release = tmp_path / "release"
    release.mkdir()
    sync.write_json(release / "snapshot.json", old)
    (tmp_path / "current").symlink_to(release)
    sync.write_json(tmp_path / "sync.json", {"phase": "collecting", "pending_database": "unfinished"})
    monkeypatch.setattr(document_sync, "export_capture",
                        lambda *_: {"records": [doc()], "collection": {}})
    monkeypatch.setattr(document_sync, "require_access", lambda *_: None)
    observed = []

    async def publish(args, candidate, state, previous, refresh, updates):
        observed.append((candidate["collection"]["completed_at"], state["pending_database"], updates))

    monkeypatch.setattr(document_sync, "publish_snapshot", publish)
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    [(timestamp, pending, updates)] = observed
    assert timestamp == completed
    assert pending == "unfinished" and "last_database" not in updates
