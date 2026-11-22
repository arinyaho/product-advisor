# Three-book validation and rollout

TL;DR: Implementation, source/index sealing, the frozen 15-question comparison, and NAS staging passed. Production activation and post-deployment MCP acceptance are pending the NAS sudo credential. The existing running service has not been replaced. Both modes passed 15/15; this evaluation shows no coverage advantage for mix and measured higher mix latency.

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

## Verification

- Final local suite: 64 passed, six subtests passed, five legacy external-provider tests deselected. The new real-provider evaluation replaces those legacy single-book paid checks for this release; they are not reported as passed.
- Sealing reproduced all 2,795 source chunks, checked exact native-source membership, native chunk bodies, graph source references, vector dimensions/IDs, shared-label links, and immutable file hashes in all four scopes.
- Regression failures were retained before fixes for real LightRAG shared-workspace leakage, missing native vector candidates, queued paid requests after failure, and serving 429 recovery. The real source seal initially failed because native custom-KG sanitization removed three control characters in two MI chunks; exact original bodies were restored without further model calls.
- Frozen fixture: eval/three_book_cases.json; SHA-256 `2ee407af9803cd52948dacaa0cfff806bd985add81e5136ea8d41226187001dd`. It contains nine book-filtered positive questions, three all-book positive questions, and three unsupported questions. Expected source text was read before retrieval. Source identities, text, locations, book filters, actual modes, token limits, and continuation are checked.
- Failed development evaluations remain private under data/. The original comparison was 14/15 in each mode. Literal translation repaired the compound pilot question; strict JSON output repaired a translation-format failure; native vector preservation repaired graph-fusion candidate loss. HTTP 429 failures were recorded separately and did not become semantic passes.
- Production retries HTTP 429 at most once when its Retry-After is at most ten seconds; indexing remains fail-fast. Other failures stay failures. A real 429 receipt reports Model busy, retry later. One earlier embedding failure did not retain its exact HTTP status; that cause remains unverified.

| Mode | Expected-source coverage | Median end-to-end seconds | Confirmed cost per 15 requests | Unknown-charge reservation |
|---|---:|---:|---:|---:|
| mix | 15/15 | 8.6723 | $0.02639106 | $0.00 |
| naive | 15/15 | 4.7155 | $0.02438288 | $0.00 |

- The comparison uses exactly the same shared English translation for each Korean question, the same source corpus, embedding/rerank models, k=5, and final evidence-token budget. Normalization latency/cost is included equally in each mode estimate. Six completed translations were reused from the prior evaluation; no production translation cache was added.
- This is one sequential development regression after tuning, not a held-out precision study. Passing means an expected source was returned, not that every extra passage is relevant or that generated business advice is correct. Graph superiority was not established.

| Case | First matched mix source | Source position |
|---|---|---|
| mi-wtp-en | `src-7834e48abaef96e4d2baa299` | PDF 57 |
| mi-configuration-ko | `src-c020686c3f005f520358b00d` | PDF 87 |
| mi-value-en | `src-26b45865f5bcede48a1a1f32` | PDF 143 |
| fs-icp-en | `src-fbba04f1edad2d723fcf242a` | PDF 124 |
| fs-buyer-ko | `src-ec07e3ad1d5616a08a69a914` | PDF 146 |
| fs-price-en | `src-3b8d2cba8c76e14532683ed3` | PDF 234 |
| de-beachhead-en | `src-cc03f188d0cb80087d605a3a` | OPS/c02.xhtml#c02-li-0009; blocks 25-27 |
| de-dmu-ko | `src-916be00040c5a514cd55c513` | OPS/c12.xhtml#head-2-119; blocks 13-17 |
| de-value-en | `src-24b97eeb51281e22ebeca7df` | OPS/c08.xhtml#c08-para-0007; blocks 10-12 |
| all-pilot-ko | `src-e607772589f77cffb9313c11` | PDF 55 |
| all-segment-ko | `src-ada17fc2444f90cce1f480c5` | PDF 76 |
| all-offer-en | `src-e607772589f77cffb9313c11` | PDF 55 |
| unsupported-venus | No result, as expected | No invented citation |
| unsupported-weather | No result, as expected | No invented citation |
| unsupported-private-contract | No result, as expected | No invented citation |

## Indexing cost

- Completed provider responses for indexing report $1.87030666: 2,795 extraction requests and 319 successful embedding batches. Twelve failed embedding requests have unknown actual charges; $0.24 is a conservative reservation, not confirmed billing. The $5 indexing bound was not exceeded.
- Development comparisons, diagnosis, and post-deployment requests are additional to indexing. Their retained usage receipts identify confirmed charges and unknown-charge reservations. The final table describes the final search profile rather than the sum of all development experiments.
- The Rerank 4 Pro probe failed with a 404 ZDR-policy restriction; the policy was not loosened. A Rerank 5 Pro probe returned an HTTP error whose body was not retained; its exact cause and charge remain unverified. Neither model was adopted.

## Deployment and rollback status

- Existing endpoint: https://prd.damcocu.synology.me/mcp. The baseline used the existing Codex configuration and credential through an actual MCP ClientSession, and successfully listed the four existing tools and MI sections. No substitute credential was used.
- Staged directory: /volume1/docker/product-advisor/staging/three-books-b7c503380791bafe. Forty-one runtime/source/index files were hash-verified on the NAS. The successful 30-query evaluation was uploaded as the activation gate. The active service is unchanged until privileged activation.
- Activation is blocked by sudo authentication: sudo: a password is required. The computer-use tool also denies Terminal app control. The user must enter the NAS credential in their own terminal; it must not be sent in chat.
- The host transaction captures the current exact image ID and runtime/pointer backup before building, then activates the sealed release. It waits for real MCP acceptance and automatically restores the prior image/runtime/pointer on failure or timeout. The rollback mechanism passed a simulated failed-build test; the live image backup is pending privileged activation.
- Post-deployment verification is prepared in scripts/verify-live-library.py. It uses the actual configured endpoint and credential through MCP ClientSession, checks three books, filtered/global searches, modes, the pilot question, TOC, each book read/continuation, and legacy MI ID 27. These post-deployment calls have not run yet.

```sh
ssh -t damcocu 'cd /volume1/docker/product-advisor && bash staging/three-books-b7c503380791bafe/library-host.sh activate three-books-b7c503380791bafe'
```

- Keep the terminal open while the agent performs MCP acceptance. Acceptance, not the startup log or health response, completes deployment.

```sh
ssh -t damcocu 'cd /volume1/docker/product-advisor && bash staging/three-books-b7c503380791bafe/library-host.sh rollback three-books-b7c503380791bafe'
```

## Evidence artifacts

- Private baseline: data/baseline-mcp.json.
- Private manifest and sealed index: data/libraries/three-books-b7c503380791bafe/.
- Final comparison: data/three-book-evaluation-vector-pool.json and its receipts directory.
- Earlier development failures: data/three-book-evaluation-before-normalization.json, data/three-book-evaluation-normalized.json, data/three-book-evaluation-schema.json, and data/three-book-evaluation-final.json.
- Local checks: /private/tmp/product-advisor-library-final-suite.log, /private/tmp/product-advisor-library-seal-vector-pool.log, /private/tmp/product-advisor-mix-pool-baseline.log, and /private/tmp/product-advisor-busy-baseline.log.

## Deferred scope and limits

- Manual concept-map expansion, team-document collection, customer memory, and CRM remain out of scope.
- Image-only book material is not OCRed. Reference B edition/publication year remain unverified. Search precision and robustness on new compound questions require an independent holdout evaluation.
- Source evidence supports a diagnosis; it does not establish the company's customer facts, WTP, or the cause of a rejected purchase.
