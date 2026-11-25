"""Run the company-only MCP inside the company network with internal models."""

import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from company_context.context import Models, build, close_engine, open_index, search, timestamp


def token_from_file(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("MCP token file must not be readable by other users")
    token = path.read_text().strip()
    if len(token) < 32:
        raise ValueError("MCP token must contain at least 32 characters")
    return token


def create_app(corpus, rag, token_file, access_state=None, sync_state=None):
    token_from_file(token_file)
    mcp = MCPServer("company-context", instructions="Internal source retrieval for one authorized reader. Graph descriptions are derived navigation, not facts. Follow sources[].read_source with get_company_source before relying on a graph claim. Compare original claims by subject, actor, decision scope and event time: same-scope replacement, different scope, coexisting perspectives, or unresolved. Recency alone does not establish authority. Never send company records to the public book MCP.")

    def current_availability():
        from company_context.sync import availability
        return availability(corpus, access_state, sync_state)

    def require_available():
        state = current_availability()
        if state != "ready":
            raise ToolError(state)

    @mcp.tool()
    def company_index_status() -> dict:
        """Check snapshot identity, reader scope and coverage before using company evidence."""
        collected = [r["collected_at"] for r in corpus.records.values()]
        return {"snapshot": corpus.version, "principal": corpus.principal, "source_count": len(corpus.records), "source_types": sorted({r["source_type"] for r in corpus.records.values()}), "collected_from": min(collected, key=timestamp), "collected_through": max(collected, key=timestamp), "coverage": corpus.snapshot.get("collection", "explicit_snapshot_not_all_company_knowledge"), "availability": current_availability(), "context_version": 3, "graph_descriptions": True, "applicability": "caller_reviews_original_sources_not_automatically_resolved"}

    @mcp.tool()
    async def search_company_context(query: str, mode: str = "mix", k: int = 5) -> dict:
        """Search company records semantically without knowing source names or exact terms.
        mix uses the native graph and vectors; naive uses vectors for comparison.
        Returns original passages, source URLs, authors, event/edit/collection timestamps,
        source/summary attribution and read handles. mix also returns bounded graph descriptions
        with sources[].read_source arguments for get_company_source, including sources absent
        from the top chunks. Read originals before using these derived descriptions. Check
        graph_coverage for omissions and make focused follow-up searches when necessary.
        Classify material evidence as same-scope replacement, different scope, coexisting
        perspectives, or unresolved; newer data does not automatically override older data.
        Contradictions and unknown reasons
        must remain explicit. Empty matches do not prove the company has no such evidence."""
        try:
            require_available()
            return await search(corpus, rag, query, mode, k)
        except ValueError as error:
            raise ToolError(str(error)) from None

    @mcp.tool()
    def get_company_source(chunk_id: str) -> dict:
        """Read an exact source chunk with provenance from the active snapshot.
        Follow previous_chunk_id/next_chunk_id to inspect the surrounding source or full thread.
        For Slack conversations, follow root_chunk_id to read the opening message; an absent
        root is explicitly outside the capture. messages retains each speaker, time and URL.
        A matching passage alone does not establish the source's final decision."""
        try:
            require_available()
            return corpus.read(chunk_id)
        except ValueError as error:
            raise ToolError(str(error)) from None

    inner = mcp.streamable_http_app(stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
            authorization = headers.get("authorization", "")
            supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
            try:
                valid = hmac.compare_digest(supplied.encode(), token_from_file(token_file).encode())
            except (OSError, ValueError):
                valid = False
            if not valid:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await inner(scope, receive, send)

    return guarded


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "serve"])
    parser.add_argument("--config", type=Path, required=True, help="Explicit model configuration; credentials are file references")
    parser.add_argument("--directory", type=Path, required=True, help="New snapshot directory for build; completed directory for serve")
    parser.add_argument("--input", type=Path, help="Complete, authorized source snapshot JSON")
    parser.add_argument("--previous", type=Path, help="Completed index to copy and update without changing the served snapshot")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--access-state", type=Path, help="Hourly channel ACL inventory; stale or missing inventory blocks evidence reads")
    parser.add_argument("--sync-state", type=Path, help="Known source changes block the previous index until replacement")
    parser.add_argument("--max-model-calls", type=int, default=200)
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.command == "build" and args.input is None:
        parser.error("build requires --input")
    if args.command == "serve" and args.token_file is None:
        parser.error("serve requires --token-file")
    if args.max_model_calls < 1 or args.budget_usd <= 0:
        parser.error("Model call and cost limits must be positive")
    config = json.loads(args.config.read_text())
    args.directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    models = Models(config, max_calls=args.max_model_calls, budget=args.budget_usd, receipts=args.directory.parent / (args.directory.name + "-receipts.jsonl"))

    async def run():
        if args.command == "build":
            ready = await build(json.loads(args.input.read_text()), args.directory, models, args.previous)
            print(json.dumps(ready))
            return
        import uvicorn
        corpus, rag = await open_index(args.directory, models)
        try:
            app = create_app(corpus, rag, args.token_file, args.access_state, args.sync_state)
            await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False)).serve()
        finally:
            await close_engine(rag)

    asyncio.run(run())


if __name__ == "__main__":
    main()
