"""Regression cases from the independent PR review; no production or provider calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import subprocess
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from company_context import sync
from company_context.collect_slack import collect, collect_channel, open_store, save_messages
from company_context.context import Corpus, Models, build, close_engine, digest, engine, verify_chunks
from company_context.test_collect_slack import API, channel
from company_context.test_context import record
from company_context.test_models import config
from company_context.test_native import SyntheticModels


def capture(path, until=20, messages=None):
    db = open_store(path, 10, until)
    with db:
        db.execute("INSERT INTO channels(id,info,done) VALUES (?,?,1)", ("C001", json.dumps(channel())))
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("reader", "U1"), ("team_id", "T1"), ("workspace_url", "https://workspace.slack.com/"), ("completed_at", "2026-11-26T00:00:00+00:00")])
        save_messages(db, "C001", messages or [], 10, until)
    return db


class SyncAPI:
    client = SimpleNamespace(close=lambda: None)
    def get(self, method, **params):
        if method == "auth.test":
            return {"team_id": "T1", "user_id": "U1", "url": "https://workspace.slack.com/"}
        if method == "conversations.info":
            return {"channel": channel()}
        return {"channels": [channel()]}


def setup_sync(tmp_path, monkeypatch, messages=None, old=None, supplement=None):
    database = tmp_path / "initial.sqlite"; capture(database, messages=messages).close()
    token = tmp_path / "token"; token.write_text("synthetic-not-a-secret-token-123456789"); token.chmod(0o600)
    configuration = tmp_path / "config.json"; configuration.write_text("{}")
    if old:
        previous = tmp_path / "old"; previous.mkdir()
        (previous / "snapshot.json").write_text(json.dumps(old))
        (tmp_path / "current").symlink_to(previous)
    supplement_path = None
    if supplement:
        supplement_path = tmp_path / "supplement.json"; supplement_path.write_text(json.dumps(supplement))
    monkeypatch.setattr(sync, "Slack", lambda _: SyncAPI())
    return SimpleNamespace(state_dir=tmp_path, token_file=token, command="sync", bootstrap_database=database, since=datetime.fromtimestamp(10, timezone.utc), supplement=supplement_path, config=configuration, max_model_calls=50, budget_usd=1, service="synthetic.service", mcp_token_file=token, port=8766)


def test_confirmed_removal_is_blocked_even_when_next_build_cannot_start(tmp_path, monkeypatch):
    old = {"principal": "slack:T1:U1", "records": [record()]}
    args = setup_sync(tmp_path, monkeypatch, old=old)
    with pytest.raises(Exception):
        asyncio.run(sync.synchronize(args))
    state = json.loads((tmp_path / "sync.json").read_text())
    assert digest(old) in state["blocked_snapshots"]
    assert sync.availability(Corpus(old), tmp_path / "access.json", tmp_path / "sync.json") != "ready"


@pytest.mark.parametrize("principal,derived", [("other-reader", []), ("slack:T1:U1", ["https://workspace.slack.com/archives/G002/p1"])])
def test_supplement_reader_and_dependencies_checked_before_model_construction(tmp_path, monkeypatch, principal, derived):
    supplement = {"principal": principal, "records": [record(source_type="docs", url="https://docs.example.com/page", record_kind="summary" if derived else "source", derived_from=derived)]}
    args = setup_sync(tmp_path, monkeypatch, supplement=supplement)
    calls = []
    def models(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("model construction reached")
    monkeypatch.setattr(sync, "Models", models)
    with pytest.raises(ValueError):
        asyncio.run(sync.synchronize(args))
    assert calls == []


def test_legacy_snapshot_is_not_reparsed_before_fresh_migration(tmp_path, monkeypatch):
    old = {"principal": "legacy-reader", "records": [record()]}
    old["records"][0].pop("slack_conversation")
    args = setup_sync(tmp_path, monkeypatch, old=old, messages=[{"ts": "12", "text": "New captured statement"}])
    models = SyntheticModels(); models.cost = 0
    monkeypatch.setattr(sync, "Models", lambda *a, **kw: models)
    observed = []
    async def intercepted(snapshot, directory, models, previous=None):
        observed.append(previous)
        raise RuntimeError("fresh build reached")
    monkeypatch.setattr(sync, "build", intercepted)
    with pytest.raises(RuntimeError, match="fresh build reached"):
        asyncio.run(sync.synchronize(args))
    assert observed == [None]
    assert digest(old) in json.loads((tmp_path / "sync.json").read_text())["blocked_snapshots"]


def test_acl_refresh_serializes_old_and_new_scans(tmp_path):
    entered, release, newer_called = Event(), Event(), Event()
    class Old(SyncAPI):
        def get(self, method, **params):
            if method == "conversations.list":
                entered.set(); assert release.wait(3)
            return super().get(method, **params)
    class New(SyncAPI):
        def get(self, method, **params):
            newer_called.set()
            return {"channels": []} if method == "conversations.list" else super().get(method, **params)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(sync.refresh_access, Old(), tmp_path / "access.json")
        assert entered.wait(3)
        second = executor.submit(sync.refresh_access, New(), tmp_path / "access.json")
        try:
            assert not newer_called.wait(.1)
        finally:
            release.set(); first.result(); second.result()
    assert json.loads((tmp_path / "access.json").read_text())["channel_ids"] == []


def test_revoked_access_stops_the_next_model_dispatch(monkeypatch, tmp_path):
    calls = []
    async def post(self, url, **kwargs):
        calls.append(True)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": 0, "embedding": [1., 0.]}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    models = Models(config(), receipts=tmp_path / "ledger")
    def revoked():
        raise ValueError("access revoked")
    models.authorize = revoked
    with pytest.raises(RuntimeError, match="access"):
        asyncio.run(models.embed(["No longer authorized"]))
    assert calls == []


def test_failed_acceptance_blocks_candidate_and_stops_service(tmp_path, monkeypatch):
    from company_context import evaluate
    from starlette.testclient import TestClient
    args = setup_sync(tmp_path, monkeypatch, messages=[{"ts": "12", "text": "Original"}])
    models = SyntheticModels(); models.cost = 0
    monkeypatch.setattr(sync, "Models", lambda *a, **kw: models)
    commands = []
    monkeypatch.setattr(sync.subprocess, "run", lambda command, **kw: commands.append(command))
    original_rpc = evaluate.rpc
    def failed(client, method, params):
        if isinstance(client, TestClient):
            return original_rpc(client, method, params)
        value = Corpus(json.loads((tmp_path / "current/snapshot.json").read_text()))
        if params["name"] == "company_index_status":
            return {"snapshot": value.version, "availability": "ready"}
        raise RuntimeError("acceptance failure")
    monkeypatch.setattr(evaluate, "rpc", failed)
    with pytest.raises(RuntimeError, match="acceptance failure"):
        asyncio.run(sync.synchronize(args))
    candidate = Corpus(json.loads((tmp_path / "current/snapshot.json").read_text()))
    assert sync.availability(candidate, tmp_path / "access.json", tmp_path / "sync.json") != "ready"
    assert ["systemctl", "--user", "stop", "synthetic.service"] in commands


def test_newly_in_range_reply_is_not_hidden_by_same_parent_signature(tmp_path):
    first = capture(tmp_path / "first.sqlite", 20)
    first.execute("UPDATE channels SET done=0")
    class Replies(API):
        def get(self, method, **params):
            if method == "conversations.history":
                return {"messages": [{"ts": "5", "text": "root", "reply_count": 2, "latest_reply": "25"}]}
            if method == "conversations.replies":
                return {"messages": [{"ts": "15", "thread_ts": "5", "text": "Earlier"}, {"ts": "25", "thread_ts": "5", "text": "Newly in range"}]}
            return super().get(method, **params)
    collect_channel(Replies(), first, "C001", 10, 20)
    second = capture(tmp_path / "second.sqlite", 30); second.execute("UPDATE channels SET done=0")
    collect_channel(Replies(), second, "C001", 10, 30, first)
    assert set(second.execute("SELECT ts FROM messages")) == {("15",), ("25",)}
    first.close(); second.close()


def test_revoked_channel_is_retired_from_pending_capture(tmp_path):
    db = capture(tmp_path / "capture.sqlite", messages=[{"ts": "12", "text": "Now revoked"}])
    class Revoked(SyncAPI):
        def get(self, method, **params):
            return {"channels": []} if method == "conversations.list" else super().get(method, **params)
    assert collect(Revoked(), db, 10, 20)
    assert list(db.execute("SELECT id FROM channels")) == []
    assert list(db.execute("SELECT channel,ts FROM messages")) == []
    db.close()


def test_expired_cursor_restarts_only_its_channel_without_retaining_stale_rows(tmp_path):
    db = capture(tmp_path / "capture.sqlite", messages=[{"ts": "12", "text": "Removed during interruption"}])
    db.execute("UPDATE channels SET done=0,cursor='expired'")
    class Expired(API):
        def get(self, method, **params):
            if method == "conversations.history":
                if params.get("cursor"):
                    raise RuntimeError("invalid_cursor")
                return {"messages": [{"ts": "18", "text": "Current"}]}
            return super().get(method, **params)
    collect_channel(Expired(), db, "C001", 10, 20)
    assert set(db.execute("SELECT ts FROM messages")) == {("18",)}
    db.close()


def test_review_cannot_discount_a_failed_reservation(tmp_path):
    path = tmp_path / "receipts"
    rows = [{"call": 1, "status": "pending_cost_unknown", "charged_or_estimated_usd": .1}, {"call": 1, "status": "failed_cost_unknown", "charged_or_estimated_usd": .1}, {"call": 1, "status": "reviewed_failure_cost_reserved", "charged_or_estimated_usd": 0, "operator_review": "Investigated"}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    models = Models(config(), receipts=path)
    assert models.cost == pytest.approx(.1)


def test_exact_document_chunk_membership_rejects_extra_associations(tmp_path):
    async def run():
        value = Corpus({"principal": "reader", "records": [record()]})
        models = SyntheticModels(); directory = tmp_path / "index"
        await build(value.snapshot, directory, models)
        ready = json.loads((directory / "ready.json").read_text())
        rag = engine(directory / "index", models, workspace=ready["workspace"])
        await rag.initialize_storages()
        try:
            doc_id = next(iter(value.document_ids.values()))
            row = await rag.doc_status.get_by_id(doc_id)
            row["chunks_list"] = row["chunks_list"] + ["unexpected-chunk"]
            await rag.doc_status.upsert({doc_id: row})
            with pytest.raises(ValueError, match="chunk"):
                await verify_chunks(value, rag)
        finally:
            await close_engine(rag)
    asyncio.run(run())


def test_empty_capture_can_replace_last_source_without_model_calls(tmp_path):
    from company_context.context import open_index, search
    async def run():
        models = SyntheticModels()
        snapshot = {"principal": "reader", "records": []}
        directory = tmp_path / "empty"
        ready = await build(snapshot, directory, models)
        assert ready["source_count"] == 0
        corpus, rag = await open_index(directory, models)
        try:
            result = await search(corpus, rag, "Deleted evidence", "mix")
            assert result["evidence_status"] == "no_matches"
            assert result["results"] == [] and result["graph"] == []
            assert models.calls == 0
        finally:
            await close_engine(rag)
    asyncio.run(run())


def test_access_denial_without_dispatch_does_not_poison_a_recovered_reader(monkeypatch, tmp_path):
    calls = []
    async def post(self, url, **kwargs):
        calls.append(True)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": 0, "embedding": [1., 0.]}]})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    models = Models(config(), receipts=tmp_path / "ledger")
    def revoked():
        raise ValueError("access revoked")
    async def run():
        models.authorize = revoked
        with pytest.raises(RuntimeError, match="access"):
            await models.embed(["Rejected locally"])
        assert models.calls == 0
        models.authorize = lambda: None
        assert (await models.embed(["Authorized again"])).shape == (1, 2)
        assert calls == [True]
    asyncio.run(run())


def test_revocation_during_search_does_not_return_retrieved_sources(tmp_path):
    from starlette.testclient import TestClient
    from company_context.server import create_app
    from company_context.test_native import parse_rpc
    from company_context.test_sync import access
    value = Corpus({"principal": "reader", "records": [record()]})
    acl = tmp_path / "access.json"; access(acl)
    token = tmp_path / "token"; token.write_text("synthetic-not-a-secret-token-123456789"); token.chmod(0o600)
    async def revoked(*args, **kwargs):
        access(acl, ids=[])
        return {"metadata": {"query_mode": "naive"}, "data": {"chunks": [{"content": value.render(next(iter(value.chunks.values())))}]}}
    with TestClient(create_app(value, SimpleNamespace(aquery_data=revoked), token, acl)) as client:
        headers = {"Authorization": "Bearer " + token.read_text(), "Accept": "application/json, text/event-stream"}
        response = parse_rpc(client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_company_context", "arguments": {"query": "Pilot decision", "mode": "naive"}}}))
    assert response["result"]["isError"] is True
    assert record()["text"] not in json.dumps(response)


def test_access_command_does_not_lock_its_inventory_twice():
    program = '''
import json,signal,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from company_context import sync
class API:
    client=SimpleNamespace(close=lambda:None)
    def get(self,method,**params):
        if method=='auth.test':return {'team_id':'T1','user_id':'U1'}
        return {'channels':[]}
def expired(*args):raise TimeoutError('access watchdog self-deadlocked')
with tempfile.TemporaryDirectory() as work:
    sync.Slack=lambda _:API()
    sys.argv=['sync','access','--state-dir',work,'--token-file','synthetic']
    signal.signal(signal.SIGALRM,expired);signal.alarm(2)
    sync.main();signal.alarm(0)
    assert json.loads((Path(work)/'access.json').read_text())['channel_ids']==[]
    print('access command completed')
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "access command completed" in result.stdout


def test_replacement_never_dispatches_reconstruction_from_withdrawn_graph(tmp_path):
    async def run():
        original = tmp_path / "original"
        await build({"principal": "reader", "records": [record("a"), record("b")]}, original, SyntheticModels())
        models = SyntheticModels()
        ready = await build({"principal": "reader", "records": []}, tmp_path / "empty", models, original)
        assert ready["source_count"] == 0
        assert models.calls == 0
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://app.slack.com/client/T1/G002", "https://workspace.slack.com./client/T1/D002"])
def test_unsupported_slack_dependency_is_not_treated_as_a_public_web_link(url):
    row = record(source_type="docs", url="https://docs.example.com/page", record_kind="summary", derived_from=[url])
    with pytest.raises(ValueError, match="Slack"):
        Corpus({"principal": "reader", "records": [row]})


def test_withdrawal_precedes_candidate_acl_rejection(tmp_path, monkeypatch):
    old = {"principal": "slack:T1:U1", "records": [record()]}
    args = setup_sync(tmp_path, monkeypatch, old=old)
    monkeypatch.setattr(sync, "export_snapshot", lambda *a: {"principal": "slack:T1:U1", "records": [], "collection": {"channel_ids": ["C001", "C002"], "completed_at": "2026-11-26T00:00:00+00:00"}})
    with pytest.raises(ValueError, match="access changed"):
        asyncio.run(sync.synchronize(args))
    assert digest(old) in json.loads((tmp_path / "sync.json").read_text())["blocked_snapshots"]


def test_completed_bootstrap_can_retire_revoked_channels_without_mutating_archive(tmp_path, monkeypatch):
    args = setup_sync(tmp_path, monkeypatch, messages=[{"ts": "12", "text": "Revoked source"}])
    class Gone(SyncAPI):
        def get(self, method, **params):
            return {"channels": []} if method == "conversations.list" else super().get(method, **params)
    monkeypatch.setattr(sync, "Slack", lambda _: Gone())
    models = SyntheticModels(); models.cost = 0
    monkeypatch.setattr(sync, "Models", lambda *a, **kw: models)
    seen = []
    async def intercepted(snapshot, *args):
        seen.extend(snapshot["records"])
        assert snapshot["collection"]["channel_ids"] == []
        raise RuntimeError("fresh authorized build reached")
    monkeypatch.setattr(sync, "build", intercepted)
    with pytest.raises(RuntimeError, match="fresh authorized build reached"):
        asyncio.run(sync.synchronize(args))
    assert seen == []
    import sqlite3
    with sqlite3.connect(args.bootstrap_database) as db:
        assert set(db.execute("SELECT ts FROM messages")) == {("12",)}
