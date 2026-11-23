"""Offline integration checks using the real LightRAG engine and synthetic text."""

import asyncio
from collections import namedtuple
import hashlib
import io
import json
import logging
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np

from advisor_graph import AdvisorIndex, CONCEPT_MAP, compile_graph, selected_concepts, source_pages
from lightrag import QueryParam
from lightrag.utils import process_chunks_unified


def embeddings(texts):
    vectors = np.zeros((len(texts), 256), dtype=np.float32)
    for index, text in enumerate(texts):
        for term in re.findall(r"[a-z]+", text.lower()):
            bucket = int(hashlib.sha256(term.encode()).hexdigest()[:8], 16) % 256
            vectors[index, bucket] += 1
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1)


async def synthetic_rerank(query, documents, top_n=None):
    vectors = embeddings([query] + documents)
    scores = vectors[1:] @ vectors[0]
    return sorted([{"index": i, "relevance_score": max(0.0, float(score))} for i, score in enumerate(scores)], key=lambda row: row["relevance_score"], reverse=True)[:top_n]


Chunk = namedtuple("Chunk", "page text")


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.chunks = [Chunk(page, f"Source PDF page {page}: {concept['name']}. {concept['decision']}") for concept in CONCEPT_MAP["concepts"] for page in concept["pdf_pages"]]
        matrix = embeddings([chunk.text for chunk in self.chunks])
        self.index = AdvisorIndex(self.directory, self.chunks, [], (matrix, np.zeros((0, 256))), embeddings, "synthetic", rerank=synthetic_rerank)
        await self.index.initialize()
        self.assertTrue(self.index.ready.is_set(), self.index.error)

    async def asyncTearDown(self):
        await self.index.shutdown()
        self.index.loop.close()
        self.temporary.cleanup()

    async def test_naive_returns_source_identity(self):
        data = await self.index._query("Unbundling separate offers customers subset capabilities", "naive", 5)
        self.assertIn(94, source_pages(data))
        self.assertEqual(data.get("entities", []), [])

    async def test_mix_returns_framework_and_book_evidence(self):
        data = await self.index._query("Unbundling separate offers product configuration willingness to pay", "mix", 5)
        ids = {concept["id"] for concept in selected_concepts(data)}
        self.assertIn("unbundling", ids)
        self.assertTrue({94, 87, 60} & set(source_pages(data)))
        self.assertTrue(data.get("relationships"))

    async def test_query_and_customer_context_are_not_persisted(self):
        marker = "customer-marker-c7ca1f-purchase-rejection"
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        logger = logging.getLogger("lightrag")
        logger.addHandler(handler)
        try:
            await self.index._query(marker + " unbundling willingness to pay", "mix", 5)
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(marker, captured.getvalue())
        for file in self.directory.rglob("*"):
            if file.is_file():
                self.assertNotIn(marker.encode(), file.read_bytes(), str(file))

    async def test_book_embeddings_are_reused(self):
        for chunk in self.chunks:
            self.assertIn(chunk.text, self.index.vectors)
        self.assertLessEqual(self.index.new_embeddings, 100)
        graph = compile_graph(self.chunks)
        self.assertEqual([(chunk["content"], chunk["file_path"]) for chunk in graph["chunks"]], [(chunk.text, f"book.pdf#page={chunk.page}") for chunk in self.chunks])
        aliases = {chunk["source_id"] for chunk in graph["chunks"]}
        self.assertTrue(all(entity["source_id"] in aliases for entity in graph["entities"]))
        self.assertTrue(all(relation["source_id"] in aliases for relation in graph["relationships"]))

    async def test_official_rerank_failure_is_not_served_as_a_success(self):
        self.index.rerank = AsyncMock(side_effect=RuntimeError("provider unavailable"))
        with self.assertRaisesRegex(RuntimeError, "Rerank provider unavailable"):
            await self.index._query("Unbundling separate offers", "naive", 5)
        self.index.rerank.assert_awaited_once()

    async def test_all_concept_sources_exist_and_unknown_links_fail(self):
        concepts = json.loads(json.dumps(CONCEPT_MAP["concepts"]))
        concepts[0]["related"] = ["not-a-concept"]
        with self.assertRaisesRegex(ValueError, "Unknown related concept"):
            compile_graph(self.chunks, concepts)
        with self.assertRaisesRegex(ValueError, "No source chunk"):
            compile_graph([])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def index(self, concepts, rerank):
        index = object.__new__(AdvisorIndex)
        index.minimum_similarity = 0.5
        index.minimum_rerank_score = 0.5
        index.minimum_framework_rerank_score = 0.5
        index.rerank = rerank
        index.chunks, index.excluded_chunks, index.vectors = [], frozenset(), {}
        index.rag = SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda value: value.split()), aquery_data=AsyncMock(return_value={"data": {"entities": [{"entity_name": concept["name"]} for concept in concepts], "chunks": []}}))
        return index

    async def test_frameworks_are_selected_by_the_configured_reranker(self):
        concept = next(item for item in CONCEPT_MAP["concepts"] if item["id"] == "value_communication")
        rerank = AsyncMock(return_value=[{"index": 0, "relevance_score": 0.9}])
        data = await self.index([concept], rerank)._query("How should we communicate customer benefits?", "mix", 5)
        rerank.assert_awaited_once()
        self.assertEqual([item["id"] for item in selected_concepts(data)], ["value_communication"])

    async def test_context_supplies_referents_missing_from_the_decision(self):
        context = {"situation": "Four ways innovations fail have been described", "decision": "How do these failure types differ?"}
        async def rerank(query, documents, top_n):
            self.assertIn(context["situation"], query)
            self.assertTrue(query.startswith("Decision to answer: " + context["decision"]))
            return [{"index": 0, "relevance_score": 0.9}]
        data = await self.index([CONCEPT_MAP["concepts"][0]], rerank)._query(json.dumps(context), "mix", 5)
        self.assertEqual([item["id"] for item in selected_concepts(data)], ["failure_patterns"])

    async def test_explicit_decision_survives_price_heavy_background(self):
        context = {"situation": "Pricing and unbundling dominate the background", "decision": "Review how the proposal communicates customer benefits", "unknowns": ["WTP", "purchase reason"]}
        async def rerank(query, documents, top_n):
            self.assertTrue(query.startswith("Decision to answer: " + context["decision"]))
            self.assertIn(context["situation"], query)
            self.assertEqual(top_n, 12)
            return [{"index": i, "relevance_score": .9 if text.startswith("Customer benefit statements") else .01} for i, text in enumerate(documents)]
        index = self.index(CONCEPT_MAP["concepts"], rerank)
        data = await index._query(json.dumps(context), "mix", 5)
        param = index.rag.aquery_data.call_args.kwargs["param"]
        self.assertGreater(param.top_k, 6)
        self.assertGreaterEqual(param.chunk_top_k, 20)
        self.assertEqual(param.max_total_tokens, 7000)
        self.assertIn("value_communication", {concept["id"] for concept in selected_concepts(data)})
        self.assertEqual(selected_concepts(data)[0]["id"], "value_communication")


class RelevanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_similarity_floor_allows_strong_evidence_and_an_empty_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            chunks = [Chunk(page, f"Source PDF page {page}: {concept['name']}. {concept['decision']}") for concept in CONCEPT_MAP["concepts"] for page in concept["pdf_pages"]]
            matrix = embeddings([chunk.text for chunk in chunks])
            index = AdvisorIndex(Path(temporary), chunks, [], (matrix, np.zeros((0, 256))), embeddings, "synthetic", minimum_similarity=0.5, rerank=synthetic_rerank)
            await index.initialize()
            self.assertTrue(index.ready.is_set(), index.error)
            try:
                positive = await index._query(chunks[0].text, "naive", 5)
                self.assertIn(chunks[0].page, source_pages(positive))
                unrelated = await index._query("mermaid rainbow starlight jellyfish", "naive", 5)
                self.assertEqual(unrelated.get("chunks", []), [])
                self.assertEqual(unrelated.get("entities", []), [])
                self.assertEqual(unrelated["routing"]["minimum_similarity"], 0.5)
            finally:
                await index.shutdown()
                index.loop.close()


class FrameworkSelectionTests(unittest.TestCase):
    def data(self, scores):
        return {"entities": [{"entity_name": concept["name"]} for concept in CONCEPT_MAP["concepts"]],
                "framework_scores": scores, "routing": {"minimum_framework_rerank_score": 0.5}}

    def test_graph_neighbors_do_not_automatically_become_recommendations(self):
        scores = {concept["id"]: 0.02 for concept in CONCEPT_MAP["concepts"]}
        scores["value_communication"] = 0.76
        self.assertEqual([item["id"] for item in selected_concepts(self.data(scores))], ["value_communication"])

    def test_more_than_three_close_relevant_candidates_are_preserved(self):
        scores = {concept["id"]: 0.3 for concept in CONCEPT_MAP["concepts"]}
        expected = ["unbundling", "configuration", "willingness_to_pay", "living_business_case"]
        scores.update(zip(expected, [0.8, 0.78, 0.76, 0.74], strict=True))
        self.assertEqual([item["id"] for item in selected_concepts(self.data(scores))], expected)

    def test_rerank_floor_excludes_unwanted_candidates(self):
        scores = {concept["id"]: 0.3 for concept in CONCEPT_MAP["concepts"]}
        scores.update({"unbundling": 0.675, "price_integrity": 0.665, "behavioral_pricing": 0.484, "configuration": 0.467, "willingness_to_pay": 0.457})
        self.assertEqual([item["id"] for item in selected_concepts(self.data(scores))], ["unbundling", "price_integrity"])

    def test_weak_top_candidate_does_not_force_a_recommendation(self):
        scores = {concept["id"]: 0.4 for concept in CONCEPT_MAP["concepts"]}
        self.assertEqual(selected_concepts(self.data(scores)), [])

    def test_selected_framework_has_a_source_before_background_passages(self):
        chunks = [Chunk(143, "Customer benefit evidence"), Chunk(60, "Buyer budget evidence"), Chunk(103, "Repeated footer 87")]
        index = object.__new__(AdvisorIndex)
        index.chunks = chunks
        index.excluded_chunks = {(103, chunks[2].text)}
        index.minimum_similarity = 0.5
        index.vectors = {chunks[0].text: np.array([1.0, 0.0]), chunks[1].text: np.array([0.1, 1.0]), chunks[2].text: np.array([1.0, 0.0])}
        data = self.data({concept["id"]: 0.8 if concept["id"] == "value_communication" else 0.3 for concept in CONCEPT_MAP["concepts"]})
        data["chunks"] = [{"file_path": "book.pdf#page=60", "content": chunks[1].text}]
        concept = next(item for item in CONCEPT_MAP["concepts"] if item["id"] == "value_communication")
        candidates = index._source_chunks(data, [concept])
        async def rerank(query, documents, top_n):
            return [{"index": i, "relevance_score": .9 if "Customer benefit" in text else .01} for i, text in enumerate(documents)]
        result = asyncio.run(process_chunks_unified("customer benefit", candidates, QueryParam(enable_rerank=True), {"rerank_model_func": rerank, "min_rerank_score": .5}))
        self.assertEqual([(item["file_path"], item["content"]) for item in result], [("book.pdf#page=143", "Customer benefit evidence")])
        self.assertIs(index.chunks, chunks)


if __name__ == "__main__":
    unittest.main()
