"""Run frozen source-identity evaluations with the same evidence budget in both modes."""

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

import server
from book_library import atomic_json, digest
from library_index import EVIDENCE_TOKENS, LibraryIndex, Provider, RETRIEVAL_VERSION


FROZEN_CATALOG_SHA256 = "317fc08f053815606ce73a60f805d39dcb6de04015dae440d73ce27e0a346d5e"


async def evaluate(directory, output, fixture_path=Path(__file__).parent / "three_book_cases.json"):
    fixture = json.loads(fixture_path.read_text())
    fixture_sha256 = digest(fixture_path.read_bytes())
    index = LibraryIndex(directory, server._openrouter)
    previous = json.loads(output.read_text()) if output.exists() else {}
    if previous:
        assert previous["version"] == index.version
        assert previous.get("retrieval_version") == RETRIEVAL_VERSION, "Use a separate output file for a changed retrieval configuration"
        assert previous.get("fixture_sha256") == fixture_sha256, "Evaluation questions or expected sources changed"
    retained = [row for row in previous.get("rows", []) if row["retrieval"].get("status") == "ready"]
    failed_attempts = previous.get("failed_attempts", []) + [row for row in previous.get("rows", []) if row["retrieval"].get("status") != "ready"]
    provider = Provider(server._openrouter, output.parent / (output.stem + "-receipts"), budget=.20, max_calls=90, retry_busy=True)
    # Account for successful calls made before durable receipt capture was introduced.
    provider.budget -= max(0, sum(row["cost_usd"] for row in retained + failed_attempts) - provider.cost)
    provider.max_calls -= max(0, sum(row["calls"] for row in retained + failed_attempts) - provider.calls)
    index.provider = provider
    # The fixed corpus stays byte-identical when a query-only reranker profile changes.
    assert digest((directory / "catalog.json").read_bytes()) == FROZEN_CATALOG_SHA256
    for chunk in index.catalog.chunks.values():
        index.catalog.verify_chunk(chunk)
    index.start()
    try:
        for _ in range(300):
            if index.ready.is_set() or index.error:
                break
            await asyncio.sleep(.1)
        assert index.ready.is_set(), index.error
        structural = []
        for book_id, book in index.catalog.books.items():
            section = next(row for row in book["sections"] if any(chunk["section_id"] == row["id"] and len(chunk["text"].split()) > 60 for chunk in book["chunks"]))
            read = index.catalog.read(section["id"], book_id, None, "excerpt")
            assert len(read["text"].split()) <= 60
            assert read["continuation"]
            following = index.catalog.read(scope="excerpt", identifier=section["id"], book_id=book_id, start_page=read["continuation"]["start_page"])
            assert following["text"] and following["locations"][0] != read["locations"][0]
            structural.append({"book_id": book_id, "section_id": section["id"], "first": read["locations"], "following": following["locations"], "passed": True})
        assert index.catalog.section(27)[1]["title"] == "The Information You Need from Those Early Pricing Talks"
        rows = retained
        for case in fixture["cases"]:
            for mode in ("mix", "naive"):
                if any(row["case"] == case["id"] and row["mode"] == mode for row in retained):
                    continue
                before = len(index.provider.events)
                started = time.monotonic()
                result = await index.search(case["query"], 5, case.get("book_id"), mode, "full")
                elapsed = time.monotonic() - started
                hits = result["results"]
                valid = result["retrieval"].get("actual_mode") == mode and result["retrieval"].get("status") == "ready"
                for hit in hits:
                    chunk = index.catalog.chunks[hit["chunk_id"]]
                    assert hit["text"] == chunk["text"] and hit["location"] == chunk["location"]
                    assert hit["book_id"] == chunk["book_id"]
                    assert not case.get("book_id") or hit["book_id"] == case["book_id"]
                tokenizer = index.engines[case.get("book_id") or "all"].tokenizer
                assert len(tokenizer.encode(json.dumps(result, ensure_ascii=False))) <= EVIDENCE_TOKENS
                identities = [hit["chunk_id"] for hit in hits]
                expected = case["expected_chunk_ids"]
                matched = sorted(set(expected) & set(identities))
                passed = valid and (bool(matched) if expected else not hits)
                events = index.provider.events[before:]
                rows.append({"case": case["id"], "mode": mode, "passed": passed, "matched_expected": matched, "returned_ids": identities, "seconds": round(elapsed, 4), "calls": len(events), "cost_usd": sum(event["charged_or_estimated_usd"] for event in events), "provider_receipts": events, "retrieval": result["retrieval"]})
                atomic_json(output, {"version": index.version, "retrieval_version": RETRIEVAL_VERSION, "fixture_sha256": fixture_sha256, "budget": EVIDENCE_TOKENS, "structural": structural, "rows": rows, "failed_attempts": failed_attempts})
                print(json.dumps(rows[-1]), flush=True)
                if result["retrieval"].get("status") != "ready":
                    raise RuntimeError("Runtime failure; partial evaluation retained and paid calls stopped")
        summary = {}
        for mode in ("mix", "naive"):
            subset = [row for row in rows if row["mode"] == mode]
            receipts = [event for row in subset for event in row["provider_receipts"]]
            confirmed = sum(event["charged_or_estimated_usd"] for event in receipts if event["status"] == "completed")
            unknown = sum(event["charged_or_estimated_usd"] for event in receipts if event["status"] != "completed")
            summary[mode] = {"passed": sum(row["passed"] for row in subset), "total": len(subset), "median_seconds": statistics.median(row["seconds"] for row in subset), "cost_usd": confirmed, "unknown_charge_reservation_usd": unknown, "calls": sum(row["calls"] for row in subset)}
        atomic_json(output, {"version": index.version, "retrieval_version": RETRIEVAL_VERSION, "fixture_sha256": fixture_sha256, "budget": EVIDENCE_TOKENS, "structural": structural, "rows": rows, "summary": summary, "failed_attempts": failed_attempts, "provider_cost_or_reservation_usd": provider.cost, "comparison_cost_policy": "Both modes receive the same unchanged original question; no query translation or answer-generation call is made. Confirmed charges and unknown-charge reservations are reported separately."})
        print(json.dumps(summary), flush=True)
        return all(row["passed"] for row in rows)
    finally:
        await index.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "three_book_cases.json")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(args.directory, args.output, args.cases)) else 1)
