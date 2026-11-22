"""Evaluate frozen synthetic cases; record once, replay without provider calls."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = Path(__file__).with_name("retrieval_cases.json")


def assess(case, response):
    frameworks = [item["id"] for item in response["frameworks"]]
    pages = [item["page"] for item in response["results"]]
    expected = set(case["required"])
    unwanted = set(frameworks) - expected - set(case["allowed"])
    missing = expected - set(frameworks)
    evidence = bool(set(pages) & set(case["evidence_pages"])) if expected else not pages
    return {"id": case["id"], "split": case["split"], "kind": case["kind"], "frameworks": frameworks, "pages": pages,
            "missing": sorted(missing), "unwanted": sorted(unwanted), "evidence_hit": evidence,
            "passed": not missing and not unwanted and evidence,
            "required_hits": len(expected - missing), "required_count": len(expected)}


def summarize(rows):
    relevant = [row for row in rows if row["required_count"]]
    returned = sum(len(row["frameworks"]) for row in rows)
    required = sum(row["required_count"] for row in rows)
    unwanted = sum(len(row["unwanted"]) for row in rows)
    return {"cases": len(rows), "passed": sum(row["passed"] for row in rows),
            "required_recall": sum(row["required_hits"] for row in rows) / required if required else None,
            "allowed_precision": (returned - unwanted) / returned if returned else None,
            "unwanted_inclusions": unwanted, "evidence_hits": sum(row["evidence_hit"] for row in relevant),
            "relevant_cases": len(relevant), "irrelevant_empty": sum(row["passed"] for row in rows if row["kind"] == "irrelevant")}


async def run(args):
    sys.path.insert(0, str(ROOT / "mcp"))
    import server
    from advisor_graph import AdvisorIndex, selected_concepts

    fixture = json.loads(CASES_FILE.read_text())
    cases = fixture["cases"]
    assert len(cases) == 20 and len({case["id"] for case in cases}) == 20
    assert {split: sum(case["split"] == split for case in cases) for split in ("development", "holdout")} == {"development": 10, "holdout": 10}
    assert {kind: sum(case["kind"] == kind for case in cases) for kind in ("relevant", "boundary", "irrelevant")} == {"relevant": 12, "boundary": 4, "irrelevant": 4}
    known = {concept["id"] for concept in server.CONCEPT_MAP["concepts"]}
    assert all(set(case["required"] + case["allowed"]) <= known and not set(case["required"]) & set(case["allowed"]) for case in cases)
    fixture_hash = hashlib.sha256(CASES_FILE.read_bytes()).hexdigest()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    report_file = output / f"{args.stage}.json"
    if report_file.exists():
        raise FileExistsError(f"Refusing to overwrite {report_file}; preserve the previous run")
    cache_file = output / "synthetic-embeddings.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    provider_calls = 0
    rerank_calls = 0
    rerank_file = output / "synthetic-rerank.json"
    rerank_cache = json.loads(rerank_file.read_text()) if rerank_file.exists() else {}

    async def rerank(query, documents, top_n=None):
        nonlocal rerank_calls
        request = {"model": server.RERANK_MODEL, "query": query, "documents": documents, "top_n": top_n}
        identity = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if identity not in rerank_cache:
            if not args.record_rerank:
                raise RuntimeError("Rerank replay miss; no provider request was made")
            if rerank_calls >= 39:
                raise RuntimeError("Rerank evaluation request cap reached (39, plus one preflight)")
            rerank_calls += 1
            results = await server._rerank(query, documents, top_n)
            rerank_cache[identity] = {"request": request, "results": results}
            rerank_file.write_text(json.dumps(rerank_cache, ensure_ascii=False))
        return rerank_cache[identity]["results"]

    def embed(texts):
        nonlocal provider_calls
        missing = list(dict.fromkeys(text for text in texts if text not in cache))
        if missing:
            if args.stage != "before":
                raise RuntimeError("Replay requires an unrecorded embedding; no provider request was made")
            provider_calls += 1
            for text, vector in zip(missing, server._embed(missing), strict=True):
                cache[text] = vector.tolist()
            cache_file.write_text(json.dumps(cache))
        return np.array([cache[text] for text in texts], dtype=np.float32)

    chunks_hash = hashlib.sha256(json.dumps([list(chunk) for chunk in server.CHUNKS + server.EXCERPTS], ensure_ascii=False).encode()).hexdigest()
    meta = {"fixture_sha256": fixture_hash, "chunks_sha256": chunks_hash, "chunks": len(server.CHUNKS), "excerpts": len(server.EXCERPTS),
            "sources": {name: hashlib.sha256((ROOT / "mcp" / name).read_bytes()).hexdigest() for name in ("advisor_graph.py", "server.py", "concept_map.json")}}
    meta.update({"rerank_model": server.RERANK_MODEL, "minimum_rerank_score": args.min_rerank_score, "minimum_framework_rerank_score": args.min_framework_rerank_score})
    if args.stage == "before":
        for name in ("advisor_graph.py", "server.py", "concept_map.json"):
            shutil.copyfile(ROOT / "mcp" / name, output / ("before-" + name))
    else:
        before = json.loads((output / "before.json").read_text())
        assert meta["fixture_sha256"] == before["meta"]["fixture_sha256"], "Frozen labels changed"
        assert meta["chunks_sha256"] == before["meta"]["chunks_sha256"], "Chunking changed"
    rows = []
    report = {"meta": meta, "rows": rows, "provider_calls": 0, "complete": False}
    report_file.write_text(json.dumps(report, indent=2))
    index = AdvisorIndex(server.DATA_DIR, server.CHUNKS, server.EXCERPTS, server._index(), embed, server.EMBED_MODEL, server.FOOTER_ONLY_CHUNKS, server.RETRIEVAL_MIN_SIMILARITY, rerank=rerank, minimum_rerank_score=args.min_rerank_score, minimum_framework_rerank_score=args.min_framework_rerank_score)
    index.start()
    deadline = time.monotonic() + 180
    while not index.ready.is_set():
        if index.error or time.monotonic() > deadline:
            raise RuntimeError(f"Index not ready: {index.error}")
        await asyncio.sleep(0.1)
    server.GRAPH_INDEX = index
    try:
        for case in cases:
            context = {"situation": case["situation"], "decision": case["decision"], "confirmed_facts": [], "unknowns": [], "constraints": []}
            query = json.dumps(context, ensure_ascii=False)
            started = time.monotonic()
            result, data = await server.graph_search(query, 5, "excerpt", "mix")
            assert result["retrieval"]["actual_mode"] == "mix", result["retrieval"]
            assert all(len(hit["text"].split()) <= 60 for hit in result["results"])
            response = {"context_status": "caller_supplied_not_verified", "frameworks": selected_concepts(data), "concept_map_version": server.CONCEPT_MAP["version"], **result}
            if hasattr(server, "pack_decision_context"):
                response = server.pack_decision_context(response, index.rag.tokenizer)
            row = assess(case, response)
            row["seconds"] = round(time.monotonic() - started, 3)
            row["response_tokens"] = len(index.rag.tokenizer.encode(json.dumps(response, ensure_ascii=False)))
            row["entity_names"] = [item["entity_name"] for item in data.get("entities", [])]
            row["routing"] = data["routing"]
            if "framework_scores" in data:
                row["framework_scores"] = data["framework_scores"]
            row["book_scores"] = [{"page": int(hit["file_path"].split("=")[-1]), "score": hit["rerank_score"]} for hit in data.get("chunks", [])]
            rows.append(row)
            report["provider_calls"] = provider_calls
            report["rerank_calls"] = rerank_calls
            report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            if case["split"] == "development" or args.stage != "before":
                print(json.dumps(row, ensure_ascii=False), flush=True)
            else:
                print(json.dumps({"id": case["id"], "status": "recorded; held out until selection is fixed"}), flush=True)
        report["complete"] = True
        report["summary"] = {split: summarize([row for row in rows if row["split"] == split]) for split in ("development", "holdout")}
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"development": report["summary"]["development"], "provider_calls": provider_calls}), flush=True)
        if args.stage != "before":
            print(json.dumps({"holdout": report["summary"]["holdout"]}), flush=True)
            if not args.record_rerank and any(not row["passed"] for row in rows):
                raise SystemExit(1)
    finally:
        await index.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("before", "after", "revised", "rerank-record", "rerank"))
    parser.add_argument("--record-rerank", action="store_true", help="Authorize up to 39 new synthetic rerank requests; no automatic retries")
    parser.add_argument("--min-rerank-score", type=float, default=0.0, help="Set zero when recording scores, then calibrate on development only")
    parser.add_argument("--min-framework-rerank-score", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True, help="Private directory under ignored runtime data")
    asyncio.run(run(parser.parse_args()))
