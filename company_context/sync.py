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
from company_context.context import Corpus, Models, build, close_engine, open_index, slack_channel_id
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
    auth = api.get("auth.test")
    if auth.get("bot_id") or not auth.get("user_id") or not auth.get("team_id"):
        raise ValueError("Slack must confirm a named human reader and workspace")
    identity = f"slack:{auth['team_id']}:{auth['user_id']}"
    path = Path(path)
    if path.exists() and json.loads(path.read_text())["principal"] != identity:
        raise ValueError("Refusing to replace another reader's access inventory")
    channels = inventory(api)
    result = {"principal": identity, "checked_at": datetime.now(timezone.utc).isoformat(), "channel_ids": sorted(channels)}
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


def publish(directory, current):
    directory, current = Path(directory).resolve(), Path(current)
    if not (directory / "ready.json").exists():
        raise ValueError("Cannot publish an unfinished index")
    temporary = current.with_name(current.name + ".next")
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(directory, target_is_directory=True)
    os.replace(temporary, current)


async def synchronize(args):
    root = args.state_dir
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
        if not state.get("last_database") and bootstrap:
            with sqlite3.connect(f"file:{bootstrap}?mode=ro", uri=True) as db:
                meta = dict(db.execute("SELECT key,value FROM metadata"))
                if not meta.get("completed_at"):
                    print(json.dumps({"phase": "waiting_for_initial_capture"}))
                    return
            # The initial capture can predate the reply-audit columns.
            migrated = open_store(bootstrap, float(meta["since"]), float(meta["until"]))
            migrated.close()
            database = bootstrap
        else:
            pending = state.get("pending_database")
            database = Path(pending) if pending else root / ("capture-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".sqlite")
            until = state["pending_until"] if pending else datetime.now(timezone.utc).timestamp()
            state.update(phase="collecting", pending_database=str(database), pending_until=until)
            write_json(state_path, state)
            db = open_store(database, args.since.timestamp(), until)
            prior = sqlite3.connect(f"file:{state['last_database']}?mode=ro", uri=True) if state.get("last_database") else None
            try:
                if not collect(api, db, args.since.timestamp(), until, prior):
                    raise ValueError("Capture is incomplete; no candidate was built")
            finally:
                db.close()
                if prior:
                    prior.close()
        # Check ACL again after the potentially long capture, before any model request.
        access = refresh_access(api, root / "access.json")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            snapshot = export_snapshot(db, previous_snapshot)
        if snapshot["principal"] != access["principal"] or not set(snapshot["collection"]["channel_ids"]) <= set(access["channel_ids"]):
            raise ValueError("Reader or channel access changed during capture")
        if args.supplement:
            supplement = json.loads(args.supplement.read_text())
            if any(r["source_type"] == "slack" for r in supplement["records"]):
                raise ValueError("Supplement cannot reintroduce Slack records")
            snapshot["records"].extend(supplement["records"])
            snapshot["collection"]["supplement_coverage"] = "Static previously selected non-Slack sources; not synchronized by this collector"
        corpus = Corpus(snapshot)
        if previous_snapshot and previous_snapshot["records"] == snapshot["records"]:
            state.update(phase="unchanged", last_database=str(database), last_collected_at=snapshot["collection"]["completed_at"])
            state.pop("pending_database", None); state.pop("pending_until", None)
            write_json(state_path, state)
            return
        config = json.loads(args.config.read_text())
        # One ledger spans all synchronization builds; a new generation does not reset spending.
        models = Models(config, args.max_model_calls, root / "sync-model-receipts.jsonl", args.budget_usd)
        candidate = root / "releases" / corpus.version[:24]
        blocked = list(state.get("blocked_snapshots", []))
        if previous_snapshot and changed_or_removed(previous_snapshot, snapshot):
            blocked.append(Corpus(previous_snapshot).version)
        state.update(phase="building", candidate=str(candidate), blocked_snapshots=sorted(set(blocked)), sources=len(corpus.records), chunks=len(corpus.chunks))
        write_json(state_path, state)
        try:
            # Legacy pilot snapshots have a different reader/chunking contract and need a fresh build.
            base = previous if previous_snapshot and previous_snapshot.get("collection", {}).get("kind") == "slack_channel_capture" else None
            await build(snapshot, candidate, models, base)
            verified, rag = await open_index(candidate, models)
            await close_engine(rag)
            access = refresh_access(api, root / "access.json")
            if availability(verified, root / "access.json") != "ready":
                raise ValueError("Channel access changed during indexing")
            publish(candidate, current)
            subprocess.run(["systemctl", "--user", "restart", args.service], check=True)
            # A running process alone is not acceptance: require an authenticated MCP status call.
            from company_context.evaluate import rpc
            from company_context.server import token_from_file
            import httpx
            for attempt in range(30):
                try:
                    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}/mcp", headers={"Authorization": "Bearer " + token_from_file(args.mcp_token_file), "Accept": "application/json, text/event-stream"}, timeout=10, trust_env=False) as client:
                        response = rpc(client, "tools/call", {"name": "company_index_status", "arguments": {}})
                        handle = next(iter(corpus.chunks))
                        original = rpc(client, "tools/call", {"name": "get_company_source", "arguments": {"chunk_id": handle}})
                    if response.get("snapshot") != corpus.version or response.get("availability") != "ready" or original != corpus.read(handle):
                        raise ValueError("Published MCP reports a different snapshot")
                    break
                except httpx.TransportError:
                    if attempt == 29:
                        raise
                    await asyncio.sleep(1)
            state.update(phase="published", last_database=str(database), published_snapshot=corpus.version, last_collected_at=snapshot["collection"]["completed_at"], model_calls=models.calls, charged_or_estimated_usd=models.cost)
            state.pop("pending_database", None); state.pop("pending_until", None)
            write_json(state_path, state)
        except BaseException:
            state["phase"] = "build_failed"
            write_json(state_path, state)
            raise
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
