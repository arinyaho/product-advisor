# Body-evidence search filter

TL;DR: The follow-up query profile removes index/contents and exact heading-only chunks before reranking. It passes the unchanged 15-question v2 regression and three additional review questions in both modes. These are local real-provider results; the accepted NAS deployment remains `three-books-12acf0596c563f6f` and has not received this follow-up filter.

## Change and preservation

- Normalize section/chapter labels and text for case, whitespace and punctuation. Index/contents sections are navigation; a chunk containing only its section/chapter heading is not evidence. Empty chunks are excluded too.
- Preserve short substantive text, explanatory notes, headings followed by body text, and sections whose names merely contain words such as "index". This is not a minimum-word-count or relevance heuristic.
- Apply the rule after selecting the native book scope and before reranking, k truncation and token packing. Both mix and naive use it. Duplicate excluded candidates are counted once.
- Report `evidence_filter=body-evidence-v1` and `excluded_non_evidence_chunks`. A request with only excluded candidates returns ready/empty without a rerank request; it does not fall back to an unfiltered engine.
- Preserve all source chunks, original positions, contents entries and section-reading behavior. The fixed corpus contains 61 excluded chunks: MI five heading-only; FS one heading-only; DE 33 heading-only and 22 navigation chunks. They remain accessible through contents and source reads.
- The original catalog and all 20 sealed native graph/vector/source-store files are byte-identical in the copied candidate. Chunking, models, score floor 0.15, source IDs and original v1/v2 fixtures are unchanged. New paid index calls: zero.
- Query configuration participates in the new release identity. Candidate `three-books-3eb6e529141434f8` has retrieval profile `71c6607078577abe`; it does not overwrite the accepted release. Copy with `reuse-query-profile`, then seal and evaluate before any later activation.

## Verification

- The unfixed runtime failed two identity-based regression tests, one per mode: a navigation hit consumed the first result before the body passage. The baseline log is preserved privately as `data/verification-receipts/product-advisor-evidence-baseline.log`.
- Local tests cover pre-rerank exclusion, k=1 membership, no rerank for navigation-only candidates, short-body retention, readable index sections, source immutability and distinct identities for query-only release changes.
- The evaluator now rejects non-evidence hits even when a correct expected source is also returned. Evaluation version `source-and-body-evidence-v1` prevents resuming historical results under the changed criterion.
- `eval/three_book_evidence_cases.json` freezes three generalized review questions: lifecycle value, post-pilot qualification and an all-book reoffer situation. Expected body passages were source-reviewed before the filter change. No customer records or book text are committed.

| Fixed questions | Mode | Expected-source coverage with no non-evidence hits | Median seconds | Confirmed cost |
|---|---|---:|---:|---:|
| Original v2, 15 | mix | 15/15 | 2.4582 | $0.02454636 |
| Original v2, 15 | naive | 15/15 | 1.3855 | $0.01952008 |
| Body regression, 3 | mix | 3/3 | 2.9775 | $0.00753640 |
| Body regression, 3 | naive | 3/3 | 1.3820 | $0.00511830 |

- Total confirmed comparison cost: $0.05672114, zero unknown-charge reservation. Each invocation was limited to $0.20 and 90 calls. Both modes received the same original questions, models, k=5 and 4,000-token final evidence budget.
- Private real-provider receipts: `data/three-book-evaluation-body-filter.json`, `data/three-book-evaluation-body-regressions.json` and their receipt directories. Historical failures and production acceptance receipts are unchanged.
- This is a development regression, not a held-out precision study. Other irrelevant body passages or bibliography-only notes can still appear. Passing the rule does not prove every substantive passage is relevant, complete answer coverage, a latency improvement over another run, or a graph quality advantage.
- Existing production MCP acceptance still applies to the older accepted profile. This follow-up requires its own staged deployment and actual MCP acceptance before production completion can be claimed.
