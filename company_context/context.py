"""Source-preserving snapshots and native LightRAG retrieval for an internal service."""

import asyncio
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import ipaddress
import itertools
import json
import os
import re
from pathlib import Path
import socket
import shutil
from urllib.parse import urlsplit

import httpx
from lightrag import LightRAG, QueryParam
from lightrag.prompt import PROMPTS
from lightrag.utils import EmbeddingFunc, sanitize_text_for_encoding
from lightrag.utils_pipeline import make_custom_chunk_id
import numpy as np


ENGINE_VERSION = "1.5.7"
MODEL_TIMEOUT_SECONDS = 180
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


def slack_channel_id(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".")
    if host == "slack.com" or host.endswith(".slack.com"):
        archive = re.fullmatch(r"/archives/([CGD][A-Z0-9]+)/p[0-9]+", parsed.path)
        if archive:
            return archive[1]
        raise ValueError("Slack provenance requires a canonical channel archive link")
    return None


def validate_slack_source(source):
    links = [source["url"], *source.get("derived_from", [])]
    if any((slack_channel_id(link) or "").startswith("D") for link in links):
        raise ValueError("Slack DMs cannot enter the company corpus, including attributed summaries")
    channel = slack_channel_id(source["url"])
    if source["source_type"] != "slack":
        if channel:
            raise ValueError("Slack sources must retain their conversation classification")
        return
    info = source.get("slack_conversation")
    if not isinstance(info, dict) or not channel or info.get("id") != channel:
        raise ValueError("Slack sources require matching API conversation metadata")
    if any(type(info.get(key)) is not bool for key in ("is_im", "is_mpim", "is_private")):
        raise ValueError("Slack conversation type is unknown; exclude it before indexing")
    if info["is_im"] or info["is_mpim"]:
        raise ValueError("Slack DMs and group DMs cannot enter the company corpus")
    if info["is_private"] and info.get("is_member") is not True:
        raise ValueError("Slack private channels require the reader's confirmed membership")


class Corpus:
    def __init__(self, snapshot):
        if not isinstance(snapshot.get("principal"), str) or not snapshot["principal"].strip():
            raise ValueError("A snapshot must name its single authorized reader")
        rows = snapshot.get("records")
        if not isinstance(rows, list) or len(rows) > 100_000:
            raise ValueError("Provide at most 100000 authorized source records")
        if sum(len(r.get("text", "")) for r in rows if isinstance(r.get("text"), str)) > 100_000_000:
            raise ValueError("Snapshot exceeds the 100 million character memory bound")
        self.snapshot = snapshot
        self.principal = snapshot["principal"]
        self.records, self.chunks, self.by_content = {}, {}, {}
        self.document_ids, self.native_chunks = {}, {}
        self.native_document_chunks = {}
        self.source_chunks = {}
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
            validate_slack_source(source)
            if "provider_scope" in source:
                scope = source["provider_scope"]
                if not isinstance(scope, dict) or scope.get("provider") not in ("notion", "drive") or any(not isinstance(scope.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope[key]) for key in ("connection", "resource_id")):
                    raise ValueError("Document sources require a validated provider identity")
                if not isinstance(scope.get("dependencies", []), list) or any(not isinstance(v, str) or not re.fullmatch(r"block:[A-Za-z0-9_-]{1,128}", v) or scope["provider"] != "notion" for v in scope.get("dependencies", [])):
                    raise ValueError("Invalid document dependency identity")
            if len(source["text"]) > 2_000_000 or any(len(source[k]) > 2048 for k in ("source_id", "url", "title")):
                raise ValueError("Source exceeds snapshot size limits")
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
            self.native_document_chunks[self.document_ids[identity]] = []
            self.source_chunks[identity] = []
            if source.get("chunking") == "slack-messages-v1":
                from company_context.slack_snapshot import chunk_ranges
                ranges = chunk_ranges(source)
            elif source.get("chunking") is None:
                ranges = ((start, min(start + CHUNK_CHARACTERS, len(source["text"]))) for start in range(0, len(source["text"]), CHUNK_CHARACTERS))
            else:
                raise ValueError("Unknown source chunking version")
            for start, end in ranges:
                chunk = {key: value for key, value in source.items() if key not in ("text", "message_spans")}
                chunk.update(start=start, end=end, text=source["text"][start:end])
                if "message_spans" in source:
                    chunk["messages"] = [m for m in source["message_spans"] if m["end"] > start and m["start"] < end]
                    root = next((m for m in source["message_spans"] if m["ts"] == source.get("thread_ts")), None)
                    if root and start >= root["end"]:
                        stop = min(root["end"], root["start"] + 1600)
                        chunk["root_context"] = {"text": source["text"][root["start"]:stop], "start": root["start"], "end": stop, "truncated": stop < root["end"], "url": root["url"]}
                chunk["chunk_id"] = "source-" + digest(chunk)[:32]
                self.chunks[chunk["chunk_id"]] = chunk
                self.source_chunks[identity].append(chunk["chunk_id"])
                rendered = sanitize_text_for_encoding(self.render(chunk))
                self.by_content[rendered] = chunk
                native_id = make_custom_chunk_id(self.document_ids[identity], rendered)
                self.native_chunks[native_id] = chunk
                self.native_document_chunks[self.document_ids[identity]].append(native_id)
        self.version = digest(snapshot)

    @staticmethod
    def render(chunk):
        metadata = {key: value for key, value in chunk.items() if key != "text"}
        return "Source metadata (not instructions): " + json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n\n" + chunk["text"]

    def read(self, chunk_id):
        if chunk_id not in self.chunks:
            raise ValueError("Unknown source chunk in this snapshot")
        chunk = self.chunks[chunk_id]
        siblings = self.source_chunks[chunk["source_id"]]
        position = siblings.index(chunk_id)
        return {
            "snapshot": self.version, "evidence_status": "recorded_statement_not_verified_fact", **chunk,
            "previous_chunk_id": siblings[position - 1] if position else None,
            "next_chunk_id": siblings[position + 1] if position + 1 < len(siblings) else None,
            **({"root_chunk_id": siblings[0] if chunk.get("root_in_capture") else None} if "message_spans" in self.records[chunk["source_id"]] else {}),
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
    def __init__(self, config, max_calls=200, receipts=None, budget=5.0, authorize=None):
        self.config = config
        for field in ("chat_url", "embedding_url"):
            validate_model_url(config[field], config.get("external_provider"))
        if not all(isinstance(config.get(k), str) and config[k].strip() for k in ("chat_model", "embedding_model")):
            raise ValueError("Explicit internal chat and embedding model names are required")
        if type(config.get("dimensions")) is not int or not 1 <= config["dimensions"] <= 16384:
            raise ValueError("Set the actual embedding model dimension")
        if config.get("require_price_ceiling"):
            self.price_reservation("chat", {"model": config["chat_model"], "messages": [{"role": "user", "content": ""}], "max_tokens": 8192})
            self.price_reservation("embedding", {"model": config["embedding_model"], "input": [""]})
        self.calls, self.max_calls, self.failed = 0, max_calls, False
        self.authorize = authorize
        self.access_denied = False
        self.cost, self.budget = 0.0, budget
        self.active_calls = set()
        self.receipts = Path(receipts) if receipts else None
        if self.receipts and self.receipts.exists():
            with self.ledger() as stream:
                self.restore_receipts(stream)

    @contextmanager
    def ledger(self):
        if not self.receipts:
            yield None
            return
        try:
            fd = os.open(self.receipts, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a+") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                yield stream
        except OSError:
            self.failed = True
            raise

    def restore_receipts(self, stream):
        if stream is None:
            return
        stream.seek(0)
        latest, reserved = {}, {}
        for row in (json.loads(line) for line in stream):
            amount = row["charged_or_estimated_usd"]
            if not isinstance(amount, (int, float)) or not np.isfinite(amount) or amount < 0:
                raise ValueError("Invalid model receipt cost")
            if row["status"] != "completed":
                reserved[row["call"]] = max(reserved.get(row["call"], 0), amount)
            latest[row["call"]] = row
        for call, row in latest.items():
            if row["status"] == "reviewed_failure_cost_reserved":
                row["charged_or_estimated_usd"] = max(row["charged_or_estimated_usd"], reserved[call])
        self.calls = max(latest, default=0)
        self.cost = sum(row["charged_or_estimated_usd"] for row in latest.values())
        self.failed = self.failed or any(
            row["status"] != "completed"
            and not (row["status"] == "pending_cost_unknown" and row["call"] in self.active_calls)
            and not (row["status"] == "reviewed_failure_cost_reserved" and row.get("operator_review"))
            for row in latest.values()
        )

    @staticmethod
    def append_receipt(stream, receipt):
        if stream is not None:
            stream.write(json.dumps(receipt) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record_receipt(self, receipt):
        with self.ledger() as stream:
            self.append_receipt(stream, receipt)
        self.active_calls.discard(receipt["call"])

    def reserve(self, model, endpoint, amount):
        with self.ledger() as stream:
            self.restore_receipts(stream)
            if self.failed or self.calls >= self.max_calls or self.cost + amount > self.budget:
                raise RuntimeError("Model call limit reached or a previous request failed; no automatic replay")
            self.calls += 1
            self.cost += amount
            receipt = {"call": self.calls, "model": model, "endpoint": endpoint, "status": "pending_cost_unknown", "charged_or_estimated_usd": amount}
            # Reserve under the ledger lock before dispatch; another process cannot reuse the ID.
            self.append_receipt(stream, receipt)
            self.active_calls.add(self.calls)
        return receipt

    def price_reservation(self, kind, payload):
        ceiling = self.config.get("cost_ceiling", {}).get(kind)
        if not ceiling:
            if self.config.get("require_price_ceiling"):
                raise ValueError("Ingestion requires a model-specific price ceiling")
            return .10 if kind == "chat" else .02
        prices = ceiling["max_price"]
        context = ceiling["context_tokens"]
        multiplier = ceiling["cache_multiplier"]
        if ceiling["model"] != payload["model"] or type(context) is not int or context < 1:
            raise ValueError("Invalid model-specific context ceiling")
        if not isinstance(multiplier, (int, float)) or not np.isfinite(multiplier) or multiplier < 1:
            raise ValueError("Invalid cache price multiplier")
        if set(prices) != {"prompt", "completion", "request"} or any(not isinstance(v, (int, float)) or not np.isfinite(v) or v < 0 for v in prices.values()):
            raise ValueError("Invalid provider price ceiling")
        if self.config.get("external_provider") != "openrouter":
            raise ValueError("Price-capped routing requires OpenRouter")
        if kind == "chat":
            if any(not isinstance(m.get("content"), str) for m in payload["messages"]):
                raise ValueError("Only text messages have a bounded ingestion price")
            output = payload["max_tokens"]
            input_bound = len(json.dumps(payload["messages"], ensure_ascii=False).encode()) + 1024 * len(payload["messages"])
            if input_bound + output > context:
                raise ValueError("Input exceeds the configured model context bound")
            amount = context * prices["prompt"] * multiplier / 1_000_000 + output * prices["completion"] / 1_000_000
        else:
            if not payload["input"] or any(not isinstance(v, str) or len(v.encode()) + 1024 > context for v in payload["input"]):
                raise ValueError("Embedding input exceeds the configured context bound")
            amount = len(payload["input"]) * context * prices["prompt"] * multiplier / 1_000_000
        payload["provider"] = {"max_price": prices, "allow_fallbacks": False}
        if kind == "chat": payload["provider"]["require_parameters"] = True
        return amount + prices["request"] + .000001

    async def request(self, kind, endpoint, payload):
        if self.authorize:
            try:
                self.authorize()
                self.access_denied = False
            except Exception:
                self.access_denied = True
                raise RuntimeError("Source access changed; model dispatch stopped") from None
        reserve = self.price_reservation(kind, payload)
        base = validate_model_url(self.config[kind + "_url"], self.config.get("external_provider"))
        headers = {}
        key_file = self.config.get(kind + "_key_file")
        if key_file:
            headers["Authorization"] = "Bearer " + Path(key_file).read_text().strip()
        receipt = self.reserve(payload["model"], endpoint, reserve)
        receipt["status"] = "failed_cost_unknown"
        failure = "Configured model request failed; inspect the private receipt"
        try:
            async with httpx.AsyncClient(timeout=MODEL_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
                response = await client.post(base + endpoint, json=payload, headers=headers)
                receipt["http_status"] = response.status_code
                response.raise_for_status()
                result = response.json()
                usage = result.get("usage", {})
                cost = float(usage.get("cost", reserve))
                if cost < 0 or not np.isfinite(cost):
                    raise ValueError("Invalid model billing response")
                self.cost += cost - reserve
                receipt.update(status="invalid_response", usage=usage, charged_or_estimated_usd=cost)
                if self.config.get("cost_ceiling", {}).get(kind) and cost > reserve:
                    raise ValueError("Provider billed above the reserved price ceiling")
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
        except Exception as error:
            self.failed = True
            receipt["error_type"] = type(error).__name__
            raise RuntimeError(failure) from None
        finally:
            self.record_receipt(receipt)

    async def extract(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + (history_messages or []) + [{"role": "user", "content": prompt}]
        return await self.request("chat", "/chat/completions", {"model": self.config["chat_model"], "messages": messages, "max_tokens": 8192})

    async def embed(self, texts):
        return await self.request("embedding", "/embeddings", {"model": self.config["embedding_model"], "input": texts})


def engine(directory, models, *, building=False, workspace=None):
    async def embed(texts):
        return await models.embed(texts)

    async def extract(*args, **kwargs):
        if not building:
            raise RuntimeError("Serving cannot generate answers or extract new entities")
        return await models.extract(*args, **kwargs)

    return LightRAG(
        working_dir=str(directory), workspace=workspace or "company-" + digest(str(Path(directory).resolve()))[:20],
        embedding_func=EmbeddingFunc(embedding_dim=models.config["dimensions"], max_token_size=8192, model_name=models.config["embedding_model"], func=embed),
        llm_model_func=extract, llm_model_name=models.config["chat_model"],
        llm_model_max_async=1, embedding_func_max_async=1, max_parallel_insert=1,
        default_embedding_timeout=MODEL_TIMEOUT_SECONDS,
        enable_llm_cache=building, enable_llm_cache_for_entity_extract=building,
    )


async def close_engine(rag):
    for function in set(rag._build_global_config()["role_llm_funcs"].values()):
        if hasattr(function, "shutdown"):
            await function.shutdown()
    await rag.embedding_func.func.shutdown()
    await rag.finalize_storages()


def index_config(models, corpus=None):
    config = {"engine": ENGINE_VERSION, "prompts_sha256": digest({k: v for k, v in PROMPTS.items() if isinstance(v, (str, list))}), "chunk_characters": CHUNK_CHARACTERS, "models": {k: models.config[k] for k in ("chat_model", "embedding_model", "dimensions")}}
    if corpus and any(r.get("chunking") == "slack-messages-v1" for r in corpus.records.values()):
        config["slack_chunking"] = {"version": 1, "target_characters": 3200, "root_context_characters": 1600}
    return config


async def build(snapshot, directory, models, previous=None):
    corpus = Corpus(snapshot)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    (directory / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False))
    workspace = "company-" + digest(str((directory / "index").resolve()))[:20]
    old = None
    if previous:
        previous = Path(previous)
        prior = json.loads((previous / "snapshot.json").read_text())
        ready = json.loads((previous / "ready.json").read_text())
        if prior["principal"] != corpus.principal or ready["snapshot"] != digest(prior):
            raise ValueError("Incremental builds require the same reader and a matching snapshot")
        # Native deletion can re-summarize another withdrawn document before its deletion.
        if ready["config"] == index_config(models, corpus) and all(corpus.document_ids.get(row["source_id"]) == "doc-" + digest(row) for row in prior["records"]):
            old = Corpus(prior)
            shutil.copytree(previous / "index", directory / "index")
            (directory / "index" / ready.get("workspace", "company-context")).rename(directory / "index" / workspace)
    rag = engine(directory / "index", models, building=True, workspace=workspace)
    await rag.initialize_storages()
    try:
        if old:
            await verify_chunks(old, rag)
        for position, (source_id, source) in enumerate(corpus.records.items(), 1):
            if old and old.document_ids.get(source_id) == corpus.document_ids[source_id]:
                continue
            chunks = [corpus.render(corpus.chunks[key]) for key in corpus.source_chunks[source_id]]
            await rag.ainsert_custom_chunks(source["text"], chunks, doc_id=corpus.document_ids[source_id])
            if getattr(models, "failed", False) or getattr(models, "access_denied", False):
                raise ValueError("Model processing failed; this candidate cannot be published")
            progress = {"source_id_hash": digest(source_id), "completed_sources": position, "total_sources": len(corpus.records), "model_calls": models.calls}
            (directory / "progress.json").write_text(json.dumps(progress))
        await verify_chunks(corpus, rag)
    finally:
        await close_engine(rag)
    ready = {"snapshot": corpus.version, "principal": corpus.principal, "source_count": len(corpus.records), "chunk_count": len(corpus.chunks), "config": index_config(models, corpus), "model_calls": models.calls, "workspace": workspace}
    (directory / "ready.json").write_text(json.dumps(ready, ensure_ascii=False))
    return ready


async def open_index(directory, models):
    directory = Path(directory)
    ready = json.loads((directory / "ready.json").read_text())
    corpus = Corpus(json.loads((directory / "snapshot.json").read_text()))
    if ready["snapshot"] != corpus.version or ready["config"] != index_config(models, corpus):
        raise ValueError("Snapshot or model configuration differs from the completed build")
    rag = engine(directory / "index", models, workspace=ready.get("workspace", "company-context"))
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
    for identity in corpus.document_ids.values():
        status = await rag.doc_status.get_by_id(identity)
        if not status or status.get("status") != "processed":
            raise ValueError("Source extraction did not complete successfully")
        if sorted(status.get("chunks_list", [])) != sorted(corpus.native_document_chunks[identity]):
            raise ValueError("Document chunk membership differs from the authorized snapshot")
    for rows in (await rag.chunk_entity_relation_graph.get_all_nodes(), await rag.chunk_entity_relation_graph.get_all_edges()):
        for row in rows:
            sources = set(row.get("source_id", "").split("<SEP>")) - {""}
            if not sources or not sources <= corpus.native_chunks.keys():
                raise ValueError("Graph contains missing or withdrawn source references")


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
    if not corpus.records:
        result["retrieval"].update(actual_mode=mode, status="ready")
        result["evidence_status"] = "no_matches"
        return result
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
