"""Compare eight bounded book-retrieval requests with real cached book vectors."""

import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

import server
from advisor_graph import AdvisorIndex, selected_concepts


CASES = [
    {"id": "unbundling", "query": "고객은 기존 AI를 계속 쓰고 자료 검색 기능만 원한다. 제품을 분리해서 판매할지 어떤 조건을 확인해야 하나?", "pdf_range": [79, 94], "framework": "unbundling"},
    {"id": "wtp", "query": "A pilot was used but the customer rejected the purchase. The rejection reason and willingness to pay are unknown. What should we validate before lowering the price?", "pdf_range": [55, 68], "framework": "willingness_to_pay"},
    {"id": "benefit", "query": "고객 제안서가 기술과 기능 설명뿐이다. 고객이 얻는 효익을 어떻게 설명하고 검증할까?", "pdf_range": [137, 150], "framework": "value_communication"},
    {"id": "charging", "query": "인당 구독료와 사용량 기반 과금 중 무엇을 선택해야 하나? 고객 수용성과 운영 비용을 확인하고 싶다.", "pdf_range": [95, 112], "framework": "monetization_model"},
]


async def main():
    index = AdvisorIndex(server.DATA_DIR, server.CHUNKS, server.EXCERPTS, server._index(), server._embed, server.EMBED_MODEL, server.FOOTER_ONLY_CHUNKS, server.RETRIEVAL_MIN_SIMILARITY, rerank=server._rerank, minimum_rerank_score=server.MIN_RERANK_SCORE)
    index.start()
    deadline = time.monotonic() + 240
    while not index.ready.is_set():
        if index.error:
            raise RuntimeError(f"LightRAG initialization failed: {index.error}")
        if time.monotonic() > deadline:
            raise TimeoutError("LightRAG initialization exceeded 240 seconds")
        await asyncio.sleep(0.25)
    server.GRAPH_INDEX = index
    results = []
    for case in CASES:
        for mode in ("naive", "mix"):
            started = time.monotonic()
            response, data = await server.graph_search(case["query"], 5, "excerpt", mode)
            pages = [item["page"] for item in response["results"]]
            frameworks = [item["id"] for item in selected_concepts(data)]
            assert response["retrieval"]["actual_mode"] == mode
            assert pages and all(len(item["text"].split()) <= server.EXCERPT_WORDS for item in response["results"])
            row = {"case": case["id"], "mode": mode, "pages": pages, "frameworks": frameworks, "book_hit": any(case["pdf_range"][0] <= page <= case["pdf_range"][1] for page in pages), "framework_hit": case["framework"] in frameworks, "seconds": round(time.monotonic() - started, 2)}
            results.append(row)
            print(json.dumps(row), flush=True)
    output = server.DATA_DIR / "decision-context-evaluation.json"
    output.write_text(json.dumps({"cases": CASES, "results": results, "new_index_embeddings": index.new_embeddings}, ensure_ascii=False, indent=2))
    await index.close()
    print(f"Saved evaluation to {output}")


if __name__ == "__main__":
    asyncio.run(main())
