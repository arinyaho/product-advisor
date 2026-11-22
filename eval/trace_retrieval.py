"""Trace one caller-supplied context without writing its text or query vectors."""

import asyncio
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

import server
from advisor_graph import AdvisorIndex, selected_concepts, source_pages


async def trace(context):
    query = json.dumps(context, ensure_ascii=False)
    memory = {}

    def embed(texts):
        missing = list(dict.fromkeys(text for text in texts if text not in memory))
        if missing:
            for text, vector in zip(missing, server._embed(missing), strict=True):
                memory[text] = vector
        return np.array([memory[text] for text in texts])

    index = AdvisorIndex(server.DATA_DIR, server.CHUNKS, server.EXCERPTS, server._index(), embed, server.EMBED_MODEL, server.FOOTER_ONLY_CHUNKS, server.RETRIEVAL_MIN_SIMILARITY, rerank=server._rerank, minimum_rerank_score=server.MIN_RERANK_SCORE)
    await index.initialize()
    if not index.ready.is_set():
        raise RuntimeError(index.error)
    try:
        results = []
        for mode in ("naive", "mix"):
            data = await index._query(query, mode, 5)
            report = {"mode": mode, "routing": data["routing"], "graph_entities": [entry["entity_name"] for entry in data.get("entities", [])], "frameworks": [entry["id"] for entry in selected_concepts(data)], "pages": source_pages(data)[:5], "footer_candidates": [entry["chunk_id"] for entry in data.get("chunks", []) if (int(entry["file_path"].split("=")[-1]), entry["content"]) in server.FOOTER_ONLY_CHUNKS]}
            results.append(report)
        print(json.dumps(results), flush=True)
        return results
    finally:
        await index.shutdown()
        index.loop.close()


if __name__ == "__main__":
    asyncio.run(trace(json.load(sys.stdin)))
