# Three-book validation and rollout

TL;DR: Candidate `three-books-12acf0596c563f6f` passes all 15 source-identity cases in both mix and naive using the explicitly versioned v2 source annotations. It is deployed on the existing NAS and accepted through the actual configured MCP endpoint. All 12 acceptance checks passed, followed by an additional source-metadata check after acceptance. The first candidate failed live MCP acceptance and was rolled back; those failures and the original v1 fixture remain unchanged. These are development regression results, not held-out retrieval quality or GraphRAG superiority evidence.

## Sources and indexed scope

Source identities, original filenames, author/edition evidence and original hashes are private. They are preserved in the source registration and authenticated catalog rather than published here.

| Public alias | Format | Sections | Indexed chunks |
|---|---|---:|---:|
| `book-a` | PDF | 146 | 532 |
| `book-b` | PDF | 100 | 1216 |
| `book-c` | EPUB | 278 | 1047 |

- Original files were not moved or edited. PDFs, EPUBs and private source registration are excluded from Git and the image.
- Numeric primary-source section IDs and original chunk boundaries remain unchanged. PDF pages and EPUB positions are distinct; unverified printed pages stay null.
- Image references and supplied alt text are retained; image-only EPUB content is not OCRed.

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
- Original fixture: eval/three_book_cases.json; SHA-256 44781921b166393aa3012f1812127de4cd81a4c644c3e72197c726c07cd0c121. The original Qwen evaluation remains 14/15 in each mode at data/three-book-evaluation-qwen.json.
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
- Unverified publication year/edition fields remain explicitly marked in the private catalog. Unmapped printed page numbers remain null. EPUB image-only text is not OCRed.
- Held-out retrieval precision, comprehensive answer coverage, and a GraphRAG advantage remain unverified. This service returns sources, not generated business answers.
- Deployment and actual configured MCP acceptance passed. The built image SHA is recorded from the host build log and installed runtime hashes were verified; no separate privileged post-acceptance docker-inspect call was performed.
- Provider charges for live MCP queries are not included in the local evaluation comparison; no live per-query billing receipt is persisted by the runtime. Earlier diagnostic/evaluation costs also remain separate rather than being mislabeled as the total bill.

Privacy note: Public bibliographic labels and aliases were anonymized without changing evaluation questions, expected source chunk identities or private historical receipts. Original fixture bytes remain only in the private recovery snapshot; public fixture hashes therefore differ from pre-anonymization receipts. New evaluations must use fresh output files.
