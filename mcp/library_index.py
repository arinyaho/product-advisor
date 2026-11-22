"""Versioned LightRAG indices with source-scoped views and resumable extraction."""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.error

import numpy as np
from lightrag import LightRAG, QueryParam
from lightrag.operate import extract_entities
from lightrag.utils import EmbeddingFunc, compute_mdhash_id, process_chunks_unified, sanitize_text_for_encoding

from book_library import Catalog, atomic_json, book_summary, chunk_identity, digest, embedding_text, rendered_chunk


ENGINE_VERSION = "1.5.7"
INDEX_SCHEMA = "three-books-native-extraction-v1"
EMBED_MODEL = "qwen/qwen3-embedding-4b"
EXTRACT_MODEL = "anthropic/claude-haiku-5.5"
RERANK_MODEL = "qwen/qwen3-reranker-8b"
DIMENSION = 2560
EVIDENCE_TOKENS = 4000
EXTRACT_SETTINGS = {"entity_extract_max_gleaning": 0, "entity_extract_max_records": 12, "entity_extract_max_entities": 6, "addon_params": {"language": "English", "entity_types": ["CONCEPT", "PROCESS", "ROLE", "METRIC", "ORGANIZATION"]}}
FIXED_CONFIG = {"schema": INDEX_SCHEMA, "lightrag": ENGINE_VERSION, "embedding": EMBED_MODEL, "dimension": DIMENSION, "extraction": EXTRACT_MODEL, "rerank": RERANK_MODEL, "extraction_settings": EXTRACT_SETTINGS, "extraction_max_tokens": 1800, "temperature": 0}
RETRIEVAL_CONFIG = {"normalization": "none_original_query", "rerank": RERANK_MODEL, "min_rerank_score": .15, "context_tokens": 24000, "mix_vector_candidate_pool": 40, "evidence_tokens": EVIDENCE_TOKENS}
RETRIEVAL_VERSION = digest(json.dumps(RETRIEVAL_CONFIG, sort_keys=True).encode())[:16]


class Provider:
    def __init__(self, transport, directory=None, budget=5.0, max_calls=4000, retry_busy=False):
        self.transport = transport
        self.directory = Path(directory) if directory else None
        self.budget = budget
        self.max_calls = max_calls
        self.lock = threading.Lock()
        self.calls, self.cost = 0, 0.0
        self.events = []
        self.failed = False
        self.retry_busy = retry_busy
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            ledger = self.directory / "usage.jsonl"
            if ledger.exists():
                self.events = [json.loads(line) for line in ledger.read_text().splitlines()]
                self.cost = sum(event.get("charged_or_estimated_usd", 0) for event in self.events)
                self.calls = len(self.events)

    def request(self, endpoint, payload, *, _busy_retry=False):
        # Reserve the maximum bounded completion before dispatching concurrent requests.
        reserve = .01 if endpoint == "chat/completions" else .02
        with self.lock:
            if self.failed:
                raise RuntimeError("Provider circuit is stopped after a failed request; inspect the receipt before resuming")
            if self.calls >= self.max_calls or self.cost + reserve > self.budget:
                raise RuntimeError("Provider indexing budget/call limit reached")
            self.calls += 1
            self.cost += reserve
        started = time.monotonic()
        retry_delay = None
        event = {"endpoint": endpoint, "model": payload["model"], "timestamp": datetime.now(timezone.utc).isoformat()}
        try:
            response = self.transport(endpoint, payload, attempts=1, timeout=120)
            usage = response.get("usage", {})
            input_tokens = usage.get("prompt_tokens", usage.get("total_tokens", 0))
            output_tokens = usage.get("completion_tokens", 0)
            estimate = (.1 * input_tokens + .5 * output_tokens) / 1_000_000 if endpoint == "chat/completions" else .02 * input_tokens / 1_000_000 if endpoint == "embeddings" else .20 * input_tokens / 1_000_000 if payload["model"] == "qwen/qwen3-reranker-8b" else .002
            cost = usage.get("cost")
            cost = float(cost) if cost is not None else estimate
            event.update({"status": "completed", "usage": usage, "charged_or_estimated_usd": cost, "cost_source": "provider" if usage.get("cost") is not None else "published_rate_estimate"})
            return response
        except Exception as error:
            cost = reserve
            event.update({"status": "failed_cost_unknown", "error_type": type(error).__name__, "charged_or_estimated_usd": reserve, "cost_source": "conservative_reservation_not_actual_billing"})
            if isinstance(error, urllib.error.HTTPError):
                event["http_status"] = error.code
                try:
                    detail = json.loads(error.read()).get("error", {})
                    event["provider_error_code"] = detail.get("code")
                    message = str(detail.get("message", ""))
                    for name in ("OPENROUTER_API_KEY",):
                        if os.environ.get(name):
                            message = message.replace(os.environ[name], "<redacted>")
                    event["provider_message"] = message[:500]
                except (ValueError, AttributeError):
                    pass
                if error.code == 429 and self.retry_busy and not _busy_retry:
                    try:
                        delay = float(error.headers.get("Retry-After") or 2)
                    except (ValueError, TypeError):
                        delay = float("inf")
                    if 0 <= delay <= 10:
                        retry_delay = max(1, delay)
                        event["retry_after_seconds"] = retry_delay
            with self.lock:
                self.failed = self.directory is not None and retry_delay is None
        finally:
            event["seconds"] = round(time.monotonic() - started, 4)
            with self.lock:
                self.cost += cost - reserve
                self.events.append(event)
                if self.directory:
                    with (self.directory / "usage.jsonl").open("a") as stream:
                        stream.write(json.dumps(event) + "\n")
        if retry_delay is not None:
            time.sleep(retry_delay)
            return self.request(endpoint, payload, _busy_retry=True)
        raise RuntimeError("Provider request failed; no further automatic retry") from None

    async def extract(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        payload = {"model": EXTRACT_MODEL, "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else []) + (history_messages or []) + [{"role": "user", "content": prompt}], "max_tokens": 1800, "temperature": 0, "reasoning": {"enabled": False}}
        identity = digest(json.dumps(payload, sort_keys=True).encode())
        if not self.directory:
            raise RuntimeError("Extraction is disabled in the serving process")
        target = self.directory / "llm-cache" / (identity + ".json")
        def run():
            if target.exists():
                response = json.loads(target.read_text())
            else:
                response = self.request("chat/completions", payload)
                atomic_json(target, response)
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop" or not choice["message"].get("content"):
                raise RuntimeError("Extraction was empty or truncated; response retained, indexing stopped")
            return choice["message"]["content"]
        return await asyncio.to_thread(run)

    async def embed(self, texts):
        unique = list(dict.fromkeys(embedding_text(text) for text in texts))
        def run():
            response = self.request("embeddings", {"model": EMBED_MODEL, "input": unique})
            rows = sorted(response["data"], key=lambda row: row["index"])
            if [row["index"] for row in rows] != list(range(len(unique))):
                raise RuntimeError("Incomplete embedding response")
            matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
            if matrix.shape != (len(unique), DIMENSION) or not np.isfinite(matrix).all():
                raise RuntimeError("Embedding model/dimension mismatch")
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise RuntimeError("Zero embedding vector")
            return matrix / norms
        vectors = await asyncio.to_thread(run)
        mapped = dict(zip(unique, vectors, strict=True))
        return np.asarray([mapped[embedding_text(text)] for text in texts], dtype=np.float32)

    async def rerank(self, query, documents, top_n=None):
        if not documents:
            return []
        response = await asyncio.to_thread(self.request, "rerank", {"model": RERANK_MODEL, "query": query, "documents": documents, "top_n": min(top_n or len(documents), len(documents))})
        result = response["results"]
        expected = min(top_n or len(documents), len(documents))
        indices = [row.get("index") for row in result]
        if len(result) != expected or len(set(indices)) != expected or any(type(index) is not int or not 0 <= index < len(documents) for index in indices):
            raise RuntimeError("Invalid/incomplete rerank source identities")
        if any(type(row.get("relevance_score")) not in (int, float) or not np.isfinite(row["relevance_score"]) or not 0 <= row["relevance_score"] <= 1 for row in result):
            raise RuntimeError("Invalid rerank scores")
        return sorted(result, key=lambda row: row["relevance_score"], reverse=True)

class StaticVectors:
    def __init__(self, directory, provider):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.index_file = self.directory / "index.json"
        self.index = json.loads(self.index_file.read_text()) if self.index_file.exists() else {}
        self.lock = asyncio.Lock()

    def seed(self, texts, vectors):
        missing = [(text, vector) for text, vector in zip(texts, vectors, strict=True) if digest(embedding_text(text).encode()) not in self.index]
        if missing:
            self.save([text for text, _ in missing], np.asarray([vector for _, vector in missing]))

    def save(self, texts, vectors):
        name = "batch-" + digest("\0".join(texts).encode())[:24] + ".npy"
        np.save(self.directory / name, vectors)
        self.index.update({digest(embedding_text(text).encode()): [name, row] for row, text in enumerate(texts)})
        atomic_json(self.index_file, self.index)

    async def embed(self, texts):
        async with self.lock:
            missing = list(dict.fromkeys(embedding_text(text) for text in texts if digest(embedding_text(text).encode()) not in self.index))
            for start in range(0, len(missing), 64):
                batch = missing[start:start + 64]
                self.save(batch, await self.provider.embed(batch))
            batches = {}
            result = []
            for text in texts:
                filename, row = self.index[digest(embedding_text(text).encode())]
                if filename not in batches:
                    batches[filename] = np.load(self.directory / filename, mmap_mode="r")
                result.append(batches[filename][row])
            return np.asarray(result, dtype=np.float32)


def engine(directory, embed, extract=None, rerank=None):
    directory = Path(directory)
    release = directory.parent.parent.name if directory.parent.name == "indices" else directory.parent.name
    workspace = release + "-" + directory.name
    # LightRAG deep-copies its dataclass configuration; closures avoid copying provider locks.
    async def embed_call(texts):
        return await embed(texts)
    async def extract_call(*args, **kwargs):
        return await extract(*args, **kwargs)
    async def rerank_call(*args, **kwargs):
        return await rerank(*args, **kwargs)
    async def no_generation(*args, **kwargs):
        raise RuntimeError("Query generation and keyword-extraction LLM calls are disabled")
    return LightRAG(working_dir=str(directory), workspace=workspace, embedding_func=EmbeddingFunc(embedding_dim=DIMENSION, max_token_size=8192, model_name=EMBED_MODEL, func=embed_call), llm_model_func=extract_call if extract else no_generation, llm_model_name=EXTRACT_MODEL, llm_model_max_async=8, embedding_func_max_async=1, embedding_batch_num=64, cosine_better_than_threshold=.5, enable_llm_cache=False, rerank_model_func=rerank_call if rerank else None, min_rerank_score=.15, **EXTRACT_SETTINGS)


async def close_engine(rag):
    for function in set(rag._build_global_config()["role_llm_funcs"].values()):
        if hasattr(function, "shutdown"):
            await function.shutdown()
    await rag.embedding_func.func.shutdown()
    await rag.finalize_storages()


async def preserve_source_text(rag, chunks):
    """Keep original source bodies after native custom-KG control-character sanitization."""
    restored = 0
    for chunk in chunks:
        original = rendered_chunk(chunk)
        sanitized = sanitize_text_for_encoding(original)
        if sanitized == original:
            continue
        identity = compute_mdhash_id(sanitized, prefix="chunk-")
        stored = await rag.text_chunks.get_by_id(identity)
        if stored is None or stored["content"] not in (sanitized, original):
            raise ValueError("Native chunk differs beyond documented control-character sanitization")
        if stored["content"] != original:
            await rag.text_chunks.upsert({identity: {**stored, "content": original, "tokens": len(rag.tokenizer.encode(original))}})
            restored += 1
    if restored:
        await rag.text_chunks.index_done_callback()
    return restored


def _canonical(name):
    return " ".join("".join(character.lower() if character.isalnum() else " " for character in name).split())



def compile_graph(catalog, directory, book_ids):
    nodes = defaultdict(list); edges = defaultdict(list); chunks = []
    for book_id in book_ids:
        for chunk in catalog.books[book_id]["chunks"]:
            chunk_id = chunk["chunk_id"]
            chunks.append({"content": rendered_chunk(chunk), "source_id": chunk_id, "file_path": f"{book_id}://{catalog.books[book_id]['sha256']}#{chunk_id}"})
            extracted = json.loads((Path(directory) / "extracted" / (chunk_id + ".json")).read_text())
            for record in extracted["nodes"]:
                nodes[(book_id, record["entity_name"])].append((chunk_id, record))
            for record in extracted["edges"]:
                edges[(book_id, record["src_id"], record["tgt_id"])].append((chunk_id, record))
                for name in (record["src_id"], record["tgt_id"]):
                    if (book_id, name) not in nodes:
                        nodes[(book_id, name)].append((chunk_id, {"entity_name": name, "entity_type": "CONCEPT", "description": "Extracted relationship endpoint; consult the source passage."}))
    graph = {"chunks": chunks, "entities": [], "relationships": []}
    common = defaultdict(list)
    for (book_id, name), records in sorted(nodes.items()):
        source, first = records[0]
        description = "\n".join(f"[{book_id}; {chunk_id}; model-extracted navigation synopsis] {record['description']}" for chunk_id, record in records)
        # Bound embedding inputs; all original extraction records remain in their per-chunk files.
        description = description[:12000]
        entity = {"entity_name": f"{book_id}::{name}", "entity_type": first.get("entity_type", "CONCEPT"), "description": description, "source_id": source, "file_path": f"{book_id}://#{source}"}
        graph["entities"].append(entity)
        common[_canonical(name)].append(entity)
    for (book_id, source, target), records in sorted(edges.items()):
        chunk_id = records[0][0]
        graph["relationships"].append({"src_id": f"{book_id}::{source}", "tgt_id": f"{book_id}::{target}", "description": "\n".join(f"[{book_id}; {identity}; model-extracted relation, not an author quotation] {record['description']}" for identity, record in records)[:12000], "keywords": records[0][1].get("keywords", "related concepts"), "weight": 1.0, "source_id": chunk_id, "file_path": f"{book_id}://#{chunk_id}"})
    shared = 0
    if len(book_ids) > 1:
        for label, records in sorted(common.items()):
            if len({record["entity_name"].split("::", 1)[0] for record in records}) < 2:
                continue
            shared += 1
            name = "Shared concept label: " + label
            first = records[0]
            graph["entities"].append({"entity_name": name, "entity_type": "NAVIGATION", "description": "Navigation by a shared normalized extracted label. Author definitions and conditions remain in separate book-scoped nodes; equivalence is not asserted.", "source_id": first["source_id"], "file_path": first["file_path"]})
            for record in records:
                graph["relationships"].append({"src_id": name, "tgt_id": record["entity_name"], "description": "Derived index link for a shared label, not an author assertion or proof that definitions are equivalent.", "keywords": label, "source_id": record["source_id"], "file_path": record["file_path"], "weight": 1.0})
    return graph, shared


async def build(directory, catalog, provider, legacy_texts, legacy_vectors, max_chunks=None):
    directory = Path(directory)
    vectors = StaticVectors(directory / "static-vectors", provider)
    vectors.seed(legacy_texts, legacy_vectors)
    extractor = engine(directory / "extractor", vectors.embed, provider.extract)
    await extractor.initialize_storages()
    try:
        config = extractor._build_global_config()
        pending = [chunk for chunk in catalog.chunks.values() if not (directory / "extracted" / (chunk["chunk_id"] + ".json")).exists()]
        pending = pending if max_chunks is None else pending[:max_chunks]
        for start in range(0, len(pending), 16):
            if (directory / "pause.request").exists():
                return {"status": "stopped_at_checkpoint", "ready": False}
            batch = pending[start:start + 16]
            prepared = {chunk["chunk_id"]: {"content": f"Source book: {catalog.books[chunk['book_id']]['title']}\nSection: {chunk['section']}\n\n{chunk['text']}", "tokens": len(extractor.tokenizer.encode(chunk["text"])), "chunk_order_index": start + index, "full_doc_id": chunk["book_id"], "file_path": f"{chunk['book_id']}://#{chunk['chunk_id']}"} for index, chunk in enumerate(batch)}
            results = await extract_entities(prepared, config)
            for chunk, (maybe_nodes, maybe_edges) in zip(batch, results, strict=True):
                record = {"chunk_id": chunk["chunk_id"], "nodes": [node for group in maybe_nodes.values() for node in group], "edges": [edge for group in maybe_edges.values() for edge in group]}
                atomic_json(directory / "extracted" / (chunk["chunk_id"] + ".json"), record)
            if start % 40 == 0:
                print(json.dumps({"stage": "extract", "completed_this_run": start + len(batch), "pending_at_start": len(pending), "provider_cost_usd": round(provider.cost, 4)}), flush=True)
    finally:
        await close_engine(extractor)
    if any(not (directory / "extracted" / (chunk["chunk_id"] + ".json")).exists() for chunk in catalog.chunks.values()):
        return {"status": "partial_extraction", "ready": False}
    scopes = [[book_id] for book_id in catalog.books] + [list(catalog.books)]
    stats = {}
    for ids in scopes:
        scope = ids[0] if len(ids) == 1 else "all"
        marker = directory / "indices" / scope / "ready.json"
        if marker.exists():
            stats[scope] = json.loads(marker.read_text())
            continue
        graph, shared = compile_graph(catalog, directory, ids)
        rag = engine(marker.parent, vectors.embed)
        await rag.initialize_storages()
        try:
            await rag.ainsert_custom_kg(graph, full_doc_id=scope)
            await preserve_source_text(rag, [chunk for chunk in catalog.chunks.values() if chunk["book_id"] in ids])
            stats[scope] = {"books": ids, "chunks": len(graph["chunks"]), "entities": len(graph["entities"]), "relationships": len(graph["relationships"]), "shared_concept_labels": shared}
            atomic_json(marker, stats[scope])
        finally:
            await close_engine(rag)
        print(json.dumps({"stage": "index_ready", "scope": scope, **stats[scope]}), flush=True)
    atomic_json(directory / "ready.json", {"config": FIXED_CONFIG, "retrieval_config": RETRIEVAL_CONFIG, "scopes": stats, "completed_at": datetime.now(timezone.utc).isoformat(), "cost_usd": provider.cost, "calls": provider.calls})
    return stats


class LibraryIndex:
    def __init__(self, directory, transport):
        self.directory = Path(directory)
        self.catalog = Catalog.load(directory)
        manifest = json.loads((self.directory / "manifest.json").read_text())
        if manifest["catalog_sha256"] != digest((self.directory / "catalog.json").read_bytes()):
            raise ValueError("Catalog hash does not match the release manifest")
        ready = json.loads((self.directory / "ready.json").read_text())
        if manifest["config"] != FIXED_CONFIG or ready["config"] != FIXED_CONFIG:
            raise ValueError("Incompatible library model/index configuration")
        if ready.get("retrieval_config") != RETRIEVAL_CONFIG:
            raise ValueError("The release has not been sealed for this retrieval configuration")
        if not ready.get("file_sha256"):
            raise ValueError("The library release has not passed source and index verification")
        for name, expected in ready["file_sha256"].items():
            path = self.directory / name
            if not path.is_relative_to(self.directory) or digest(path.read_bytes()) != expected:
                raise ValueError("Library index file hash differs from the sealed release")
        self.version = manifest["version"]
        self.provider = Provider(transport, budget=float("inf"), max_calls=2**63, retry_busy=True)
        self.engines = {}
        self.ready = threading.Event()
        self.error = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="book-library")

    def start(self):
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.initialize())
        self.loop.run_forever()

    async def initialize(self):
        try:
            for scope in [*self.catalog.books, "all"]:
                if not (self.directory / "indices" / scope / "ready.json").exists():
                    raise RuntimeError("A scoped index is missing")
                rag = engine(self.directory / "indices" / scope, self.provider.embed)
                await rag.initialize_storages()
                self.engines[scope] = rag
            import logging
            logging.getLogger("lightrag").setLevel(logging.WARNING)
            self.ready.set()
            print("Book library index ready", flush=True)
        except Exception as error:
            self.error = type(error).__name__

    async def close(self):
        async def finish():
            for rag in self.engines.values():
                await close_engine(rag)
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(finish(), self.loop))
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join, 5)

    async def search(self, query, k, book_id, mode, scope):
        if mode not in ("mix", "naive") or not 1 <= k <= 20 or not 3 <= len(query.strip()) <= 8000:
            raise ValueError("Use mode mix/naive, k 1..20, and a query of 3..8000 characters")
        if book_id is not None and book_id not in self.catalog.books:
            raise ValueError("Unknown book_id")
        if not self.ready.is_set():
            return {"retrieval": {"requested_mode": mode, "actual_mode": None, "status": self.error or "building", "fallback": False}, "results": []}
        future = asyncio.run_coroutine_threadsafe(self._search(query, k, book_id, mode, scope), self.loop)
        return await asyncio.wrap_future(future)

    async def _search(self, query, k, book_id, mode, scope):
        started = time.monotonic()
        rag = self.engines[book_id or "all"]
        param = QueryParam(mode=mode, top_k=40, chunk_top_k=40, max_total_tokens=RETRIEVAL_CONFIG["context_tokens"], max_entity_tokens=2500, max_relation_tokens=2000, ll_keywords=[query] if mode == "mix" else [], hl_keywords=[query] if mode == "mix" else [], enable_rerank=False)
        try:
            raw = await rag.aquery_data(query, param=param)
            actual_mode = raw.get("metadata", {}).get("query_mode") or raw.get("metadata", {}).get("mode")
            if actual_mode != mode:
                raise RuntimeError("LightRAG did not confirm the requested execution mode")
            candidates = raw.get("data", {}).get("chunks", [])
            if mode == "mix":
                # Native mix packing can discard strong vector evidence before external reranking.
                candidates = candidates + await rag.chunks_vdb.query(query, top_k=40)
            # The strict book filter was applied by selecting its own graph and vector stores.
            mapped = []
            for hit in candidates:
                chunk = self.catalog.chunks[chunk_identity(hit["content"])]
                if book_id and chunk["book_id"] != book_id:
                    raise RuntimeError("Scoped index leaked another book")
                if chunk["chunk_id"] not in [item["chunk_id"] for item in mapped]:
                    mapped.append(chunk)
            candidate_count = len(mapped)
            if mapped:
                failures = []
                async def rerank(**arguments):
                    try:
                        return await self.provider.rerank(**arguments)
                    except Exception:
                        failures.append(True)
                        raise RuntimeError("Rerank provider unavailable") from None
                ranked = await process_chunks_unified(query, [{"chunk_id": chunk["chunk_id"], "content": chunk["text"]} for chunk in mapped], QueryParam(enable_rerank=True, chunk_top_k=40), {"rerank_model_func": rerank, "min_rerank_score": .15})
                if failures:
                    raise RuntimeError("Rerank provider unavailable")
                mapped = [{**self.catalog.chunks[hit["chunk_id"]], "rerank_score": hit["rerank_score"]} for hit in ranked]
            result = {"semantic": True, "retrieval": {"requested_mode": mode, "actual_mode": actual_mode, "status": "ready", "fallback": False, "index_version": self.version, "retrieval_version": RETRIEVAL_VERSION, "query_normalization": "none_original_query", "query_normalization_model": None, "lightrag_version": ENGINE_VERSION, "rerank_model": RERANK_MODEL, "evidence_token_budget": EVIDENCE_TOKENS, "graph_entities": len(raw.get("data", {}).get("entities", [])), "graph_relations": len(raw.get("data", {}).get("relationships", [])), "candidate_chunks": len(candidates), "graph_provenance": "model_extracted_navigation_not_author_assertions", "scope": book_id or "all"}, "results": []}
            for chunk in mapped[:k]:
                book = self.catalog.books[chunk["book_id"]]
                record = {**chunk, "title": book["title"], "edition": book["edition"], "authors": book["authors"], "source_sha256": book["sha256"], "read_section": {"id": chunk["section_id"], "book_id": chunk["book_id"]}}
                if chunk["location"]["kind"] == "pdf":
                    record["page"] = chunk["location"]["pdf_page"]
                record["text"] = chunk["text"] if scope == "full" else " ".join(chunk["text"].split()[:60])
                result["results"].append(record)
                if len(rag.tokenizer.encode(json.dumps(result, ensure_ascii=False))) > EVIDENCE_TOKENS:
                    result["results"].pop()
                    break
            result["retrieval"]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            result["retrieval"]["candidate_chunks"] = candidate_count
            result["retrieval"]["candidate_expansion"] = "same_lightrag_scoped_vector_pool" if mode == "mix" else "none"
            while result["results"] and len(rag.tokenizer.encode(json.dumps(result, ensure_ascii=False))) > EVIDENCE_TOKENS:
                result["results"].pop()
            return result
        except Exception as error:
            return {"retrieval": {"requested_mode": mode, "actual_mode": None, "status": "unavailable", "fallback": False, "error_type": type(error).__name__, "scope": book_id or "all"}, "results": []}
