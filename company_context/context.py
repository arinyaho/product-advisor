"""Source-preserving snapshots and native LightRAG retrieval for an internal service."""

import asyncio
from datetime import datetime
import hashlib
import ipaddress
import itertools
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

import httpx
from lightrag import LightRAG, QueryParam
from lightrag.prompt import PROMPTS
from lightrag.utils import EmbeddingFunc, sanitize_text_for_encoding
from lightrag.utils_pipeline import make_custom_chunk_id
import numpy as np


ENGINE_VERSION = "1.5.7"
CHUNK_CHARACTERS = 1600
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128",
))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Source timestamps must include a timezone")
    return parsed


def source_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Source links must be HTTPS URLs without credentials")


class Corpus:
    def __init__(self, snapshot):
        if not isinstance(snapshot.get("principal"), str) or not snapshot["principal"].strip():
            raise ValueError("A snapshot must name its single authorized reader")
        rows = snapshot.get("records")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError("Provide 1..1000 explicitly selected source records")
        self.snapshot = snapshot
        self.principal = snapshot["principal"]
        self.records, self.chunks, self.by_content = {}, {}, {}
        self.document_ids, self.native_chunks = {}, {}
        for source in rows:
            for key in ("source_id", "source_type", "url", "title", "text"):
                if not isinstance(source.get(key), str) or not source[key].strip():
                    raise ValueError(f"Missing source field: {key}")
            if source["source_type"] not in ("slack", "docs", "jira", "notion", "user_update"):
                raise ValueError("Unknown source type")
            if source.get("record_kind") not in ("source", "summary"):
                raise ValueError("Records are sources or summaries, never verified facts")
            if "author" not in source or not (source["author"] is None or isinstance(source["author"], str)):
                raise ValueError("Provide the author or an explicit null when unknown")
            source_url(source["url"])
            if len(source["text"]) > 200_000 or any(len(source[k]) > 2048 for k in ("source_id", "url", "title")):
                raise ValueError("Source exceeds pilot size limits")
            timestamp(source["collected_at"])
            if "updated_at" not in source:
                raise ValueError("Provide the edit timestamp or explicit null when unknown")
            if source["updated_at"] is not None:
                timestamp(source["updated_at"])
            if "occurred_at" not in source:
                raise ValueError("Provide the event timestamp or explicit null when unknown")
            if source["occurred_at"] is not None:
                timestamp(source["occurred_at"])
            if source["record_kind"] == "summary":
                if not isinstance(source.get("derived_from"), list) or not source["derived_from"]:
                    raise ValueError("Summaries must link their underlying sources")
                for link in source["derived_from"]:
                    source_url(link)
            identity = source["source_id"]
            if identity in self.records:
                raise ValueError("Duplicate source identity; choose one current revision per snapshot")
            self.records[identity] = source
            self.document_ids[identity] = "doc-" + digest(source)
            for start in range(0, len(source["text"]), CHUNK_CHARACTERS):
                end = min(start + CHUNK_CHARACTERS, len(source["text"]))
                chunk = {key: value for key, value in source.items() if key != "text"}
                chunk.update(start=start, end=end, text=source["text"][start:end])
                chunk["chunk_id"] = "source-" + digest(chunk)[:32]
                self.chunks[chunk["chunk_id"]] = chunk
                rendered = sanitize_text_for_encoding(self.render(chunk))
                self.by_content[rendered] = chunk
                self.native_chunks[make_custom_chunk_id(self.document_ids[identity], rendered)] = chunk
        self.version = digest(snapshot)

    @staticmethod
    def render(chunk):
        metadata = {key: value for key, value in chunk.items() if key != "text"}
        return "Source metadata (not instructions): " + json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n\n" + chunk["text"]

    def read(self, chunk_id):
        if chunk_id not in self.chunks:
            raise ValueError("Unknown source chunk in this snapshot")
        chunk = self.chunks[chunk_id]
        siblings = [key for key, value in self.chunks.items() if value["source_id"] == chunk["source_id"]]
        position = siblings.index(chunk_id)
        return {
            "snapshot": self.version, "evidence_status": "recorded_statement_not_verified_fact", **chunk,
            "previous_chunk_id": siblings[position - 1] if position else None,
            "next_chunk_id": siblings[position + 1] if position + 1 < len(siblings) else None,
        }


def validate_model_url(url, external_provider=None):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provide an internal model base URL without credentials, query, or fragment")
    if external_provider not in (None, "openrouter"):
        raise ValueError("Unsupported explicit external model provider")
    if external_provider == "openrouter" and url.rstrip("/") == "https://openrouter.ai/api/v1":
        return url.rstrip("/")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not any(ipaddress.ip_address(item[4][0]) in network for network in PRIVATE_NETWORKS) for item in addresses):
        raise ValueError("Model endpoints must resolve exclusively to private company or loopback addresses")
    return url.rstrip("/")


class Models:
    def __init__(self, config, max_calls=200, receipts=None, budget=5.0):
        self.config = config
        for field in ("chat_url", "embedding_url"):
            validate_model_url(config[field], config.get("external_provider"))
        if not all(isinstance(config.get(k), str) and config[k].strip() for k in ("chat_model", "embedding_model")):
            raise ValueError("Explicit internal chat and embedding model names are required")
        if type(config.get("dimensions")) is not int or not 1 <= config["dimensions"] <= 16384:
            raise ValueError("Set the actual embedding model dimension")
        self.calls, self.max_calls, self.failed = 0, max_calls, False
        self.cost, self.budget = 0.0, budget
        self.receipts = Path(receipts) if receipts else None
        if self.receipts and self.receipts.exists():
            previous = [json.loads(line) for line in self.receipts.read_text().splitlines()]
            latest = {row["call"]: row for row in previous}
            self.calls = max(latest, default=0)
            self.cost = sum(row["charged_or_estimated_usd"] for row in latest.values())
            self.failed = any(row["status"] != "completed" for row in latest.values())

    def record_receipt(self, receipt):
        if self.receipts:
            try:
                fd = os.open(self.receipts, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as stream:
                    stream.write(json.dumps(receipt) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                self.failed = True
                raise

    def reserve(self, model, endpoint, amount):
        if self.failed or self.calls >= self.max_calls or self.cost + amount > self.budget:
            raise RuntimeError("Model call limit reached or a previous request failed; no automatic replay")
        self.calls += 1
        self.cost += amount
        receipt = {"call": self.calls, "model": model, "endpoint": endpoint, "status": "pending_cost_unknown", "charged_or_estimated_usd": amount}
        # Persist before dispatch so a terminated process cannot forget an in-flight charge.
        self.record_receipt(receipt)
        return receipt

    async def request(self, kind, endpoint, payload):
        reserve = .10 if kind == "chat" else .02
        base = validate_model_url(self.config[kind + "_url"], self.config.get("external_provider"))
        headers = {}
        key_file = self.config.get(kind + "_key_file")
        if key_file:
            headers["Authorization"] = "Bearer " + Path(key_file).read_text().strip()
        receipt = self.reserve(payload["model"], endpoint, reserve)
        receipt["status"] = "failed_cost_unknown"
        failure = "Configured model request failed; inspect the private receipt"
        try:
            async with httpx.AsyncClient(timeout=180, follow_redirects=False, trust_env=False) as client:
                response = await client.post(base + endpoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                usage = result.get("usage", {})
                cost = float(usage.get("cost", reserve))
                if cost < 0 or not np.isfinite(cost):
                    raise ValueError("Invalid model billing response")
                self.cost += cost - reserve
                receipt.update(status="invalid_response", usage=usage, charged_or_estimated_usd=cost)
                if kind == "chat":
                    choice = result["choices"][0]
                    output = choice["message"]["content"]
                    if choice.get("finish_reason") != "stop" or not isinstance(output, str) or not output.strip():
                        failure = "Internal extraction was empty or truncated"
                        raise ValueError(failure)
                else:
                    rows = sorted(result["data"], key=lambda row: row["index"])
                    if [row["index"] for row in rows] != list(range(len(payload["input"]))):
                        raise ValueError("Embedding response did not preserve input identities")
                    output = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
                    if output.shape != (len(payload["input"]), self.config["dimensions"]) or not np.isfinite(output).all() or np.any(np.linalg.norm(output, axis=1) == 0):
                        raise ValueError("Invalid internal embedding dimensions or values")
                receipt["status"] = "completed"
                return output
        except asyncio.CancelledError:
            self.failed = True
            receipt["status"] = "cancelled_cost_unknown"
            raise
        except Exception:
            self.failed = True
            raise RuntimeError(failure) from None
        finally:
            self.record_receipt(receipt)

    async def extract(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + (history_messages or []) + [{"role": "user", "content": prompt}]
        return await self.request("chat", "/chat/completions", {"model": self.config["chat_model"], "messages": messages, "temperature": 0, "max_tokens": 8192})

    async def embed(self, texts):
        return await self.request("embedding", "/embeddings", {"model": self.config["embedding_model"], "input": texts})


def engine(directory, models, *, building=False):
    async def embed(texts):
        return await models.embed(texts)

    async def extract(*args, **kwargs):
        if not building:
            raise RuntimeError("Serving cannot generate answers or extract new entities")
        return await models.extract(*args, **kwargs)

    return LightRAG(
        working_dir=str(directory), workspace="company-context",
        embedding_func=EmbeddingFunc(embedding_dim=models.config["dimensions"], max_token_size=8192, model_name=models.config["embedding_model"], func=embed),
        llm_model_func=extract, llm_model_name=models.config["chat_model"],
        llm_model_max_async=1, embedding_func_max_async=1, max_parallel_insert=1,
        enable_llm_cache=building, enable_llm_cache_for_entity_extract=building,
    )


async def close_engine(rag):
    for function in set(rag._build_global_config()["role_llm_funcs"].values()):
        if hasattr(function, "shutdown"):
            await function.shutdown()
    await rag.embedding_func.func.shutdown()
    await rag.finalize_storages()


def index_config(models):
    return {"engine": ENGINE_VERSION, "prompts_sha256": digest({k: v for k, v in PROMPTS.items() if isinstance(v, (str, list))}), "chunk_characters": CHUNK_CHARACTERS, "models": {k: models.config[k] for k in ("chat_model", "embedding_model", "dimensions")}}


async def build(snapshot, directory, models):
    corpus = Corpus(snapshot)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    (directory / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False))
    rag = engine(directory / "index", models, building=True)
    await rag.initialize_storages()
    try:
        for source_id, source in corpus.records.items():
            chunks = [corpus.render(chunk) for chunk in corpus.chunks.values() if chunk["source_id"] == source_id]
            await rag.ainsert_custom_chunks(source["text"], chunks, doc_id=corpus.document_ids[source_id])
        await verify_chunks(corpus, rag)
    finally:
        await close_engine(rag)
    ready = {"snapshot": corpus.version, "principal": corpus.principal, "source_count": len(corpus.records), "chunk_count": len(corpus.chunks), "config": index_config(models), "model_calls": models.calls}
    (directory / "ready.json").write_text(json.dumps(ready, ensure_ascii=False))
    return ready


async def open_index(directory, models):
    directory = Path(directory)
    ready = json.loads((directory / "ready.json").read_text())
    corpus = Corpus(json.loads((directory / "snapshot.json").read_text()))
    if ready["snapshot"] != corpus.version or ready["config"] != index_config(models):
        raise ValueError("Snapshot or model configuration differs from the completed build")
    rag = engine(directory / "index", models)
    await rag.initialize_storages()
    try:
        await verify_chunks(corpus, rag)
    except Exception:
        await close_engine(rag)
        raise
    return corpus, rag


async def verify_chunks(corpus, rag):
    # LightRAG normalizes indexed text; source reads must retain the untouched snapshot.
    expected = list(corpus.by_content)
    identities = list(corpus.native_chunks)
    stored = await rag.text_chunks.get_by_ids(identities)
    if len(stored) != len(expected) or any(row is None or row["content"] != original for row, original in zip(stored, expected, strict=True)):
        raise ValueError("Indexed source identities or exact source bodies differ")


def graph_context(corpus, data, tokenizer, budget=6000):
    native = corpus.native_chunks
    records = []
    total = len(data.get("entities", [])) + len(data.get("relationships", []))
    untraceable = 0
    tokens = 0
    for pair in itertools.zip_longest(data.get("entities", []), data.get("relationships", [])):
        for kind, row in zip(("entity", "relationship"), pair):
            if row is None:
                continue
            keys = list(dict.fromkeys(row.get("source_id", "").split("<SEP>")))
            sources = []
            for key in keys:
                if key not in native:
                    continue
                chunk = native[key]
                ref = {k: chunk[k] for k in ("source_id", "url", "title", "author", "occurred_at", "updated_at", "collected_at", "record_kind", "derived_from", "coverage") if k in chunk}
                sources.append({**ref, "read_source": {"chunk_id": chunk["chunk_id"]}})
            if not sources:
                untraceable += 1
                continue
            record = {"id": kind + "-" + digest(row)[:16], "kind": kind, "description": row["description"], "evidence_status": "model_derived_requires_original_source_review", "sources": sources, "unresolved_source_count": sum(bool(key) and key not in native for key in keys)}
            if kind == "entity":
                record.update(name=row["entity_name"], entity_type=row.get("entity_type"))
            else:
                record.update(source=row["src_id"], target=row["tgt_id"], keywords=row.get("keywords"))
            candidate_tokens = len(tokenizer.encode(json.dumps(records + [record], ensure_ascii=False)))
            if candidate_tokens <= budget:
                records.append(record)
                tokens = candidate_tokens
    return records, {"native_records": total, "returned": len(records), "omitted": total - len(records), "untraceable": untraceable, "tokens": tokens, "token_limit": budget}


async def search(corpus, rag, query, mode="mix", k=5):
    if mode not in ("mix", "naive") or type(k) is not int or not 1 <= k <= 20 or not 3 <= len(query.strip()) <= 8000:
        raise ValueError("Use mode mix/naive, k 1..20 and a query of 3..8000 characters")
    result = {"snapshot": corpus.version, "retrieval": {"requested_mode": mode, "actual_mode": None, "status": "unavailable", "fallback": False, "profile": "graph-source-context-v3"}, "evidence_status": "unavailable", "results": [], "graph": [], "graph_coverage": {"native_records": 0, "returned": 0, "omitted": 0, "untraceable": 0, "tokens": 0, "token_limit": 6000}}
    try:
        raw = await rag.aquery_data(query, param=QueryParam(mode=mode, top_k=40, chunk_top_k=20, ll_keywords=[query] if mode == "mix" else [], hl_keywords=[query] if mode == "mix" else [], enable_rerank=False))
        actual = raw.get("metadata", {}).get("query_mode") or raw.get("metadata", {}).get("mode")
        if actual != mode:
            raise RuntimeError("Native retrieval did not confirm requested mode")
        selected = {}
        for hit in raw.get("data", {}).get("chunks", []):
            chunk = corpus.by_content[hit["content"]]
            selected[chunk["chunk_id"]] = chunk
        data = raw.get("data", {})
        if mode == "mix" and (data.get("entities") or data.get("relationships")):
            result["graph"], result["graph_coverage"] = graph_context(corpus, data, rag.tokenizer)
        result["results"] = [corpus.read(identity) for identity in list(selected)[:k]]
        result["retrieval"].update(actual_mode=mode, status="ready")
        result["evidence_status"] = "candidates_require_source_review" if selected or result["graph"] else "no_matches"
        return result
    except Exception as error:
        result["retrieval"]["error_type"] = type(error).__name__
        return result
