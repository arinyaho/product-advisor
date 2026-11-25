"""Turn a complete channel capture into traceable conversations without summarizing it."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from company_context.collect_slack import channel_allowed, status


CHUNK_TARGET = 3200
TIMELINE_GAP = 30 * 60


def iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def permalink(workspace, channel, ts):
    return f"{workspace.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"


def conversations(messages):
    """Threads never mix with the channel timeline; adjacency does not imply a topic."""
    threads, timeline = {}, []
    for message in sorted(messages, key=lambda m: float(m["ts"])):
        root = message.get("thread_ts") or (message["ts"] if message.get("reply_count") else None)
        if root:
            threads.setdefault(("thread", root), []).append(message)
        else:
            timeline.append(message)
    groups = dict(threads)
    current = []
    for message in timeline:
        if current and (float(message["ts"]) - float(current[-1]["ts"]) > TIMELINE_GAP or iso(message["ts"])[:10] != iso(current[-1]["ts"])[:10]):
            groups[("timeline", current[0]["ts"])] = current
            current = []
        current.append(message)
    if current:
        groups[("timeline", current[0]["ts"])] = current
    return sorted(groups.items(), key=lambda pair: float(pair[0][1]))


def source_record(team, workspace, info, kind, root, messages, collected):
    text, spans = "", []
    for message in messages:
        body = message.get("text", "")
        if not body.strip():
            body = "[No message text; attachment contents are not collected.]"
        url = permalink(workspace, info["id"], message["ts"])
        author = message.get("user") or message.get("bot_id")
        block = f"[{iso(message['ts'])} | {author or 'unknown author'} | {url}]\n{body}\n\n"
        spans.append({"ts": message["ts"], "author": author, "url": url, "occurred_at": iso(message["ts"]), "updated_at": iso(message["edited"]["ts"]) if message.get("edited", {}).get("ts") else None, "start": len(text), "end": len(text) + len(block)})
        text += block
    edits = [s["updated_at"] for s in spans if s["updated_at"]]
    return {
        "source_id": f"slack:{team}:{info['id']}:{kind}:{root}", "source_type": "slack", "record_kind": "source",
        "url": permalink(workspace, info["id"], root), "title": f"#{info.get('name', info['id'])} {kind} {iso(root)}",
        "author": None, "occurred_at": spans[0]["occurred_at"], "updated_at": max(edits) if edits else None,
        "collected_at": collected, "slack_conversation": {k: info.get(k) for k in ("id", "is_im", "is_mpim", "is_private", "is_member")},
        "conversation_kind": kind, "thread_ts": root if kind == "thread" else None,
        "root_in_capture": kind == "thread" and any(s["ts"] == root for s in spans),
        "coverage": "Captured message text only; files, linked documents, reactions and attachments are not indexed. Timeline groups express proximity, not a shared topic. An older thread root can be outside the capture interval.",
        "message_spans": spans, "chunking": "slack-messages-v1", "text": text,
    }


def chunk_ranges(source):
    spans, text = source["message_spans"], source["text"]
    position = 0
    for span in spans:
        if span["start"] != position or not position < span["end"] <= len(text):
            raise ValueError("Message spans must exactly partition captured text")
        position = span["end"]
    if not spans or position != len(text):
        raise ValueError("Message spans must exactly partition captured text")
    start, end = 0, 0
    for span in spans:
        if end > start and span["end"] - start > CHUNK_TARGET:
            yield start, end
            start = end
        while span["end"] - start > CHUNK_TARGET:
            limit = start + CHUNK_TARGET
            # A single oversized message uses a paragraph/sentence boundary, then a hard bound.
            candidates = [text.rfind(marker, start + CHUNK_TARGET // 2, limit) + len(marker) for marker in ("\n", ". ", "。", "다. ")]
            cut = max(candidates)
            cut = cut if cut > start + CHUNK_TARGET // 2 else limit
            yield start, cut
            start = cut
        end = span["end"]
    if end > start:
        yield start, end


def export_snapshot(db, previous=None):
    meta = dict(db.execute("SELECT key,value FROM metadata"))
    progress = status(db)
    if not meta.get("completed_at") or progress["failed_channels"] or progress["channels"] != progress["complete_channels"]:
        raise ValueError("Only a complete, successful channel capture can be exported")
    if not all(meta.get(key) for key in ("reader", "team_id", "workspace_url", "since", "until")):
        raise ValueError("Capture is missing its authenticated reader, workspace or interval")
    records = []
    for identity, encoded in db.execute("SELECT id,info FROM channels ORDER BY id"):
        info = json.loads(encoded)
        if info["id"] != identity or not channel_allowed(info):
            raise ValueError("Capture contains an unclassified or inaccessible conversation")
        messages = [json.loads(row[0]) for row in db.execute("SELECT payload FROM messages WHERE channel=? ORDER BY CAST(ts AS REAL)", (identity,))]
        for (kind, root), grouped in conversations(messages):
            records.append(source_record(meta["team_id"], meta["workspace_url"], info, kind, root, grouped, meta["completed_at"]))
    # Preserve collection provenance for unchanged records, so a new polling time is not an edit.
    prior = {r["source_id"]: r for r in (previous or {}).get("records", [])}
    for record in records:
        old = prior.get(record["source_id"])
        if old and {k: v for k, v in old.items() if k != "collected_at"} == {k: v for k, v in record.items() if k != "collected_at"}:
            record["collected_at"] = old["collected_at"]
    return {"principal": f"slack:{meta['team_id']}:{meta['reader']}", "records": records, "collection": {
        "kind": "slack_channel_capture", "since": iso(meta["since"]), "through": iso(meta["until"]),
        "completed_at": meta["completed_at"], "channels": progress["channels"],
        "channel_ids": [row[0] for row in db.execute("SELECT id FROM channels ORDER BY id")],
        "messages": progress["messages"], "credential_messages_omitted": progress["credential_messages_omitted"],
        "dm_included": False, "files_downloaded": False, "atomic_provider_snapshot": False,
    }}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.database}?mode=ro", uri=True) as db:
        snapshot = export_snapshot(db, json.loads(args.previous.read_text()) if args.previous else None)
    with args.output.open("x") as stream:
        json.dump(snapshot, stream, ensure_ascii=False)
    print(json.dumps({"sources": len(snapshot["records"]), "collection": snapshot["collection"]}))


if __name__ == "__main__":
    main()
