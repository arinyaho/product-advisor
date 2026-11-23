#!/usr/bin/env python3
"""Verify the deployed library through the user's configured MCP connection."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
from book_library import Catalog, atomic_json, words, digest


async def verify(release, output, host=None, fixture_path=None):
    root = Path(__file__).resolve().parent.parent
    fixture_path = fixture_path or root / "eval/three_book_cases.json"
    fixture_sha256 = digest(fixture_path.read_bytes())
    catalog = Catalog.load(release)
    version = json.loads((release / "manifest.json").read_text())["version"]
    retrieval_version = json.loads((release / "ready.json").read_text())["retrieval_version"]
    config = tomllib.loads((Path.home() / ".codex/config.toml").read_text())["mcp_servers"]["product-advisor"]
    headers = dict(config.get("http_headers", {}))
    headers.update({name: os.environ[variable] for name, variable in config.get("env_http_headers", {}).items()})
    if config.get("bearer_token_env_var"):
        headers["Authorization"] = "Bearer " + os.environ[config["bearer_token_env_var"]]
    assert any(name.lower() == "authorization" for name in headers), "The configured client has no credential; no substitute key is used"
    report = {"version": version, "fixture_sha256": fixture_sha256, "endpoint": config["url"], "credential_source": "existing Codex MCP configuration", "checks": []}
    async with httpx2.AsyncClient(headers=headers, timeout=120) as http:
        async with streamable_http_client(config["url"], http_client=http) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                async def call(name, arguments):
                    result = await session.call_tool(name, arguments, read_timeout_seconds=120)
                    assert not result.is_error, name + " returned a tool error"
                    value = result.structured_content
                    if value is not None:
                        return value["result"] if set(value) == {"result"} else value
                    content = [block.text for block in result.content if block.type == "text"]
                    assert len(content) == 1, "Unexpected MCP result shape"
                    try:
                        return json.loads(content[0])
                    except ValueError:
                        return content[0]
                tools = await session.list_tools()
                assert {"list_books", "search_book", "list_sections", "get_section"} <= {tool.name for tool in tools.tools}
                books = await call("list_books", {})
                assert {book["book_id"] for book in books} == set(catalog.books)
                for book in books:
                    expected = catalog.books[book["book_id"]]
                    assert book["sha256"] == expected["sha256"] and book["edition"] == expected["edition"]
                    assert book["index_status"] == "ready" and book["index_version"] == version
                report["checks"].append({"tool": "list_books", "book_ids": sorted(catalog.books), "passed": True})
                for book_id, book in catalog.books.items():
                    toc = await call("list_sections", {"book_id": book_id, "limit": 100})
                    assert toc["total"] == len(book["sections"])
                    assert [row["id"] for row in toc["sections"]] == [row["id"] for row in book["sections"][:100]]
                    assert all(row["book_id"] == book_id for row in toc["sections"])
                    if toc["next_offset"] is not None:
                        next_toc = await call("list_sections", {"book_id": book_id, "offset": toc["next_offset"], "limit": 100})
                        assert next_toc["sections"][0]["id"] == book["sections"][toc["next_offset"]]["id"]
                    section = max(book["sections"], key=lambda row: len(catalog.read(row["id"], book_id, None, "full")["text"]))
                    first = await call("get_section", {"id": section["id"], "book_id": book_id})
                    expected = catalog.read(section["id"], book_id, None, first["scope"])
                    assert first == expected and first["continuation"]
                    following = await call("get_section", first["continuation"])
                    assert following == catalog.read(section["id"], book_id, first["continuation"]["start_page"], first["scope"])
                    report["checks"].append({"tool": "toc/read/continue", "book_id": book_id, "section_id": section["id"], "first_chunk": first["locations"][0]["chunk_id"], "following_chunk": following["locations"][0]["chunk_id"], "scope": first["scope"], "passed": True})
                combined_toc = await call("list_sections", {})
                assert combined_toc["total"] == sum(len(book["sections"]) for book in catalog.books.values())
                assert all("book_id" in row for row in combined_toc["sections"])
                legacy = await call("get_section", {"id": 27})
                assert isinstance(legacy, str) and "The Information You Need from Those Early Pricing Talks" in legacy
                report["checks"].append({"tool": "legacy get_section", "id": 27, "passed": True})
                cases = {case["id"]: case for case in json.loads(fixture_path.read_text())["cases"]}
                for name, mode in [("mi-wtp-en", "mix"), ("fs-icp-en", "mix"), ("de-beachhead-en", "mix"), ("all-pilot-ko", "mix"), ("all-segment-ko", "mix"), ("all-segment-ko", "naive"), ("unsupported-venus", "mix")]:
                    case = {**cases[name], "book_id": catalog.resolve_book_id(cases[name].get("book_id"))}
                    arguments = {"query": case["query"], "k": 5, "mode": mode}
                    if case.get("book_id"):
                        arguments["book_id"] = case["book_id"]
                    result = await call("search_book", arguments)
                    retrieval = result["retrieval"]
                    check = {"tool": "search_book", "case": name, "mode": mode, "source_ids": [hit["chunk_id"] for hit in result["results"]], "retrieval": retrieval, "passed": False}
                    report["checks"].append(check)
                    atomic_json(output, report)
                    assert retrieval["status"] == "ready" and retrieval["actual_mode"] == mode
                    assert retrieval["requested_mode"] == mode and not retrieval["fallback"] and retrieval["index_version"] == version
                    assert retrieval["retrieval_version"] == retrieval_version
                    identities = []
                    for hit in result["results"]:
                        source = catalog.chunks[hit["chunk_id"]]
                        assert hit["location"] == source["location"] and words(source["text"]).startswith(words(hit["text"]))
                        assert not case.get("book_id") or hit["book_id"] == case["book_id"]
                        identities.append(hit["chunk_id"])
                    assert bool(set(identities) & set(case["expected_chunk_ids"])) if case["expected_chunk_ids"] else not identities
                    check["passed"] = True
                    atomic_json(output, report)
    report["passed"] = True
    atomic_json(output, report)
    if host:
        target = "/volume1/docker/product-advisor/staging/" + version + "/accepted.json"
        command = "umask 077; cat > " + shlex.quote(target + ".tmp") + " && mv " + shlex.quote(target + ".tmp") + " " + shlex.quote(target)
        subprocess.run(["ssh", host, command], input=json.dumps({"version": version, "fixture_sha256": fixture_sha256, "status": "accepted", "checks": len(report["checks"])}), text=True, check=True)
    print(json.dumps({"version": version, "endpoint": config["url"], "passed": True, "checks": len(report["checks"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--accept-host")
    parser.add_argument("--cases", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.release, args.output, args.accept_host, args.cases))
