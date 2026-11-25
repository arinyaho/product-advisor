"""Notion capture follows database rows and nested blocks without model calls."""

import json

import httpx
import pytest

from company_context.collect_notion import collect, open_store, sanitize_files, status


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
    assert set(db.execute("SELECT kind,id FROM objects")) == {("page", "p1"), ("page", "p2"), ("data_source", "d1"), ("block", "b1"), ("block", "b2"), ("block", "nested")}
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
