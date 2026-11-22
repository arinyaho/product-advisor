"""MCP boundary checks with a real PDF, cached vectors, and no provider calls."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import server
from starlette.testclient import TestClient


class DecisionContextTests(unittest.TestCase):
    def setUp(self):
        self.graph = SimpleNamespace(ready=SimpleNamespace(is_set=lambda: True), query=AsyncMock(return_value={
            "entities": [{"entity_name": "Unbundling and separate offers"}],
            "chunks": [{"file_path": "book.pdf#page=94", "content": server.PAGES[94]}],
            "relationships": [],
        }))
        self.patches = [patch.object(server, "GRAPH_INDEX", self.graph), patch.object(server, "_bearer", side_effect=lambda headers: {"Bearer excerpt-test": "excerpt", "Bearer full-test": "full"}.get(headers.get("authorization")))]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(server.app())
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()

    def rpc(self, method, params, key="excerpt-test", session=None):
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Authorization": "Bearer " + key}
        if session:
            headers["mcp-session-id"] = session
        return self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    def tool(self, name, arguments, key="excerpt-test"):
        init = self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "decision-test", "version": "1"}}, key)
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, key, init.headers.get("mcp-session-id"))
        response.raise_for_status()
        return response.text

    def test_unknown_key_is_rejected_before_retrieval(self):
        response = self.rpc("tools/list", {}, key="invalid")
        self.assertEqual(response.status_code, 401)
        self.graph.query.assert_not_awaited()

    def test_excerpt_scope_caps_book_text_in_new_tool(self):
        response = self.tool("retrieve_decision_context", {"situation": "Pilot completed but purchase was rejected", "decision": "Should we unbundle the offer?", "confirmed_facts": ["Purchase was rejected"], "unknowns": ["Rejection reason"], "constraints": ["Do not infer a price objection"]})
        self.assertIn("Unbundling and separate offers", response)
        self.assertIn("curated_application", response)
        self.assertNotIn(server.PAGES[94], response)
        query, mode, count = self.graph.query.call_args.args
        self.assertEqual(json.loads(query)["unknowns"], ["Rejection reason"])
        self.assertEqual(json.loads(query)["constraints"], ["Do not infer a price objection"])
        self.assertEqual((mode, count), ("mix", 5))
        payload = next((line[6:] for line in response.splitlines() if line.startswith("data: ")), response)
        envelope = json.loads(payload)
        result = json.loads(envelope["result"]["content"][0]["text"])
        self.assertEqual([item["page"] for item in result["results"]], [94])
        self.assertLessEqual(len(result["results"][0]["text"].split()), server.EXCERPT_WORDS)
        self.assertEqual([item["id"] for item in result["frameworks"]], ["unbundling"])

    def test_naive_search_is_selected_explicitly(self):
        response = self.tool("search_book", {"query": "unbundling", "mode": "naive", "k": 2}, key="full-test")
        self.assertIn("naive", response)
        self.assertEqual(self.graph.query.call_args.args, ("unbundling", "naive", 2))

    def test_naive_preserves_the_matched_section_within_a_page(self):
        titles = {section["id"]: section["title"] for section in server.SECTIONS}
        chunk = next(chunk for chunk in server.CHUNKS if titles[chunk.section] != server._section_of(chunk.page))
        self.graph.query.return_value = {"chunks": [{"file_path": f"book.pdf#page={chunk.page}", "content": chunk.text}]}
        response = self.tool("search_book", {"query": "pricing", "mode": "naive"}, key="full-test")
        payload = next((line[6:] for line in response.splitlines() if line.startswith("data: ")), response)
        result = json.loads(json.loads(payload)["result"]["content"][0]["text"])
        self.assertEqual([(item["page"], item["section"]) for item in result["results"]], [(chunk.page, titles[chunk.section])])

    def test_invalid_mode_and_empty_situation_are_tool_errors(self):
        self.assertIn("isError", self.tool("search_book", {"query": "unbundling", "mode": "invalid"}))
        self.assertIn("isError", self.tool("retrieve_decision_context", {"situation": "", "decision": "Choose a price"}))
        self.graph.query.assert_not_awaited()

    def test_building_index_is_reported_as_legacy_fallback(self):
        self.graph.ready.is_set = lambda: False
        self.graph.error = None
        with patch.object(server, "search", return_value={"semantic": False, "results": []}):
            response = self.tool("search_book", {"query": "unbundling"})
        self.assertIn("legacy", response)
        self.graph.query.assert_not_awaited()

    def test_provider_failure_uses_keyword_fallback_without_replaying_embedding(self):
        self.graph.query.side_effect = OSError("provider unavailable")
        with patch.object(server, "search", return_value={"semantic": False, "results": []}) as fallback:
            response = self.tool("search_book", {"query": "unbundling"})
        fallback.assert_called_once_with("unbundling", 5, "excerpt", False)
        self.assertIn("legacy", response)
        self.assertIn("unavailable", response)

    def test_footer_only_candidate_is_excluded_before_page_selection(self):
        body = next(chunk for chunk in server.CHUNKS if chunk.page == 143 and len(chunk.text.split()) > 80)
        self.graph.query.return_value = {"chunks": [
            {"file_path": "book.pdf#page=103", "content": "Go beyond the Price Point 87"},
            {"file_path": "book.pdf#page=143", "content": body.text},
        ]}
        response = self.tool("search_book", {"query": "proposal benefits", "mode": "naive"})
        payload = next((line[6:] for line in response.splitlines() if line.startswith("data: ")), response)
        result = json.loads(json.loads(payload)["result"]["content"][0]["text"])
        self.assertEqual([item["page"] for item in result["results"]], [143])

    def test_short_substantive_passage_is_not_filtered_as_a_footer(self):
        content = "Recommendation: Identify the price thresholds for your products and stay on the cliff. 144 Reference A"
        self.graph.query.return_value = {"chunks": [{"file_path": "book.pdf#page=160", "content": content}]}
        response = self.tool("search_book", {"query": "price thresholds", "mode": "naive"})
        payload = next((line[6:] for line in response.splitlines() if line.startswith("data: ")), response)
        result = json.loads(json.loads(payload)["result"]["content"][0]["text"])
        self.assertEqual([item["page"] for item in result["results"]], [160])
        self.assertEqual(result["results"][0]["text"], content)

    def test_footer_filter_preserves_original_book_chunks_and_embeddings(self):
        original_chunks, original_excerpts = server._structure_chunks()
        self.assertEqual(server.CHUNKS, original_chunks)
        self.assertEqual(server.EXCERPTS, original_excerpts)
        self.assertIn((103, "Go beyond the Price Point 87"), server.FOOTER_ONLY_CHUNKS)
        self.assertNotIn((143, "The Three Steps to Create Great Value Communications"), server.FOOTER_ONLY_CHUNKS)
        from advisor_graph import compile_graph
        graph = compile_graph(server.CHUNKS, excluded_chunks=server.FOOTER_ONLY_CHUNKS)
        self.assertEqual([(row["content"], row["file_path"]) for row in graph["chunks"]], [(chunk.text, f"book.pdf#page={chunk.page}") for chunk in original_chunks if (chunk.page, chunk.text) not in server.FOOTER_ONLY_CHUNKS])
        self.assertNotIn("Go beyond the Price Point 87", {row["content"] for row in graph["chunks"]})

    def test_final_response_budget_includes_framework_descriptions(self):
        tokenizer = SimpleNamespace(encode=lambda text: list(text))
        response = {"frameworks": [{"id": "first", "description": "x" * 100}, {"id": "second", "description": "x" * 1000}], "results": [{"page": 143, "text": "Customer benefit evidence"}], "retrieval": {"actual_mode": "mix"}}
        packed = server.pack_decision_context(response, tokenizer, max_tokens=400)
        self.assertEqual([item["id"] for item in packed["frameworks"]], ["first"])
        self.assertEqual([item["page"] for item in packed["results"]], [143])
        self.assertLessEqual(len(tokenizer.encode(json.dumps(packed, ensure_ascii=False))), 400)
        self.assertEqual([item["id"] for item in response["frameworks"]], ["first", "second"])

    def test_final_budget_rejects_metadata_that_cannot_fit(self):
        tokenizer = SimpleNamespace(encode=lambda text: list(text))
        with self.assertRaisesRegex(ValueError, "metadata exceeds"):
            server.pack_decision_context({"frameworks": [], "results": [], "metadata": "x" * 100}, tokenizer, max_tokens=50)


class RerankAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_endpoint_model_and_no_retry(self):
        with patch.object(server, "_openrouter", return_value={"results": [{"index": 1, "relevance_score": .8}, {"index": 0, "relevance_score": .1}]}) as request:
            results = await server._rerank("benefits", ["weak", "strong"], top_n=2)
        request.assert_called_once_with("rerank", {"model": server.RERANK_MODEL, "query": "benefits", "documents": ["weak", "strong"], "top_n": 2}, attempts=1, timeout=30)
        self.assertEqual([item["index"] for item in results], [1, 0])

    async def test_invalid_or_incomplete_results_are_rejected(self):
        for results in ([], [{"index": 2, "relevance_score": .8}], [{"index": True, "relevance_score": .8}], [{"index": 0, "relevance_score": float("nan")}], [{"index": 0, "relevance_score": 1.2}], [{"index": 0, "relevance_score": .8}, {"index": 0, "relevance_score": .7}]):
            with self.subTest(results=results), patch.object(server, "_openrouter", return_value={"results": results}):
                with self.assertRaisesRegex(RuntimeError, "ValueError"):
                    await server._rerank("benefits", ["one", "two"])

    async def test_failure_does_not_expose_provider_body_or_replay_request(self):
        with patch.object(server, "_openrouter", side_effect=RuntimeError("private-customer-marker")) as request:
            with self.assertRaises(RuntimeError) as caught:
                await server._rerank("benefits", ["one"])
        self.assertNotIn("private-customer-marker", str(caught.exception))
        self.assertEqual(request.call_count, 1)

    async def test_empty_candidates_make_no_request(self):
        with patch.object(server, "_openrouter") as request:
            self.assertEqual(await server._rerank("benefits", []), [])
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
