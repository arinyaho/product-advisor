# OpenRouter reranker evaluation

TL;DR: OpenRouter `cohere/rerank-v3.5` replaces the cosine-weight and score-drop selector. The unchanged 20 known regression cases passed 19/20. All 17 required framework memberships were retained; two unwanted memberships remained in one boundary case. The previous tuned selector passed 20/20. No superiority or unseen-question accuracy is claimed. NAS deployment has not been performed.

## Configuration and bounded provider use

- Keep LightRAG 1.5.7, the original PDF chunking, cached book vectors, and the initial vector floor of 0.5.
- Use OpenRouter `POST /api/v1/rerank` with the existing server key. Put the explicit decision before the complete caller-supplied context. No LLM generates advice or extracts keywords.
- Score framework descriptions through the same adapter as book passages. Passages use the official LightRAG `process_chunks_unified` rerank/filter/count/token pipeline after merging existing curated sources. Disable reranking on preliminary retrieval to avoid charging twice.
- Development-only calibration selected a framework floor of 0.2 and a passage floor of 0.15. A shared 0.2 floor removed one labeled book-evidence hit. These scores are model- and text-type-specific, not probabilities.
- One synthetic preflight and 32 score-recording requests were made, within the stated maximum of 40. The final 20-case replay made zero provider requests. No additional embedding request or book re-embedding occurred. No request was automatically retried.
- OpenRouter listed $0.002/search on 2026-11-22. A simple estimate for 33 requests is $0.066; actual account billing was not inspected. See [the model page](https://openrouter.ai/cohere/rerank-v3.5).

## Results

| Measure | Original broad retrieval | Previous tuned selector | OpenRouter reranker |
|---|---:|---:|---:|
| Passed cases | 8/20 | 20/20 | 19/20 |
| Required framework recall | 100% | 100% | 100% |
| Allowed framework precision | 43.3% | 100% | 91.7% |
| Unwanted framework memberships | 59 | 0 | 2 |
| Relevant cases with labeled evidence | 15/16 | 16/16 | 16/16 |
| Irrelevant cases with empty output | 4/4 | 4/4 | 4/4 |

The final regression command exited with status 1 because `hold-scope-vs-discount` failed. Its frozen required labels are `unbundling` and `price_integrity`. Both were returned, together with unwanted `segmentation` and `behavioral_pricing`. Expected labels were preserved; no extra cutoff was tuned to that case.

Development retained 10/10 passing cases; the former holdout retained 9/10. All 20 cases were already known before this change. This is a regression comparison, not a new blind evaluation.

## Reproduction and evidence

Only record synthetic fixtures. Recording sends query text and book candidates to OpenRouter. Keep artifacts under ignored `data/`. Production queries and scores are not persisted. The recorder refuses to overwrite reports; replays fail closed on any unrecorded request.

```sh
# Existing recorded cohort; provider access is disabled unless --record-rerank is given.
DATA_DIR="$PWD/data" .venv/bin/python eval/retrieval_quality.py rerank \
  --output data/quality-20-20261122 --min-rerank-score 0.15 \
  --min-framework-rerank-score 0.2
```

The command refuses to overwrite the retained `rerank.json`. Preserve the previous report before intentionally starting a separate replay. A new recording requires `--record-rerank`; the callback caps that run at 39 requests, plus the separately retained preflight. Baseline `before.json` and synthetic embeddings are required for a comparison replay.

- Private artifacts: `rerank-record.json`, `synthetic-rerank.json`, `rerank.json`, `rerank-development-policy-two-floors.json`, and `rerank-final-verification.json` under `data/quality-20-20261122/`.
- Fixture SHA-256 remains `f47b37904cb4499e2a7bca7f3210e53f027d0a37e9165065453ce4e9dc0a7abf`.
- Original chunk/excerpt SHA-256 remains `a5fea4bcfcef8b5eb180224439aba8f2017e221a24fa249f6ead846785ad2cdf`: 544 original chunks, 2,014 excerpt windows, 12 footer-only exclusions, 532 searchable chunks.
- The new reranker-wiring regression failed on the previous implementation: `/private/tmp/product-advisor-rerank-baseline.log`.
- Reranker failures and malformed results are reported as unavailable. The application prevents the official helper's fallback to unranked chunks from being presented as successful reranking.
- Final local tests: 51 passed, 5 existing checks deselected, 6 parameterized subtests passed. Compilation, deployment-shell syntax, and `git diff --check` passed.
- Four real local MCP HTTP replays used the real LightRAG index and recorded provider scores, with authentication, exact framework/page identities, 60-word excerpt limits, and the final token budget verified. All four transport/scope/budget checks passed; the known scope-versus-discount quality failure remained visible. No provider call was made by these HTTP checks.
- Prior cosine-cluster test fixtures were replaced with rerank-score fixtures because the cluster rule was removed. Membership, empty-result, source-identity, and more-than-three-candidate assertions remain covered.

## Limits

- One frozen boundary case still fails. Do not report the whole quality suite as passing.
- A page-label hit checks evidence coverage, not the usefulness of every passage or the correctness of advice.
- The model and floors are configurable; a model change requires fresh calibration.
- Reranking adds up to two external calls per `mix` request or one per `naive` request, each with a 30-second network timeout and no automatic retry.
- NAS deployment and external HTTPS acceptance remain unverified for this revision.
