"""Publish completed document captures through the shared company-index pipeline."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from company_context.collect_slack import Slack
from company_context.context import Corpus, digest, timestamp
from company_context.document_sources import IncompleteCapture, export_capture, merge_records, refresh_document_access, resource_identity
from company_context.sync import publish_snapshot, refresh_access, reported_coverage, require_access, withdraw_changed, write_json


def snapshots_in_use(root):
    result = []
    current = root / "current" / "snapshot.json"
    if current.exists(): result.append(json.loads(current.read_text()))
    preflight = root / "document-preflight.json"
    if preflight.exists(): result.append(json.loads(preflight.read_text()))
    state_path = root / "sync.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("phase") == "building" and state.get("candidate"):
            candidate = Path(state["candidate"]) / "snapshot.json"
            if candidate.exists(): result.append(json.loads(candidate.read_text()))
    return result


def validate_batch_limits(settings):
    sources = settings.get("batch_sources", 500)
    characters = settings.get("batch_characters", 2_000_000)
    if type(sources) is not int or sources < 1 or type(characters) is not int or characters < 2_000_000:
        raise ValueError("Positive document batch count and capacity for the largest supported source required")
    return sources, characters


async def synchronize(root, settings, *, slack_batch=None):
    try:
        return await _synchronize(root, settings, slack_batch)
    finally:
        with (root / "document-access.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            (root / "document-preflight.json").unlink(missing_ok=True)


async def _synchronize(root, settings, slack_batch):
    current = root / "current"
    previous = current.resolve() if current.is_symlink() else None
    old = json.loads((previous / "snapshot.json").read_text()) if previous else {"principal": settings["principal"], "records": []}
    if old["principal"] not in (settings["principal"], settings.get("legacy_principal")):
        raise ValueError("Current index belongs to a different reader")
    state_path = root / "sync.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("phase") in ("building", "build_failed"):
        raise ValueError("An unfinished paid build requires operator review; no automatic replay")
    verified_collection = reported_coverage(old, digest(old), state_path)
    snapshot = {"principal": settings["principal"], "records": list(old["records"]), "collection": dict(verified_collection) if isinstance(verified_collection, dict) else {}}
    slack_updates = {}
    if slack_batch:
        snapshot, slack_updates = slack_batch
        if snapshot["principal"] != settings["principal"]:
            raise ValueError("Slack reader differs from document authorization")
    coverage = dict(snapshot["collection"].get("managed_sources", {}))
    imports = dict(state.get("document_imports", {}))
    deferred = dict(state.get("document_access_deferred", {}))
    selected_ids = set()
    status = {}
    changed = slack_batch is not None
    batch_sources, batch_characters = validate_batch_limits(settings)
    remaining, remaining_characters = batch_sources, batch_characters
    providers = list(settings["providers"])
    start = providers.index(state["next_document_provider"]) if state.get("next_document_provider") in providers else 0
    providers = providers[start:] + providers[:start]
    next_provider = providers[0] if providers else None
    for provider in providers:
        if slack_batch:
            # Each provider gets a bounded window in the shared candidate.
            remaining, remaining_characters = batch_sources, batch_characters
        config = settings["providers"][provider]
        try:
            incoming = export_capture(provider, config, settings["principal"])
        except IncompleteCapture as error:
            status[provider] = {"phase": str(error)}
            continue
        if provider == "jira":
            has_managed_jira = any(
                row.get("provider_scope", {}).get("provider") == "jira" for row in old["records"]
            )
            if has_managed_jira and coverage.get("jira", {}).get("capture_id") != incoming["collection"]["capture_id"]:
                status[provider] = {"phase": "jira_capture_changed"}
                continue
            aliases = {
                resource_identity(row, "jira") for row in old["records"]
                if row.get("provider_scope", {}).get("provider") != "jira"
            }
            if any(resource_identity(row, "jira") in aliases for row in incoming["records"]):
                status[provider] = {"phase": "jira_unmanaged_alias_overlap"}
                continue
        complete = incoming["collection"].get("capture_complete", True)
        if not complete and any(r.get("provider_scope", {}).get("provider") == provider for r in old["records"]) and coverage.get(provider, {}).get("capture_id") != incoming["collection"].get("capture_id"):
            status[provider] = {"phase": "new_capture_in_progress"}
            continue
        identity = digest(incoming)
        status[provider] = {"phase": "already_indexed" if imports.get(provider) == identity and complete else "waiting_for_more_pages" if imports.get(provider) == identity else "ready_to_index", "sources": len(incoming["records"]), "coverage": incoming["collection"]}
        if imports.get(provider) == identity: continue
        existing = {r["source_id"]: r for r in old["records"] if r.get("provider_scope", {}).get("provider") == provider}
        retained = [r for r in incoming["records"] if existing.get(r["source_id"]) == r]
        pending = sorted([r for r in incoming["records"] if existing.get(r["source_id"]) != r], key=lambda r: (timestamp(r.get("updated_at") or r["collected_at"]), r["source_id"]), reverse=True)
        # Retry denied sources after untried sources, oldest access check first.
        pending.sort(key=lambda r: deferred.get(r["source_id"], ""))
        selected = []
        for row in pending:
            if not remaining or len(row["text"]) > remaining_characters: continue
            selected.append(row)
            selected_ids.add(row["source_id"])
            remaining -= 1; remaining_characters -= len(row["text"])
        snapshot["records"] = merge_records(snapshot["records"], sorted(retained + selected, key=lambda r: r["source_id"]), provider)
        count = len(retained) + len(selected)
        coverage[provider] = {**incoming["collection"], "available_sources": len(incoming["records"]), "indexed_sources": count, "index_complete": complete and count == len(incoming["records"])}
        status[provider].update(indexed_sources=len(retained), batch_sources=len(selected), remaining_sources=len(pending))
        if selected: next_provider = providers[(providers.index(provider) + 1) % len(providers)]
        if count == len(incoming["records"]): imports[provider] = identity
        else: imports.pop(provider, None)
        changed = True
    write_json(root / "document-ingestion.json", status)
    withdraw_changed(old if previous else None, snapshot, state, state_path)
    # Independent ACL scans must see candidate scopes before the paid build exists.
    write_json(root / "document-preflight.json", snapshot)
    refresh_document_access(root, settings, [old, snapshot])
    access = json.loads((root / "document-access.json").read_text())
    if access["principal"] != settings["principal"]:
        raise ValueError("Document access inventory belongs to another reader")
    admitted = []
    for row in snapshot["records"]:
        scope = row.get("provider_scope")
        if scope:
            provider = scope["provider"]
            verified = access["providers"][provider]
            if scope["connection"] != verified["connection"]:
                raise ValueError("Document connection changed")
            if not {scope["resource_id"], *scope.get("dependencies", [])} <= set(verified["resource_ids"]):
                deferred[row["source_id"]] = verified["checked_at"]
                imports.pop(provider, None)
                changed = True
                count = sum(r.get("provider_scope", {}).get("provider") == provider for r in snapshot["records"])
                coverage.setdefault(provider, {"available_sources": count, "indexed_sources": count})
                coverage[provider]["indexed_sources"] -= 1
                coverage[provider]["index_complete"] = False
                entry = status[provider]
                entry.setdefault("coverage", dict(coverage[provider]))
                entry.setdefault("indexed_sources", count)
                entry.setdefault("batch_sources", 0)
                entry.setdefault("remaining_sources", max(0, coverage[provider]["available_sources"] - count))
                entry["phase"] = "ready_to_index"
                entry["access_deferred_sources"] = entry.get("access_deferred_sources", 0) + 1
                entry["batch_sources" if row["source_id"] in selected_ids else "indexed_sources"] -= 1
                if row["source_id"] not in selected_ids: entry["remaining_sources"] += 1
                continue
            deferred.pop(row["source_id"], None)
        admitted.append(row)
    snapshot["records"] = admitted
    if not changed:
        return
    state["document_access_deferred"] = deferred
    write_json(state_path, state)
    write_json(root / "document-ingestion.json", status)
    collection = snapshot["collection"]
    collection.update(kind="company_capture", managed_sources=coverage)
    if "completed_at" not in collection or (not slack_batch and "slack_capture_started_at" not in collection):
        collection["completed_at"] = datetime.now(timezone.utc).isoformat()
    if snapshot["principal"] == old["principal"] and sorted(snapshot["records"], key=lambda r: r["source_id"]) == sorted(old["records"], key=lambda r: r["source_id"]):
        state.update(document_imports=imports, next_document_provider=next_provider, coverage_snapshot=digest(old), verified_collection=snapshot["collection"], **slack_updates)
        if slack_updates:
            state.update(phase="unchanged", last_collected_at=snapshot["collection"]["completed_at"])
            state.pop("pending_database", None)
            state.pop("pending_until", None)
        write_json(state_path, state)
        for entry in status.values():
            if entry["phase"] == "ready_to_index": entry["phase"] = "indexing" if entry.get("remaining_sources") else "indexed" if entry["coverage"].get("capture_complete", True) else "waiting_for_more_pages"
        write_json(root / "document-ingestion.json", status)
        return
    # Persist known withdrawals before ACL or budget failure can reject the candidate.
    withdraw_changed(old if previous else None, snapshot, state, state_path)
    corpus = Corpus(snapshot)
    api = Slack(settings["slack_token_file"])
    try:
        access = refresh_access(api, root / "access.json")
        if access["principal"] != settings["principal"]: raise ValueError("Slack reader differs from document authorization")
        require_access(corpus, root / "access.json")
        args = SimpleNamespace(state_dir=root, config=Path(settings["model_config"]), budget_usd=settings["budget_usd"], max_model_calls=settings.get("max_model_calls", 50000), mcp_token_file=Path(settings["mcp_token_file"]), service=settings.get("service", "company-context.service"), port=settings.get("port", 8766), index_parallelism=settings.get("index_parallelism", 1) if slack_batch else 1)
        await publish_snapshot(args, snapshot, state, previous, lambda: refresh_access(api, root / "access.json"), {"document_imports": imports, "next_document_provider": next_provider, **slack_updates})
        for provider in status:
            if status[provider]["phase"] == "ready_to_index":
                status[provider]["indexed_sources"] += status[provider].get("batch_sources", 0)
                status[provider]["remaining_sources"] -= status[provider].get("batch_sources", 0)
                status[provider]["phase"] = "indexing" if status[provider]["remaining_sources"] else "indexed" if status[provider]["coverage"].get("capture_complete", True) else "waiting_for_more_pages"
        write_json(root / "document-ingestion.json", status)
    finally:
        api.client.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("sync", "access"))
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.state_dir
    settings = json.loads((root / "document-sources.json").read_text())
    if settings["budget_usd"] <= 0: raise ValueError("Positive budget required")
    validate_batch_limits(settings)
    if args.command == "access":
        refresh_document_access(root, settings, [])
        return
    if settings.get("index_parallelism", 1) > 1 and (service := settings.get("combined_sync_service")):
        subprocess.run(["systemctl", "--user", "start", "--no-block", "--", service], check=True)
        return
    with (root / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"phase": "queued_behind_active_sync"}))
            return
        asyncio.run(synchronize(root, settings))


if __name__ == "__main__":
    main()
