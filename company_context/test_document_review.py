"""Independent review regressions, with synthetic providers and source identity checks."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import requests

from company_context import document_sources, document_sync, sync
from company_context.collect_drive import Drive, Omitted
from company_context.collect_notion import open_store, retain
from company_context.context import Models
from company_context.test_context import record
from company_context.test_document_sync import READER, cache, doc, setup_publication
from company_context.test_models import config


@pytest.mark.parametrize("withdrawn", [record("slack-old"), doc()])
def test_another_publisher_cannot_carry_withdrawn_evidence_under_a_new_hash(tmp_path, monkeypatch, withdrawn):
    previous = tmp_path / "old"; previous.mkdir()
    old = {"principal": READER, "records": [withdrawn]}
    (previous / "snapshot.json").write_text(json.dumps(old))
    state = {}; sync.withdraw_changed(old, {"records": []}, state, tmp_path / "sync.json")
    cache(tmp_path)
    calls = []
    def models(*a, **kw): calls.append(True); raise RuntimeError("model construction reached")
    monkeypatch.setattr(sync, "Models", models)
    path = tmp_path / "config"; path.write_text("{}")
    args = SimpleNamespace(state_dir=tmp_path, config=path, max_model_calls=1, budget_usd=200)
    incoming = {"principal": READER, "records": [withdrawn, doc("drive", "f1")], "collection": {"completed_at": "2026-11-26T00:00:00Z"}}
    with pytest.raises(ValueError, match="withdrawn"):
        asyncio.run(sync.publish_snapshot(args, incoming, state, previous, lambda: None, {}))
    assert calls == []


def finish(db):
    with db:
        db.execute("UPDATE tasks SET done=1")
        db.execute("INSERT INTO metadata VALUES ('finished_at','2026-11-26T00:00:00Z')")


@pytest.mark.parametrize("kind", ["title", "rich_text", "people", "relation", "rollup"])
def test_notion_property_descriptors_do_not_replace_paginated_values(tmp_path, kind):
    path = tmp_path / "notion.sqlite"; db = open_store(path, {"workspace_id": "W", "connection_id": "bot1"})
    with db:
        retain(db, {"object": "page", "id": "p1", "properties": {"Property": {"type": kind, "id": "r"}}}, ("search", "all"), 0)
        retain(db, {"object": "property_item", "id": "p1/r:0", "type": kind, kind: {"value": "retained-result"}}, ("property", "p1/r"), 0)
        retain(db, {"object": "response_metadata", "id": "property:p1/r:", "response": {"object": "list", "next_cursor": None, "property_item": {"id": "r", "type": kind, kind: {"type": "array"} if kind == "rollup" else {}}}}, ("property", "p1/r"), -1)
    finish(db); db.close()
    snapshot = document_sources.export_capture("notion", {"database": str(path), "workspace_id": "W", "connection_id": "bot1"}, READER)
    assert "retained-result" in snapshot["records"][0]["text"]


def test_one_oversized_notion_page_is_an_explicit_omission(tmp_path):
    path = tmp_path / "notion.sqlite"; db = open_store(path, {"workspace_id": "W", "connection_id": "bot1"})
    with db:
        for identity, text in [("large", "x" * 2_000_001), ("small", "Useful source")]:
            retain(db, {"object": "page", "id": identity, "properties": {}}, ("search", "all"), 0)
            retain(db, {"object": "block", "id": "block-" + identity, "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": text}]}}, ("children", identity), 0)
    finish(db); db.close()
    result = document_sources.export_capture("notion", {"database": str(path), "workspace_id": "W", "connection_id": "bot1"}, READER)
    assert {r["source_id"] for r in result["records"]} == {"notion:W:small"}
    assert result["collection"]["export_omissions"]["source_size_limit"] == ["large"]


def test_native_sheet_export_size_limit_is_not_a_failed_capture():
    response = requests.Response(); response.status_code = 403
    response._content = json.dumps({"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}}).encode(); response._content_consumed = True
    api = Drive.__new__(Drive); api.client = SimpleNamespace(get=lambda *a, **kw: response)
    with pytest.raises(Omitted, match="native_export_size_limit"):
        api.request("https://www.googleapis.com/drive/v3/files/synthetic/export")


def test_drive_constructor_identity_failure_invalidates_previous_access(tmp_path, monkeypatch):
    cache(tmp_path, ["f1"])
    def wrong(*args): raise ValueError("Drive account differs from the authorized reader")
    monkeypatch.setattr(document_sources, "Drive", wrong)
    settings = {"principal": READER, "providers": {"drive": {"credentials_file": "synthetic", "email": "reader@example.com", "permission_id": "reader"}}}
    with pytest.raises(ValueError): document_sources.refresh_document_access(tmp_path, settings, [{"records": [doc("drive", "f1")]}])
    assert json.loads((tmp_path / "document-access.json").read_text())["providers"]["drive"]["resource_ids"] == []


def test_batch_keeps_unselected_static_alias_until_its_replacement_is_ready(tmp_path, monkeypatch):
    settings, _ = setup_publication(tmp_path, monkeypatch); settings["providers"] = {"drive": {}}
    old = []
    for identity in ("f1", "f2"):
        row = doc("drive", identity); row.pop("provider_scope"); row["source_id"] = "legacy-" + identity; old.append(row)
    previous = tmp_path / "old"; previous.mkdir(); (previous / "snapshot.json").write_text(json.dumps({"principal": READER, "records": old})); (tmp_path / "current").symlink_to(previous)
    monkeypatch.setattr(document_sync, "export_capture", lambda *a: {"principal": READER, "records": [doc("drive", "f1"), doc("drive", "f2")], "collection": {"completed_at": "2026-11-26T00:00:00Z"}})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    actual = json.loads((tmp_path / "current/snapshot.json").read_text())
    assert {r["source_id"] for r in actual["records"]} == {"legacy-f1", "f2"}


def test_record_order_alone_does_not_start_another_paid_build(tmp_path, monkeypatch):
    from company_context.test_ingestion_review import setup_sync
    slack = record("slack")
    old = {"principal": READER, "records": [doc("drive", "f1"), doc(), slack]}
    args = setup_sync(tmp_path, monkeypatch, old=old); cache(tmp_path)
    monkeypatch.setattr(sync, "export_snapshot", lambda *a: {"principal": READER, "records": [slack], "collection": {"channel_ids": ["C001"], "completed_at": "2026-11-26T00:00:00Z"}})
    builds = []
    async def publish(*a): builds.append(True)
    monkeypatch.setattr(sync, "publish_snapshot", publish)
    asyncio.run(sync.synchronize(args))
    assert builds == []


def test_model_specific_ceiling_is_reserved_before_dispatch(monkeypatch):
    calls = []
    async def post(*a, **kw):
        calls.append(True)
        return httpx.Response(200, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"), json={"choices": [{"finish_reason": "stop", "message": {"content": "synthetic"}}], "usage": {"cost": .2}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    value = config(); value["cost_ceiling"] = {"chat": {"model": "synthetic", "context_tokens": 1_000_000, "max_price": {"prompt": 1, "completion": 2, "request": 0}, "cache_multiplier": 2}}
    model = Models(value, budget=.15)
    with pytest.raises(RuntimeError): asyncio.run(model.extract("bounded synthetic input"))
    assert calls == []


@pytest.mark.parametrize("replacement", [[], [dict(record("old"), text="Corrected text")]])
def test_reconciled_withdrawal_reaches_model_setup(tmp_path, monkeypatch, replacement):
    previous = tmp_path / "old"; previous.mkdir()
    old = {"principal": READER, "records": [record("old")]}
    (previous / "snapshot.json").write_text(json.dumps(old))
    state = {}; sync.withdraw_changed(old, {"records": []}, state, tmp_path / "sync.json")
    cache(tmp_path)
    def models(*a, **kw): raise RuntimeError("reconciliation accepted")
    monkeypatch.setattr(sync, "Models", models)
    path = tmp_path / "config"; path.write_text("{}")
    args = SimpleNamespace(state_dir=tmp_path, config=path, max_model_calls=1, budget_usd=200)
    with pytest.raises(RuntimeError, match="reconciliation accepted"):
        asyncio.run(sync.publish_snapshot(args, {"principal": READER, "records": replacement}, state, previous, lambda: None, {}))


def test_price_ceiling_applies_routing_and_bounds_each_embedding_input(monkeypatch):
    value = config(); value["cost_ceiling"] = {"embedding": {"model": "synthetic", "context_tokens": 32768, "max_price": {"prompt": .02, "completion": 0, "request": 0}, "cache_multiplier": 1}}
    payloads = []
    async def post(*a, **kw):
        payloads.append(kw["json"])
        return httpx.Response(200, request=httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings"), json={"data": [{"index": 0, "embedding": [1, 0]}], "usage": {"cost": .000001}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    model = Models(value)
    asyncio.run(model.embed(["synthetic"]))
    assert payloads[0]["provider"] == {"max_price": value["cost_ceiling"]["embedding"]["max_price"], "allow_fallbacks": False}
    with pytest.raises(ValueError, match="context bound"): asyncio.run(model.embed(["x" * 32768]))
    assert len(payloads) == 1
    value["require_price_ceiling"] = True
    with pytest.raises(ValueError, match="price ceiling"): asyncio.run(Models(value).extract("synthetic"))


def test_over_ceiling_provider_bill_is_retained_and_stops_replay(tmp_path, monkeypatch):
    value = config(); value["cost_ceiling"] = {"embedding": {"model": "synthetic", "context_tokens": 32768, "max_price": {"prompt": .02, "completion": 0, "request": 0}, "cache_multiplier": 1}}
    calls = []
    async def post(*a, **kw):
        calls.append(True)
        return httpx.Response(200, request=httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings"), json={"data": [{"index": 0, "embedding": [1, 0]}], "usage": {"cost": .5}})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    model = Models(value, receipts=tmp_path / "receipts")
    with pytest.raises(RuntimeError): asyncio.run(model.embed(["synthetic"]))
    restored = Models(value, receipts=tmp_path / "receipts")
    assert restored.cost == .5 and restored.failed
    with pytest.raises(RuntimeError): asyncio.run(restored.embed(["synthetic"]))
    assert calls == [True]


def test_legacy_unknown_withdrawal_is_not_replaced_by_a_new_partial_set(tmp_path, monkeypatch):
    previous = tmp_path / "old"; previous.mkdir()
    old = {"principal": READER, "records": [record("original-withdrawal"), record("new-change")]}
    incoming = {"principal": READER, "records": [record("original-withdrawal")]}
    (previous / "snapshot.json").write_text(json.dumps(old))
    state = {"blocked_snapshots": [sync.digest(old)]}
    sync.withdraw_changed(old, incoming, state, tmp_path / "sync.json")
    cache(tmp_path)
    calls = []
    def models(*a, **kw): calls.append(True); raise RuntimeError("model construction reached")
    monkeypatch.setattr(sync, "Models", models)
    path = tmp_path / "config"; path.write_text("{}")
    args = SimpleNamespace(state_dir=tmp_path, config=path, max_model_calls=1, budget_usd=200)
    with pytest.raises(ValueError, match="withdrawn"):
        asyncio.run(sync.publish_snapshot(args, incoming, state, previous, lambda: None, {}))
    assert calls == []


def test_deleted_managed_resource_cannot_return_from_static_supplement(tmp_path, monkeypatch):
    from company_context.test_ingestion_review import setup_sync
    root = tmp_path / "documents"; root.mkdir()
    settings, _ = setup_publication(root, monkeypatch); settings["providers"] = {"drive": {}}
    managed = doc("drive", "f1"); retained = record("retained")
    previous = root / "old"; previous.mkdir()
    (previous / "snapshot.json").write_text(json.dumps({"principal": READER, "records": [managed, retained]}))
    (root / "current").symlink_to(previous)
    monkeypatch.setattr(document_sync, "export_capture", lambda *a: {"principal": READER, "records": [], "collection": {"completed_at": "2026-11-26T00:00:00Z"}})
    asyncio.run(document_sync.synchronize(root, settings))
    published = json.loads((root / "current/snapshot.json").read_text())
    assert {r["source_id"] for r in published["records"]} == {"retained"}
    following = tmp_path / "slack"; following.mkdir()
    alias = dict(managed); alias.pop("provider_scope"); alias["source_id"] = "static-f1"
    args = setup_sync(following, monkeypatch, old=published, supplement={"principal": READER, "records": [alias]})
    (following / "sync.json").write_text((root / "sync.json").read_text())
    monkeypatch.setattr(sync, "export_snapshot", lambda *a: {"principal": READER, "records": [retained, record("new")], "collection": {"channel_ids": ["C001"], "completed_at": "2026-11-26T00:00:00Z"}})
    observed = []
    async def publish(args, snapshot, *a): observed.extend(snapshot["records"])
    monkeypatch.setattr(sync, "publish_snapshot", publish)
    asyncio.run(sync.synchronize(args))
    assert {r["source_id"] for r in observed} == {"retained", "new"}


def test_unstructured_drive_denial_preserves_revocation_signal():
    response = requests.Response(); response.status_code = 403
    response._content = b'{"error":"Forbidden"}'; response._content_consumed = True
    api = Drive.__new__(Drive); api.client = SimpleNamespace(get=lambda *a, **kw: response)
    with pytest.raises(RuntimeError, match="drive_http_403"):
        api.request("https://www.googleapis.com/drive/v3/files/synthetic")


def test_ingestion_validates_both_model_ceilings_before_any_request():
    value = config(); value["require_price_ceiling"] = True
    value["cost_ceiling"] = {"embedding": {"model": "synthetic", "context_tokens": 32768, "max_price": {"prompt": .02, "completion": 0, "request": 0}, "cache_multiplier": 1}}
    with pytest.raises(ValueError, match="price ceiling"):
        Models(value)
