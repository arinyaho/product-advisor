"""SSH/stdio access to the internal MCP; the token never leaves the company host."""

import argparse

import httpx
from mcp.server.mcpserver import MCPServer

from company_context.evaluate import rpc
from company_context.server import token_from_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    mcp = MCPServer("company-context", instructions="Read graph sources with get_company_source before drawing conclusions. Compare original claims by subject, actor, decision scope and event time: same-scope replacement, different scope, coexisting perspectives, or unresolved. Recency alone does not establish authority. Company evidence must stay out of the public book MCP.")

    def call(name, arguments):
        headers = {"Authorization": "Bearer " + token_from_file(args.token_file), "Accept": "application/json, text/event-stream"}
        with httpx.Client(base_url=f"http://127.0.0.1:{args.port}/mcp", headers=headers, timeout=180, trust_env=False) as client:
            return rpc(client, "tools/call", {"name": name, "arguments": arguments})

    @mcp.tool()
    def company_index_status() -> dict:
        """Check the internal company snapshot, authorized reader and collection coverage."""
        return call("company_index_status", {})

    @mcp.tool()
    def search_company_context(query: str, mode: str = "mix", k: int = 5) -> dict:
        """Search internal company evidence semantically, with source links, dates and authors.
        Results are source candidates, not verified facts. Read sources and preserve contradictions.
        mix returns graph descriptions with sources[].read_source arguments for get_company_source.
        These may lead to relevant originals outside the top chunks. Descriptions are model-derived;
        inspect graph_coverage and search again if needed. Read originals before classifying material
        claims as same-scope replacement, different scope, coexisting perspectives, or unresolved.
        Do not forward company records to the public book MCP."""
        return call("search_company_context", {"query": query, "mode": mode, "k": k})

    @mcp.tool()
    def get_company_source(chunk_id: str) -> dict:
        """Read the exact internal source passage and provenance returned by company search.
        Follow previous_chunk_id/next_chunk_id for surrounding passages and later thread replies.
        Follow root_chunk_id for the opening message; messages retains individual speakers and times."""
        return call("get_company_source", {"chunk_id": chunk_id})

    mcp.run()


if __name__ == "__main__":
    main()
