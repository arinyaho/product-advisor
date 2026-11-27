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


def test_capture_handles_malformed_pdf_text_and_only_metadata_404(tmp_path):
    class Recovery(API):
        def scopes(self): return ["user"]
        def page(self, scope, cursor):
            return {"files": [{"id": identity, "mimeType": "text/plain"} for identity in
                              ("pdf", "missing", "shortcut", "media", "vanished")]}
        def metadata(self, identity):
            if identity in ("missing", "target") or (identity == "vanished" and identity in self.contents):
                raise RuntimeError("drive_http_404")
            item = super().metadata(identity)
            if identity == "shortcut":
                return {**item, "mimeType": "application/vnd.google-apps.shortcut",
                        "shortcutDetails": {"targetId": "target"}}
            if identity == "pdf":
                return {**item, "mimeType": "application/pdf"}
            return item
        def content(self, item):
            if item["id"] == "media":
                raise RuntimeError("drive_http_404")
            if item["id"] == "pdf":
                return "Before \ud800 valid pair \ud83d\ude00 after", "PDF text"
            return super().content(item)

    db = open_store(tmp_path / "drive.sqlite", API.identity)
    collect(Recovery(), db)
    assert set(db.execute("SELECT id,status,reason,text FROM files")) == {
        ("pdf", "ready", None, "Before \ufffd valid pair \U0001f600 after"),
        ("missing", "omitted", "file_not_accessible", None),
        ("shortcut", "omitted", "shortcut_target_not_accessible", None),
        ("media", "failed", "drive_http_404", None),
        ("vanished", "omitted", "file_not_accessible", None),
    }
    assert "malformed surrogate" in db.execute("SELECT coverage FROM files WHERE id='pdf'").fetchone()[0]
    assert "finished_at" not in dict(db.execute("SELECT key,value FROM metadata"))
    db.close()


def test_operator_requeues_only_known_failures_without_rewriting_history(tmp_path):
    from company_context.collect_drive import retry_capture_errors

    db = open_store(tmp_path / "drive.sqlite", API.identity)
    with db:
        db.executemany("INSERT INTO files(id,metadata,status,reason) VALUES (?,'{}',?,?)", [
            ("pdf", "failed", "UnicodeEncodeError"),
            ("missing", "failed", "drive_http_404"),
            ("unknown", "failed", "drive_http_403"),
            ("ready", "ready", None),
            ("omitted", "omitted", "unsupported_mime_type"),
        ])
    assert retry_capture_errors(db) == 2
    assert retry_capture_errors(db) == 0
    assert set(db.execute("SELECT id,status,reason FROM files")) == {
        ("pdf", "pending", None), ("missing", "pending", None),
        ("unknown", "failed", "drive_http_403"), ("ready", "ready", None),
        ("omitted", "omitted", "unsupported_mime_type"),
    }
    assert set(db.execute("SELECT id,previous_error FROM operator_retries")) == {
        ("pdf", "UnicodeEncodeError"), ("missing", "drive_http_404"),
    }
    db.close()


def test_404_with_known_resource_key_remains_pending_operator_recovery(tmp_path):
    class LinkShared(API):
        def scopes(self): return ["user"]
        def page(self, scope, cursor):
            return {"files": [
                {"id": "known-key", "mimeType": "text/plain", "resourceKeyPresent": True},
                {"id": "legacy-link", "mimeType": "text/plain",
                 "webViewLink": "https://drive.google.com/file/d/legacy-link/view?resourcekey=synthetic"},
                {"id": "shortcut", "mimeType": "application/vnd.google-apps.shortcut"},
            ]}
        def metadata(self, identity):
            if identity == "shortcut":
                return {"id": identity, "mimeType": "application/vnd.google-apps.shortcut",
                        "shortcutDetails": {"targetId": "target", "targetResourceKeyPresent": True}}
            raise RuntimeError("drive_http_404")

    db = open_store(tmp_path / "drive.sqlite", API.identity)
    collect(LinkShared(), db)
    assert set(db.execute("SELECT id,status,reason FROM files")) == {
        (identity, "failed", "drive_http_404") for identity in ("known-key", "legacy-link", "shortcut")
    }
    assert "finished_at" not in dict(db.execute("SELECT key,value FROM metadata"))
    db.close()


def test_google_metadata_reports_presence_without_retaining_raw_resource_keys(monkeypatch):
    from company_context.collect_drive import Drive

    def item():
        return {"id": "link", "resourceKey": "synthetic-file-key",
                "shortcutDetails": {"targetId": "target", "targetResourceKey": "synthetic-target-key"}}

    def read(path, **params):
        assert "resourceKey" in params["fields"]
        assert "targetResourceKey" in params["fields"]
        return {"files": [item()]} if path == "files" else item()

    api = Drive.__new__(Drive)
    monkeypatch.setattr(api, "json", read)
    expected = {"id": "link", "resourceKeyPresent": True,
                "shortcutDetails": {"targetId": "target", "targetResourceKeyPresent": True}}
    assert api.page("user", "")["files"] == [expected]
    assert api.metadata("link") == expected
