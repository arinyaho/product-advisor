"""Capture a bounded set of Jira Cloud issues under one verified account."""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import httpx

from company_context.collect_slack import SECRET
from company_context.context import JIRA_CONNECTION, JIRA_ISSUE_ID, JIRA_PROJECT, JIRA_TENANT


ISSUE_FIELDS = "project,summary,description,reporter,created,updated,status,issuetype,parent,issuelinks"
OMITTED_FIELDS = ("comments", "attachments", "changelog", "linked_issue_bodies")
BLOCK_NODES = {"paragraph", "heading", "listItem", "codeBlock", "blockquote", "tableRow"}


class JiraIdentityError(ValueError):
    pass


class JiraHTTPError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"jira_http_{status}")
        self.status = status


class Jira:
    def __init__(self, tenant, client):
        self.client = client
        self.tenant = tenant
        try:
            me = self.get("myself")
        except JiraHTTPError as error:
            if error.status in (401, 403):
                raise JiraIdentityError("Jira rejected the reader credentials") from None
            raise
        account = me.get("accountId")
        if me.get("active") is not True or not isinstance(account, str) or not account:
            raise JiraIdentityError("Jira did not identify an active account")
        if str(me.get("emailAddress", "")).casefold() != os.environ["ATLASSIAN_EMAIL"].casefold():
            raise JiraIdentityError("Jira account differs from the configured reader")
        self.connection = hashlib.sha256(f"{tenant}\n{account}".encode()).hexdigest()

    def get(self, path, **params):
        try:
            response = self.client.get("/rest/api/3/" + path, params=params)
        except httpx.RequestError:
            raise RuntimeError("Jira request failed; rerun the bounded capture after review") from None
        if response.status_code != 200:
            raise JiraHTTPError(response.status_code)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected a Jira response object")
        return data


def description_text(node):
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    text = "".join(description_text(child) for child in node.get("content", []))
    return text + "\n" if kind in BLOCK_NODES else text


def issue_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}-[0-9]+", value):
        raise ValueError("Invalid Jira issue key")
    return value


def issue_timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Jira timestamps must include a timezone")
    return result.astimezone(timezone.utc).isoformat()


def issue_record(issue, tenant, connection, project, collected_at):
    identity = issue.get("id")
    if not isinstance(identity, str) or not JIRA_ISSUE_ID.fullmatch(identity):
        raise ValueError("Invalid Jira issue ID")
    key = issue_key(issue.get("key"))
    fields = issue["fields"]
    if fields["project"]["key"] != project or not key.startswith(project + "-"):
        raise ValueError("Jira result is outside the requested project")
    title = fields["summary"].strip()
    if not title:
        raise ValueError("Jira issue is missing its summary")
    parts = [f"{key}: {title}", f"Type: {fields['issuetype']['name']}", f"Status: {fields['status']['name']}"]
    description = description_text(fields.get("description")).strip()
    if description:
        parts.append(description)
    if parent := fields.get("parent"):
        parts.append(f"Parent: https://{tenant}/browse/{issue_key(parent['key'])}")
    for link in fields.get("issuelinks") or []:
        for direction in ("outward", "inward"):
            if linked := link.get(direction + "Issue"):
                parts.append(f"{link['type'][direction]}: https://{tenant}/browse/{issue_key(linked['key'])}")
    return {
        "source_id": f"jira:{tenant}:{identity}",
        "source_type": "jira",
        "url": f"https://{tenant}/browse/{key}",
        "title": title,
        "text": "\n\n".join(parts),
        "author": (fields.get("reporter") or {}).get("accountId"),
        "occurred_at": issue_timestamp(fields["created"]),
        "updated_at": issue_timestamp(fields["updated"]),
        "collected_at": collected_at,
        "record_kind": "source",
        "coverage": "Issue summary, description, type, status, parent and issue links. Comments, attachments, changelog and linked issue bodies excluded.",
        "provider_scope": {"provider": "jira", "connection": connection, "resource_id": identity},
    }


def capture(api, project, since, max_issues):
    collected_at = datetime.now(timezone.utc).isoformat()
    params = {"jql": f'project = "{project}" AND updated >= "{since.isoformat()}" ORDER BY updated ASC, key ASC',
              "fields": ISSUE_FIELDS, "maxResults": min(max_issues, 50)}
    records, seen, cursors = [], set(), set()
    omissions = {"credential_pattern": 0, "source_size_limit": 0}
    while True:
        page = api.get("search/jql", **params)
        for issue in page["issues"]:
            row = issue_record(issue, api.tenant, api.connection, project, collected_at)
            identity = row["provider_scope"]["resource_id"]
            if identity in seen:
                raise ValueError("Jira returned a repeated issue ID; capture not saved")
            seen.add(identity)
            if len(seen) > max_issues:
                raise ValueError("Jira issue limit exceeded; narrow the date range")
            if SECRET.search(row["text"]):
                omissions["credential_pattern"] += 1
            elif len(row["text"]) > 2_000_000 or len(row["title"]) > 2048:
                omissions["source_size_limit"] += 1
            else:
                records.append(row)
        if page.get("isLast") is True:
            break
        cursor = page.get("nextPageToken")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("Jira pagination did not advance")
        if len(seen) >= max_issues:
            raise ValueError("Jira issue limit exceeded; narrow the date range")
        cursors.add(cursor)
        params["nextPageToken"] = cursor
    return {
        "records": records,
        "collection": {"provider": "jira", "capture_schema": 1, "tenant": api.tenant,
                       "connection": api.connection, "project": project, "since": since.isoformat(),
                       "completed_at": datetime.now(timezone.utc).isoformat(), "discovered_issues": len(seen),
                       "omissions": omissions, "omitted_fields": list(OMITTED_FIELDS)},
    }


def save_capture(output, snapshot):
    if output.parent.stat().st_mode & 0o077:
        raise ValueError("Jira capture must be stored in a private directory")
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, encoding="utf-8") as staged:
        json.dump(snapshot, staged, ensure_ascii=False)
        staged.flush()
        os.fsync(staged.fileno())
        os.link(staged.name, output)


def access_receipt(tenant, client, project, connection, issues):
    receipt = {"visible": [], "denied": [], "failure": None}
    try:
        api = Jira(tenant, client)
        if api.connection != connection:
            raise JiraIdentityError("Jira account fingerprint changed")
        for identity, key in sorted(issues.items()):
            try:
                item = api.get("issue/" + identity, fields="project")
            except JiraHTTPError as error:
                if error.status not in (403, 404):
                    raise
                receipt["denied"].append(identity)
                continue
            if item.get("id") != identity:
                raise ValueError("Jira returned another issue")
            membership = item["fields"]["project"]["key"]
            if not isinstance(membership, str) or not JIRA_PROJECT.fullmatch(membership):
                raise ValueError("Jira returned invalid project membership")
            current_key = issue_key(item.get("key"))
            if membership != project or current_key != key:
                receipt["denied"].append(identity)
            else:
                receipt["visible"].append(identity)
    except JiraIdentityError:
        receipt["failure"] = "identity"
    except JiraHTTPError as error:
        receipt["failure"] = f"http_{error.status}"
    except (ValueError, KeyError, TypeError, RuntimeError):
        receipt["failure"] = "lookup"
    return receipt


def mien_access(config, issues):
    # Keep Jira credentials inside the explicitly configured Mien child.
    command = [
        config["mien_executable"], "exec", config["mien_profile"], "--",
        sys.executable, "-m", "company_context.collect_jira", "access",
        "--tenant", config["tenant"], "--project", config["project"], "--connection", config["connection"],
    ]
    try:
        response = subprocess.run(
            command, input=json.dumps(issues), capture_output=True, text=True, timeout=180, check=True,
            env={name: value for name, value in os.environ.items() if not name.startswith("ATLASSIAN_")},
        )
        result = json.loads(response.stdout)
        visible, denied, failure = result["visible"], result["denied"], result["failure"]
        if (
            not isinstance(visible, list) or not isinstance(denied, list)
            or any(not isinstance(v, str) or v not in issues for v in visible + denied)
            or len(set(visible + denied)) != len(visible + denied)
            or (failure is None and set(visible + denied) != set(issues))
            or (failure is not None and (
                not isinstance(failure, str) or not re.fullmatch(r"(?:identity|lookup|http_[1-5][0-9]{2})", failure)
            ))
        ):
            raise ValueError("Invalid Jira ACL receipt")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise RuntimeError("Jira ACL worker did not return a valid receipt") from None
    return visible, denied, failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "access"), nargs="?", default="capture")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--max-issues", type=int, default=25)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--connection")
    args = parser.parse_args()
    if not JIRA_TENANT.fullmatch(args.tenant):
        parser.error("Tenant must be an Atlassian Cloud hostname")
    if not JIRA_PROJECT.fullmatch(args.project) or not 1 <= args.max_issues <= 100:
        parser.error("Provide a valid project key and an issue limit between 1 and 100")
    origin = "https://" + args.tenant
    if os.environ.get("ATLASSIAN_BASE_URL", "").rstrip("/") != origin:
        parser.error("Jira tenant differs from the configured reader's tenant")
    if not os.environ.get("ATLASSIAN_EMAIL") or not os.environ.get("ATLASSIAN_API_TOKEN"):
        parser.error("Run with the authorized Jira credentials")
    if args.command == "access":
        if not args.connection or not JIRA_CONNECTION.fullmatch(args.connection):
            parser.error("Jira access checks require a pinned account fingerprint")
        try:
            issues = json.load(sys.stdin)
            if not isinstance(issues, dict) or not 1 <= len(issues) <= 100 or any(
                not JIRA_ISSUE_ID.fullmatch(identity) or not issue_key(key).startswith(args.project + "-")
                for identity, key in issues.items()
            ):
                raise ValueError
        except (TypeError, ValueError):
            parser.error("Jira access checks require a bounded issue ID and key map")
    elif args.since is None or args.output is None:
        parser.error("Provide a date and a private output path for Jira capture")
    with httpx.Client(base_url=origin, auth=(os.environ["ATLASSIAN_EMAIL"], os.environ["ATLASSIAN_API_TOKEN"]),
                      headers={"Accept": "application/json"}, timeout=30, trust_env=False) as client:
        if args.command == "access":
            print(json.dumps(access_receipt(args.tenant, client, args.project, args.connection, issues)))
            return
        snapshot = capture(Jira(args.tenant, client), args.project, args.since, args.max_issues)
    save_capture(args.output, snapshot)
    print(json.dumps({"captured_issues": len(snapshot["records"]), "omissions": snapshot["collection"]["omissions"]}))


if __name__ == "__main__":
    main()
