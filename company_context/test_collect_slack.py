"""Channel-only collection keeps recent replies to older parents and resumes safely."""

import json

import pytest

from company_context.collect_slack import collect_channel, inventory, open_store, save_messages, status


def channel(identity="C001", **fields):
    return {"id": identity, "is_im": False, "is_mpim": False, "is_private": False, "is_member": False, **fields}


class API:
    def __init__(self):
        self.calls = []
    def get(self, method, **params):
        self.calls.append((method, params))
        if method == "conversations.list":
            assert params["types"] == "public_channel,private_channel"
            return {"channels": [channel(), channel("D001", is_im=True), channel("G001", is_mpim=True), channel("C002", is_private=True, is_member=True), {"id": "C003"}]}
        if method == "conversations.info":
            return {"channel": channel(params["channel"])}
        if method == "conversations.history":
            return {"messages": [{"ts": "5", "text": "old parent", "reply_count": 1, "latest_reply": "15"}, {"ts": "12", "text": "recent root"}], "has_more": False}
        if method == "conversations.replies":
            assert params["ts"] == "5"
            return {"messages": [{"ts": "5", "text": "old parent"}, {"ts": "15", "thread_ts": "5", "text": "recent reply"}]}
        raise AssertionError(method)


def test_inventory_excludes_dm_mpim_and_unclassified_conversations():
    api = API()
    assert set(inventory(api)) == {"C001", "C002"}
    assert [name for name, _ in api.calls] == ["conversations.list"]


def test_recent_reply_to_old_thread_is_collected_without_old_parent_body(tmp_path):
    db = open_store(tmp_path / "messages.sqlite", 10, 20)
    with db:
        db.execute("INSERT INTO channels(id, info) VALUES (?,?)", ("C001", json.dumps(channel())))
    api = API()
    collect_channel(api, db, "C001", 10, 20)
    assert set(db.execute("SELECT channel,ts FROM messages")) == {("C001", "12"), ("C001", "15")}
    assert json.loads(db.execute("SELECT payload FROM messages WHERE ts='15'").fetchone()[0])["thread_ts"] == "5"
    assert status(db)["complete_channels"] == 1
    first_calls = len(api.calls)
    collect_channel(api, db, "C001", 10, 20)
    assert len(api.calls) == first_calls + 1
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
    db.close()
    with pytest.raises(ValueError, match="same frozen"):
        open_store(tmp_path / "messages.sqlite", 11, 20)


def test_dm_reclassification_blocks_history_read(tmp_path):
    db = open_store(tmp_path / "messages.sqlite", 10, 20)
    api = API()
    api.get = lambda method, **params: {"channel": channel("D001", is_im=True)}
    with pytest.raises(ValueError, match="accessible channel"):
        collect_channel(api, db, "C001", 10, 20)
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    db.close()


def test_obvious_credentials_are_omitted_and_storage_is_private(tmp_path):
    path = tmp_path / "messages.sqlite"
    db = open_store(path, 10, 20)
    with db:
        save_messages(db, "C001", [{"ts": "12", "text": "ordinary message"}, {"ts": "13", "text": "xoxb-" + "a" * 32}], 10, 20)
    assert list(db.execute("SELECT ts FROM messages")) == [("12",)]
    assert list(db.execute("SELECT ts FROM omitted_credentials")) == [("13",)]
    assert path.stat().st_mode & 0o777 == 0o600
    db.close()


def test_credential_edit_removes_previously_retained_version(tmp_path):
    db = open_store(tmp_path / "messages.sqlite", 10, 20)
    with db:
        save_messages(db, "C001", [{"ts": "12", "text": "old version"}], 10, 20)
        save_messages(db, "C001", [{"ts": "12", "text": "xoxb-" + "a" * 32}], 10, 20)
    assert list(db.execute("SELECT ts FROM messages")) == []
    assert list(db.execute("SELECT ts FROM omitted_credentials")) == [("12",)]
    db.close()


def test_unchanged_thread_reuse_expires_and_deleted_threads_do_not_survive(tmp_path):
    def store(name, until):
        db = open_store(tmp_path / name, 10, until)
        with db:
            db.execute("INSERT INTO channels(id,info) VALUES (?,?)", ("C001", json.dumps(channel())))
        return db
    first = store("first.sqlite", 20)
    collect_channel(API(), first, "C001", 10, 20)
    second = store("second.sqlite", 30)
    api = API()
    collect_channel(api, second, "C001", 10, 30, first)
    assert not any(method == "conversations.replies" for method, _ in api.calls)
    assert set(second.execute("SELECT ts FROM messages")) == {("12",), ("15",)}
    audited = store("audit.sqlite", 90000)
    api = API()
    original = api.get
    def changed(method, **params):
        page = original(method, **params)
        if method == "conversations.replies":
            page["messages"] = [{"ts": "15", "thread_ts": "5", "text": "Edited reply", "edited": {"ts": "80000"}}]
        return page
    api.get = changed
    collect_channel(api, audited, "C001", 10, 90000, second)
    assert json.loads(audited.execute("SELECT payload FROM messages WHERE ts='15'").fetchone()[0])["text"] == "Edited reply"
    deleted = store("deleted.sqlite", 90010)
    api = API()
    api.get = lambda method, **params: {"channel": channel()} if method == "conversations.info" else {"messages": [{"ts": "12", "text": "retained"}]}
    collect_channel(api, deleted, "C001", 10, 90010, audited)
    assert set(deleted.execute("SELECT ts FROM messages")) == {("12",)}
    for db in (first, second, audited, deleted):
        db.close()
