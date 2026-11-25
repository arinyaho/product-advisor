"""Notion capture follows database rows and nested blocks without model calls."""

import json

import httpx
import pytest

from company_context.collect_notion import collect, enqueue, open_store, retain, sanitize_files, status


class API:
    def __init__(self):
        self.calls = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "search":
            return {"object": "list", "results": [{"object": "page", "id": "p1", "properties": {}}, {"object": "data_source", "id": "d1"}], "has_more": False}
        if path == "data_sources/d1/query":
            return {"object": "list", "results": [{"object": "page", "id": "p2", "properties": {}}]}
        if path.startswith("pages/"):
            return {"object": "page", "id": path.split('/')[1], "properties": {}}
        if path == "blocks/p1/children":
            if kwargs["params"].get("start_cursor"):
                return {"object": "list", "results": [{"object": "block", "id": "b2", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Second"}]}}]}
            return {"object": "list", "results": [{"object": "block", "id": "b1", "type": "toggle", "has_children": True}], "has_more": True, "next_cursor": "next"}
        if path == "blocks/b1/children":
            return {"object": "list", "results": [{"object": "block", "id": "nested", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Nested text"}]}}]}
        if path == "comments":
            raise httpx.HTTPStatusError("denied", request=httpx.Request("GET", "https://api.notion.com/v1/comments"), response=httpx.Response(403))
        return {"object": "list", "results": []}


def test_full_capture_preserves_exact_object_identities_and_denied_comment_coverage(tmp_path):
    db = open_store(tmp_path / "notion.sqlite", {"workspace_id": "W1", "connection_id": "bot1", "api_version": "2025-09-03"})
    api = API(); result = collect(api, db)
    assert set(db.execute("SELECT kind,id FROM objects")) == {("page", "p1"), ("page", "p2"), ("data_source", "d1"), ("block", "b1"), ("block", "b2"), ("block", "nested"), *[("response_metadata", key) for key in ("search:all:", "query:d1:", "children:p1:", "children:p1:next", "children:p2:", "children:b1:")]}
    assert set(db.execute("SELECT id,reason FROM omissions")) == {("p1", "comments_not_accessible"), ("p2", "comments_not_accessible")}
    assert result["failed_tasks"] == 0 and result["model_calls"] == 0 and result["index_published"] is False
    calls = len(api.calls); collect(api, db); assert len(api.calls) == calls
    assert json.loads(db.execute("SELECT payload FROM objects WHERE id='nested'").fetchone()[0])["paragraph"]["rich_text"][0]["plain_text"] == "Nested text"
    db.close()


def test_wrong_connection_fails_before_fetching_and_file_credentials_are_not_retained(tmp_path):
    path = tmp_path / "notion.sqlite"
    open_store(path, {"workspace_id": "W1", "connection_id": "bot1"}).close()
    with pytest.raises(ValueError, match="another"):
        open_store(path, {"workspace_id": "W2", "connection_id": "bot1"})
    assert path.stat().st_mode & 0o777 == 0o600
    value = {"file": {"url": "https://example.com/file?X-Amz-Signature=synthetic"}, "text": "Original text"}
    result = sanitize_files(value)
    assert result["file"]["url"] == "[temporary signed file URL omitted]"
    assert result["text"] == "Original text"


def test_property_pages_count_omitted_positions_without_overwriting_values(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", {"workspace_id": "W"})
    with db: enqueue(db, "property", "p/r")
    class PropertyAPI:
        def request(self, method, path, **kwargs):
            if path == "search": return {"object": "list", "results": []}
            values = ["Second retained value"] if kwargs["params"].get("start_cursor") else ["sk-" + "s" * 24, "First retained value"]
            return {"object": "list", "results": [{"object": "property_item", "id": "r", "type": "rich_text", "rich_text": {"plain_text": v}} for v in values], "next_cursor": "next" if len(values) == 2 else None}
    collect(PropertyAPI(), db)
    rows = {identity: json.loads(payload)["rich_text"]["plain_text"] for identity, payload in db.execute("SELECT id,payload FROM objects WHERE kind='property_item'")}
    assert rows == {"p/r:1": "First retained value", "p/r:2": "Second retained value"}
    db.close()


def test_rollup_envelope_and_incomplete_database_query_are_preserved(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", {"workspace_id": "W"})
    with db:
        enqueue(db, "property", "p/r")
        enqueue(db, "query", "d")
    class SpecialAPI:
        def request(self, method, path, **kwargs):
            if path.startswith("pages/"):
                return {"object": "list", "results": [], "property_item": {"type": "rollup", "rollup": {"type": "number", "number": 42}}}
            if path.startswith("data_sources/"):
                return {"object": "list", "results": [], "has_more": False, "request_status": {"type": "incomplete"}}
            return {"object": "list", "results": []}
    collect(SpecialAPI(), db)
    envelope = db.execute("SELECT payload FROM objects WHERE kind='response_metadata' AND id='property:p/r:'").fetchone()
    assert envelope and json.loads(envelope[0])["response"]["property_item"]["rollup"]["number"] == 42
    assert db.execute("SELECT done,error FROM tasks WHERE kind='query' AND id='d'").fetchone() == (0, "incomplete_query")
    db.close()


def test_credential_parent_still_schedules_children_and_formula_is_fetched(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", {"workspace_id": "W"})
    with db:
        retain(db, {"object": "block", "id": "parent", "type": "toggle", "has_children": True, "text": "sk-" + "s" * 24}, ("children", "p"), 0)
        retain(db, {"object": "page", "id": "formula-page", "properties": {"computed": {"id": "f", "type": "formula"}}}, ("search", "all"), 1)
    tasks = set(db.execute("SELECT kind,id FROM tasks"))
    assert ("children", "parent") in tasks
    assert ("property", "formula-page/f") in tasks
    assert db.execute("SELECT id FROM objects WHERE id='parent'").fetchone() is None
    db.close()


def test_hosted_file_url_is_removed_independently_of_signing_scheme():
    value = {"type": "file", "file": {"url": "https://files.example.com/bearer-path", "expiry_time": "2026-11-26T00:00:00Z"}}
    assert sanitize_files(value)["file"]["url"] == "[temporary signed file URL omitted]"


def test_incomplete_query_cannot_report_completion(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", {"workspace_id": "W"})
    with db: enqueue(db, "query", "capped")
    class CappedAPI:
        def request(self, method, path, **kwargs):
            return {"object": "list", "results": [], "has_more": False, **({"request_status": {"type": "incomplete"}} if path.startswith("data_sources/") else {})}
    result = collect(CappedAPI(), db)
    assert db.execute("SELECT done,error FROM tasks WHERE kind='query'").fetchone() == (0, "incomplete_query")
    assert result["failed_tasks"] == 1
    db.close()


def test_formula_property_has_its_own_retrieval_task(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", {"workspace_id": "W"})
    with db: retain(db, {"object": "page", "id": "p", "properties": {"formula": {"id": "f", "type": "formula"}}}, ("search", "all"), 0)
    assert set(db.execute("SELECT id FROM tasks WHERE kind='property'")) == {("p/f",)}
    db.close()


def test_legacy_capture_is_preserved_and_cannot_silently_resume(tmp_path):
    path = tmp_path / "capture.sqlite"
    db = open_store(path, {"workspace_id": "W"})
    with db: db.execute("DELETE FROM metadata WHERE key='capture_schema'")
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="start a new database"):
        open_store(path, {"workspace_id": "W"})
    assert path.read_bytes() == before


def test_capped_query_reads_remaining_pages_before_reporting_incomplete(tmp_path):
    db = open_store(tmp_path / "capture.sqlite", {"workspace_id": "W"})
    with db: enqueue(db, "query", "capped")
    calls = []
    class CappedAPI(API):
        def request(self, method, path, **kwargs):
            if path == "search": return {"object": "list", "results": []}
            if path == "data_sources/capped/query":
                cursor = kwargs["json"].get("start_cursor", ""); calls.append(cursor)
                return {"object": "list", "results": [{"object": "page", "id": "second" if cursor else "first", "properties": {}}], "has_more": not cursor, "next_cursor": None if cursor else "next", **({"request_status": {"type": "incomplete"}} if not cursor else {})}
            return super().request(method, path, **kwargs)
    collect(CappedAPI(), db)
    assert calls == ["", "next"]
    assert set(db.execute("SELECT id FROM objects WHERE kind='page'")) == {("first",), ("second",)}
    assert db.execute("SELECT cursor,consumed,done,error FROM tasks WHERE kind='query'").fetchone() == ("", 2, 0, "incomplete_query")
    db.close()


def test_interrupted_legacy_initialization_cannot_be_labeled_schema_two(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE tasks(kind TEXT,id TEXT,cursor TEXT NOT NULL DEFAULT '',done INTEGER NOT NULL DEFAULT 0,error TEXT,PRIMARY KEY(kind,id));")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="start a new database"):
        open_store(path, {"workspace_id": "W"})
    assert path.read_bytes() == before
