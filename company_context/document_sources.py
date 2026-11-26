"""Export completed document captures and revalidate their provider access."""

from datetime import datetime, timezone
import fcntl
import json
import re
from pathlib import Path
import sqlite3

import httpx

from company_context.collect_drive import Drive
from company_context.collect_notion import Notion
from company_context.collect_slack import SECRET
from company_context.context import digest


class IncompleteCapture(ValueError):
    pass


def object_row(db, kind, identity):
    row = db.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
    return json.loads(row[0]) if row else None


def members(db, kind, identity, captured=None):
    rows = db.execute("SELECT o.payload,o.captured_at FROM members m JOIN objects o ON o.kind=m.object_kind AND o.id=m.object_id WHERE m.task_kind=? AND m.task_id=? ORDER BY m.position,o.id", (kind, identity)).fetchall()
    if captured is not None: captured.extend(row[1] for row in rows)
    return [json.loads(row[0]) for row in rows]


def plain(value):
    if isinstance(value, list): return "".join(plain(v) for v in value)
    if isinstance(value, dict):
        if "plain_text" in value: return value["plain_text"]
        if "text" in value: return value["text"].get("content", "")
    return ""


def notion_records(db, settings, finished):
    tasks = {(kind, identity): bool(done) and error is None for kind, identity, done, error in db.execute("SELECT kind,id,done,error FROM tasks")}
    properties_by_page = {}
    for kind, identity in tasks:
        if kind == "property": properties_by_page.setdefault(identity.split("/", 1)[0], []).append(identity)
    def require_task(kind, identity):
        if not tasks.get((kind, identity), False): raise IncompleteCapture("page_dependencies_incomplete")
    def children(identity, visiting, dependencies, captured):
        if identity in visiting:
            return ["[Cyclic synced-block reference omitted]"]
        require_task("children", identity)
        result = []
        if db.execute("SELECT 1 FROM omissions WHERE kind='children' AND id=? AND reason='api_unsupported_block'", (identity,)).fetchone():
            result.append("[External page body unsupported by the Notion API]")
        for block in members(db, "children", identity, captured):
            if block.get("object") != "block" or block.get("archived") or block.get("in_trash"): continue
            kind = block.get("type"); value = block.get(kind, {})
            if value.get("rich_text"): result.append(plain(value["rich_text"]))
            if value.get("cells"): result.append("\t".join(plain(cell) for cell in value["cells"]))
            for key in ("title", "expression", "url"):
                if isinstance(value.get(key), str): result.append(value[key])
            if value.get("caption"): result.append(plain(value["caption"]))
            if kind == "synced_block" and value.get("synced_from"):
                original = value["synced_from"]["block_id"]
                dependencies.add("block:" + original)
                result.extend(children(original, visiting | {identity}, dependencies, captured))
            elif block.get("has_children") and kind not in ("child_page", "child_database"):
                result.extend(children(block["id"], visiting | {identity}, dependencies, captured))
        return result

    records, omissions = [], {}
    for (identity,) in db.execute("SELECT id FROM tasks WHERE kind='page' ORDER BY id"):
        page = object_row(db, "page", identity) or {}
        if page.get("archived") or page.get("in_trash"): continue
        captured = [row[0] for row in db.execute("SELECT captured_at FROM objects WHERE kind='page' AND id=?", (identity,))]
        dependencies = set()
        try:
            require_task("page", identity)
            for key in properties_by_page.get(identity, []): require_task("property", key)
            body = children(identity, set(), dependencies, captured)
        except IncompleteCapture:
            omissions.setdefault("page_dependencies_incomplete", []).append(identity)
            continue
        properties = page.get("properties", {})
        title = next((plain(p.get("title", [])) for p in properties.values() if p.get("type") == "title"), "") or "Notion page " + identity
        parts = [title]
        for name, prop in properties.items():
            key = identity + "/" + prop.get("id", "")
            values = members(db, "property", key, captured)
            envelopes = [v["response"].get("property_item") for v in values if v.get("object") == "response_metadata" and not v["response"].get("next_cursor")]
            items = [v for v in values if v.get("object") == "property_item"]
            if db.execute("SELECT 1 FROM omissions WHERE kind='property' AND id=? AND reason='invalid_rollup_relations'", (key,)).fetchone():
                value = "[Rollup unavailable: Notion reported invalid relations]"
            elif items:
                value = [{k: v for k, v in item.items() if k not in ("id", "object")} for item in items]
                if prop.get("type") == "rollup" and envelopes and envelopes[-1]:
                    value = {"items": value, "aggregate": envelopes[-1].get("rollup")}
            elif prop.get("type") == "rollup" and envelopes and envelopes[-1]:
                value = envelopes[-1]
            elif db.execute("SELECT 1 FROM tasks WHERE kind='property' AND id=?", (key,)).fetchone():
                value = "[Property omitted or empty; see capture coverage]"
            else:
                value = prop
            parts.append(name + ": " + json.dumps(value, ensure_ascii=False, sort_keys=True))
        parts.extend(body)
        if not captured or (not page and not body):
            omissions.setdefault("no_retained_content", []).append(identity)
            continue
        text = "\n\n".join(part for part in parts if part)
        omission = "source_size_limit" if len(text) > 2_000_000 else "credential_pattern" if SECRET.search(text) else None
        if omission:
            omissions.setdefault(omission, []).append(identity)
            continue
        records.append({"source_id": "notion:" + settings["workspace_id"] + ":" + identity, "source_type": "notion", "url": page.get("url") or "https://www.notion.so/" + identity.replace("-", ""), "title": title, "text": text, "author": page.get("created_by", {}).get("id"), "occurred_at": page.get("created_time"), "updated_at": page.get("last_edited_time"), "collected_at": max(captured, default=finished), "record_kind": "source", "coverage": "Captured page properties and ordered block text; formatting normalized. Files, unsupported blocks, credential-pattern payloads and comments are excluded from indexing. Page metadata can be withheld independently of safe descendant text.", "provider_scope": {"provider": "notion", "connection": settings["connection_id"], "resource_id": identity, "dependencies": sorted(dependencies)}})
    return records, omissions


def export_capture(provider, settings, principal):
    path = Path(settings["database"])
    if not path.exists(): raise IncompleteCapture("capture_not_started")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        meta = dict(db.execute("SELECT key,value FROM metadata"))
        partial = provider == "notion" and settings.get("allow_partial") is True
        if "finished_at" not in meta and not partial: raise IncompleteCapture("capture_in_progress")
        finished = meta.get("finished_at") or db.execute("SELECT max(captured_at) FROM objects").fetchone()[0]
        if provider == "notion":
            if meta.get("capture_schema") != "2" or meta.get("workspace_id") != settings["workspace_id"] or meta.get("connection_id") != settings["connection_id"]:
                raise ValueError("Notion capture identity or schema differs")
            unfinished = db.execute("SELECT count(*) FROM tasks WHERE done=0 OR error IS NOT NULL").fetchone()[0]
            if unfinished and not partial: raise IncompleteCapture("capture_has_unfinished_tasks")
            if partial and not meta.get("capture_id"): raise IncompleteCapture("capture_identity_missing")
            rows, omissions = notion_records(db, settings, finished)
            if not rows and unfinished: raise IncompleteCapture("capture_has_no_complete_pages")
            pending_pages = len(omissions.pop("page_dependencies_incomplete", []))
            empty_pages = len(omissions.pop("no_retained_content", []))
            coverage = {"capture_id": meta.get("capture_id"), "capture_complete": not unfinished, "pending_tasks": unfinished, "pending_pages": pending_pages, "empty_withheld_pages": empty_pages, "omissions": dict(db.execute("SELECT reason,count(*) FROM omissions GROUP BY reason")), "export_omissions": omissions}
        elif provider == "drive":
            if meta.get("email") != settings["email"] or meta.get("permission_id") != settings["permission_id"] or meta.get("capture_schema") != "1": raise ValueError("Drive capture identity or schema differs")
            if db.execute("SELECT count(*) FROM scopes WHERE done=0").fetchone()[0] or db.execute("SELECT count(*) FROM files WHERE status IN ('pending','failed')").fetchone()[0]: raise IncompleteCapture("capture_has_unfinished_tasks")
            rows = []
            for identity, metadata, text, coverage_text, collected in db.execute("SELECT id,metadata,text,coverage,collected_at FROM files WHERE status='ready' ORDER BY id"):
                item = json.loads(metadata)
                rows.append({"source_id": "drive:" + settings["permission_id"] + ":" + identity, "source_type": "docs", "url": item.get("webViewLink") or "https://drive.google.com/file/d/" + identity + "/view", "title": item.get("name") or identity, "text": text, "author": next((v.get("emailAddress") for v in item.get("owners", [])), None), "occurred_at": item.get("createdTime"), "updated_at": item.get("modifiedTime"), "collected_at": collected, "record_kind": "source", "coverage": coverage_text, "provider_scope": {"provider": "drive", "connection": settings["permission_id"], "resource_id": identity}})
            coverage = {"omissions": dict(db.execute("SELECT reason,count(*) FROM files WHERE status='omitted' GROUP BY reason"))}
        else: raise ValueError("Unknown document provider")
        return {"principal": principal, "records": rows, "collection": {"provider": provider, "completed_at": finished, **coverage}}


def resource_identity(row, provider):
    scope = row.get("provider_scope", {})
    if scope.get("provider") == provider: return scope["resource_id"].replace("-", "") if provider == "notion" else scope["resource_id"]
    from urllib.parse import parse_qs, urlsplit
    url = urlsplit(row["url"])
    if provider == "drive" and url.hostname in ("drive.google.com", "docs.google.com"):
        match = re.search(r"/d/([A-Za-z0-9_-]+)", url.path)
        return match[1] if match else next(iter(parse_qs(url.query).get("id", [])), None)
    if provider == "notion" and (url.hostname == "notion.so" or (url.hostname or "").endswith((".notion.so", ".notion.site"))):
        match = re.search(r"([a-fA-F0-9]{32})$", url.path.replace("-", ""))
        return match[1].lower() if match else None
    return None


def merge_records(previous, incoming, provider):
    resources = {resource_identity(r, provider) for r in incoming} - {None}
    return [r for r in previous if r.get("provider_scope", {}).get("provider") != provider and resource_identity(r, provider) not in resources] + incoming


def refresh_document_access(root, settings, snapshots):
    from company_context.sync import write_json
    root = Path(root)
    with (root / "document-access.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        from company_context.document_sync import snapshots_in_use
        snapshots = [*snapshots, *snapshots_in_use(root)]
        path = root / "document-access.json"
        result = json.loads(path.read_text()) if path.exists() else {"principal": settings["principal"], "providers": {}}
        if result["principal"] != settings["principal"]: raise ValueError("Document access inventory belongs to another reader")
        for provider, config in settings["providers"].items():
            def revoke(identity=None):
                previous = result["providers"].get(provider)
                if previous:
                    previous["resource_ids"] = [v for v in previous["resource_ids"] if identity is not None and v != identity]
                    write_json(path, result)
            scopes = [r["provider_scope"] for snapshot in snapshots for r in snapshot.get("records", []) if r.get("provider_scope", {}).get("provider") == provider]
            required = {identity for scope in scopes for identity in [scope["resource_id"], *scope.get("dependencies", [])]}
            if not required: continue
            checked = datetime.now(timezone.utc).isoformat()
            if provider == "notion":
                api = Notion(config["token_file"])
                try:
                    identity = api.request("GET", "users/me")
                    if identity.get("id") != config["connection_id"] or identity.get("bot", {}).get("workspace_id") != config["workspace_id"]:
                        revoke(); raise ValueError("Notion integration identity changed")
                    visible, cursor = set(), ""
                    # Small batches need only direct reads of their exact dependencies.
                    while len(required) > 100:
                        page = api.request("POST", "search", json={"page_size": 100, "filter": {"value": "page", "property": "object"}, **({"start_cursor": cursor} if cursor else {})})
                        visible.update(v["id"] for v in page.get("results", []) if not v.get("archived") and not v.get("in_trash"))
                        following = page.get("next_cursor") or ""
                        if not following: break
                        if following == cursor: raise ValueError("Notion access pagination did not advance")
                        cursor = following
                    for identity in required - visible:
                        try:
                            page = api.request("GET", "blocks/" + identity[6:] if identity.startswith("block:") else "pages/" + identity)
                            if not page.get("archived") and not page.get("in_trash"): visible.add(identity)
                            else: revoke(identity)
                        except httpx.HTTPStatusError as error:
                            if error.response.status_code not in (403, 404): raise
                            revoke(identity)
                    connection = config["connection_id"]
                finally: api.client.close()
            elif provider == "drive":
                try:
                    api = Drive(config["credentials_file"], config["email"])
                except ValueError:
                    revoke()
                    raise
                try:
                    connection = api.identity["permissionId"]
                    if connection != config["permission_id"]:
                        revoke(); raise ValueError("Drive reader identity changed")
                    visible = set()
                    for scope in api.scopes():
                        cursor = ""
                        while True:
                            page = api.page(scope, cursor)
                            if page.get("incompleteSearch"): raise ValueError("Drive access inventory incomplete")
                            visible.update(v["id"] for v in page.get("files", []) if not v.get("trashed") and v.get("capabilities", {}).get("canDownload") is True)
                            following = page.get("nextPageToken", "")
                            if not following: break
                            if following == cursor: raise ValueError("Drive access pagination did not advance")
                            cursor = following
                    for identity in required - visible:
                        try:
                            item = api.metadata(identity)
                            if not item.get("trashed") and item.get("capabilities", {}).get("canDownload") is True: visible.add(identity)
                            else: revoke(identity)
                        except RuntimeError as error:
                            if str(error) not in ("drive_http_403", "drive_http_404"): raise
                            revoke(identity)
                finally: api.client.close()
            else: raise ValueError("Unknown document provider")
            result["providers"][provider] = {"connection": connection, "checked_at": checked, "resource_ids": sorted(visible)}
            write_json(path, result)
        return result


def document_availability(corpus, path):
    sources = [r["provider_scope"] for r in corpus.records.values() if "provider_scope" in r]
    if not sources: return "ready"
    state = json.loads(Path(path).read_text())
    if state["principal"] != corpus.principal: return "blocked_document_reader_mismatch"
    for provider in {s["provider"] for s in sources}:
        access = state["providers"][provider]
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(access["checked_at"])).total_seconds()
        if not 0 <= age <= 7200: return "blocked_document_access_expired"
        visible = set(access["resource_ids"])
        if any(s["connection"] != access["connection"] or not {s["resource_id"], *s.get("dependencies", [])} <= visible for s in sources if s["provider"] == provider): return "blocked_document_access_changed"
    return "ready"
