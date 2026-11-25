# Company context service

TL;DR: Run company source storage and LightRAG inside the company environment. The public book MCP remains separate. This service returns original company evidence, not generated business conclusions. The first deployment is a single-reader snapshot; it is not a company-wide authorization system.

## Data boundary

- Source records, embeddings, graphs, extraction caches and receipts stay on the company host. Never deploy this directory's runtime data to the public book server.
- Internal OpenAI-compatible models are supported. External model processing is disabled unless `external_provider` explicitly equals `openrouter`; only `https://openrouter.ai/api/v1` is then permitted as an external endpoint. OpenRouter receives source text during extraction/embedding and queries during retrieval. This is not fully on-premises inference.
- Model credentials and the MCP bearer token are read from files, never command arguments. The MCP token file must have mode `600`.
- The server binds to loopback. Reach it through an authenticated company tunnel or an internal TLS proxy. Do not expose it publicly.
- DNS checks reject public model destinations by default. Network egress controls must enforce the same policy; application DNS checks alone cannot prevent DNS rebinding.
- A token grants access to the whole explicitly selected snapshot for its named reader. Original source permissions are not rechecked on each query. Do not share that token or aggregate different readers' material. Rebuild the snapshot after permission revocation or deletion, and retire the previous service and snapshot according to company retention policy.

## Source snapshot

The input is JSON with `principal` and `records`. Every record has `source_id`, `source_type` (`slack`, `docs`, `jira`, `notion`, `user_update`), `url`, `title`, `author`, `occurred_at`, `updated_at`, `collected_at`, `record_kind`, and `text`.

- `source_id` is the source system's stable identity, namespaced by system/workspace. Duplicate identities within a snapshot are rejected.
- `text` preserves the connector's fetched text representation. Chunk offsets address this captured representation, not original provider byte offsets.
- Unknown author, event time and edit time are explicit `null`. Collection time is required. Known times include timezone information; an edit timestamp is not an event timestamp.
- `record_kind` is `source` or `summary`. Summaries also require `derived_from`, containing source URLs. Neither kind establishes that every assertion is true.
- Optional `coverage` describes omitted comments, child pages, attachments or other uncollected material. Do not represent a selected excerpt as the entire source.
- Credentials must be excluded before collection. Customer records, registrations, private evaluation questions and runtime files must not enter Git.
- The pilot accepts 1–1,000 records, each at most 200,000 characters. New snapshot directories are required for changes; an existing directory is never overwritten. Failed builds retain their evidence and do not acquire a `ready.json` marker.

## Extraction and retrieval

- LightRAG 1.5.7 uses its default extraction prompt, entity types and extraction limits. No reviewed ontology, customer schema or manual graph is injected.
- Original text is split into 1,600-character passages with provenance attached. This is a baseline; a boundary may split a sentence. Retrieval quality must be measured before changing it.
- Native custom-chunk insertion preserves document-scoped source identities. The completed build and serving startup both validate expected source content and identities after LightRAG's encoding normalization. The immutable snapshot retains original whitespace and HTML entities for exact source reads.
- `search_company_context(query, mode="mix", k=5)` accepts natural-language questions. `mix` and `naive` use the same source snapshot. Retrieval does not silently fall back and does not generate an answer.
- Context version 3 adds `graph` descriptions to `mix` responses without reducing the original `results`. Entity and relationship descriptions alternate in native rank order within a separate 6,000-token budget. `graph_coverage` reports omitted and untraceable records; it is not completeness of the company corpus. Oversized descriptions are omitted intact, not cut into partial assertions. `naive` returns an empty graph.
- Every graph record is marked model-derived. Its `sources[].read_source` arguments lead to exact originals through `get_company_source`, including passages outside the first `k` chunks. Source metadata retains actor, event/edit/collection times, summary attribution and coverage. Unresolved references are counted; records with no traceable original are excluded. Graph extraction timestamps are not substituted for source event times.
- `get_company_source(chunk_id)` returns the captured passage, metadata and previous/next chunk handles for reading the surrounding source or a full thread. Handles never cross source identities. `company_index_status()` reports the snapshot identity, reader and collection coverage.
- Returned passages are candidates requiring source review. A match is not proof of purchase intent, a graph edge is not an established fact, and no matches do not prove the company has no evidence.

## Configuration and execution

Create a private JSON configuration on the company host. Set `chat_url`, `embedding_url`, `chat_model`, `embedding_model` and `dimensions` explicitly. Optional `chat_key_file` and `embedding_key_file` point to credential files. For authorized OpenRouter use, add `"external_provider": "openrouter"` and use its exact API base URL for both endpoints. There are no default public model endpoints.

From the code root:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r company_context/requirements.txt
.venv/bin/python -m company_context.server build --config /private/models.json --input /private/snapshot.json --directory /private/releases/pilot-1 --max-model-calls 200 --budget-usd 5
.venv/bin/python -m company_context.server serve --config /private/models.json --directory /private/releases/pilot-1 --token-file /private/mcp-token --port 8766 --max-model-calls 1000 --budget-usd 5
```

The model budget reserves $0.10 per chat request and $0.02 per embedding request, replacing each reservation with reported usage cost when available. It is a conservative stopping estimate, not a provider-enforced billing cap. No retries are automatic. A failed request stops further model calls. Receipts contain model, status and usage; no request bodies or credentials. Usage and failed-request state are restored from the snapshot's receipt ledger after restart. Inspect failed receipts before explicitly starting a new attempt; restarting does not reset the budget. Production usage limits require operator configuration after pilot acceptance.

For Codex access, run `company_context.proxy` over authenticated SSH to the company host, passing only `--token-file /private/mcp-token`. The proxy reads the token on that host and connects to the loopback HTTP service. No token or company corpus is copied into the client configuration.

## Advisor integration and acceptance

1. Read company evidence through the internal MCP, including dates, authors, coverage and conflicting records.
2. Keep confirmed observations, hypotheses and missing information separate. Source summaries do not automatically become customer statements.
3. Ask the public book MCP generic conceptual questions, excluding customer names, internal facts, quotations, prices and identifiers. Read returned book passages before citing them.
4. Combine both evidence sets in the authorized user's session. Name the next action, the person/role to ask, the question, and the answer that would change the decision. Preserve unknown buying authority and rejection reasons.
5. Record only user-confirmed actions/outcomes when explicitly authorized; do not index an advisor's suggestion as an accomplished action.

Before a material decision, the advisor reads relevant graph-linked originals and classifies their applicability as `same_scope_replacement`, `different_scope`, `coexisting_perspectives`, or `unresolved`. Replacement requires the same subject and decision scope plus evidence that the earlier position changed or was withdrawn. Newer timestamps alone do not establish authority; different stakeholder opinions coexist. Separate budget paragraphs, customers and internal versus customer decisions remain separate. These are caller-reviewed classifications with source citations, not automated server verdicts or a new graph ontology. The installed Product Advisor skill and both MCP transports carry this workflow.

Freeze evaluation queries and expected source identities before indexing or tuning. Include several opportunities, unnamed semantic questions, changed decisions, conflicting perspectives and unsupported claims. Compare `naive` and `mix` against the same expected sources. Retrieval tests are not an answer-quality verdict: separately review whether the advisor invents causes, conflates a pilot with a purchase, or misses contradictory evidence.

## Checks and remaining acceptance

```sh
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest company_context -q
```

Synthetic tests exercise native LightRAG stores, original source membership, the actual HTTP MCP protocol, token rotation and provenance. They mock model responses and do not establish real semantic quality. Company-host model calls, four-opportunity retrieval, advisor decisions and the actual user connection require separate recorded acceptance. Source synchronization is a complete-snapshot import at this stage; unattended Slack/Docs/Jira/Notion synchronization is not implemented.

## Work-intent experiment

`compare_context.py` compares vector chunks, graph-selected chunks, and graph descriptions with chunks using a frozen private protocol. It copies an existing snapshot, uses the same answer model and book principles for every arm, and enforces the same maximum company-context token budget. Graph descriptions replace part of the chunk budget. Original extraction and the serving service are unchanged.

Run it on the company host with `--experiment`, `--snapshot`, and `--config`. The new experiment directory must contain `protocol.json` and its exact SHA-256 in `protocol.sha256`. Questions, context budgets, arm order, book principles and the review rubric must be frozen before model calls. Company questions, model requests, provider responses and manual reviews belong only in that private directory. Failed runs are retained; retries require a separately identified run. Generation is bounded to 80 model calls and a $3 conservative stopping estimate, including embeddings.

Review context connections, changed decisions and counterevidence, action relevance, and unsupported claims separately. Citation identity does not establish that a claim is supported. The experiment retains graph-to-original-source references for inspection. Its single-pass answers and same-agent manual review do not establish statistical superiority, independent answer quality, or the behavior of a different final answer model. The production whole-query keyword policy is held fixed; this experiment does not evaluate native keyword extraction or agentic follow-up retrieval.
