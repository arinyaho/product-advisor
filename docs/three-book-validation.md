# Three-book validation and rollout

TL;DR: Candidate `three-books-12acf0596c563f6f` passes all 15 source-identity cases in both mix and naive using the explicitly versioned v2 source annotations. It is deployed on the existing NAS and accepted through the actual configured MCP endpoint. All 12 acceptance checks passed, followed by an additional source-metadata check after acceptance. The first candidate failed live MCP acceptance and was rolled back; those failures and the original v1 fixture remain unchanged. These are development regression results, not held-out retrieval quality or GraphRAG superiority evidence.

## Sources and indexed scope

| Book ID | Original filename | Verified edition | Sections | Chunks | SHA-256 |
|---|---|---|---:|---:|---|
| `book-a` | `"source-1.pdf"` | Authorized India Edition; copyright 2016; publication year not verified | 146 | 532 | `c12d805c9f6e2bc2a1493ee7f99b0e0dd6321b3a957d7e43332503eb28b12a88` |
| `book-b` | `"source-2.pdf"` | Unnumbered PDF text edition; publication year and edition not verified | 100 | 1216 | `a31eda82a68867a489097ce907141cef7adccb0d06d7bcb631c2f602ff33dcc9` |
| `book-c` | `"source-3.epub"` | Expanded & Updated; 10th-anniversary edition; 2024 | 278 | 1047 | `98b82e301b7fb7a67fc23d0258464872c2f13de2f7b4da6e1191f20a5790cf67` |

- Original files were rehashed after implementation and still match their recorded hashes. No original was moved or edited. PDFs and EPUBs are ignored by Git and excluded from the Docker build context.
- Reference A has 259 PDF pages; its existing section IDs and 200-word chunk boundaries are preserved. Twelve footer-only chunks are excluded, leaving 532 searchable chunks.
- Reference B has 400 PDF pages. All 100 contents headings were located on their actual PDF pages. A page-by-page word comparison found no loss from text normalization. The uploaded PDF contains no verified publication year or author credit; the author/title pair is corroborated by the official [book site](https://www.private-reference.example/). Its filename does not establish the edition.
- Reference C has 42 EPUB spine items. OPF/title/copyright/preface evidence identifies Author C and the 2024 expanded 10th-anniversary edition. Image references and supplied alt text are preserved; image-only text is not OCRed.
- PDF page numbers and print page numbers are separate. Unverified print pages remain null. EPUB citations use document/anchor/block/word coordinates.

## Fixed implementation

- Index: `three-books-b7c503380791bafe`; parser `three-books-1`; schema `three-books-native-extraction-v1`; retrieval profile `5b4bbffee60521f4`.
- LightRAG 1.5.7 was reused without upgrading. MCP 2.3.0, PyMuPDF 2.1, NumPy 2.5.5, and Uvicorn 0.54.0 are pinned.
- Embedding: qwen/qwen3-embedding-4b, 2,560 dimensions. Existing compatible MI vectors and all completed extraction/vector caches were reused.
- Extraction: anthropic/claude-haiku-5.5; maximum 1,800 output tokens; no gleaning; maximum 12 records and six entities; English extraction; reasoning disabled; temperature 0 requested. Cached responses preserve actual extraction outputs.
- Query translation: the same Haiku model; literal Korean-to-English only, 512 output tokens, strict JSON schema. English queries skip translation. No answer-generation step was added. Production query text and translations are not persisted.
- Reranking: cohere/rerank-v3.5 through OpenRouter; minimum score 0.15. The threshold and expected-source labels were not lowered or changed to pass evaluation.
- Both modes use 40-candidate ceilings and the same 4,000-token final evidence budget. Candidate packing uses 24,000 tokens. Mix preserves the same scoped native vector pool before a single reranking pass; this is disclosed in response metadata and adds one query embedding call.
- Physical book-specific graph/vector views enforce book filters before retrieval. The all-book graph has 7,494 nodes and 10,200 edges, including 236 shared-label navigation nodes. Author definitions remain book-scoped; extracted and derived links are not represented as author assertions.

## MCP changes

- Added list_books(). search_book(query, k=5, mode="mix", book_id=None) searches all books when book_id is absent; naive remains diagnostic. Results identify book, title, edition, chapter/section, source location, source hash, chunk ID, score, and read_section arguments.
- list_sections(book_id=None, offset=0, limit=100) returns a bounded object with sections, total, and next_offset. Every item identifies its book.
- get_section(id, start_page=None, book_id=None) accepts legacy MI integer IDs and namespaced new-book IDs. New-style reads return bounded source text and continuation arguments; EPUB continuation is a chunk/word cursor.
- Legacy integer MI calls keep the same section identities and existing full-text/summary access behavior. Library reads cap full keys at 2,000 words and ten PDF pages, and excerpt keys at 60 words. No key was issued, replaced, or copied into deployment artifacts.
- retrieve_decision_context remains callable for compatibility, but library mode returns no manual framework cards. Manual concept maps, company ingestion, customer memory, and CRM were not implemented.

## Engine, models, and index

- LightRAG 1.5.7 is unchanged. Storage remains native NetworkX/JSON/NanoVectorDB in the existing service.
- Source schema: three-books-native-extraction-v1. Candidate index: three-books-12acf0596c563f6f, derived from three-books-b7c503380791bafe.
- Embedding: qwen/qwen3-embedding-4b, 2,560 dimensions. Extraction: anthropic/claude-haiku-5.5, temperature 0 requested, 1,800 output tokens, reasoning disabled, gleaning 0, at most 12 records and six entities per chunk. Completed outputs are reused; deterministic output is not claimed.
- Query reranker: qwen/qwen3-reranker-8b through OpenRouter. Query profile: 93d35e33910a777e. Original Korean/English questions reach retrieval and reranking unchanged; no translation or answer generation is performed.
- Same source catalog, graph and embedding bytes were reused for the query-only model change. New paid index calls: zero. Sealing verifies 2,795 exact source chunks and original positions in all four physical scopes.
- Native book scopes: MI 1,587 nodes / 1,922 edges; FS 3,049 / 4,148; DE 2,622 / 3,607. The all-book scope has 7,494 nodes / 10,200 edges and 236 derived shared-label navigation nodes. Model-extracted or derived relationships are not author assertions.
- A book filter selects a native graph and vector scope before retrieval. Mix preserves a top-40 vector candidate pool from that same LightRAG scope before shared reranking. This adds one query embedding; it does not add a separate RAG engine or database.

## Verification and annotation correction

- Local suite: 69 passed, six subtests passed, five legacy external-provider tests deselected. Excluded checks are not reported as passed. The paid three-book evaluation is separate.
- Original fixture: eval/three_book_cases.json; SHA-256 2ee407af9803cd52948dacaa0cfff806bd985add81e5136ea8d41226187001dd. The original Qwen evaluation remains 14/15 in each mode at data/three-book-evaluation-qwen.json.
- V2 fixture: eval/three_book_cases_v2.json; SHA-256 f484d8e31fe3152518ab4a6f79ef1c862a9f5d44d2edc392fdd48fcae5a6c4f7. The user authorized continuing with a source-reviewed correction on 2026-11-23. It adds exactly five reviewed sources to all-pilot-ko; all questions, original sources, filters, negative cases, k=5, score floor 0.15, and 4,000-token final budget remain unchanged. See source-annotation-audit.md for exact positions and reasons.
- V2 was corrected after inspecting v1 retrieval. Passing it does not demonstrate an independent general-quality improvement. The criterion is at least one verified expected source for positive questions and no hits for unsupported questions; it does not prove every extra hit is relevant or every component of a compound question is covered.
- Source identity, exact text, original location, strict book filters, actual modes, bounded response size, all-book retrieval, TOC/read/continuation and legacy MI section 27 are checked. Expected identities were not replaced or removed.
- Regression failures were retained before fixes for native workspace leakage, vector candidate loss, queued paid requests after failure, 429 recovery, and accidental rebuilding of a sealed index. A new failing-before/fixed-after test confirms a fixture mismatch stops activation before any runtime mutation. Automatic rollback remains tested.
- Full v2 results and provider receipts are private under data/three-book-evaluation-v2.json and its receipts directory. Confirmed charges and unknown-charge reservations are separate.

| Mode | Expected-source coverage | Median seconds | Confirmed cost / 15 requests | Unknown-charge reservation |
|---|---:|---:|---:|---:|
| mix | 15/15 | 3.7244 | $0.02510776 | $0.00 |
| naive | 15/15 | 2.1224 | $0.01971848 | $0.00 |

- Both modes use identical original questions, source corpus, models, k=5 and final evidence-token budget. This single sequential comparison establishes no graph quality advantage. Mix is slower and costs more in this run.
- The three unsupported questions concern Venus pressure, future weather, and a private contract absent from the books; both modes return no evidence.
- Three additional unchanged Korean pilot-question repetitions per mode passed all six source-identity/mode/location/budget checks. Their separate receipt is data/three-book-pilot-repeat-v2.json; confirmed cost $0.01724536, no unknown-charge reservation. These repeated checks do not make the development fixture held-out.

## Costs and preservation

- Initial successful indexing responses report $1.87030666 for 2,795 extraction calls and 319 successful embedding batches. Twelve failed embedding calls have unknown actual charges; $0.24 is a conservative reservation, not actual billing. The original $5 indexing cap was respected.
- The query-only candidate incurred zero new paid index calls. V2 comparison cost is $0.04482624, with zero unknown-charge reservation. Earlier evaluations, diagnostic probes, repeated checks and subsequent live queries are additional and retained in separate receipts; the comparison cost is not an all-session billing total.
- Completed extraction responses and vectors remain preserved. Raw PDF/EPUB files, private catalogs/indices, credentials and billing caches are not tracked or baked into the image. Existing key stores and expiry rules are unchanged.

## Deployment and actual MCP status

- Existing endpoint: https://prd.damcocu.synology.me/mcp. Verification uses the existing Codex URL and credential through an actual MCP ClientSession, with no substitute key.
- First candidate three-books-b7c503380791bafe built image sha256:d52931b97bcc41a9121eab734a734346b1e5dbbdb4e9d04fa12394c471ff4ea1. Live book listing, each book's TOC/read/continuation, legacy MI 27, and three filtered searches passed; all-pilot-ko/mix then failed its expected-source check. No acceptance file was issued. The failed response IDs were not retained before the assertion, so the precise original cause is unverified. The verifier now records attempts before assertions.
- Automatic rollback restored /volume1/docker/product-advisor/backups/three-books-b7c503380791bafe-20261122T211201Z. The old MCP tools and legacy section 27 were independently checked afterward; data/rollback-confirmation.json retains that evidence.
- Deployed release: three-books-12acf0596c563f6f. The NAS hash-verified all 43 staged files and later verified all ten installed runtime files against that inventory. Runtime code commit: 6d1c57a. Built image: sha256:9e2ca02a63303b63666f7b99d09e1f02b489d20aa293f41ac1a0c91083e900d1. The active pointer identifies this release.
- Actual MCP acceptance passed 12 checks: three-book registration/metadata; each book TOC/read/continuation; legacy MI section 27; each book filtered mix search; global Korean pilot and segment mix searches; global segment naive search; and unsupported Venus no-evidence behavior. The receipt is data/live-library-acceptance-v2.json. Requested and actual modes match, status is ready, and fallback is false.
- The host reported DEPLOYMENT_ACCEPTED and the activation process exited. The acceptance marker binds this release to the exact v2 fixture SHA-256. No claim relies only on a local test or HTTP health check.
- A separate MCP session after acceptance reran the Korean pilot question and verified all five hits against their source catalog: book ID, chunk ID, chapter, section, title, edition, authors, original location, original hash, text and follow-up read arguments. It returned sources from three books without a forced per-book quota. Receipt: data/post-acceptance-metadata-v2.json.
- Representative pilot evidence: MI PDF 66 (WTP research questions), FS PDF 237 (purchasing/budget qualification), and DE OPS/c16.xhtml#c16-li-0006, block 15 (DMU authority limits and price friction). These source positions were checked directly; printed page numbers are not inferred.
- Staging includes an explicit runtime whitelist, sealed index, source files, exact fixture, passing evaluation and integrity inventory. It excludes keys, .env and build caches. The host verifies evaluation/fixture hashes and all 30 case/mode identities before mutation; the live acceptance marker must match the same fixture hash and release.
- Activation uses the existing /volume1/docker/product-advisor path and Docker Compose service. It preserves the exact old image, runtime files and active pointer. Missing acceptance, rejection, build failure or startup failure automatically restores that snapshot.
- The user supplied sudo authentication only in their own NAS terminal. No password was received or stored in chat. No credentials or key stores were replaced.
- Current rollback snapshot: /volume1/docker/product-advisor/backups/three-books-12acf0596c563f6f-20261123T003711Z. The exact previous image ID/tag and runtime/compose files are present; the activation script tagged the previous image before building. The earlier live failed-acceptance transaction demonstrated automatic rollback; a rollback of the accepted new release was not executed because it would undo the requested deployment.

Rollback after activation:

```sh
ssh -t damcocu 'cd /volume1/docker/product-advisor && bash staging/three-books-12acf0596c563f6f/library-host.sh rollback three-books-12acf0596c563f6f'
```

## Deferred and unverified

- Manual concept maps, company ingestion, customer-specific memory and CRM are outside this scope.
- Reference B publication year/numbered edition remain unverified. Unmapped printed page numbers remain null. EPUB image-only text is not OCRed.
- Held-out retrieval precision, comprehensive answer coverage, and a GraphRAG advantage remain unverified. This service returns sources, not generated business answers.
- Deployment and actual configured MCP acceptance passed. The built image SHA is recorded from the host build log and installed runtime hashes were verified; no separate privileged post-acceptance docker-inspect call was performed.
- Provider charges for live MCP queries are not included in the local evaluation comparison; no live per-query billing receipt is persisted by the runtime. Earlier diagnostic/evaluation costs also remain separate rather than being mislabeled as the total bill.
