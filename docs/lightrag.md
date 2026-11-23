# LightRAG and the concept map

TL;DR: This document records the earlier single-book concept-map implementation. The active three-book implementation is described in [the library guide](three-book-library.md); it uses native model extraction and returns source evidence without manual framework cards. Historical scores below do not evaluate the three-book release.

TL;DR: `search_book` and `retrieve_decision_context` use LightRAG 1.5.7 and an OpenRouter reranker. `mix` searches the graph and book; `naive` searches book vectors. Both rerank book passages. Framework guidance is not a customer diagnosis. The frozen reranker regression passed 19/20 cases; see [the evaluation](rerank-evaluation.md).

## Knowledge and retrieval

- `mcp/concept_map.json` holds 12 reviewed framework records: aliases, the decision, application conditions, questions, cautions, PDF sources, and related frameworks. `curated_application` distinguishes our application guidance from quoted book claims.
- `advisor_graph.py` compiles the map into framework and decision nodes, application links, related-framework links, and the existing 200-word book chunks. Every node and edge points to a real book chunk and PDF page.
- Both `naive` and `mix` receive exactly the existing paragraph-based chunks inside section and page boundaries. Custom-KG insertion bypasses LightRAG's default text splitter. Chunk text and source-page identities are tested, and search results preserve the section of the matched chunk when a page contains multiple sections.
- Repeated margin blocks containing a page number are detected from PDF coordinates. Only independent chunks exactly matching those repeated blocks are excluded from the searchable index. Original chunks, mixed body/margin chunks, section headings, PDF pages, and existing embedding caches are preserved. For the current PDF, 12 of 544 original chunks are excluded, leaving 532 searchable chunks.
- LightRAG's `naive` mode searches book vectors. `mix` also searches framework nodes and their relationships. No LLM extracts new facts or generates final advice in this path.
- The existing embedding model and all matching book vectors are reused. New framework and relationship vectors are cached separately; initialization refuses to embed more than 150 previously uncached texts per process. Model, book text, library version, and map content determine the LightRAG index directory.
- Queries and request context are embedded by OpenRouter but never added to persistent vector caches, the graph, or the book. Caller-supplied facts and unknowns are not verified by this service. The agent must preserve user corrections and distinguish evidence from inference.
- `retrieval.actual_mode` reports `legacy` while LightRAG is unavailable or building. This fallback uses the previous book search and does not claim graph retrieval.
- Excerpt keys receive at most 60 words of book text per result, including through the new decision-context tool. Framework guidance is original summary/application text.

## Candidate selection and relevance

Structured decision-context requests use the explicit decision as the graph-search focus. The whole context remains the book-vector query. Plain book questions use the question itself. There is no application-side top-three framework gate and no list of framework names selected before LightRAG searches its graph.

LightRAG uses its default candidate ceilings (`top_k=40`, `chunk_top_k=20`), then packs candidates within 7,000 context tokens, including ceilings of 2,500 entity tokens and 2,000 relationship tokens. The response's `k` limits returned book pages, not the internal candidate pool.

Appearing in the graph is not sufficient for a framework recommendation. OpenRouter's `POST /api/v1/rerank` scores the retrieved framework descriptions, with the explicit decision first and the caller-supplied context following it. `RERANK_MODEL` defaults to `cohere/rerank-v3.5`; the adapter reuses `OPENROUTER_API_KEY`. The previous 60/40 cosine weighting and score-drop grouping have been removed.

`RETRIEVAL_MIN_SIMILARITY=0.5` remains the initial Qwen vector-search floor. It is separate from reranker scores. `MIN_FRAMEWORK_RERANK_SCORE=0.2` filters framework cards, while `MIN_RERANK_SCORE=0.15` filters book passages. A development-only scan showed that one shared floor either retained unwanted framework cards or removed labeled book evidence. These separate floors are model- and text-type-specific, not probabilities. No fixed framework-count limit is applied; all above-floor cards remain eligible until the response budget is reached.

The retrieved book passages and existing curated source passages for candidate frameworks are deduplicated, with footer-only chunks excluded. LightRAG's official `process_chunks_unified` function then applies reranking, the minimum passage score, the native 20-chunk ceiling, and the token budget. The initial `aquery_data` call deliberately disables reranking: reranking happens once after source merging, rather than twice. A `mix` request makes up to two rerank calls (cards and passages); `naive` makes up to one. No additional query embedding batch is required for selection. Chunk text, boundaries, page identities, and cached book embeddings remain unchanged.

`retrieve_decision_context` packs the final JSON, including framework descriptions and evidence, within 7,000 tokens using LightRAG's tokenizer. The allowance is a ceiling, not a requirement to fill space. Production queries and provider rerank scores are not written to caches.

The [earlier cosine-selector evaluation](retrieval-evaluation.md) remains a historical record. The [reranker evaluation](rerank-evaluation.md) passed 19/20 known regression cases, retained all 17 required framework memberships, and left two unwanted framework memberships in one boundary case. The earlier selector passed 20/20 after repairs on that same set; this comparison does not establish that the reranker has better unseen-question quality.

The rerank adapter sends a single request with a 30-second network timeout and no automatic retry. Invalid, duplicate, incomplete, or out-of-range result indices/scores are rejected. LightRAG normally falls back to the original chunks when a reranker fails; the application detects that failure and reports the existing explicit `legacy`/`unavailable` fallback instead of claiming successful reranking. `retrieval.rerank` and `retrieval.rerank_model` report completion and the configured model on successful retrieval.

LightRAG informational query logging is disabled because its keyword fields now carry caller-supplied intent. Query text and query embeddings remain transient.

## Traced proposal-review regression

The original structured proposal-review request ranked `value_communication` fourth at 0.6541 when all background facts, unknowns, and constraints were embedded together. The old top-three seed gate dropped it just below `failure_patterns` at 0.6548. The explicit decision alone ranked value communication first at 0.7239. It was present in the map and index; the loss occurred in query routing, before graph retrieval.

Regression checks retain the original failure: a footer-only result occupied a returned page, and the value framework was excluded by price-heavy background. The corrected path searches broadly using explicit intent and token budgets, while retaining relevance filtering and the existing source chunks. `eval/trace_retrieval.py` can inspect candidate settings, graph entity names, framework ids, source pages, and footer candidates without persisting customer context or query vectors.

The final local MCP check used the real book, current provider embeddings, and excerpt scope. The original structured proposal context returned `value_communication`; an unrelated cooking request returned no framework and no book passage. Both checks reported actual `mix` retrieval and respected the 60-word excerpt cap. These are bounded regression checks, not a general precision estimate or proof that every related framework must be returned.

## Operating the index

The container builds LightRAG in the background after the book-vector cache loads. It prints `LightRAG index ready` after storage commits; the deployment script waits for this and `section summaries ready`. Existing book text, keys, and caches remain in the mounted data directory. The reviewed graph is rebuilt into a new directory when its inputs change. A completed marker is written only after insertion succeeds; a partial build is not served.

This book-sized deployment uses LightRAG's local NetworkX, JSON, and NanoVectorDB stores in one process. It does not require a separate graph database. Reassess the storage choice before adding many books or multiple server workers.

## Verification

```sh
python -m unittest discover -s mcp -p 'test_advisor_graph.py' -v
DATA_DIR=/path/to/cached/book/data python -m unittest discover -s mcp -p 'test_decision_context.py' -v
```

The first suite uses the real LightRAG library with synthetic text and deterministic embeddings. It checks source identities, graph retrieval, book-vector reuse, invalid links, and absence of persisted customer markers. The second exercises the MCP HTTP boundary with a real PDF and a mocked retriever, including authentication, excerpt restrictions, request facts/unknowns, mode selection, validation, and fallback reporting. These checks do not establish retrieval quality on real customer questions.

## Initial bounded comparison

On 2026-11-22, `eval/decision_context.py` ran four synthetic work situations in both modes against the real book and the existing `qwen/qwen3-embedding-4b` vectors: unbundling, rejection with unknown WTP, benefit communication, and charging metrics. Both modes returned a page in the expected chapter range in all four cases. `mix` also returned the expected framework in all four cases. Initial construction embedded 57 new framework/relationship texts and reused all existing book chunks. Observed retrieval time was 0.78–1.20 seconds for `naive` and 1.74–2.73 seconds for `mix` in this run.

The comparison has only four cases, checks page/framework coverage rather than advice correctness, and does not establish that `mix` improves decisions or generally outperforms `naive`. Raw results remain in the private runtime file `data/decision-context-evaluation.json`. Re-running `python eval/decision_context.py` makes eight retrieval requests and uses the configured provider; preserve the previous result before intentionally starting another paid run.
