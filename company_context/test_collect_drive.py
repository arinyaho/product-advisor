"""Drive capture keeps identities, frozen revisions and explicit omissions."""

import json

import pytest

from company_context.collect_drive import collect, extract, open_store, text_runs


class API:
    identity = {"emailAddress": "reader@example.com", "permissionId": "reader"}
    def __init__(self): self.pages = []; self.contents = []
    def scopes(self): return ["user", "shared"]
    def page(self, scope, cursor):
        self.pages.append((scope, cursor))
        return {"files": [{"id": "b" if cursor else "a", "mimeType": "text/plain"}], **({"nextPageToken": "next"} if scope == "user" and not cursor else {})}
    def metadata(self, identity): return {"id": identity, "mimeType": "text/plain", "version": "1", "modifiedTime": "2026-11-26T00:00:00Z"}
    def content(self, item): self.contents.append(item["id"]); return "Original " + item["id"], "text"


def test_capture_shared_files_deduplicates_and_resumes_completed_items(tmp_path):
    api = API(); db = open_store(tmp_path / "drive.sqlite", api.identity)
    result = collect(api, db)
    assert api.pages == [("user", ""), ("user", "next"), ("shared", "")]
    assert set(db.execute("SELECT id,text,status FROM files")) == {("a", "Original a", "ready"), ("b", "Original b", "ready")}
    finished = dict(db.execute("SELECT key,value FROM metadata"))["finished_at"]
    collect(api, db)
    assert dict(db.execute("SELECT key,value FROM metadata"))["finished_at"] == finished
    assert api.contents == ["a", "b"]
    assert result["model_calls"] == 0 and result["index_published"] is False
    assert dict(db.execute("SELECT key,value FROM metadata"))["finished_at"]
    db.close()


def test_incomplete_inventory_and_changed_source_cannot_finish(tmp_path):
    class Incomplete(API):
        def page(self, *a): return {"incompleteSearch": True, "files": []}
    db = open_store(tmp_path / "drive.sqlite", API.identity)
    with pytest.raises(ValueError, match="incomplete"): collect(Incomplete(), db)
    assert "finished_at" not in dict(db.execute("SELECT key,value FROM metadata"))
    class Changed(API):
        def metadata(self, identity): return {**super().metadata(identity), "version": "2" if identity in self.contents else "1"}
    collect(Changed(), db)
    assert set(db.execute("SELECT id,status,reason FROM files")) == {("a", "failed", "source_changed_during_capture"), ("b", "failed", "source_changed_during_capture")}
    assert "finished_at" not in dict(db.execute("SELECT key,value FROM metadata"))
    db.close()


def test_workspace_mismatch_and_credential_body_omission(tmp_path):
    path = tmp_path / "drive.sqlite"; db = open_store(path, API.identity)
    with pytest.raises(ValueError, match="another reader"): open_store(path, {"emailAddress": "other@example.com", "permissionId": "other"})
    class Secret(API):
        def content(self, item): return "sk-" + "s" * 24, "text"
    collect(Secret(), db)
    assert set(db.execute("SELECT id,text,status,reason FROM files")) == {("a", None, "omitted", "credential_pattern"), ("b", None, "omitted", "credential_pattern")}
    db.close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_native_text_runs_include_nested_tabs_tables_and_notes():
    value = [{"documentTab": {"body": {"content": [{"textRun": {"content": "First"}}]}}, "childTabs": [{"textRun": {"content": "Second"}}]}, {"notesPage": {"table": [{"textRun": {"content": "Notes"}}]}}]
    assert text_runs(value) == "FirstSecondNotes"
    assert extract(b"original\ntext", "text/plain")[0] == "original\ntext"
