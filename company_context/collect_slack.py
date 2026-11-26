"""Resumable channel-only Slack collection; no model calls or index publication."""

import argparse
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

import httpx

from company_context.context import validate_slack_source
from company_context.server import token_from_file


SECRET = re.compile(r"(?:xox[baprs]-[A-Za-z0-9-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:or-v1-)?[A-Za-z0-9_-]{24,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def channel_allowed(info):
    identity = info.get("id", "")
    if not re.fullmatch(r"[CG][A-Z0-9]+", identity):
        return False
    try:
        validate_slack_source({"source_type": "slack", "url": f"https://workspace.slack.com/archives/{identity}/p0", "slack_conversation": info})
        return True
    except ValueError:
        return False


class Slack:
    def __init__(self, token_file):
        self.client = httpx.Client(base_url="https://slack.com/api/", headers={"Authorization": "Bearer " + token_from_file(token_file)}, timeout=60, trust_env=False, follow_redirects=False)
        self.previous = 0.
        self.calls = 0

    def get(self, method, **params):
        interval = 3.1 if method == "conversations.list" else 1.3
        for attempt in range(6):
            time.sleep(max(0, self.previous + interval - time.monotonic()))
            self.previous = time.monotonic()
            self.calls += 1
            response = self.client.get(method, params=params)
            if response.status_code == 429:
                remaining = max(1, int(response.headers.get("retry-after", "60")))
                while remaining > 0:
                    delay = min(60, remaining)
                    time.sleep(delay)
                    remaining -= delay
                continue
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(str(data.get("error", "slack_api_error")))
            return data
        raise RuntimeError("slack_rate_limit_retry_exhausted")


def open_store(path, since, until):
    if not all(math.isfinite(float(value)) for value in (since, until)) or float(since) >= float(until):
        raise ValueError("Collection interval must be finite and increasing")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, info TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '', history_done INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS threads (channel TEXT, ts TEXT, cursor TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel, ts));
        CREATE TABLE IF NOT EXISTS messages (channel TEXT, ts TEXT, payload TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(channel, ts));
        CREATE TABLE IF NOT EXISTS omitted_credentials (channel TEXT, ts TEXT, PRIMARY KEY(channel, ts));
    """)
    columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
    for name, definition in (("revision", "TEXT"), ("audited_at", "REAL")):
        if name not in columns:
            db.execute(f"ALTER TABLE threads ADD COLUMN {name} {definition}")
    expected = {"since": str(since), "until": str(until)}
    existing = dict(db.execute("SELECT key, value FROM metadata"))
    if existing and any(k not in existing or float(existing[k]) != float(v) for k, v in expected.items()):
        db.close()
        raise ValueError("Resume with the same frozen collection interval")
    with db:
        db.executemany("INSERT OR IGNORE INTO metadata VALUES (?, ?)", expected.items())
        if not existing:
            db.execute("INSERT INTO metadata VALUES ('capture_started_at', ?)", (datetime.now(timezone.utc).isoformat(),))
    return db


def inventory(api):
    result = {}; cursor = ""
    while True:
        page = api.get("conversations.list", types="public_channel,private_channel", exclude_archived="false", limit=200, cursor=cursor)
        for info in page.get("channels", []):
            if channel_allowed(info):
                result[info["id"]] = info
        cursor = page.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return result


def save_messages(db, channel, messages, since, until):
    for message in messages:
        ts = message.get("ts", "")
        if not ts or not since <= float(ts) <= until:
            continue
        payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
        if SECRET.search(payload):
            db.execute("DELETE FROM messages WHERE channel=? AND ts=?", (channel, ts))
            db.execute("INSERT OR IGNORE INTO omitted_credentials VALUES (?, ?)", (channel, ts))
            continue
        db.execute("DELETE FROM omitted_credentials WHERE channel=? AND ts=?", (channel, ts))
        db.execute("INSERT INTO messages VALUES (?, ?, ?, ?) ON CONFLICT(channel, ts) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256", (channel, ts, payload, hashlib.sha256(payload.encode()).hexdigest()))


def next_cursor(page):
    cursor = page.get("response_metadata", {}).get("next_cursor", "")
    if page.get("has_more") and not cursor:
        raise RuntimeError("Incomplete Slack pagination without a cursor")
    return cursor


def collect_channel(api, db, identity, since, until, previous=None, *, restarted=False):
    info = api.get("conversations.info", channel=identity)["channel"]
    if info.get("id") != identity or not channel_allowed(info):
        raise ValueError("Conversation is no longer an accessible channel")
    prior_until = float(dict(previous.execute("SELECT key,value FROM metadata"))["until"]) if previous else None
    while True:
        cursor, done = db.execute("SELECT cursor, history_done FROM channels WHERE id=?", (identity,)).fetchone()
        if done:
            break
        # Scan older parent metadata too: an old thread can contain replies inside the requested year.
        try:
            page = api.get("conversations.history", channel=identity, latest=str(until), inclusive="true", limit=200, cursor=cursor)
        except RuntimeError as error:
            if str(error) != "invalid_cursor" or not cursor or restarted:
                raise
            reset_channel(db, identity)
            return collect_channel(api, db, identity, since, until, previous, restarted=True)
        following = next_cursor(page)
        if cursor and following == cursor:
            raise RuntimeError("Slack history pagination did not advance")
        cursor = following
        with db:
            save_messages(db, identity, page.get("messages", []), since, until)
            for message in page.get("messages", []):
                if message.get("reply_count") and float(message.get("latest_reply") or until) >= since:
                    revision = json.dumps([message.get("reply_count"), message.get("latest_reply")])
                    db.execute("INSERT OR IGNORE INTO threads(channel, ts, revision) VALUES (?, ?, ?)", (identity, message["ts"], revision))
                    prior = previous.execute("SELECT revision,audited_at FROM threads WHERE channel=? AND ts=? AND done=1", (identity, message["ts"])).fetchone() if previous else None
                    if prior and prior[0] == revision and prior[1] and until - prior[1] < 86400 and message.get("latest_reply") and float(message["latest_reply"]) <= prior_until:
                        # Slack has no edit cursor: unchanged replies are re-read at least daily.
                        rows = [json.loads(r[0]) for r in previous.execute("SELECT payload FROM messages WHERE channel=? AND json_extract(payload,'$.thread_ts')=? AND ts!=?", (identity, message["ts"], message["ts"]))]
                        save_messages(db, identity, rows, since, until)
                        db.execute("UPDATE threads SET done=1,audited_at=? WHERE channel=? AND ts=?", (prior[1], identity, message["ts"]))
            db.execute("UPDATE channels SET info=?, cursor=?, history_done=? WHERE id=?", (json.dumps(info), cursor, int(not cursor), identity))
    while True:
        pending = db.execute("SELECT ts, cursor FROM threads WHERE channel=? AND done=0 ORDER BY ts LIMIT 1", (identity,)).fetchone()
        if not pending:
            break
        ts, cursor = pending
        try:
            page = api.get("conversations.replies", channel=identity, ts=ts, oldest=str(since), latest=str(until), inclusive="true", limit=200, cursor=cursor)
        except RuntimeError as error:
            if str(error) != "invalid_cursor" or not cursor or restarted:
                raise
            reset_channel(db, identity)
            return collect_channel(api, db, identity, since, until, previous, restarted=True)
        following = next_cursor(page)
        if cursor and following == cursor:
            raise RuntimeError("Slack thread pagination did not advance")
        cursor = following
        with db:
            save_messages(db, identity, page.get("messages", []), since, until)
            db.execute("UPDATE threads SET cursor=?, done=?,audited_at=? WHERE channel=? AND ts=?", (cursor, int(not cursor), until if not cursor else None, identity, ts))
    with db:
        db.execute("UPDATE channels SET done=1,error=NULL WHERE id=?", (identity,))


def reset_channel(db, identity):
    with db:
        for table in ("messages", "threads", "omitted_credentials"):
            db.execute(f"DELETE FROM {table} WHERE channel=?", (identity,))
        db.execute("UPDATE channels SET cursor='',history_done=0,done=0,error=NULL WHERE id=?", (identity,))


def status(db):
    return {"channels": db.execute("SELECT count(*) FROM channels").fetchone()[0], "complete_channels": db.execute("SELECT count(*) FROM channels WHERE done=1").fetchone()[0], "failed_channels": db.execute("SELECT count(*) FROM channels WHERE error IS NOT NULL").fetchone()[0], "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0], "text_characters": db.execute("SELECT coalesce(sum(length(json_extract(payload,'$.text'))),0) FROM messages").fetchone()[0], "credential_messages_omitted": db.execute("SELECT count(*) FROM omitted_credentials").fetchone()[0], "index_published": False}


def collect(api, db, since, until, previous=None):
    auth = api.get("auth.test")
    if auth.get("bot_id") or not auth.get("user_id"):
        raise ValueError("Use the named reader's user token, not a bot token")
    if not auth.get("team_id") or not auth.get("url", "").startswith("https://"):
        raise ValueError("Slack must confirm the reader's workspace")
    expected = {"reader": auth["user_id"], "team_id": auth["team_id"], "workspace_url": auth["url"]}
    for store in (db, previous):
        known = dict(store.execute("SELECT key,value FROM metadata")) if store else {}
        if any(key in known and known[key] != value for key, value in expected.items()):
            raise ValueError("The collection belongs to a different reader or workspace")
    if previous and not dict(previous.execute("SELECT key,value FROM metadata")).get("completed_at"):
        raise ValueError("Only a completed capture can supply unchanged replies")
    channels = inventory(api)
    with db:
        db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", expected.items())
        db.execute("DELETE FROM metadata WHERE key='completed_at'")
        for identity, info in channels.items():
            db.execute("INSERT INTO channels(id,info) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET info=excluded.info", (identity, json.dumps(info)))
        for (identity,) in list(db.execute("SELECT id FROM channels")):
            if identity not in channels:
                for table in ("messages", "threads", "omitted_credentials"):
                    db.execute(f"DELETE FROM {table} WHERE channel=?", (identity,))
                db.execute("DELETE FROM channels WHERE id=?", (identity,))
    for identity in channels:
        if db.execute("SELECT done FROM channels WHERE id=?", (identity,)).fetchone()[0]:
            continue
        try:
            collect_channel(api, db, identity, since, until, previous)
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            with db:
                code = str(error) if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z_]{1,60}", str(error)) else type(error).__name__
                db.execute("UPDATE channels SET error=? WHERE id=?", (code, identity))
        print(json.dumps(status(db)), flush=True)
    result = status(db)
    if result["failed_channels"] or result["complete_channels"] != result["channels"]:
        return False
    with db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('completed_at', ?)", (datetime.now(timezone.utc).isoformat(),))
    return True


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--since", required=True, help="Timezone-aware ISO timestamp")
    parser.add_argument("--until", required=True, help="Frozen timezone-aware ISO timestamp; reuse when resuming")
    parser.add_argument("--previous-database", help="A completed capture; unchanged replies are audited every 24 hours")
    args = parser.parse_args()
    dates = [datetime.fromisoformat(value) for value in (args.since, args.until)]
    if any(d.tzinfo is None for d in dates) or dates[0] >= dates[1]:
        parser.error("Provide an increasing timezone-aware interval")
    since, until = [d.timestamp() for d in dates]
    lock_path = Path(args.database).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = lock_path.open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = open_store(args.database, since, until)
    api = Slack(args.token_file)
    previous = sqlite3.connect(f"file:{Path(args.previous_database).resolve()}?mode=ro", uri=True) if args.previous_database else None
    try:
        if not collect(api, db, since, until, previous):
            raise SystemExit(1)
    finally:
        if previous:
            previous.close()
        api.client.close()
        db.close()
        lock.close()


if __name__ == "__main__":
    main()
