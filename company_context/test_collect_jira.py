"""Bounded Jira capture preserves issue provenance and fails closed."""

from datetime import date
import json

import httpx
import pytest

from company_context.collect_jira import Jira, capture, save_capture


TENANT = "example.atlassian.net"
SINCE = date(2026, 11, 1)


def issue(identity="12", *, project="ES2"):
    return {
        "id": identity,
        "key": f"{project}-{identity}",
        "fields": {
            "project": {"key": project},
            "summary": "Validate pilot scope",
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Check requirements"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "with the buyer."},
                ]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Second paragraph."}]},
            ]},
            "reporter": {"accountId": "jira-user:1", "emailAddress": "never-index@example.net"},
            "created": "2026-11-05T10:00:00.000+0900",
            "updated": "2026-11-06T10:00:00.000+0900",
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "parent": {"key": "ES2-1", "fields": {"summary": "omit parent body"}},
            "issuelinks": [{"type": {"outward": "blocks"},
                            "outwardIssue": {"key": "ES2-4", "fields": {"summary": "omit linked body"}}}],
        },
    }


def jira(monkeypatch, handler, *, email="worker@example.net"):
    monkeypatch.setenv("ATLASSIAN_EMAIL", "worker@example.net")

    def request(req):
        assert req.url.host == TENANT
        if req.url.path.endswith("/myself"):
            return httpx.Response(200, json={"active": True, "accountId": "jira-user:1", "emailAddress": email})
        return handler(req)

    return httpx.Client(transport=httpx.MockTransport(request), base_url=f"https://{TENANT}")


def test_capture_preserves_exact_issue_membership_and_provenance(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    calls = []

    def handler(req):
        assert req.url.path == "/rest/api/3/search/jql"
        assert req.url.params["jql"] == 'project = "ES2" AND updated >= "2026-11-01" ORDER BY updated ASC, key ASC'
        assert "comment" not in req.url.params["fields"]
        cursor = req.url.params.get("nextPageToken")
        calls.append(cursor)
        if cursor is None:
            return httpx.Response(200, json={"issues": [issue()], "isLast": False, "nextPageToken": "next"})
        return httpx.Response(200, json={"issues": [issue("13")], "isLast": True})

    with jira(monkeypatch, handler) as client:
        api = Jira(TENANT, client)
        result = capture(api, "ES2", SINCE, 2)
    assert calls == [None, "next"]
    assert {row["source_id"] for row in result["records"]} == {f"jira:{TENANT}:12", f"jira:{TENANT}:13"}
    row = result["records"][0]
    assert row["url"] == f"https://{TENANT}/browse/ES2-12"
    assert row["provider_scope"] == {"provider": "jira", "connection": api.connection, "resource_id": "12"}
    assert row["author"] == "jira-user:1"
    assert row["occurred_at"] == "2026-11-05T01:00:00+00:00"
    assert row["updated_at"] == "2026-11-06T01:00:00+00:00"
    assert "Check requirements\nwith the buyer.\nSecond paragraph." in row["text"]
    assert f"Parent: https://{TENANT}/browse/ES2-1" in row["text"]
    assert f"blocks: https://{TENANT}/browse/ES2-4" in row["text"]
    assert "omit parent body" not in row["text"] and "omit linked body" not in row["text"]
    assert "never-index@example.net" not in json.dumps(result)
    assert result["collection"]["omitted_fields"] == ["comments", "attachments", "changelog", "linked_issue_bodies"]
    output = tmp_path / "jira.json"
    save_capture(output, result)
    assert json.loads(output.read_text()) == result
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_capture(output, {"records": []})
    assert json.loads(output.read_text()) == result


@pytest.mark.parametrize(
    ("issues", "last", "max_issues", "match"),
    [
        ([issue()], False, 1, "issue limit exceeded"),
        ([issue("12"), issue("12")], True, 2, "repeated issue ID"),
        ([issue(project="OTHER")], True, 2, "outside the requested project"),
    ],
)
def test_capture_rejects_untrusted_or_incomplete_issue_batches(monkeypatch, issues, last, max_issues, match):
    def handler(req):
        return httpx.Response(200, json={"issues": issues, "isLast": last, "nextPageToken": "next"})
    with jira(monkeypatch, handler) as client, pytest.raises(ValueError, match=match):
        capture(Jira(TENANT, client), "ES2", SINCE, max_issues)


def test_capture_refuses_identity_mismatch(monkeypatch):
    def unexpected(req):
        pytest.fail("Jira issue query must not run for the wrong account")
    with jira(monkeypatch, unexpected, email="other@example.net") as client, pytest.raises(ValueError, match="differs"):
        Jira(TENANT, client)


def test_capture_counts_secret_pattern_as_omission(monkeypatch):
    row = issue()
    row["fields"]["description"] = "sk-" + "X" * 32
    def handler(req):
        return httpx.Response(200, json={"issues": [row], "isLast": True})
    with jira(monkeypatch, handler) as client:
        result = capture(Jira(TENANT, client), "ES2", SINCE, 10)
    assert result["records"] == []
    assert result["collection"]["discovered_issues"] == 1
    assert result["collection"]["omissions"] == {"credential_pattern": 1, "source_size_limit": 0}
