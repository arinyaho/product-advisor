"""Freeze and retain actual MCP retrieval results; do not grade business conclusions automatically."""

import argparse
import hashlib
import json
from pathlib import Path

import httpx

from company_context.server import token_from_file


def rpc(client, method, params):
    url = client.base_url.copy_with(path=client.base_url.path.rstrip("/"))
    response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    if response.headers.get("content-type", "").startswith("application/json"):
        message = response.json()
    else:
        message = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]
    if "error" in message:
        raise RuntimeError("MCP protocol error")
    result = message["result"]
    if result.get("isError"):
        raise RuntimeError("MCP tool returned an error")
    if "content" in result:
        return result.get("structuredContent") or json.loads(next(row["text"] for row in result["content"] if row["type"] == "text"))
    return result


def evaluate(url, token_file, cases_path, output):
    cases_bytes = Path(cases_path).read_bytes()
    evaluation = json.loads(cases_bytes)
    if not evaluation.get("frozen_before_indexing"):
        raise ValueError("Freeze source expectations before indexing")
    scores = {mode: {"passed": 0, "total": 0} for mode in ("naive", "mix")}
    headers = {"Authorization": "Bearer " + token_from_file(token_file), "Accept": "application/json, text/event-stream"}
    with Path(output).open("x") as stream, httpx.Client(base_url=url, headers=headers, timeout=180, trust_env=False, follow_redirects=False) as client:
        rpc(client, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "company-context-evaluation", "version": "1"}})
        status = rpc(client, "tools/call", {"name": "company_index_status", "arguments": {}})
        stream.write(json.dumps({"fixture_sha256": hashlib.sha256(cases_bytes).hexdigest(), "status": status}) + "\n")
        stream.flush()
        for case in evaluation["cases"]:
            for mode in scores:
                arguments = {"query": case["query"], "mode": mode, "k": 5}
                result = rpc(client, "tools/call", {"name": "search_company_context", "arguments": arguments})
                expected = case["expected"]
                found = [any(hit["source_id"] == item["source_id"] and item["contains"] in hit["text"] for hit in result["results"]) for item in expected]
                ready = result["retrieval"]["status"] == "ready" and result["retrieval"]["actual_mode"] == mode
                source_reads = []
                for hit in result["results"]:
                    read = rpc(client, "tools/call", {"name": "get_company_source", "arguments": {"chunk_id": hit["chunk_id"]}})
                    if read != hit:
                        raise RuntimeError("Search and read returned different source evidence")
                    source_reads.append(read["chunk_id"])
                passed = ready and all(found) if expected else None
                if expected:
                    scores[mode]["total"] += 1
                    scores[mode]["passed"] += int(passed)
                stream.write(json.dumps({"case": case["id"], "group": case["group"], "request": arguments, "response": result, "expected_source_ids": [e["source_id"] for e in expected], "expected_passages_found": found, "source_reads_verified": source_reads, "retrieval_passed": passed, "answer_review": "required"}, ensure_ascii=False) + "\n")
                stream.flush()
                print(json.dumps({"case": case["id"], "mode": mode, "retrieval_passed": passed}), flush=True)
        stream.write(json.dumps({"summary": scores, "answer_quality": "not_automatically_evaluated"}) + "\n")
    return scores


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.url, args.token_file, args.cases, args.output)))
