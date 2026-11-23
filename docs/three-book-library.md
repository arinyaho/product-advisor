# Three-book library

TL;DR: The library serves source evidence from three private books through the existing MCP endpoint. Search defaults to all books and LightRAG `mix`; `naive` is retained for diagnosis. A release must pass source checks and the frozen retrieval evaluation before activation. The host transaction restores the old image and pointer unless real MCP acceptance succeeds.

Current rollout status: three-books-12acf0596c563f6f is deployed and accepted through the existing configured MCP endpoint. All 12 actual acceptance checks passed, followed by source-metadata verification in a separate session. The first candidate failed live acceptance and was rolled back; the original frozen fixture and failures remain preserved alongside the separate user-authorized v2 [source-annotation correction](source-annotation-audit.md). See [validation and rollback evidence](three-book-validation.md).

## Registered sources

| book_id | Verified source | Edition evidence | Search scope |
|---|---|---|---|
| `book-a` | Author A1 and Author A2, *Reference A*; PDF | Copyright PDF page 8: Authorized India Edition, copyright 2016; publication year unverified. The filename's 2022 is not edition evidence. | 259 PDF pages, 146 sections, 532 original searchable chunks; 12 footer-only chunks excluded. |
| `book-b` | *Reference B: Private reference material*; PDF | Title from PDF page 1; Author B attribution supplied by the user. This file contains no verified author credit, publication year, or numbered edition. | 400 PDF pages; 100 printed-TOC sections, 1,216 paragraph chunks. Front title and contents remain in the source file. |
| `book-c` | Author C, *Reference C Expanded & Updated: Private reference material*; EPUB | OPF metadata, ISBN private-isbn, publication date 2024-04-02; copyright 2024; navigation/preface identify the expanded 10th-anniversary edition. | 42 spine items, 278 retained sections, 1,047 chunks. Image references and supplied alt text are retained; image-only contents are not OCRed. |

- Each private manifest records original filename, SHA-256, metadata evidence, parser version, fixed index configuration, and book counts. Original files are never edited.
- The author/title pair for Reference B is also corroborated by [the author's official book site](https://www.private-reference.example/). This does not identify the uploaded PDF's edition or publication year.
- PDF locations identify the PDF page and normalized text range. `printed_page=null` means the printed page has not been mapped. EPUB locations identify document, anchor, block range, and word range; they never invent PDF pages.
- The Reference A chunking and numeric section identities are retained. New PDF/EPUB chunks respect section boundaries, paragraphs, tables, and quoted blocks. Book hashes and parser versions participate in chunk identities.
- Source catalogs and indices are private runtime data. `.gitignore` excludes PDFs, EPUBs, vectors, environment files, and `data/`; the Docker context uses a whitelist.

## MCP interface

| Tool | Behavior |
|---|---|
| `list_books()` | Registered titles, authors, verified/unknown editions, source hashes, index version and readiness. |
| `search_book(query, k=5, mode="mix", book_id=None)` | Omitting `book_id` searches all books. A specified book selects a separate graph and vector scope before retrieval. Supports Korean situation questions and English concepts. Returns at most 20 requested chunks and 4,000 tokens including metadata. |
| `list_sections(book_id=None, offset=0, limit=100)` | Book-identified sections with `total` and `next_offset`; all books when the filter is absent. |
| `get_section(id, start_page=None, book_id=None)` | Namespaced new-book sections or legacy MI integer IDs. Follow returned continuation arguments; EPUB uses a chunk/word cursor through `start_page`. |

- Search results identify book, title, edition, chapter, section, source hash, location, chunk ID, rerank score, and arguments for further reading.
- `mix` uses graph and source chunks; `naive` uses source vectors. The actual native execution mode is checked. Failures return `actual_mode=null`, `status=unavailable`, and `fallback=false`; an unfiltered retriever is never substituted.
- Search returns evidence and does not generate an answer. Model-extracted relationships and shared-label navigation links are not presented as author assertions or proof of equivalent definitions.
- Existing authentication and expiry remain in the mounted key store. Full library reads are bounded by 2,000 words and ten PDF pages; excerpt reads by 60 words. Legacy integer MI reads retain their existing full-text/summary behavior. Search excerpt hits are limited to 60 words each.
- `retrieve_decision_context` remains callable but returns no manual framework cards in library mode. Manual concept maps, company ingestion, customer memory, and CRM are deferred.

## Fixed engine and models

- LightRAG `1.5.7`, NetworkX/JSON/NanoVectorDB local storage, one serving process; no new database or vanilla retrieval engine.
- Embeddings: `qwen/qwen3-embedding-4b`, 2,560 dimensions; compatible MI vectors reused.
- Native extraction: `anthropic/claude-haiku-5.5`, temperature 0 requested, maximum 1,800 output tokens, reasoning disabled, no gleaning, at most 12 extracted records and six entities per source chunk. A fixed request does not guarantee deterministic provider outputs; completed extraction responses are retained.
- Candidate reranker: OpenRouter `qwen/qwen3-reranker-8b`. The official LightRAG chunk processor applies the minimum score 0.15, then final source JSON is packed within the same 4,000-token budget in both modes. Scores are ranking signals, not probabilities of support. The embedding and graph-extraction models are unchanged.
- The initial graph/vector candidate ceilings are 40; the pre-rerank context has 24,000 total tokens, 2,500 entity tokens and 2,000 relationship tokens. This prevents early graph packing from dropping source candidates before reranking. Final evidence remains capped at 4,000 tokens. Both modes use the same source chunks, embedding model, reranker, and final evidence budget.
- `mix` also preserves the top 40 candidates from the same scoped LightRAG vector store before one shared reranking pass. Native mix fusion had dropped a vector candidate retained by native naive retrieval. This uses the existing LightRAG stores and does not create another RAG engine or database. Responses disclose `candidate_expansion=same_lightrag_scoped_vector_pool`; native execution remains `mix`. The extra vector lookup adds one query embedding request in this implementation.
- A book-specific entity name prevents author definitions from being merged. The all-book graph connects equal normalized labels through explicitly derived navigation nodes.
- Runtime query text, embeddings and rerank scores are not written to caches. Index-building caches and billing receipts are private and resumable. Serving never performs extraction or answer generation.
- The revised query path sends the original Korean or English question directly to multilingual embedding and reranking. It does not translate, rewrite, or answer the question. `query_normalization=none_original_query` records that behavior. The earlier translation workaround was rejected by live acceptance and is retained only as a historical experiment.
- The first frozen comparison passed 14/15 in each mode. A compound Korean pilot/purchase question had valid expected candidates, but the v3.5 reranker scored its key DMU passage at 0.149, below 0.15. Sentence splitting and adding chapter metadata did not repair it. Rerank 4 Pro was rejected by the account's ZDR policy; the policy was not loosened. Literal translation followed by English retrieval/reranking found the fixed expected sources without lowering the score floor or changing source labels. Preserve both the original and normalized evaluation results; the latter is a development regression rather than an unseen general-quality guarantee.
- Native custom-KG insertion removes non-printing controls. Two legacy source chunks contain three such characters. The build restores their exact original bodies in the source-text store after insertion, retaining native vector IDs and original source locations. Sealing still requires exact source-body equality.

## Build and verify

Run from an isolated worktree with the existing source PDF at `DATA_DIR/book.pdf` and credentials provided through the environment's secret mechanism. Never place secret values in command arguments.

```sh
export DATA_DIR="$PWD/data" PYTHONPATH=mcp
python mcp/library_cli.py prepare --source-root /Volumes/storage1/Projects/product-advisor
python mcp/library_cli.py build --directory data/libraries/<version>
python mcp/seal_library.py data/libraries/<version>
python eval/three_books.py data/libraries/<version> --cases eval/three_book_cases_v2.json --output data/three-book-evaluation-v2.json
```

- Preparation verifies all source locations before paid calls. A model/configuration change produces another release; incompatible vectors are rejected.
- `library_cli.py reuse-query-profile --from-directory <completed-release>` copies a sealed release only when every source, embedding and graph-extraction setting matches, allowing a query-only reranker change without paid reindexing. Original source/index files are preserved, native workspace directories receive the new release identity, and the copy must be sealed again. `build` validates and returns zero new paid calls for an already sealed release instead of re-extracting it.
- Extraction has one paid request per uncached source chunk. Completed raw responses, parsed extraction records, and static vectors are reused. The build limit is $5 including conservative reservations for requests whose actual charge is unknown. Index building never retries automatically. After a request fails, queued indexing calls stop; inspect the receipt before resuming. Serving retries HTTP 429 once when Retry-After is absent or at most ten seconds; it does not retry other errors or ignore a longer Retry-After. Both attempts receive separate billing receipts in evaluation. Production receipts remain in memory and contain no query text.
- `pause.request` stops between extraction batches. An exclusive build lock prevents concurrent writers.
- Sealing verifies exact source membership in all four scopes, original chunk bodies, graph source references, vector dimensions and IDs, cross-book navigation links, and immutable index hashes. The runtime rejects unsealed or modified index files.
- `eval/three_book_cases.json` was fixed before retrieval. It contains 12 positive and three unsupported questions. Expected source IDs were checked against the source. The runner checks identity/content/location, scope isolation, bounded responses, continuation, legacy IDs, actual modes, latency and provider costs. Exact expected-source coverage does not by itself measure the precision of every extra returned passage.
- Preserve failed evaluation files. The user-authorized v2 source annotation adds five directly reviewed passages omitted from all-pilot-ko; see [source annotation audit](source-annotation-audit.md). V1 stays unchanged and its 14/15 failures remain failures. V2 is a post-hoc development regression, not a held-out quality benchmark. The runner records the fixture hash, sends the original question unchanged, resumes only incomplete runtime failures, and retains failed attempts separately. Changed retrieval configurations use separate output files. A passing local test or health check does not establish a successful production rollout.

## Stage, activate, accept, rollback

```sh
python scripts/deploy-library.py damcocu data/libraries/<version> --cases eval/three_book_cases_v2.json --evaluation data/three-book-evaluation-v2.json
ssh -t damcocu 'cd /volume1/docker/product-advisor && bash staging/<version>/library-host.sh activate <version>'
```

- Stage transfers an explicit runtime whitelist, source catalog, source files, sealed indices, a fixture-bound passing evaluation, the exact fixture, and an integrity inventory. It excludes all keys, environment files, extraction/LLM caches, test data and the local active pointer. The NAS verifies every staged file before activation. The running service is unchanged during staging.
- Activation needs the NAS sudo credential in its terminal and a matching 15/15 result for both modes. It preserves the previous container image, runtime files and library pointer under `backups/`, builds the new image, writes the new pointer atomically, and starts the existing compose service. Key files, `.env`, existing book data and old indices remain in place. The transaction writes `staging/<version>/activation.log` for monitoring.
- After startup it waits 300 seconds for `staging/<version>/accepted.json`. The operator must run real authenticated MCP calls through the configured public endpoint, checking book list, each book, all-book retrieval, modes, TOC, source reading and continuation. Run `scripts/verify-live-library.py <release> --cases eval/three_book_cases_v2.json --output data/live-library-acceptance-v2.json --accept-host damcocu` to write the acceptance file only after these checks pass. The host checks the version and fixture hash before acceptance. `rejected.json`, startup failure, build failure, or acceptance timeout restores the previous runtime/image/pointer.
- Roll back an accepted version using the same terminal authentication:

```sh
ssh -t damcocu 'cd /volume1/docker/product-advisor && bash staging/<version>/library-host.sh rollback <version>'
```

- The restore path uses the captured exact image ID; it does not rebuild the previous application or overwrite the key store. Runtime acceptance and rollback results must be reported separately from local checks.
