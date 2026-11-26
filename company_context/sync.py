"""Single-reader capture, budgeted candidate build, and validated atomic publication."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from company_context.collect_slack import Slack, collect, inventory, open_store
from company_context.context import Corpus, Models, build, close_engine, open_index, slack_channel_id, digest, index_config, timestamp
from company_context.slack_snapshot import export_snapshot


def write_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    temporary = Path(name)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def refresh_access(api, path):
    path = Path(path)
    with path.with_name(path.name + ".refresh.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        checked_at = datetime.now(timezone.utc).isoformat()
        auth = api.get("auth.test")
        if auth.get("bot_id") or not auth.get("user_id") or not auth.get("team_id"):
            raise ValueError("Slack must confirm a named human reader and workspace")
        identity = f"slack:{auth['team_id']}:{auth['user_id']}"
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous and previous["principal"] != identity:
            raise ValueError("Refusing to replace another reader's access inventory")
        channels = inventory(api)
        revoked = dict(previous.get("revoked_channels", {}))
        for channel in set(previous.get("channel_ids", [])) - set(channels):
            revoked[channel] = checked_at
        result = {"principal": identity, "checked_at": checked_at, "channel_ids": sorted(channels), "revoked_channels": revoked}
        write_json(path, result)
        return result


def availability(corpus, access_path=None, sync_path=None):
    try:
        if access_path:
            access = json.loads(Path(access_path).read_text())
            checked = datetime.fromisoformat(access["checked_at"])
            age = (datetime.now(timezone.utc) - checked).total_seconds()
            if not 0 <= age <= 7200:
                return "blocked_access_inventory_expired"
            if corpus.principal.startswith("slack:") and corpus.principal != access["principal"]:
                return "blocked_reader_mismatch"
            channels = {slack_channel_id(u) for r in corpus.records.values() for u in [r["url"], *r.get("derived_from", [])]} - {None}
            if not channels <= set(access["channel_ids"]):
                return "blocked_channel_access_changed"
        if any("provider_scope" in r for r in corpus.records.values()):
            if not access_path:
                return "blocked_document_access_missing"
            from company_context.document_sources import document_availability
            document_status = document_availability(corpus, Path(access_path).parent / "document-access.json")
            if document_status != "ready":
                return document_status
        if sync_path and Path(sync_path).exists():
            state = json.loads(Path(sync_path).read_text())
            if corpus.version in state.get("blocked_snapshots", []):
                return "blocked_known_source_change"
        return "ready"
    except (OSError, ValueError, KeyError, TypeError):
        return "blocked_invalid_sync_state"


def changed_or_removed(previous, current):
    old = {r["source_id"]: r for r in previous["records"]}
    new = {r["source_id"]: r for r in current["records"]}
    return {identity for identity, row in old.items() if new.get(identity) != row}


def withdraw_changed(previous, current, state, path, *, slack_only=False):
    if not previous:
        return
    old = previous if not slack_only else {"records": [r for r in previous["records"] if r["source_type"] == "slack"]}
    changed = changed_or_removed(old, current)
    if changed:
        withdrawn = state.setdefault("withdrawn_sources", {})
        version = digest(previous)
        if version not in state.get("blocked_snapshots", []) or version in withdrawn:
            withdrawn[version] = sorted(set(withdrawn.get(version, [])) | changed)
        state["blocked_snapshots"] = sorted(set(state.get("blocked_snapshots", [])) | {digest(previous)})
        write_json(path, state)


def managed_resources(state, *snapshots):
    from company_context.document_sources import resource_identity
    result = {key: set(values) for key, values in state.get("managed_document_resources", {}).items()}
    for snapshot in snapshots:
        for row in (snapshot or {}).get("records", []):
            if "provider_scope" in row:
                provider = row["provider_scope"]["provider"]
                result.setdefault(provider, set()).add(resource_identity(row, provider))
    return {key: sorted(values) for key, values in result.items()}


def require_access(corpus, path):
    if availability(corpus, path) != "ready":
        raise ValueError("Source access is not current for the authorized reader")


def accept_mcp(client, corpus):
    from company_context.evaluate import rpc
    response = rpc(client, "tools/call", {"name": "company_index_status", "arguments": {}})
    if response.get("snapshot") != corpus.version or response.get("availability") != "ready":
        raise ValueError("MCP reports a different or unavailable snapshot")
    if corpus.chunks:
        handle = next(iter(corpus.chunks))
        original = rpc(client, "tools/call", {"name": "get_company_source", "arguments": {"chunk_id": handle}})
        if original != corpus.read(handle):
            raise ValueError("MCP returned a different source")


def reported_coverage(snapshot, version, state_path):
    if state_path and Path(state_path).exists():
        try:
            state = json.loads(Path(state_path).read_text())
            if state.get("coverage_snapshot") == version and isinstance(state.get("verified_collection"), dict):
                return state["verified_collection"]
        except (OSError, ValueError):
            pass
    return snapshot.get("collection", "explicit_snapshot_not_all_company_knowledge")


def progress(state_path):
    if not state_path or not Path(state_path).exists():
        return None
    try:
        state = json.loads(Path(state_path).read_text())
        result = {key: state[key] for key in ("phase", "last_collected_at", "sources", "chunks", "model_calls", "charged_or_estimated_usd") if key in state}
        database = state.get("pending_database") or state.get("last_database") or state.get("initial_database")
        if database:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                total, complete, failed = db.execute("SELECT count(*),coalesce(sum(done),0),coalesce(sum(error IS NOT NULL),0) FROM channels").fetchone()
                result["capture"] = {"channels": total, "complete_channels": complete, "failed_channels": failed, "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0]}
        if state.get("phase") == "building":
            path = Path(state["candidate"]) / "progress.json"
            if path.exists():
                result["build"] = {k: v for k, v in json.loads(path.read_text()).items() if k in ("completed_sources", "total_sources", "model_calls")}
        document_status = Path(state_path).parent / "document-ingestion.json"
        if document_status.exists():
            result["documents"] = json.loads(document_status.read_text())
        return result
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return {"phase": "progress_unavailable"}


def publish(directory, current):
    directory, current = Path(directory).resolve(), Path(current)
    if not (directory / "ready.json").exists():
        raise ValueError("Cannot publish an unfinished index")
    temporary = current.with_name(current.name + ".next")
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(directory, target_is_directory=True)
    os.replace(temporary, current)


async def publish_snapshot(args, snapshot, state, previous, refresh, updates):
    root = args.state_dir
    state_path = root / "sync.json"
    previous_snapshot = json.loads((previous / "snapshot.json").read_text()) if previous else None
    if previous_snapshot and digest(previous_snapshot) in state.get("blocked_snapshots", []):
        withdrawn = state.get("withdrawn_sources", {}).get(digest(previous_snapshot))
        replaced = changed_or_removed(previous_snapshot, snapshot)
        if withdrawn is None or not set(withdrawn) <= replaced:
            raise ValueError("Candidate carries withdrawn evidence; reconcile the affected sources first")
    corpus = Corpus(snapshot)
    current = root / "current"
    require_access(corpus, root / "access.json")
    config = json.loads(args.config.read_text())
    config["require_price_ceiling"] = True
    # One ledger spans all synchronization builds; a new generation does not reset spending.
    models = Models(config, args.max_model_calls, root / "sync-model-receipts.jsonl", args.budget_usd, authorize=lambda: require_access(corpus, root / "access.json"))
    candidate = root / "releases" / corpus.version[:24]
    state.update(phase="building", candidate=str(candidate), sources=len(corpus.records), chunks=len(corpus.chunks))
    write_json(state_path, state)
    switched = False
    try:
        # Legacy pilot snapshots have a different reader/chunking contract and need a fresh build.
        base = previous if previous_snapshot and previous_snapshot.get("collection", {}).get("kind") in ("slack_channel_capture", "company_capture") else None
        if base and json.loads((base / "ready.json").read_text())["config"] != index_config(models, corpus):
            base = None
        await build(snapshot, candidate, models, base)
        verified, rag = await open_index(candidate, models)
        from company_context.server import create_app, token_from_file
        from starlette.testclient import TestClient
        headers = {"Authorization": "Bearer " + token_from_file(args.mcp_token_file), "Accept": "application/json, text/event-stream"}
        try:
            refresh()
            if any("provider_scope" in r for r in corpus.records.values()):
                from company_context.document_sources import refresh_document_access
                settings = json.loads((root / "document-sources.json").read_text())
                refresh_document_access(root, settings, [snapshot])
            require_access(verified, root / "access.json")
            # Exercise the candidate's authenticated MCP before changing the served pointer.
            with TestClient(create_app(verified, rag, args.mcp_token_file, root / "access.json"), base_url="http://testserver/mcp", headers=headers) as client:
                accept_mcp(client, verified)
        finally:
            await close_engine(rag)
        publish(candidate, current)
        switched = True
        subprocess.run(["systemctl", "--user", "restart", args.service], check=True)
        # A running process alone is not acceptance: require an authenticated MCP status call.
        import httpx
        for attempt in range(30):
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{args.port}/mcp", headers=headers, timeout=10, trust_env=False) as client:
                    accept_mcp(client, corpus)
                break
            except httpx.TransportError:
                if attempt == 29:
                    raise
                await asyncio.sleep(1)
        state["managed_document_resources"] = managed_resources(state, previous_snapshot, snapshot)
        state.update(phase="published", published_snapshot=corpus.version, last_collected_at=snapshot["collection"]["completed_at"], model_calls=models.calls, charged_or_estimated_usd=models.cost, **updates)
        if "last_database" in updates:
            state.pop("pending_database", None); state.pop("pending_until", None)
        write_json(state_path, state)
    except BaseException:
        state["phase"] = "build_failed"
        state["blocked_snapshots"] = sorted(set(state.get("blocked_snapshots", [])) | {corpus.version})
        write_json(state_path, state)
        if switched:
            subprocess.run(["systemctl", "--user", "stop", args.service], check=False)
        raise


async def synchronize(args):
    root = args.state_dir
    policy_path = root / "document-sources.json"
    policy = {}
    if args.command == "sync" and policy_path.exists():
        policy = json.loads(policy_path.read_text())
        args.budget_usd = policy["budget_usd"]
        args.max_model_calls = policy.get("max_model_calls", args.max_model_calls)
    api = Slack(args.token_file)
    try:
        access = refresh_access(api, root / "access.json")
        if args.command == "access":
            print(json.dumps({"accessible_channels": len(access["channel_ids"])}))
            return
        state_path = root / "sync.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("phase") in ("building", "build_failed"):
            raise ValueError("An unfinished paid build requires operator review; no automatic replay")
        current = root / "current"
        previous = current.resolve() if current.is_symlink() else None
        previous_snapshot = json.loads((previous / "snapshot.json").read_text()) if previous else None
        bootstrap = args.bootstrap_database
        if state.get("slack_batch_database"):
            database = Path(state["slack_batch_database"])
        elif not state.get("last_database") and not state.get("pending_database") and bootstrap:
            state.update(phase="initial_collection", initial_database=str(bootstrap))
            write_json(state_path, state)
            with bootstrap.with_suffix(".lock").open("a") as initial_lock:
                try:
                    fcntl.flock(initial_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(json.dumps({"phase": "waiting_for_initial_capture"}))
                    return
                # Resume an interrupted bootstrap; never compete with its original collector.
                with sqlite3.connect(f"file:{bootstrap}?mode=ro", uri=True) as db:
                    meta = dict(db.execute("SELECT key,value FROM metadata"))
                if not meta.get("completed_at"):
                    db = open_store(bootstrap, float(meta["since"]), float(meta["until"]))
                    try:
                        if not collect(api, db, float(meta["since"]), float(meta["until"])):
                            raise ValueError("Initial capture is incomplete; no candidate was built")
                    finally:
                        db.close()
                # Preserve the completed archive while revalidating its current channel scope.
                database = root / ("capture-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".sqlite")
                with sqlite3.connect(f"file:{bootstrap}?mode=ro", uri=True) as source, sqlite3.connect(database) as copy:
                    source.backup(copy)
            state.update(phase="collecting", pending_database=str(database), pending_until=float(meta["until"]))
            write_json(state_path, state)
            db = open_store(database, float(meta["since"]), float(meta["until"]))
            try:
                if not collect(api, db, float(meta["since"]), float(meta["until"])):
                    raise ValueError("Initial capture revalidation is incomplete; no candidate was built")
            finally:
                db.close()
        else:
            pending = state.get("pending_database")
            database = Path(pending) if pending else root / ("capture-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".sqlite")
            until = state["pending_until"] if pending else datetime.now(timezone.utc).timestamp()
            since = args.since.timestamp()
            if pending and database.exists():
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as saved:
                    bounds = dict(saved.execute("SELECT key,value FROM metadata"))
                since, until = float(bounds["since"]), float(bounds["until"])
            state.update(phase="collecting", pending_database=str(database), pending_until=until)
            write_json(state_path, state)
            db = open_store(database, since, until)
            prior = sqlite3.connect(f"file:{state['last_database']}?mode=ro", uri=True) if state.get("last_database") else None
            if prior:
                prior_time = timestamp(dict(prior.execute("SELECT key,value FROM metadata"))["completed_at"])
                if any(timestamp(value) >= prior_time for value in access.get("revoked_channels", {}).values()):
                    prior.close(); prior = None
            try:
                if not collect(api, db, since, until, prior):
                    raise ValueError("Capture is incomplete; no candidate was built")
            finally:
                db.close()
                if prior:
                    prior.close()
        # Check ACL again after the potentially long capture, before any model request.
        access = refresh_access(api, root / "access.json")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            if state.get("slack_batch_database"):
                captured_channels = {row[0] for row in db.execute("SELECT id FROM channels")}
                captured_at = timestamp(dict(db.execute("SELECT key,value FROM metadata"))["completed_at"])
                observed_revocations = {channel for channel, revoked_at in access.get("revoked_channels", {}).items() if timestamp(revoked_at) >= captured_at}
                excluded = set(state.get("slack_batch_excluded_channels", [])) | (captured_channels - set(access["channel_ids"])) | (captured_channels & observed_revocations)
                state["slack_batch_excluded_channels"] = sorted(excluded)
                write_json(state_path, state)
                snapshot = export_snapshot(db, previous_snapshot, allowed_channels=set(access["channel_ids"]) - excluded)
            else:
                snapshot = export_snapshot(db, previous_snapshot)
        if snapshot["principal"] != access["principal"]:
            raise ValueError("Reader or channel access changed during capture")
        withdraw_changed(previous_snapshot, snapshot, state, state_path, slack_only=True)
        if not set(snapshot["collection"]["channel_ids"]) <= set(access["channel_ids"]):
            raise ValueError("Reader or channel access changed during capture")
        if args.supplement:
            supplement = json.loads(args.supplement.read_text())
            if supplement.get("principal") != snapshot["principal"]:
                raise ValueError("Supplement belongs to a different authorized reader")
            if any(r["source_type"] == "slack" for r in supplement["records"]):
                raise ValueError("Supplement cannot reintroduce Slack records")
            snapshot["records"].extend(supplement["records"])
            snapshot["collection"]["supplement_coverage"] = "Static previously selected non-Slack sources; not synchronized by this collector"
        from company_context.document_sources import merge_records, resource_identity
        history = managed_resources(state, previous_snapshot)
        for provider, identities in history.items():
            retired_aliases = set(identities)
            snapshot["records"] = [r for r in snapshot["records"] if resource_identity(r, provider) not in retired_aliases]
        managed = [r for r in (previous_snapshot or {}).get("records", []) if "provider_scope" in r]
        if managed:
            for provider in sorted({r["provider_scope"]["provider"] for r in managed}):
                snapshot["records"] = merge_records(snapshot["records"], [r for r in managed if r["provider_scope"]["provider"] == provider], provider)
            snapshot["collection"]["managed_sources"] = previous_snapshot.get("collection", {}).get("managed_sources", {})
        withdraw_changed(previous_snapshot, snapshot, state, state_path)
        limit = policy.get("slack_batch_sources", 100)
        capacity = policy.get("slack_batch_characters", 500_000)
        if type(limit) is not int or limit < 1 or type(capacity) is not int or capacity < 1:
            raise ValueError("Positive Slack source count and character capacity required")
        previous_rows = {r["source_id"]: r for r in (previous_snapshot or {}).get("records", [])}
        slack_rows = [r for r in snapshot["records"] if r["source_type"] == "slack"]
        retained = [r for r in slack_rows if previous_rows.get(r["source_id"]) == r]
        def activity(row):
            dates = [row.get("updated_at"), row.get("occurred_at"), (row.get("message_spans") or [{}])[-1].get("occurred_at")]
            return max((timestamp(value) for value in dates if value), default=timestamp(row["collected_at"])), row["source_id"]
        pending = sorted([r for r in slack_rows if previous_rows.get(r["source_id"]) != r], key=activity, reverse=True)
        selected = []
        for row in pending:
            if len(selected) >= limit: break
            if selected and len(row["text"]) > capacity: continue
            selected.append(row); capacity -= len(row["text"])
        snapshot["records"] = [r for r in snapshot["records"] if r["source_type"] != "slack"] + retained + selected
        complete = len(retained) + len(selected) == len(slack_rows)
        snapshot["collection"]["slack_indexing"] = {"available_sources": len(slack_rows), "indexed_sources": len(retained) + len(selected), "complete": complete}
        updates = {"last_database": str(database), "slack_batch_database": None if complete else str(database), "slack_batch_excluded_channels": [] if complete else state.get("slack_batch_excluded_channels", [])}
        corpus = Corpus(snapshot)
        require_access(corpus, root / "access.json")
        if previous_snapshot and sorted(previous_snapshot["records"], key=lambda r: r["source_id"]) == sorted(snapshot["records"], key=lambda r: r["source_id"]):
            state.update(phase="unchanged", last_collected_at=snapshot["collection"]["completed_at"], coverage_snapshot=digest(previous_snapshot), verified_collection=snapshot["collection"], **updates)
            state.pop("pending_database", None); state.pop("pending_until", None)
            write_json(state_path, state)
            return
        await publish_snapshot(args, snapshot, state, previous, lambda: refresh_access(api, root / "access.json"), updates)
    finally:
        api.client.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("access", "sync"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--bootstrap-database", type=Path)
    parser.add_argument("--supplement", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--budget-usd", type=float, default=50)
    parser.add_argument("--max-model-calls", type=int, default=50000)
    parser.add_argument("--mcp-token-file", type=Path)
    parser.add_argument("--service", default="company-context.service")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.command == "sync" and (not args.since or not args.since.tzinfo or not args.config or not args.mcp_token_file or args.budget_usd <= 0 or args.max_model_calls < 1):
        parser.error("Sync requires an aware start date, model config, MCP token file and positive budget")
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state_dir / (args.command + ".lock")).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(synchronize(args))


if __name__ == "__main__":
    main()
