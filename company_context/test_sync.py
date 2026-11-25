"""Revoked access, known edits, incomplete builds and reader changes fail closed."""

from datetime import datetime, timedelta, timezone
import json
import asyncio
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from company_context.server import create_app
from company_context.sync import availability, changed_or_removed, publish, refresh_access, write_json
from company_context.test_context import corpus, record
from company_context.test_native import parse_rpc, tool_result
from company_context.test_collect_slack import channel


def access(path, ids=None, age=0):
    write_json(path, {"principal": "slack:T1:U1", "checked_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(), "channel_ids": ["C001"] if ids is None else ids})


def test_access_expiry_and_known_changes_block_reads_but_keep_status_available(tmp_path):
    value = corpus()
    path, state = tmp_path / "access.json", tmp_path / "sync.json"
    assert availability(value, path) == "blocked_invalid_sync_state"
    access(path)
    assert availability(value, path) == "ready"
    access(path, age=7201)
    assert availability(value, path) == "blocked_access_inventory_expired"
    access(path, ids=[])
    assert availability(value, path) == "blocked_channel_access_changed"
    access(path)
    write_json(state, {"blocked_snapshots": [value.version]})
    assert availability(value, path, state) == "blocked_known_source_change"
    token = tmp_path / "token"
    token.write_text("synthetic-not-a-secret-token-123456789")
    token.chmod(0o600)
    with TestClient(create_app(value, None, token, path, state)) as client:
        headers = {"Authorization": "Bearer " + token.read_text(), "Accept": "application/json, text/event-stream"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "company_index_status", "arguments": {}}}
        response = parse_rpc(client.post("/mcp", headers=headers, json=request))
        assert tool_result(response)["availability"] == "blocked_known_source_change"
        request["params"] = {"name": "get_company_source", "arguments": {"chunk_id": next(iter(value.chunks))}}
        response = parse_rpc(client.post("/mcp", headers=headers, json=request))
        assert response["result"]["isError"] is True
        assert record()["text"] not in json.dumps(response)


def test_summary_dependencies_also_follow_channel_access(tmp_path):
    value = corpus([record(record_kind="summary", derived_from=["https://workspace.slack.com/archives/G002/p1"])])
    path = tmp_path / "access.json"
    access(path)
    assert availability(value, path) == "blocked_channel_access_changed"


def test_changed_identity_set_and_incomplete_publication(tmp_path):
    old = corpus([record(), record("removed"), record("edited", text="old")]).snapshot
    new = corpus([record(), record("added"), record("edited", text="new")]).snapshot
    assert changed_or_removed(old, new) == {"removed", "edited"}
    candidate = tmp_path / "candidate"; candidate.mkdir()
    current = tmp_path / "current"
    with pytest.raises(ValueError, match="unfinished"):
        publish(candidate, current)
    assert not current.exists()


def test_access_refresh_cannot_change_reader(tmp_path):
    class API:
        def get(self, method, **params):
            if method == "auth.test":
                return {"team_id": "T1", "user_id": "U2"}
            return {"channels": [channel()]}
    path = tmp_path / "access.json"
    access(path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="another reader"):
        refresh_access(API(), path)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("initial_complete", [True, False])
def test_complete_capture_builds_then_publishes_and_accepts_exact_mcp_source(tmp_path, monkeypatch, initial_complete):
    from company_context import sync
    from company_context.collect_slack import open_store, save_messages
    from company_context.context import Corpus
    from company_context.test_native import SyntheticModels

    database = tmp_path / "initial.sqlite"
    db = open_store(database, 10, 20)
    with db:
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("reader", "U1"), ("team_id", "T1"), ("workspace_url", "https://workspace.slack.com/"), ("completed_at", "2026-11-26T00:00:00+00:00")])
        db.execute("INSERT INTO channels(id,info,done) VALUES (?,?,1)", ("C001", json.dumps(channel())))
        save_messages(db, "C001", [{"ts": "12", "text": "The pilot was evaluated.", "user": "U1"}], 10, 20)
        if not initial_complete:
            db.execute("DELETE FROM metadata WHERE key='completed_at'")
            db.execute("UPDATE channels SET done=0")
    db.close()
    class API:
        client = SimpleNamespace(close=lambda: None)
        def get(self, method, **params):
            if method == "auth.test":
                return {"team_id": "T1", "user_id": "U1", "url": "https://workspace.slack.com/"}
            if method == "conversations.info":
                return {"channel": channel()}
            if method == "conversations.history":
                return {"messages": [{"ts": "12", "text": "The pilot was evaluated.", "user": "U1"}]}
            return {"channels": [channel()]}
    monkeypatch.setattr(sync, "Slack", lambda _: API())
    models = SyntheticModels(); models.cost = 0
    monkeypatch.setattr(sync, "Models", lambda *a, **kw: models)
    restarted = []
    monkeypatch.setattr(sync.subprocess, "run", lambda command, **kw: restarted.append(command))
    accepted = []
    def rpc(client, method, params):
        value = Corpus(json.loads((tmp_path / "current" / "snapshot.json").read_text()))
        accepted.append(params["name"])
        if params["name"] == "company_index_status":
            return {"snapshot": value.version, "availability": "ready"}
        return value.read(params["arguments"]["chunk_id"])
    monkeypatch.setattr("company_context.evaluate.rpc", rpc)
    config = tmp_path / "config.json"; config.write_text("{}")
    token = tmp_path / "token"; token.write_text("synthetic-not-a-secret-token-123456789"); token.chmod(0o600)
    args = SimpleNamespace(state_dir=tmp_path, token_file=token, command="sync", bootstrap_database=database, since=datetime.fromtimestamp(10, timezone.utc), supplement=None, config=config, max_model_calls=50, budget_usd=1, service="synthetic.service", mcp_token_file=token, port=8766)
    asyncio.run(sync.synchronize(args))
    state = json.loads((tmp_path / "sync.json").read_text())
    assert state["phase"] == "published"
    report = sync.progress(tmp_path / "sync.json")
    assert report["capture"] == {"channels": 1, "complete_channels": 1, "failed_channels": 0, "messages": 1}
    assert str(tmp_path) not in json.dumps(report)
    assert (tmp_path / "current").is_symlink()
    published = json.loads((tmp_path / "current" / "snapshot.json").read_text())
    assert {r["source_id"] for r in published["records"]} == {"slack:T1:C001:timeline:12"}
    assert accepted == ["company_index_status", "get_company_source"]
    assert restarted == [["systemctl", "--user", "restart", "synthetic.service"]]
