"""Jira pilot must retain an exact capture, source identities and Jira visibility."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from company_context import collect_jira, document_sources, document_sync, sync
from company_context.collect_jira import Jira, OMITTED_FIELDS, access_receipt, issue_record, mien_access, save_capture
from company_context.context import Corpus, close_engine, open_index, search
from company_context.test_collect_jira import SINCE, TENANT, issue, jira
from company_context.test_context import record
from company_context.test_document_sync import READER, cache, doc, setup_publication
from company_context.test_native import SyntheticModels


CONNECTION = hashlib.sha256(f"{TENANT}\njira-user:1".encode()).hexdigest()
COLLECTED = "2026-11-27T00:00:00+00:00"


def jira_row(identity="12"):
    return issue_record(issue(identity), TENANT, CONNECTION, "ES2", COLLECTED)


def frozen(tmp_path, rows, name="jira.json"):
    path = tmp_path / name
    tmp_path.chmod(0o700)
    save_capture(path, {"records": rows, "collection": {
        "provider": "jira", "capture_schema": 1, "tenant": TENANT, "connection": CONNECTION,
        "project": "ES2", "since": SINCE.isoformat(), "completed_at": COLLECTED,
        "discovered_issues": len(rows), "omitted_fields": list(OMITTED_FIELDS),
        "omissions": {"credential_pattern": 0, "source_size_limit": 0},
    }})
    return {"tenant": TENANT, "project": "ES2", "connection": CONNECTION, "principal": READER,
            "since": SINCE.isoformat(), "capture_file": str(path),
            "capture_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_export_pins_bytes_reader_and_scope(tmp_path):
    row = jira_row()
    config = frozen(tmp_path, [row])
    result = document_sources.export_capture("jira", config, READER)
    assert result["records"] == [row]
    assert result["collection"]["capture_id"] == config["capture_sha256"]
    with pytest.raises(ValueError, match="reader scope"):
        document_sources.export_capture("jira", config, "slack:other:reader")
    with pytest.raises(ValueError, match="digest"):
        document_sources.export_capture("jira", {**config, "capture_sha256": "0" * 64}, READER)
    with pytest.raises(ValueError, match="pinned scope"):
        document_sources.export_capture("jira", {**config, "since": "2026-11-27"}, READER)
    unrelated = {**row, "url": f"https://{TENANT}/browse/OTHER-12"}
    different_project = frozen(tmp_path, [unrelated], "other.json")
    with pytest.raises(ValueError, match="outside the pinned capture scope"):
        document_sources.export_capture("jira", different_project, READER)


@pytest.mark.parametrize("changes", [
    {"source_id": f"jira:{TENANT}:99"},
    {"url": f"https://{TENANT}/browse/ES2-12?share=wrong"},
    {"url": "https://different.atlassian.net/browse/ES2-12"},
    {"source_type": "docs"},
])
def test_corpus_rejects_false_managed_jira_identity(changes):
    row = {**jira_row(), **changes}
    with pytest.raises(ValueError, match="Jira source identity"):
        Corpus({"principal": READER, "records": [row]})


def test_issue_access_only_allows_exact_id_project_and_key(monkeypatch):
    requests = []

    def handler(req):
        assert list(req.url.params.items()) == [("fields", "project")]
        identity = req.url.path.rsplit("/", 1)[-1]
        requests.append(identity)
        if identity.isdigit() and int(identity) in (13, 14, 17):
            return httpx.Response({13: 403, 14: 404, 17: 503}[int(identity)])
        project = "OTHER" if identity == "15" else "ES2"
        key = f"{project}-{identity}" if identity != "16" else "ES2-99"
        return httpx.Response(200, json={"id": identity, "key": key, "fields": {"project": {"key": project}}})

    with jira(monkeypatch, handler) as client:
        receipt = access_receipt(TENANT, client, "ES2", CONNECTION, {str(i): f"ES2-{i}" for i in range(12, 18)})
    assert requests == ["12", "13", "14", "15", "16", "17"]
    assert receipt == {"visible": ["12"], "denied": ["13", "14", "15", "16"], "failure": "http_503"}


def test_wrong_issue_id_cannot_pass_as_visible(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"id": "99", "key": "ES2-12", "fields": {"project": {"key": "ES2"}}})
    with jira(monkeypatch, handler) as client:
        assert access_receipt(TENANT, client, "ES2", CONNECTION, {"12": "ES2-12"}) == {
            "visible": [], "denied": [], "failure": "lookup",
        }


def test_single_mien_child_has_no_parent_jira_credentials_and_rejects_extra_ids(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "previous-profile-token")
    config = {"mien_executable": "/fixed/mien", "mien_profile": "cryptolab",
              "tenant": TENANT, "project": "ES2", "connection": CONNECTION}
    calls = []
    response = {"visible": ["12"], "denied": ["13"], "failure": None}
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ["/fixed/mien", "exec", "cryptolab", "--"]
        assert "previous-profile-token" not in argv and "ATLASSIAN_API_TOKEN" not in kwargs["env"]
        assert json.loads(kwargs["input"]) == {"12": "ES2-12", "13": "ES2-13"}
        return SimpleNamespace(stdout=json.dumps(response))
    monkeypatch.setattr(collect_jira.subprocess, "run", run)
    assert mien_access(config, {"12": "ES2-12", "13": "ES2-13"}) == (["12"], ["13"], None)
    assert len(calls) == 1
    response["visible"] = ["99"]
    with pytest.raises(RuntimeError, match="did not return a valid receipt"):
        mien_access(config, {"12": "ES2-12", "13": "ES2-13"})


def test_jira_revocation_persists_without_refreshing_stale_acl_after_unknown_error(tmp_path, monkeypatch):
    rows = [jira_row("12"), jira_row("13")]
    config = frozen(tmp_path, rows)
    path = tmp_path / "document-access.json"
    sync.write_json(path, {"principal": READER, "providers": {
        "jira": {"connection": CONNECTION, "checked_at": COLLECTED, "resource_ids": ["12", "13"]},
    }})
    def access(_, issues):
        assert issues == {"12": "ES2-12", "13": "ES2-13"}
        return [], ["12"], "http_503"
    monkeypatch.setattr(document_sources, "mien_access", access)
    with pytest.raises(RuntimeError, match="http_503"):
        document_sources.refresh_document_access(tmp_path, {"principal": READER, "providers": {"jira": config}}, [{"records": rows}])
    assert json.loads(path.read_text())["providers"]["jira"] == {
        "connection": CONNECTION, "checked_at": COLLECTED, "resource_ids": ["13"],
    }


def test_jira_publishes_by_existing_mcp_path_and_refuses_replacement(tmp_path, monkeypatch):
    settings, model_calls = setup_publication(tmp_path, monkeypatch)
    row = jira_row()
    config = frozen(tmp_path, [row])
    settings["providers"] = {"jira": config}
    notion = doc()
    drive = doc("drive", "f1")
    slack = record("slack")
    static_jira = record("static-jira", source_type="jira", url=f"https://{TENANT}/browse/ES2-9")
    original = {"principal": READER, "records": [notion, drive, slack, static_jira]}
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    sync.write_json(old_dir / "snapshot.json", original)
    (tmp_path / "current").symlink_to(old_dir)
    sync.write_json(tmp_path / "document-sources.json", settings)

    def refresh(*_):
        cache(tmp_path)
        path = tmp_path / "document-access.json"
        access = json.loads(path.read_text())
        access["providers"]["jira"] = {"connection": CONNECTION,
            "resource_ids": ["12"], "checked_at": access["providers"]["notion"]["checked_at"]}
        sync.write_json(path, access)
    monkeypatch.setattr(document_sync, "refresh_document_access", refresh)
    monkeypatch.setattr(document_sources, "refresh_document_access", refresh)
    monkeypatch.setattr(document_sync, "refresh_access", lambda *_: {"principal": READER})

    async def run():
        await document_sync.synchronize(tmp_path, settings)
        current = tmp_path / "current"
        published = json.loads((current / "snapshot.json").read_text())
        assert {r["source_id"]: r for r in published["records"]} == {
            r["source_id"]: r for r in original["records"] + [row]
        }
        assert published["collection"]["managed_sources"]["jira"]["indexed_sources"] == 1
        corpus, engine = await open_index(current, SyntheticModels())
        try:
            result = await search(corpus, engine, "pilot requirements with the buyer", "mix", 10)
            assert result["retrieval"]["actual_mode"] == "mix"
            matches = [r for r in result["results"] if r["source_id"] == row["source_id"]]
            assert matches and corpus.read(matches[0]["chunk_id"])["text"] == row["text"]
        finally:
            await close_engine(engine)
        settings["providers"]["jira"] = frozen(tmp_path, [], "empty.json")
        await document_sync.synchronize(tmp_path, settings)
        result = json.loads((tmp_path / "document-ingestion.json").read_text())
        assert result["jira"]["phase"] == "jira_capture_changed"
        assert json.loads((current / "snapshot.json").read_text()) == published

    asyncio.run(run())
    assert model_calls == [(tmp_path / "sync-model-receipts.jsonl", 200)]


def test_static_jira_alias_overlap_does_not_publish_or_remove_legacy_record(tmp_path, monkeypatch):
    settings, model_calls = setup_publication(tmp_path, monkeypatch)
    settings["providers"] = {"jira": frozen(tmp_path, [jira_row()])}
    static_jira = record("static-jira", source_type="jira", url=f"https://{TENANT}/browse/ES2-12?legacy=yes")
    old = {"principal": READER, "records": [static_jira]}
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    sync.write_json(old_dir / "snapshot.json", old)
    (tmp_path / "current").symlink_to(old_dir)
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert not model_calls
    assert json.loads((tmp_path / "document-ingestion.json").read_text())["jira"]["phase"] == "jira_unmanaged_alias_overlap"
    assert json.loads((tmp_path / "current/snapshot.json").read_text()) == old
