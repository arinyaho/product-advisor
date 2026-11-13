# Architecture

product-advisor helps a product team reason about pricing, willingness to pay (WTP), product-market fit, go-to-market, packaging, and sales by grounding Claude's advice in a reference book served from a PDF. It has two parts: a Claude Code skill that decides when and how to consult the book, and an MCP server that searches and reads the book. The server holds the book and nothing about the team; customer, deal, and hypothesis records stay in the team's own systems.

## Components

| Component | Runs on | Responsibility |
|---|---|---|
| Skill `product-advisor` | Each user's Claude Code | Loads when a conversation turns to pricing, WTP, PMF, GTM, packaging, or sales. Maps the situation to the book's frameworks with a concept map, tells Claude which chapters to read and which terms to search, points to team records in Notion, Google Drive, and Slack, and defines the one-page output for evaluating a project handed down without a business case |
| MCP server `product-advisor` | One Docker host | Extracts the PDF, builds a search index and section summaries, and serves `list_sections`, `search_book`, and `get_section` over HTTP to holders of a valid key |
| OpenRouter | External API | Embeds the book and each query (`qwen/qwen3-embedding-4b`) and writes section summaries once (`anthropic/claude-haiku-5.5`) |
| Team systems (Notion, Drive, Slack, a CRM) | Their own services | Hold customers, deals, interviews, and hypotheses. Each user reads them with their own access rights; the server never sees them |

The server keeps no business state. Its data directory holds the PDF, the key store (access control), and two caches derived from the PDF (vectors and summaries) that are rebuilt whenever the text or the method changes. It stores no queries, answers, or team information.

## Data flow

Indexing runs once per text version, in the background after the server starts:

```
book.pdf
  │  per page: text without superscript note markers, ligatures unfolded (NFKC), paragraphs kept as blank lines
  ▼
page text (Chapter 1 to the endnotes; front matter and index excluded)
  │  section boundaries at each heading's position in the text, found by matching the table-of-contents title
  ▼
section text (a leaf section whole; a parent section only its introduction before the first subsection)
  │  whole paragraphs grouped up to 200 words, cut at every section heading and every page break
  ▼
chunks ──────────────► embedded; used to rank pages and to name the section that matched
  │  60-word windows sliding by 30 words inside each chunk
  ▼
excerpt windows ─────► embedded; the passage an excerpt-scope key receives

section text ────────► one LLM summary per section with at least 40 words of its own text
endnote pages ───────► parsed per chapter into numbered notes, linked to the body's superscript markers
```

Searching combines two rankings of pages:

```
query ─┬─► BM25 over page tokens ───────────────┐
       └─► query embedding · chunk embeddings ──┤  best chunk per page
                                                ▼
                         reciprocal rank fusion (k = 60) → top k pages
                                                │
               full key: page text      excerpt key: the page's excerpt window closest to the query
                                                ▼
        { page, section of the matching chunk, text, sources: endnotes the page cites }
```

## MCP tools

| Tool | Input | Full scope returns | Excerpt scope returns |
|---|---|---|---|
| `list_sections` | none | Table of contents: id, level, title, PDF page range | Same |
| `search_book` | query (Korean or English, a situation or the book's terms), `k` (default 5) | Top k pages: page number, matching section, page text, cited endnotes; `semantic` tells whether embeddings were used | Same, with a passage of at most 60 words instead of the page text |
| `get_section` | section id, optional `start_page` | The section's text with page markers, ten pages per call, and a note on where to continue | Summaries of the section and every subsection under it, with page ranges |

Claude answers questions that span a chapter by combining these tools: the table of contents narrows the range, search finds the passages, and `get_section` reads the whole section. This is the reason the design needs no knowledge graph for a single book; see [experiments](experiments.md#synthesis-questions).

## Access control

Every request carries `Authorization: Bearer <key>`. A key has a name, an expiry, and a scope, and the server stores only its SHA-256 hash in `data/keys.json`. The server reads the key store on every request, so issuing, renewing, and revoking keys take effect without a restart.

| Scope | Intended for | Book text it can receive |
|---|---|---|
| `full` | People who own a copy of the book | Whole pages and whole sections |
| `excerpt` | Teammates who do not own a copy | 60-word passages per search hit and LLM-written section summaries |

The excerpt scope limits how much book text flows to people who do not own the book in ordinary use. It does not hide topics, and it is not a barrier against deliberate collection: many searches can gather many passages.

| Threat | Mitigation |
|---|---|
| Request without a valid key | Rejected with 401 before reaching the MCP handler |
| Leaked key | Expiry on every key; `revoke-key` takes effect on the next request |
| Leaked OpenRouter key | A key made only for this server, with a credit limit and a reset period set in OpenRouter |
| Key store read from disk | Only hashes are stored; the file is written with mode 0600 |
| Exposure of the container port | The port binds to 127.0.0.1; only the host's reverse proxy, which terminates HTTPS, reaches it |
| Team data passing through the server | None does; team systems are read by each user's Claude with that user's own rights |
| Queries sent to OpenRouter | Queries leave the network for embedding. The OpenRouter account's data policy admits only providers that do not retain data, which also excludes several embedding models |

## Failure handling

| Condition | Behavior |
|---|---|
| Index or summaries still being built | Search answers from BM25 alone with `"semantic": false`; excerpt-scope `get_section` asks the caller to use search meanwhile |
| OpenRouter unreachable or rate limited | Requests retry with backoff that honors `Retry-After`; search falls back to BM25; the background build retries until it succeeds |
| Summary request returns no content | The build fails loudly naming the section and the finish reason, and retries; summaries are requested with reasoning disabled, because reasoning can consume the whole token budget |
| Unknown section id | `get_section` returns an error that names `list_sections` |

## Caches

Both caches live in the data directory and are keyed by a hash of their inputs, so a change to the PDF, the extraction, the chunking, the prompt, or the model creates new files instead of reusing stale ones.

| Cache | File | Key |
|---|---|---|
| Vectors | `vectors-<model>-<hash>.npz` | Embedding model and the text of every chunk and excerpt window |
| Summaries | `summaries-<model>-<hash>.json` | Summary model, prompt, and every section's own text |

A first build takes about three minutes and costs a few cents in OpenRouter usage.

## Design decisions

| Decision | Reason |
|---|---|
| Hybrid BM25 and embedding search over chunks, no knowledge graph | One book's structure is already explicit in its table of contents; tool-using Claude covered every expected item of multi-section questions with search, the table of contents, and section reads. A graph earns its cost when it links many documents, which here would mean indexing team data |
| `qwen/qwen3-embedding-4b` without a query instruction | Found the expected chapter for all 11 Korean and 11 English test situations; local alternatives scored lower or needed a GPU on every machine |
| Chunks of whole paragraphs within one section and one page | Results and citations are pages, so a chunk that crossed a page break credited only its first page; within-page structure chunks ranked as well as page windows and name the matching section correctly |
| No section-title prefix on chunks | It did not improve ranking in a blind pooled evaluation and biases retrieval toward titles |
| Endnotes linked, not searched | A note alone is a citation without the claim it supports; returned as `sources`, it attributes the claim instead |
| One server, keys per person | Teammates install only the plugin; the book, the OpenRouter key, and the caches stay on one host |
| Section summaries for excerpt keys | Lets teammates without the book answer chapter-wide questions without receiving the chapter's text |
| No team data in the server | Keeps the server stateless and leaves every team record under its original system's permissions |

## Files

| Path | Content |
|---|---|
| `mcp/server.py` | The server: extraction, indexing, search, summaries, keys, HTTP app, key commands |
| `mcp/test_server.py` | Tests against the real PDF and OpenRouter |
| `mcp/Dockerfile`, `docker-compose.yml` | Container build and service definition |
| `scripts/deploy.sh` | Deploys to and operates a Docker host over ssh |
| `skills/product-advisor/SKILL.md` | The skill |
| `.claude-plugin/`, `.mcp.json` | Plugin manifest, marketplace entry, and the MCP connection users install |
| `eval/` | Evaluation scripts and recorded results; see [eval/README.md](../eval/README.md) |
| `docs/` | This document, [deployment](deployment.md), and [experiments](experiments.md) |
