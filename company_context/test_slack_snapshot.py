"""Conversation boundaries and exact message provenance survive chunking and export."""

import json

import pytest

from company_context.collect_slack import open_store, save_messages
from company_context.context import Corpus
from company_context.slack_snapshot import conversations, export_snapshot, source_record
from company_context.test_collect_slack import channel


def source(messages, kind="thread", root="10"):
    return source_record("T1", "https://workspace.slack.com/", channel(), kind, root, messages, "2026-11-26T00:00:00+00:00")


def test_thread_and_timeline_membership_are_disjoint():
    rows = [{"ts": "10", "text": "root", "reply_count": 1}, {"ts": "11", "text": "timeline"}, {"ts": "12", "thread_ts": "10", "text": "reply"}, {"ts": "13", "text": "nearby"}, {"ts": "3000", "text": "later"}]
    groups = {key: [m["ts"] for m in messages] for key, messages in conversations(rows)}
    assert groups == {("thread", "10"): ["10", "12"], ("timeline", "11"): ["11", "13"], ("timeline", "3000"): ["3000"]}


def test_messages_stay_whole_and_root_context_is_traceable():
    row = source([{"ts": str(10 + i), "user": f"U{i}", "text": str(i) * 900} for i in range(6)])
    corpus = Corpus({"principal": "reader", "records": [row]})
    chunks = list(corpus.chunks.values())
    boundaries = {s["start"] for s in row["message_spans"]} | {len(row["text"])}
    assert all(c["start"] in boundaries and c["end"] in boundaries for c in chunks)
    assert "".join(c["text"] for c in chunks) == row["text"]
    assert {m["ts"] for c in chunks for m in c["messages"]} == {str(i) for i in range(10, 16)}
    for c in chunks[1:]:
        read = corpus.read(c["chunk_id"])
        assert corpus.read(read["root_chunk_id"])["messages"][0]["ts"] == "10"
        assert read["root_context"]["text"] == row["text"][:row["message_spans"][0]["end"]]


def test_oversized_message_has_exact_offsets_and_missing_root_is_explicit():
    row = source([{"ts": "20", "text": "가나다\n" * 4000}], root="1")
    corpus = Corpus({"principal": "reader", "records": [row]})
    chunks = list(corpus.chunks.values())
    assert "".join(c["text"] for c in chunks) == row["text"]
    assert all(len(c["text"]) <= 3200 for c in chunks)
    assert all(corpus.read(c["chunk_id"])["root_chunk_id"] is None for c in chunks)
    assert all(c["root_in_capture"] is False for c in chunks)


def test_export_requires_complete_capture_and_keeps_unchanged_provenance(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", 10, 20)
    with db:
        db.execute("INSERT INTO channels(id,info,done) VALUES (?,?,1)", ("C001", json.dumps(channel())))
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("reader", "U1"), ("team_id", "T1"), ("workspace_url", "https://workspace.slack.com/")])
        save_messages(db, "C001", [{"ts": "12", "text": "Exact original", "user": "U2"}], 10, 20)
    with pytest.raises(ValueError, match="complete"):
        export_snapshot(db)
    with db:
        db.execute("INSERT INTO metadata VALUES ('completed_at','2026-11-26T01:00:00+00:00')")
    first = export_snapshot(db)
    assert {r["source_id"] for r in first["records"]} == {"slack:T1:C001:timeline:12"}
    with db:
        db.execute("UPDATE metadata SET value='2026-11-26T02:00:00+00:00' WHERE key='completed_at'")
    assert export_snapshot(db, first)["records"] == first["records"]
    with db:
        db.execute("UPDATE channels SET error='access_not_confirmed'")
    with pytest.raises(ValueError, match="complete"):
        export_snapshot(db)
    db.close()
