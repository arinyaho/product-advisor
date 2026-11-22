"""LightRAG retrieval over book chunks and a reviewed, source-linked concept map."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import unicodedata

import numpy as np
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc, process_chunks_unified, sanitize_text_for_encoding


CONCEPT_MAP = json.loads(Path(__file__).with_name("concept_map.json").read_text())
MAX_CONTEXT_TOKENS = 7000


def framework_text(concept):
    return concept["name"] + "\n" + json.dumps({key: value for key, value in concept.items() if key != "related"}, ensure_ascii=False)


def normalized_text(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def footer_only_chunks(document, chunks):
    margins = []
    for page in document:
        for block in page.get_text("blocks"):
            if not (block[3] <= page.rect.height * 0.12 or block[1] >= page.rect.height * 0.9):
                continue
            lines = [line.strip() for line in unicodedata.normalize("NFKC", block[4]).splitlines() if line.strip()]
            folios = [line for line in lines if re.fullmatch(r"\d+|[ivxlcdm]+", line, re.IGNORECASE)]
            if folios:
                signature = " ".join(line for line in lines if line not in folios).casefold()
                margins.append((page.number + 1, normalized_text(block[4]), signature))
    repetitions = {}
    for page, _, signature in margins:
        repetitions.setdefault(signature, set()).add(page)
    repeated = {(page, text) for page, text, signature in margins if len(repetitions[signature]) >= 2}
    return frozenset((chunk.page, chunk.text) for chunk in chunks if (chunk.page, normalized_text(chunk.text)) in repeated)


def compile_graph(chunks, concepts=CONCEPT_MAP["concepts"], excluded_chunks=()):
    excluded = set(excluded_chunks)
    graph = {"chunks": [], "entities": [], "relationships": []}
    names = {concept["id"]: concept["name"] for concept in concepts}
    if len(names) != len(concepts):
        raise ValueError("Concept ids must be unique")
    for index, chunk in enumerate(chunks):
        if (chunk.page, chunk.text) in excluded:
            continue
        graph["chunks"].append({"content": chunk.text, "source_id": f"book:{index}", "file_path": f"book.pdf#page={chunk.page}", "chunk_order_index": index})
    for concept in concepts:
        candidates = [(index, chunk) for index, chunk in enumerate(chunks) if chunk.page in concept["pdf_pages"] and (chunk.page, chunk.text) not in excluded]
        if not candidates:
            raise ValueError(f"No source chunk for concept {concept['id']}")
        index, chunk = candidates[0]
        source = {"source_id": f"book:{index}", "file_path": f"book.pdf#page={chunk.page}"}
        description = framework_text(concept).split("\n", 1)[1]
        decision = f"Decision: {concept['decision']}"
        graph["entities"].extend([
            {"entity_name": concept["name"], "entity_type": "FRAMEWORK", "description": description, **source},
            {"entity_name": decision, "entity_type": "DECISION", "description": concept["applies_when"], **source},
        ])
        graph["relationships"].append({"src_id": concept["name"], "tgt_id": decision, "description": concept["applies_when"], "keywords": "application conditions decision", **source})
        for related in concept["related"]:
            if related not in names:
                raise ValueError(f"Unknown related concept: {related}")
            graph["relationships"].append({"src_id": concept["name"], "tgt_id": names[related], "description": f"Curated application link: consider {names[related]} when applying {concept['name']}. This is not a causal claim about a customer.", "keywords": "related decision framework", **source})
    return graph


def selected_concepts(data, concepts=CONCEPT_MAP["concepts"]):
    positions = {entity["entity_name"]: index for index, entity in enumerate(data.get("entities", []))}
    names = set(positions)
    selected = [{**concept, "provenance": CONCEPT_MAP["provenance"]} for concept in concepts if concept["name"] in names or f"Decision: {concept['decision']}" in names]
    if "framework_scores" in data:
        scores = data["framework_scores"]
        floor = data["routing"]["minimum_framework_rerank_score"]
        selected = [concept for concept in selected if scores.get(concept["id"], -1) >= floor]
        selected.sort(key=lambda concept: scores[concept["id"]], reverse=True)
        return selected
    return sorted(selected, key=lambda concept: min(positions.get(concept["name"], len(positions)), positions.get(f"Decision: {concept['decision']}", len(positions))))


def source_pages(data):
    pages = []
    for chunk in data.get("chunks", []):
        match = re.fullmatch(r"book\.pdf#page=(\d+)", chunk.get("file_path", ""))
        if match and int(match[1]) not in pages:
            pages.append(int(match[1]))
    return pages


class AdvisorIndex:
    def __init__(self, data_dir, chunks, excerpts, vectors, embed, model, excluded_chunks=(), minimum_similarity=0.2, *, rerank=None, minimum_rerank_score=0.15, minimum_framework_rerank_score=0.2):
        self.chunks = chunks
        self.excluded_chunks = frozenset(excluded_chunks)
        self.embed = embed
        self.model = model
        if not 0 <= minimum_similarity <= 1:
            raise ValueError("minimum_similarity must be between 0 and 1")
        self.minimum_similarity = minimum_similarity
        if not 0 <= minimum_rerank_score <= 1 or not 0 <= minimum_framework_rerank_score <= 1:
            raise ValueError("Rerank score floors must be between 0 and 1")
        self.rerank = rerank
        self.minimum_rerank_score = minimum_rerank_score
        self.minimum_framework_rerank_score = minimum_framework_rerank_score
        self.vectors = {sanitize_text_for_encoding(chunk.text): vector for items, matrix in zip((chunks, excerpts), vectors, strict=True) for chunk, vector in zip(items, matrix, strict=True)}
        self.dimension = vectors[0].shape[1]
        digest = hashlib.sha256((model + "1.5.7" + json.dumps(CONCEPT_MAP, sort_keys=True) + json.dumps(sorted(self.excluded_chunks)) + "\0".join(chunk.text for chunk in chunks)).encode()).hexdigest()[:16]
        self.directory = Path(data_dir) / f"lightrag-{digest}"
        self.extra_file = Path(data_dir) / f"concept-vectors-{model.replace('/', '_')}.json"
        self.extra_vectors = json.loads(self.extra_file.read_text()) if self.extra_file.exists() else {}
        self.new_embeddings = 0
        self.ready = threading.Event()
        self.error = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="advisor-lightrag")

    def start(self):
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.initialize())
        self.loop.run_forever()

    async def _embeddings(self, texts):
        if self.ready.is_set():
            return await self._query_embedding(texts)
        return await self._cached_embeddings(texts)

    async def _cached_embeddings(self, texts):
        async with self.embedding_lock:
            missing = list(dict.fromkeys(text for text in texts if text not in self.vectors and hashlib.sha256(text.encode()).hexdigest() not in self.extra_vectors))
            if missing:
                if self.new_embeddings + len(missing) > 150:
                    raise RuntimeError("Initial concept embedding limit exceeded (150 texts); existing book vectors must be reused")
                self.new_embeddings += len(missing)
                matrix = await asyncio.to_thread(self.embed, missing)
                for text, vector in zip(missing, matrix, strict=True):
                    self.extra_vectors[hashlib.sha256(text.encode()).hexdigest()] = vector.tolist()
                temporary = self.extra_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.extra_vectors))
                temporary.replace(self.extra_file)
            return np.array([self.vectors[text] if text in self.vectors else self.extra_vectors[hashlib.sha256(text.encode()).hexdigest()] for text in texts], dtype=np.float32)

    async def _query_embedding(self, texts):
        return await asyncio.to_thread(self.embed, texts)

    async def _no_generation(self, *args, **kwargs):
        raise RuntimeError("Advisor retrieval does not generate answers or extract customer facts")

    async def initialize(self):
        self.embedding_lock = asyncio.Lock()

        async def embed_batch(texts):
            return await self._embeddings(texts)

        async def no_generation(*args, **kwargs):
            return await self._no_generation(*args, **kwargs)

        try:
            self.rag = LightRAG(
                working_dir=str(self.directory),
                embedding_func=EmbeddingFunc(embedding_dim=self.dimension, max_token_size=8192, model_name=self.model, func=embed_batch),
                llm_model_func=no_generation,
                embedding_batch_num=64,
                embedding_func_max_async=1,
                default_embedding_timeout=120,
                cosine_better_than_threshold=self.minimum_similarity,
                enable_llm_cache=False,
            )
            await self.rag.initialize_storages()
            logging.getLogger("lightrag").setLevel(logging.WARNING)
            marker = self.directory / "advisor-ready.json"
            if not marker.exists():
                graph = compile_graph(self.chunks, excluded_chunks=self.excluded_chunks)
                await self.rag.ainsert_custom_kg(graph, full_doc_id="book-a")
                excluded_count = sum((chunk.page, chunk.text) in self.excluded_chunks for chunk in self.chunks)
                marker.write_text(json.dumps({"concept_map_version": CONCEPT_MAP["version"], "chunks": len(graph["chunks"]), "excluded_footer_chunks": excluded_count}))
            self.ready.set()
            print("LightRAG index ready", flush=True)
        except Exception as error:
            self.error = type(error).__name__
            logging.getLogger(__name__).exception("LightRAG initialization failed")

    async def query(self, query, mode, k):
        if mode not in ("naive", "mix"):
            raise ValueError("mode must be naive or mix")
        if not self.ready.is_set():
            raise RuntimeError(f"LightRAG is not ready ({self.error or 'building'})")
        future = asyncio.run_coroutine_threadsafe(self._query(query, mode, k), self.loop)
        return await asyncio.wrap_future(future)

    async def shutdown(self):
        await self.rag.embedding_func.func.shutdown()
        await self.rag.finalize_storages()

    async def close(self):
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.shutdown(), self.loop))
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join, 5)
        if not self.thread.is_alive():
            self.loop.close()

    async def _query(self, query, mode, k):
        try:
            context = json.loads(query)
        except ValueError:
            context = None
        decision = context.get("decision") if isinstance(context, dict) else None
        focus = decision.strip() if isinstance(decision, str) and decision.strip() else query
        keywords = [focus] if mode == "mix" else []
        parameters = QueryParam(mode=mode, max_total_tokens=MAX_CONTEXT_TOKENS, max_entity_tokens=2500, max_relation_tokens=2000, ll_keywords=keywords, hl_keywords=keywords, enable_rerank=False)
        result = await self.rag.aquery_data(query, param=parameters)
        data = result.get("data", {})
        data["routing"] = {"focus": "decision" if focus != query else "query", "candidate_top_k": parameters.top_k, "chunk_candidate_top_k": parameters.chunk_top_k, "minimum_similarity": self.minimum_similarity, "max_total_tokens": parameters.max_total_tokens, "max_entity_tokens": parameters.max_entity_tokens, "max_relation_tokens": parameters.max_relation_tokens}
        if self.rerank is None:
            raise RuntimeError("Reranker is not configured")
        rerank_query = f"Decision to answer: {focus}\nContext: {query}" if decision else query
        candidates = selected_concepts(data) if mode == "mix" else []
        chunks = self._source_chunks(data, candidates)
        data["routing"]["minimum_rerank_score"] = self.minimum_rerank_score
        data["routing"]["minimum_framework_rerank_score"] = self.minimum_framework_rerank_score
        if candidates:
            scores = await self.rerank(query=rerank_query, documents=[framework_text(concept) for concept in candidates], top_n=len(candidates))
            data["framework_scores"] = {candidates[item["index"]]["id"]: item["relevance_score"] for item in scores}
        failures = []

        async def guarded_rerank(**arguments):
            try:
                return await self.rerank(**arguments)
            except Exception:
                failures.append(True)
                raise RuntimeError("Rerank provider unavailable") from None

        # Rerank once after adding curated sources; use LightRAG's official chunk pipeline.
        parameters.enable_rerank = True
        data["chunks"] = await process_chunks_unified(rerank_query, chunks, parameters, {"rerank_model_func": guarded_rerank, "min_rerank_score": self.minimum_rerank_score, "tokenizer": self.rag.tokenizer}, chunk_token_limit=MAX_CONTEXT_TOKENS)
        if failures:
            raise RuntimeError("Rerank provider unavailable")
        data["routing"]["rerank"] = "completed"
        return data

    def _source_chunks(self, data, concepts):
        """Let the reranker compare original passages with existing curated sources."""
        sources = []
        pages = {page for concept in concepts for page in concept["pdf_pages"]}
        for index, chunk in enumerate(self.chunks):
            if chunk.page in pages and (chunk.page, chunk.text) not in self.excluded_chunks:
                sources.append({"chunk_id": f"book:{index}", "file_path": f"book.pdf#page={chunk.page}", "content": chunk.text})
        seen = set()
        result = []
        for chunk in data.get("chunks", []) + sources:
            identity = (chunk["file_path"], chunk["content"])
            page = re.fullmatch(r"book\.pdf#page=(\d+)", chunk["file_path"])
            excluded = page and (int(page[1]), chunk["content"]) in self.excluded_chunks
            if identity not in seen and not excluded:
                result.append(chunk)
                seen.add(identity)
        return result
