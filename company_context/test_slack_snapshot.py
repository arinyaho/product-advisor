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


def test_continuous_timeline_is_bounded_without_dropping_or_duplicating_messages(tmp_path):
    start = 1788739200
    messages = [{"ts": str(start + i * 5), "user": "U1", "text": str(i).zfill(5) + "x" * 165} for i in range(11268)]
    db = open_store(tmp_path / "capture.sqlite", start - 1, start + 86400)
    with db:
        db.execute("INSERT INTO channels(id,info,done) VALUES (?,?,1)", ("C001", json.dumps(channel())))
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("reader", "U1"), ("team_id", "T1"), ("workspace_url", "https://workspace.slack.com/"), ("completed_at", "2026-11-26T01:00:00+00:00")])
        save_messages(db, "C001", messages, start - 1, start + 86400)
    snapshot = export_snapshot(db)
    corpus = Corpus(snapshot)
    assert [span["ts"] for row in snapshot["records"] for span in row["message_spans"]] == [message["ts"] for message in messages]
    assert all(len(row["text"]) <= 32000 and len(row["message_spans"]) <= 100 for row in snapshot["records"])
    for row in snapshot["records"]:
        assert float(row["message_spans"][-1]["ts"]) - float(row["message_spans"][0]["ts"]) <= 1800
        assert "".join(corpus.chunks[identity]["text"] for identity in corpus.source_chunks[row["source_id"]]) == row["text"]
    assert len({r["source_id"] for r in snapshot["records"]}) == len(snapshot["records"])
    db.close()


def test_timeline_window_is_measured_from_start_and_threads_remain_separate():
    rows = [{"ts": str(100 + i * 600), "text": "nearby"} for i in range(8)]
    rows += [{"ts": "110", "text": "root", "reply_count": 1}, {"ts": "5000", "thread_ts": "110", "text": "late reply"}]
    grouped = {key: [m["ts"] for m in values] for key, values in conversations(rows)}
    assert grouped == {("timeline", "100"): ["100", "700", "1300", "1900"], ("thread", "110"): ["110", "5000"], ("timeline", "2500"): ["2500", "3100", "3700", "4300"]}


def test_export_excludes_only_bot_conversations_and_retains_human_thread(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", 10, 9000)
    messages = [{"ts": "20", "bot_id": "B1", "text": "alert"}, {"ts": "21", "user": "U1", "subtype": "channel_join", "text": "joined"}, {"ts": "30", "bot_id": "B1", "reply_count": 1, "text": "bot root"}, {"ts": "31", "bot_id": "B1", "thread_ts": "30", "text": "bot reply"}, {"ts": "40", "bot_id": "B1", "reply_count": 2, "text": "keep bot root"}, {"ts": "41", "user": "U1", "thread_ts": "40", "text": "human decision"}, {"ts": "42", "bot_id": "B1", "thread_ts": "40", "text": "keep bot reply"}, {"ts": "5000", "user": "U1", "text": "human timeline"}, {"ts": "5001", "bot_id": "B1", "text": "nearby bot"}]
    with db:
        db.execute("INSERT INTO channels(id,info,done) VALUES (?,?,1)", ("C001", json.dumps(channel())))
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("reader", "U1"), ("team_id", "T1"), ("workspace_url", "https://workspace.slack.com/"), ("completed_at", "2026-11-26T01:00:00+00:00")])
        save_messages(db, "C001", messages, 10, 9000)
    snapshot = export_snapshot(db)
    assert {s["ts"] for r in snapshot["records"] for s in r["message_spans"]} == {"40", "41", "42", "5000", "5001"}
    assert snapshot["collection"]["bot_only_messages_excluded"] == 4
    assert {ts for (ts,) in db.execute("SELECT ts FROM messages")} == {m["ts"] for m in messages}
    db.close()


@pytest.mark.parametrize('message,expected', [({'user':'UB','bot_id':'B1'},False),({'app_id':'A1'},False),({'subtype':'bot_message'},False),({'user':'U1','subtype':'channel_join'},False),({'user':'U1','subtype':'file_share'},True),({},True)])
def test_only_explicit_bot_or_system_authorship_is_excluded(message, expected):
    from company_context.slack_snapshot import has_human_content
    assert has_human_content([message | {'ts':'10','text':'content'}]) is expected


def test_one_long_timeline_message_keeps_original_text_and_chunk_offsets():
    from company_context.slack_snapshot import message_block
    messages=[{'ts':'10','user':'U1','text':'original ' * 5000},{'ts':'11','user':'U2','text':'next'}]
    groups=conversations(messages,lambda m:len(message_block('https://workspace.slack.com/','C001',m)))
    assert [[m['ts'] for m in group] for _,group in groups] == [['10'],['11']]
    row=source(messages[:1],kind='timeline')
    corpus=Corpus({'principal':'reader','records':[row]})
    assert ''.join(c['text'] for c in corpus.chunks.values()) == row['text']
    assert all(len(c['text'])<=3200 for c in corpus.chunks.values())
