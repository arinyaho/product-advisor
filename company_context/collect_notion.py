"""Read-only, resumable Notion capture on the company host; no model calls."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import httpx

from company_context.collect_slack import SECRET
from company_context.server import token_from_file


class Notion:
    def __init__(self, token_file):
        self.client = httpx.Client(base_url="https://api.notion.com/v1/", headers={"Authorization": "Bearer " + token_from_file(token_file), "Notion-Version": "2025-09-03"}, timeout=60, trust_env=False, follow_redirects=False)
        self.previous = 0.

    def request(self, method, path, **kwargs):
        for attempt in range(6):
            time.sleep(max(0, self.previous + .4 - time.monotonic()))
            self.previous = time.monotonic()
            response = self.client.request(method, path, **kwargs)
            if response.status_code == 429:
                remaining = max(1, int(response.headers.get("retry-after", "60")))
                while remaining:
                    delay = min(60, remaining); time.sleep(delay); remaining -= delay
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Notion rate-limit retry exhausted")


def open_store(path, identity):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (kind TEXT, id TEXT, cursor TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0, error TEXT, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT, payload TEXT NOT NULL, sha256 TEXT NOT NULL, captured_at TEXT NOT NULL, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS members (task_kind TEXT, task_id TEXT, object_kind TEXT, object_id TEXT, position INTEGER, PRIMARY KEY(task_kind,task_id,object_kind,object_id));
        CREATE TABLE IF NOT EXISTS omissions (kind TEXT, id TEXT, reason TEXT, PRIMARY KEY(kind,id,reason));
    """)
    known = dict(db.execute("SELECT key,value FROM metadata"))
    if known and any(known.get(k) != v for k, v in identity.items()):
        db.close()
        raise ValueError("Capture belongs to another Notion connection/workspace")
    with db:
        db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", identity.items())
        enqueue(db, "search", "all")
    return db


def enqueue(db, kind, identity):
    db.execute("INSERT OR IGNORE INTO tasks(kind,id) VALUES (?,?)", (kind, identity))


def sanitize_files(value):
    if isinstance(value, list):
        return [sanitize_files(v) for v in value]
    if isinstance(value, dict):
        return {key: ("[temporary signed file URL omitted]" if key == "url" and ("X-Amz-Signature=" in str(child) or "X-Amz-Credential=" in str(child)) else sanitize_files(child)) for key, child in value.items()}
    return value


def retain(db, item, task, position):
    kind, identity = item.get("object", "unknown"), item.get("id")
    if not identity:
        raise ValueError("Notion returned an object without a stable identity")
    payload = json.dumps(sanitize_files(item), sort_keys=True, ensure_ascii=False)
    if SECRET.search(payload):
        db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, identity))
        db.execute("INSERT OR IGNORE INTO omissions VALUES (?,?,?)", (kind, identity, "credential_pattern"))
        return
    db.execute("INSERT INTO objects VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,sha256=excluded.sha256,captured_at=excluded.captured_at", (kind, identity, payload, hashlib.sha256(payload.encode()).hexdigest(), datetime.now(timezone.utc).isoformat()))
    db.execute("INSERT OR REPLACE INTO members VALUES (?,?,?,?,?)", (*task, kind, identity, position))
    if item.get("archived") or item.get("in_trash"):
        return
    if kind == "page":
        enqueue(db, "page", identity)
        enqueue(db, "children", identity)
        enqueue(db, "comments", identity)
        for prop in item.get("properties", {}).values():
            if prop.get("type") in ("relation", "people", "rich_text", "title", "rollup") and prop.get("id"):
                enqueue(db, "property", identity + "/" + prop["id"])
    elif kind == "data_source":
        enqueue(db, "query", identity)
    elif kind == "database":
        for source in item.get("data_sources", []):
            enqueue(db, "data_source", source["id"])
    elif kind == "block":
        block_type = item.get("type")
        if item.get("has_children"):
            enqueue(db, "children", identity)
        if block_type == "child_page":
            enqueue(db, "page", identity)
        if block_type == "child_database":
            enqueue(db, "database", identity)
        if block_type == "synced_block":
            original = item.get("synced_block", {}).get("synced_from") or {}
            if original.get("block_id"):
                enqueue(db, "children", original["block_id"])
        if block_type == "unsupported":
            db.execute("INSERT OR IGNORE INTO omissions VALUES (?,?,?)", (kind, identity, "unsupported_block"))


def fetch(api, kind, identity, cursor):
    params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
    if kind == "search":
        return api.request("POST", "search", json=params)
    if kind == "query":
        return api.request("POST", f"data_sources/{identity}/query", json=params)
    if kind == "children":
        return api.request("GET", f"blocks/{identity}/children", params=params)
    if kind == "comments":
        return api.request("GET", "comments", params={**params, "block_id": identity})
    if kind == "property":
        page, prop = identity.split("/", 1)
        return api.request("GET", f"pages/{page}/properties/{prop}", params=params)
    paths = {"page": "pages", "database": "databases", "data_source": "data_sources"}
    return api.request("GET", f"{paths[kind]}/{identity}")


def status(db):
    return {"pages": db.execute("SELECT count(*) FROM objects WHERE kind='page'").fetchone()[0], "blocks": db.execute("SELECT count(*) FROM objects WHERE kind='block'").fetchone()[0], "data_sources": db.execute("SELECT count(*) FROM objects WHERE kind='data_source'").fetchone()[0], "tasks": db.execute("SELECT count(*) FROM tasks").fetchone()[0], "completed_tasks": db.execute("SELECT count(*) FROM tasks WHERE done=1").fetchone()[0], "failed_tasks": db.execute("SELECT count(*) FROM tasks WHERE error IS NOT NULL").fetchone()[0], "omissions": db.execute("SELECT count(*) FROM omissions").fetchone()[0], "model_calls": 0, "index_published": False}


def collect(api, db):
    while True:
        task = db.execute("SELECT kind,id,cursor FROM tasks WHERE done=0 AND error IS NULL ORDER BY CASE kind WHEN 'search' THEN 0 WHEN 'query' THEN 1 ELSE 2 END,rowid LIMIT 1").fetchone()
        if not task:
            break
        kind, identity, cursor = task
        try:
            response = fetch(api, kind, identity, cursor)
            following = response.get("next_cursor") or ""
            if response.get("has_more") and (not following or following == cursor):
                raise ValueError("Notion pagination did not advance")
            items = response.get("results", []) if response.get("object") == "list" else [response]
            with db:
                offset = db.execute("SELECT count(*) FROM members WHERE task_kind=? AND task_id=?", (kind, identity)).fetchone()[0]
                for index, item in enumerate(items, offset):
                    if kind == "property":
                        # Property items reuse the property's ID; preserve every paginated value.
                        item = {**item, "id": identity + ":" + str(index)}
                    retain(db, item, (kind, identity), index)
                db.execute("UPDATE tasks SET cursor=?,done=? WHERE kind=? AND id=?", (following, int(not following), kind, identity))
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
            with db:
                if kind == "comments" and code in (403, 404):
                    db.execute("INSERT OR IGNORE INTO omissions VALUES (?,?,?)", (kind, identity, "comments_not_accessible"))
                    db.execute("UPDATE tasks SET done=1 WHERE kind=? AND id=?", (kind, identity))
                else:
                    db.execute("UPDATE tasks SET error=? WHERE kind=? AND id=?", (str(code), kind, identity))
        print(json.dumps(status(db)), flush=True)
    result = status(db)
    with db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('finished_at',?)", (datetime.now(timezone.utc).isoformat(),))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage',?)", ("Accessible API pages, properties and recursively fetched blocks; comment denials and unsupported blocks are explicit omissions. Files are not downloaded. Search is not an atomic or guaranteed exhaustive workspace export.",))
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    args = parser.parse_args()
    api = Notion(args.token_file)
    args.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.database.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = api.request("GET", "users/me")
        if identity.get("type") != "bot" or identity.get("bot", {}).get("workspace_id") != args.workspace_id:
            raise ValueError("Notion connection is not in the authorized company workspace")
        db = open_store(args.database, {"workspace_id": args.workspace_id, "connection_id": identity["id"], "api_version": "2025-09-03"})
        try:
            result = collect(api, db)
            if result["failed_tasks"]:
                raise SystemExit(1)
        finally:
            db.close()
            api.client.close()


if __name__ == "__main__":
    main()
