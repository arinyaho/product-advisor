"""Capture readable Drive files for one verified company account, without model calls."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import subprocess
import sys
from urllib.parse import parse_qsl, urlsplit

from company_context.collect_slack import SECRET
from company_context.context import digest


FIELDS = "id,name,mimeType,version,modifiedTime,createdTime,webViewLink,trashed,owners(emailAddress),capabilities(canDownload),shortcutDetails(targetId,targetMimeType,targetResourceKey),resourceKey,size"
MAX_BYTES = 50_000_000


from company_context.extract_document import Omitted, extract


def text_runs(value):
    if isinstance(value, list):
        return "".join(text_runs(v) for v in value)
    if isinstance(value, dict):
        if "textRun" in value:
            return value["textRun"].get("content", "")
        return "".join(text_runs(v) for v in value.values())
    return ""


def note_resource_keys(item):
    if item.pop("resourceKey", None):
        item["resourceKeyPresent"] = True
    shortcut = item.get("shortcutDetails", {})
    if shortcut.pop("targetResourceKey", None):
        shortcut["targetResourceKeyPresent"] = True
    return item


class Drive:
    def __init__(self, credentials_file, expected_email):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        path = Path(credentials_file)
        if path.stat().st_mode & 0o077:
            raise ValueError("Drive credentials must be private")
        credentials, _ = google.auth.load_credentials_from_file(path)
        self.client = AuthorizedSession(credentials)
        self.client.trust_env = False
        self.identity = self.json("about", fields="user(emailAddress,permissionId)")["user"]
        if self.identity["emailAddress"] != expected_email:
            self.client.close()
            raise ValueError("Drive account differs from the authorized reader")

    def request(self, url, **params):
        for attempt in range(5):
            response = self.client.get(url, params=params, timeout=60, stream=True, allow_redirects=False)
            if response.status_code in (429, 500, 502, 503, 504):
                response.close(); time.sleep(min(2 ** attempt, 16)); continue
            if response.status_code != 200:
                code = response.status_code
                try:
                    body = b""
                    for chunk in response.iter_content(4096):
                        body += chunk
                        if len(body) > 65536: break
                    error = json.loads(body) if len(body) <= 65536 else {}
                    if code == 403 and any(v.get("reason") == "exportSizeLimitExceeded" for v in error.get("error", {}).get("errors", [])):
                        raise Omitted("native_export_size_limit")
                except Omitted:
                    raise
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
                finally:
                    response.close()
                raise RuntimeError(f"drive_http_{code}")
            try:
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise Omitted("download_size_limit")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
        raise RuntimeError("drive_retry_exhausted")

    def json(self, path, **params):
        return json.loads(self.request("https://www.googleapis.com/drive/v3/" + path, **params))

    def scopes(self):
        result, cursor = ["user"], ""
        while True:
            page = self.json("drives", pageSize=100, pageToken=cursor, fields="nextPageToken,drives(id)")
            result.extend(d["id"] for d in page.get("drives", []))
            following = page.get("nextPageToken", "")
            if not following:
                return result
            if following == cursor:
                raise ValueError("Drive pagination did not advance")
            cursor = following

    def page(self, scope, cursor):
        params = {"corpora": "user" if scope == "user" else "drive", "q": "trashed = false", "pageSize": 1000, "pageToken": cursor, "includeItemsFromAllDrives": "true", "supportsAllDrives": "true", "fields": "nextPageToken,incompleteSearch,files(" + FIELDS + ")"}
        if scope != "user":
            params["driveId"] = scope
        page = self.json("files", **params)
        for item in page.get("files", []):
            note_resource_keys(item)
        return page

    def metadata(self, identity):
        return note_resource_keys(self.json("files/" + identity, fields=FIELDS, supportsAllDrives="true"))

    def content(self, item):
        identity, mime = item["id"], item["mimeType"]
        if item.get("capabilities", {}).get("canDownload") is not True:
            raise Omitted("download_not_authorized")
        if mime == "application/vnd.google-apps.document":
            doc = json.loads(self.request("https://docs.googleapis.com/v1/documents/" + identity, includeTabsContent="true"))
            return text_runs(doc.get("tabs", doc.get("body", {}))), "Google Docs text runs across tabs; images and visual layout excluded"
        if mime == "application/vnd.google-apps.presentation":
            doc = json.loads(self.request("https://slides.googleapis.com/v1/presentations/" + identity))
            return text_runs(doc.get("slides", [])), "Google Slides text runs, tables and notes; images and charts excluded"
        if mime == "application/vnd.google-apps.spreadsheet":
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            data = self.request("https://www.googleapis.com/drive/v3/files/" + identity + "/export", mimeType=mime)
        else:
            supported = mime.startswith("text/") or mime in ("application/json", "application/xml", "application/pdf") or mime.endswith(("wordprocessingml.document", "spreadsheetml.sheet", "presentationml.presentation"))
            if not supported:
                raise Omitted("unsupported_mime_type")
            if int(item.get("size", 0)) > MAX_BYTES:
                raise Omitted("download_size_limit")
            data = self.request("https://www.googleapis.com/drive/v3/files/" + identity, alt="media", supportsAllDrives="true")
        try:
            result = subprocess.run([sys.executable, "-m", "company_context.extract_document", mime], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=70)
        except subprocess.TimeoutExpired:
            raise Omitted("document_extraction_timeout") from None
        if result.returncode:
            raise Omitted("document_extraction_resource_limit")
        extracted = json.loads(result.stdout)
        if "omission" in extracted: raise Omitted(extracted["omission"])
        return extracted["text"], extracted["coverage"]


def open_store(path, identity):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path); path.chmod(0o600)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS scopes(id TEXT PRIMARY KEY,cursor TEXT NOT NULL DEFAULT '',done INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,text TEXT,coverage TEXT,sha256 TEXT,collected_at TEXT,status TEXT NOT NULL DEFAULT 'pending',reason TEXT);
    """)
    expected = {"email": identity["emailAddress"], "permission_id": identity["permissionId"], "capture_schema": "1"}
    known = dict(db.execute("SELECT key,value FROM metadata"))
    if known and any(known.get(k) != v for k, v in expected.items()):
        db.close(); raise ValueError("Drive capture belongs to another reader or schema")
    with db: db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", expected.items())
    return db


def status(db):
    return {"files": db.execute("SELECT count(*) FROM files").fetchone()[0], "states": dict(db.execute("SELECT status,count(*) FROM files GROUP BY status")), "pending_scopes": db.execute("SELECT count(*) FROM scopes WHERE done=0").fetchone()[0], "model_calls": 0, "index_published": False}


def retry_capture_errors(db):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS operator_retries(at TEXT,id TEXT,previous_error TEXT)")
        selected = list(db.execute("SELECT id,reason FROM files WHERE status='failed' AND reason IN ('UnicodeEncodeError','drive_http_404')"))
        at = datetime.now(timezone.utc).isoformat()
        db.executemany("INSERT INTO operator_retries VALUES (?,?,?)", [(at, identity, error) for identity, error in selected])
        db.executemany("UPDATE files SET status='pending',reason=NULL WHERE id=?", [(identity,) for identity, _ in selected])
    return len(selected)


def has_resource_key(item):
    # Older captures may have the key only in the provider's link.
    return item.get("resourceKeyPresent") or any(
        name.lower() == "resourcekey" for name, _ in parse_qsl(urlsplit(item.get("webViewLink", "")).query)
    )


def capture_metadata(api, identity, omission="file_not_accessible", *, resource_key_present=False):
    try:
        return api.metadata(identity)
    except RuntimeError as error:
        if str(error) == "drive_http_404" and not resource_key_present:
            raise Omitted(omission) from None
        raise


def collect(api, db):
    if dict(db.execute("SELECT key,value FROM metadata")).get("finished_at") and not db.execute("SELECT count(*) FROM scopes WHERE done=0").fetchone()[0] and not db.execute("SELECT count(*) FROM files WHERE status IN ('pending','failed')").fetchone()[0]:
        return status(db)
    with db:
        db.execute("DELETE FROM metadata WHERE key='finished_at'")
        db.executemany("INSERT OR IGNORE INTO scopes(id) VALUES (?)", [(s,) for s in api.scopes()])
    for scope, cursor in list(db.execute("SELECT id,cursor FROM scopes WHERE done=0")):
        while True:
            page = api.page(scope, cursor)
            if page.get("incompleteSearch"):
                raise ValueError("Drive inventory is incomplete")
            following = page.get("nextPageToken", "")
            if following and following == cursor:
                raise ValueError("Drive pagination did not advance")
            with db:
                for item in page.get("files", []):
                    secret = bool(SECRET.search(json.dumps(item)))
                    safe = {"id": item["id"], "mimeType": item["mimeType"]} if secret else item
                    db.execute("INSERT OR IGNORE INTO files(id,metadata,status,reason) VALUES (?,?,?,?)", (item["id"], json.dumps(safe, ensure_ascii=False), "omitted" if secret else "pending", "credential_pattern" if secret else None))
                db.execute("UPDATE scopes SET cursor=?,done=? WHERE id=?", (following, int(not following), scope))
            print(json.dumps(status(db)), flush=True)
            if not following: break
            cursor = following
    while pending := db.execute("SELECT id,metadata FROM files WHERE status='pending' ORDER BY id LIMIT 1").fetchone():
        identity, saved = pending
        try:
            saved_key = has_resource_key(json.loads(saved))
            item = capture_metadata(api, identity, resource_key_present=saved_key)
            if item.get("trashed"): raise Omitted("trashed")
            if SECRET.search(json.dumps(item)): raise Omitted("credential_pattern")
            if item["mimeType"] == "application/vnd.google-apps.shortcut":
                shortcut = item["shortcutDetails"]
                target = capture_metadata(api, shortcut["targetId"], "shortcut_target_not_accessible",
                                          resource_key_present=shortcut.get("targetResourceKeyPresent"))
                if SECRET.search(json.dumps(target)): raise Omitted("credential_pattern")
                with db: db.execute("INSERT OR IGNORE INTO files(id,metadata) VALUES (?,?)", (target["id"], json.dumps(target, ensure_ascii=False)))
                raise Omitted("shortcut_target_queued")
            text, coverage = api.content(item)
            if re.search(r"[\ud800-\udfff]", text):
                text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
                coverage += "; Unicode surrogate pairs normalized and malformed surrogates replaced"
            if not text.strip(): raise Omitted("no_text_layer_or_empty")
            if len(text) > 2_000_000: raise Omitted("text_size_limit")
            if SECRET.search(text) or SECRET.search(json.dumps(item)): raise Omitted("credential_pattern")
            after = capture_metadata(api, identity, resource_key_present=saved_key or has_resource_key(item))
            if (item.get("version"), item.get("modifiedTime")) != (after.get("version"), after.get("modifiedTime")) or after.get("trashed"):
                raise RuntimeError("source_changed_during_capture")
            with db:
                db.execute("UPDATE files SET metadata=?,text=?,coverage=?,sha256=?,collected_at=?,status='ready',reason=NULL WHERE id=?", (json.dumps(item, ensure_ascii=False), text, coverage, digest(text), datetime.now(timezone.utc).isoformat(), identity))
        except Exception as error:
            reason = str(error) if isinstance(error, (Omitted, RuntimeError)) else type(error).__name__
            with db: db.execute("UPDATE files SET status=?,reason=? WHERE id=?", ("omitted" if isinstance(error, Omitted) else "failed", reason, identity))
        print(json.dumps(status(db)), flush=True)
    result = status(db)
    if not result["pending_scopes"] and not any(result["states"].get(key, 0) for key in ("pending", "failed")):
        with db: db.execute("INSERT OR REPLACE INTO metadata VALUES ('finished_at',?)", (datetime.now(timezone.utc).isoformat(),))
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--retry-capture-errors", action="store_true")
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.database.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        api = Drive(args.credentials_file, args.email)
        db = open_store(args.database, api.identity)
        try:
            if args.retry_capture_errors:
                print(json.dumps({"capture_reads_requeued": retry_capture_errors(db)}), flush=True)
            result = collect(api, db)
            if result["states"].get("failed", 0): raise SystemExit(1)
        finally:
            db.close(); api.client.close()


if __name__ == "__main__":
    main()
