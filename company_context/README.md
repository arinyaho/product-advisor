# Company context service

TL;DR: Run company source storage and LightRAG inside the company environment. A single-reader Slack pipeline captures accessible public/private channels, excludes DMs, preserves conversations, and publishes only validated graph snapshots. The public book MCP stays separate. Configure the reader, collection start, model budget and private file paths before enabling the hourly units. This is not a multi-user authorization system.

## Data boundary

- Source records, embeddings, graphs, extraction caches and receipts stay on the company host. Never deploy this directory's runtime data to the public book server.
- Internal OpenAI-compatible models are supported. External model processing is disabled unless `external_provider` explicitly equals `openrouter`; only `https://openrouter.ai/api/v1` is then permitted as an external endpoint. OpenRouter receives source text during extraction/embedding and queries during retrieval. This is not fully on-premises inference.
- Model credentials and the MCP bearer token are read from files, never command arguments. The MCP token file must have mode `600`.
- The server binds to loopback. Reach it through an authenticated company tunnel or an internal TLS proxy. Do not expose it publicly.
- DNS checks reject public model destinations by default. Network egress controls must enforce the same policy; application DNS checks alone cannot prevent DNS rebinding.
- A token grants access to the snapshot for its named reader. Do not share it or aggregate different readers' material. With `--access-state`, every evidence read checks the latest channel inventory; revoked access or an inventory older than two hours blocks retrieval. This is hourly ACL polling, not a source-provider check on each query. With `--sync-state`, a known source revision/removal blocks the old snapshot until its replacement is validated. Historical private artifacts remain on disk until an operator applies the company's retention policy; exclusion from retrieval is not erasure from backups or an external model provider.

## Source snapshot

The input is JSON with `principal` and `records`. Every record has `source_id`, `source_type` (`slack`, `docs`, `jira`, `notion`, `user_update`), `url`, `title`, `author`, `occurred_at`, `updated_at`, `collected_at`, `record_kind`, and `text`.

- `source_id` is the source system's stable identity, namespaced by system/workspace. Duplicate identities within a snapshot are rejected.
- Slack records require API-derived `slack_conversation` metadata with the matching `id`, boolean `is_im`, `is_mpim`, and `is_private` fields, and `is_member=true` for private channels. One-to-one and group DMs, direct-DM source links in attributed summaries, and unknown conversation types are rejected before snapshot creation or model calls. Legacy snapshots without conversation classification must be re-collected and rebuilt. Public channels need not be joined; private channels must be accessible to the named reader. Collection must request only `public_channel,private_channel` and recheck these flags before reading message bodies.
- `text` preserves the connector's fetched text representation. Chunk offsets address this captured representation, not original provider byte offsets.
- Unknown author, event time and edit time are explicit `null`. Collection time is required. Known times include timezone information; an edit timestamp is not an event timestamp.
- `record_kind` is `source` or `summary`. Summaries also require `derived_from`, containing source URLs. Neither kind establishes that every assertion is true.
- Optional `coverage` describes omitted comments, child pages, attachments or other uncollected material. Do not represent a selected excerpt as the entire source.
- Credentials must be excluded before collection. Customer records, registrations, private evaluation questions and runtime files must not enter Git.
- The memory bounds are 1–100,000 records, two million characters per source and 100 million characters per snapshot. Exceeding a bound fails the entire build rather than truncating coverage. New snapshot directories are required for changes; an existing directory is never overwritten. Failed builds retain their evidence and do not acquire a `ready.json` marker.

## Extraction and retrieval

- LightRAG 1.5.7 uses its default extraction prompt, entity types and extraction limits. No reviewed ontology, customer schema or manual graph is injected.
- Imported legacy sources retain their 1,600-character chunks. New Slack captures group each thread separately; unthreaded channel messages form timeline windows separated by a 30-minute gap or UTC date boundary. Timeline proximity is not a claim of a common subject. Threads and timelines never mix, nor do different channels.
- Slack chunks target 3,200 characters and end at message boundaries. A single oversized message is split at a paragraph/sentence boundary when one is available, otherwise at the hard character bound. Captured text and offsets reconstruct exactly; no LLM summary replaces the originals. Each included message retains its author ID, event/edit times and permalink. Later chunks include up to 1,600 characters of the original opening message as explicitly bounded context, plus `root_chunk_id` and previous/next handles. An opening message outside the capture interval is marked missing, never invented. These are character bounds, not claimed token counts or a measured retrieval optimum.
- Native custom-chunk insertion preserves document-scoped source identities. Build completion and serving startup validate exact chunk identities/content, processed document status, and every graph reference against the authorized snapshot. Each release has a separate LightRAG workspace so in-process storage caches cannot cross releases. The immutable snapshot retains original whitespace and HTML entities for exact source reads.
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

The model budget reserves $0.10 per chat request and $0.02 per embedding request, replacing each reservation with reported usage cost when available. It is a conservative stopping estimate, not a provider-enforced billing cap. The append-only receipt ledger flushes a pending reservation to disk before dispatch and a final event afterward; restart restores the latest event per call, including legacy single-event receipts. Reservations reread the ledger under an exclusive file lock so another service process cannot reuse a call ID or dispatch over an unfinished reservation. This service requires a Unix host and a local filesystem supporting `flock`. Failed, cancelled, or unfinished requests stop further model calls until operator review. Receipts contain model, status and usage; no request bodies or credentials. Inspect failed receipts before explicitly starting a new attempt; restarting does not reset the budget. Production usage limits require operator configuration after pilot acceptance.

Receipts also retain the HTTP status and exception type without provider response bodies. After investigating a failure, an operator can append a receipt for the same call with `status=reviewed_failure_cost_reserved` and a nonempty `operator_review`, retaining its full conservative cost. The original failure remains in the ledger. This is never automatic; restarting after that explicit review permits new calls within the remaining cumulative budget. A failed sync candidate still requires separate operator review and a new attempt identity.

For Codex access, run `company_context.proxy` over authenticated SSH to the company host with `--token-file /private/mcp-token`. If the service uses a different port, pass the same `--port` to the proxy; both default to `8766`. The proxy reads the token on that host and connects to the loopback HTTP service. No token or company corpus is copied into the client configuration.

## Advisor integration and acceptance

1. Read company evidence through the internal MCP, including dates, authors, coverage and conflicting records.
2. Keep confirmed observations, hypotheses and missing information separate. Source summaries do not automatically become customer statements.
3. Ask the public book MCP generic conceptual questions, excluding customer names, internal facts, quotations, prices and identifiers. Read returned book passages before citing them.
4. Combine both evidence sets in the authorized user's session. Name the next action, the person/role to ask, the question, and the answer that would change the decision. Preserve unknown buying authority and rejection reasons.
5. Record only user-confirmed actions/outcomes when explicitly authorized; do not index an advisor's suggestion as an accomplished action.

Before a material decision, the advisor reads relevant graph-linked originals and classifies their applicability as `same_scope_replacement`, `different_scope`, `coexisting_perspectives`, or `unresolved`. Replacement requires the same subject and decision scope plus evidence that the earlier position changed or was withdrawn. Newer timestamps alone do not establish authority; different stakeholder opinions coexist. Separate budget paragraphs, customers and internal versus customer decisions remain separate. These are caller-reviewed classifications with source citations, not automated server verdicts or a new graph ontology. The installed Product Advisor skill and both MCP transports carry this workflow.

Freeze evaluation queries and expected source identities before indexing or tuning. Include several opportunities, unnamed semantic questions, changed decisions, conflicting perspectives and unsupported claims. Compare `naive` and `mix` against the same expected sources. Evaluation output contains private evidence and is created with owner-only permissions regardless of the shell's umask. Retrieval tests are not an answer-quality verdict: separately review whether the advisor invents causes, conflates a pilot with a purchase, or misses contradictory evidence.

## Checks and remaining acceptance

```sh
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest company_context -q
```

Synthetic tests exercise native LightRAG stores, exact source membership, changes/removals in an isolated candidate, the actual HTTP MCP protocol, ACL expiry/revocation, token rotation, conversation boundaries and capture-to-publication orchestration. They mock model responses and do not establish real semantic quality. Company-host model calls, four-opportunity retrieval, advisor decisions and both clients' actual connections require separate recorded acceptance. Automatic synchronization covers Slack message text only; Docs/Jira/Notion remain static selected supplements. Files and linked documents are not downloaded by this collector.

## Channel capture and hourly synchronization

1. `collect_slack` enumerates `public_channel,private_channel`, validates API flags before reading bodies, and pins the authenticated reader/workspace. Public channels need not be joined; private channels require membership. It scans parent history to discover recent replies under older roots, stores only messages inside the frozen interval, paginates threads, and checkpoints SQLite pages and cursors transactionally. Raw storage is private on the company host. Recognizable credential-bearing messages are omitted; the regex is not a guarantee that all sensitive text is detected.
2. Initial collection resumes the same frozen interval. A failed or incomplete channel prevents export. Coverage reports distinguish channels, complete/failed channels, messages and credential omissions. Collection does not call a model or publish a graph. Slack reads are not an atomic point-in-time snapshot: source mutations during pagination are reconciled in subsequent cycles.
3. Subsequent cycles re-read parent history and compare each thread's reply count/latest-reply signature. Unchanged replies can be reused from the prior completed capture for at most 24 hours, after which they are fully re-read to detect edits that Slack does not expose in a change cursor. Deleted roots/replies disappear from the next complete capture. Revoked channels are separately blocked through the hourly access inventory. New captures do not copy unobserved old rows into the exported snapshot.
4. Changed source content produces a new document identity. The candidate copies the previous completed index, removes superseded documents through LightRAG's native deletion/reconstruction, inserts changed/new sources, and verifies all graph references before publishing. Unchanged source collection provenance stays stable. First migration from the legacy pilot uses a fresh graph. Failed deletion, extraction, budget checks or acceptance leave the candidate unpublished; no automatic paid replay occurs.
5. `sync` atomically switches `current`, restarts the loopback MCP service, and checks its reported snapshot and an exact source through authenticated MCP calls. The service must use `--directory /private/current --access-state /private/access.json --sync-state /private/sync.json`. A failed restart/acceptance requires operator repair; there is no automatic rollback to withdrawn evidence.
6. The included user-systemd timers trigger hourly. A long-running job is not overlapped; hourly triggering does not promise one-hour end-to-end freshness. The ACL job runs independently, including during collection/indexing. The single append-only `sync-model-receipts.jsonl` spans all sync generations: the configured budget is cumulative, not reset hourly. Review retained failures before authorizing another paid attempt.

Initial capture and standalone export:

```sh
.venv/bin/python -m company_context.collect_slack --token-file /private/slack-token --database /private/slack-initial.sqlite --since 2025-11-26T00:00:00+09:00 --until 2026-11-26T00:00:00+09:00
.venv/bin/python -m company_context.slack_snapshot --database /private/slack-initial.sqlite --output /private/snapshot.json
```

Adapt `company_context/systemd/` to the actual host paths, frozen start date, bootstrap capture, optional static non-Slack supplement and approved cumulative model budget. Copy the service/timer files to the reader's `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, perform one manual access refresh and sync check, then enable the two timers. Keep the active graph outside Git. No Slack message, source export, API credential, runtime ledger or customer evaluation belongs in this repository.

## Work-intent experiment

`compare_context.py` compares vector chunks, graph-selected chunks, and graph descriptions with chunks using a frozen private protocol. It copies an existing snapshot, uses the same answer model and book principles for every arm, and enforces the same maximum company-context token budget. Graph descriptions replace part of the chunk budget. Original extraction and the serving service are unchanged.

Run it on the company host with `--experiment`, `--snapshot`, and `--config`. The new experiment directory must contain `protocol.json` and its exact SHA-256 in `protocol.sha256`. Questions, context budgets, arm order, book principles and the review rubric must be frozen before model calls. Company questions, model requests, provider responses and manual reviews belong only in that private directory. Failed runs are retained; retries require a separately identified run. Generation is bounded to 80 model calls and a $3 conservative stopping estimate, including embeddings.

Review context connections, changed decisions and counterevidence, action relevance, and unsupported claims separately. Citation identity does not establish that a claim is supported. The experiment retains graph-to-original-source references for inspection. Its single-pass answers and same-agent manual review do not establish statistical superiority, independent answer quality, or the behavior of a different final answer model. The production whole-query keyword policy is held fixed; this experiment does not evaluate native keyword extraction or agentic follow-up retrieval.
