"""Publish completed document captures through the shared company-index pipeline."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

from company_context.collect_slack import Slack
from company_context.context import Corpus, digest, timestamp
from company_context.document_sources import IncompleteCapture, export_capture, merge_records, refresh_document_access
from company_context.sync import publish_snapshot, refresh_access, require_access, withdraw_changed, write_json


def snapshots_in_use(root):
    result = []
    current = root / "current" / "snapshot.json"
    if current.exists(): result.append(json.loads(current.read_text()))
    state_path = root / "sync.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("phase") == "building" and state.get("candidate"):
            candidate = Path(state["candidate"]) / "snapshot.json"
            if candidate.exists(): result.append(json.loads(candidate.read_text()))
    return result


async def synchronize(root, settings):
    current = root / "current"
    previous = current.resolve() if current.is_symlink() else None
    old = json.loads((previous / "snapshot.json").read_text()) if previous else {"principal": settings["principal"], "records": []}
    if old["principal"] not in (settings["principal"], settings.get("legacy_principal")):
        raise ValueError("Current index belongs to a different reader")
    state_path = root / "sync.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("phase") in ("building", "build_failed"):
        raise ValueError("An unfinished paid build requires operator review; no automatic replay")
    snapshot = {"principal": settings["principal"], "records": list(old["records"]), "collection": dict(old.get("collection", {}))}
    coverage = dict(snapshot["collection"].get("managed_sources", {}))
    imports = dict(state.get("document_imports", {}))
    status = {}
    changed = False
    remaining = settings.get("batch_sources", 500)
    remaining_characters = settings.get("batch_characters", 2_000_000)
    providers = list(settings["providers"])
    start = providers.index(state["next_document_provider"]) if state.get("next_document_provider") in providers else 0
    providers = providers[start:] + providers[:start]
    next_provider = providers[0] if providers else None
    for provider in providers:
        config = settings["providers"][provider]
        try:
            incoming = export_capture(provider, config, settings["principal"])
        except IncompleteCapture as error:
            status[provider] = {"phase": str(error)}
            continue
        identity = digest(incoming)
        status[provider] = {"phase": "already_indexed" if imports.get(provider) == identity else "ready_to_index", "sources": len(incoming["records"]), "coverage": incoming["collection"]}
        if imports.get(provider) == identity: continue
        existing = {r["source_id"]: r for r in old["records"] if r.get("provider_scope", {}).get("provider") == provider}
        retained = [r for r in incoming["records"] if existing.get(r["source_id"]) == r]
        pending = sorted([r for r in incoming["records"] if existing.get(r["source_id"]) != r], key=lambda r: (timestamp(r.get("updated_at") or r["collected_at"]), r["source_id"]), reverse=True)
        selected = []
        for row in pending:
            if not remaining or len(row["text"]) > remaining_characters: continue
            selected.append(row)
            remaining -= 1; remaining_characters -= len(row["text"])
        snapshot["records"] = merge_records(snapshot["records"], sorted(retained + selected, key=lambda r: r["source_id"]), provider)
        count = len(retained) + len(selected)
        coverage[provider] = {**incoming["collection"], "available_sources": len(incoming["records"]), "indexed_sources": count, "index_complete": count == len(incoming["records"])}
        status[provider].update(indexed_sources=len(retained), batch_sources=len(selected), remaining_sources=len(pending))
        if selected: next_provider = providers[(providers.index(provider) + 1) % len(providers)]
        if count == len(incoming["records"]): imports[provider] = identity
        else: imports.pop(provider, None)
        changed = True
    write_json(root / "document-ingestion.json", status)
    if not changed:
        return
    if snapshot["principal"] == old["principal"] and sorted(snapshot["records"], key=lambda r: r["source_id"]) == sorted(old["records"], key=lambda r: r["source_id"]):
        state["document_imports"] = imports
        write_json(state_path, state)
        for entry in status.values():
            if entry["phase"] == "ready_to_index": entry["phase"] = "indexing" if entry.get("remaining_sources") else "indexed"
        write_json(root / "document-ingestion.json", status)
        return
    snapshot["collection"].update(kind="company_capture", managed_sources=coverage, completed_at=datetime.now(timezone.utc).isoformat())
    # Persist known withdrawals before ACL or budget failure can reject the candidate.
    withdraw_changed(old if previous else None, snapshot, state, state_path)
    corpus = Corpus(snapshot)
    api = Slack(settings["slack_token_file"])
    try:
        access = refresh_access(api, root / "access.json")
        if access["principal"] != settings["principal"]: raise ValueError("Slack reader differs from document authorization")
        refresh_document_access(root, settings, [old, snapshot])
        require_access(corpus, root / "access.json")
        args = SimpleNamespace(state_dir=root, config=Path(settings["model_config"]), budget_usd=settings["budget_usd"], max_model_calls=settings.get("max_model_calls", 50000), mcp_token_file=Path(settings["mcp_token_file"]), service=settings.get("service", "company-context.service"), port=settings.get("port", 8766))
        await publish_snapshot(args, snapshot, state, previous, lambda: refresh_access(api, root / "access.json"), {"document_imports": imports, "next_document_provider": next_provider})
        for provider in status:
            if status[provider]["phase"] == "ready_to_index":
                status[provider]["indexed_sources"] += status[provider].get("batch_sources", 0)
                status[provider]["remaining_sources"] -= status[provider].get("batch_sources", 0)
                status[provider]["phase"] = "indexing" if status[provider]["remaining_sources"] else "indexed"
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
    if settings["budget_usd"] <= 0 or settings.get("batch_sources", 500) < 1 or settings.get("batch_characters", 2_000_000) < 2_000_000: raise ValueError("Positive budget/batch count and capacity for the largest supported source required")
    if args.command == "access":
        refresh_document_access(root, settings, snapshots_in_use(root))
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
