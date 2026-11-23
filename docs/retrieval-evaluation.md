# Retrieval evaluation

TL;DR: The frozen 20-case baseline passed 8/20 cases. The first revision passed 8/10 held-out cases. After repairing both failures, the same 20 known cases passed as regression checks. This is not an unseen-question accuracy estimate. NAS deployment remains separate.

This records the previous cosine selector, which has been replaced by the OpenRouter reranker. See [the current reranker evaluation](rerank-evaluation.md) for behavior and current reproduction commands. Commands below describe the historical implementation.

## Frozen protocol

- Freeze `eval/retrieval_cases.json` before the first provider request: 12 relevant, 4 boundary, and 4 irrelevant synthetic questions; 10 development and 10 held-out questions. No customer records are included.
- Label required framework identities, optional allowed identities, and acceptable PDF source pages. Every other framework is an unwanted inclusion. Irrelevant cases require both framework and passage lists to be empty.
- Run the actual LightRAG 1.5.7 `mix` pipeline with the real PDF and cached Qwen book vectors. Record synthetic query embeddings once, then replay them with provider access disabled. Compare complete source/chunk hashes between stages.
- Pass a relevant case only when all required frameworks appear, no unwanted framework appears, and at least one returned page matches its evidence label. This does not grade every returned passage or final advice.

## Results

| Measure | Baseline | First revision | Final regression |
|---|---:|---:|---:|
| Passed cases | 8 | 18 | 20 |
| Unwanted framework inclusions | 59 | 1 | 0 |
| Required framework hits | 100.0% | 94.1% | 100.0% |
| Allowed framework precision | 43.3% | 94.1% | 100.0% |
| Relevant cases with labeled evidence | 15 | 16 | 16 |
| Irrelevant cases with empty output | 4 | 4 | 4 |

- The first revision passed 10/10 development cases and 8/10 held-out cases. It missed `failure_patterns` in an explicit failure-types question and included `behavioral_pricing` in a scope-versus-discount question.
- Preserve those failures. After their investigation, the original held-out set became a regression set; the final 20/20 is not another blind evaluation.
- Final selection weights explicit-decision similarity 60% and complete-context similarity 40%, removes scores below 0.5, and separates the strongest score group at the largest adjacent drop when that drop is at least 0.05. It imposes no framework-count cap.
- A development-only scan of separation thresholds 0.025, 0.05, 0.075, and 0.1 passed all ten development framework-label checks at each value. The chosen 0.05 remains a heuristic; the scan does not establish an optimal universal threshold.
- Selected frameworks bring their most decision-relevant passage from the existing curated source pages. Final JSON packing includes framework descriptions and evidence in a 7,000-token budget.

## Case membership

| Case | Original split | Baseline pass | Final framework ids | Final pass |
|---|---|---|---|---|
| dev-wtp | development | no | `willingness_to_pay` | yes |
| dev-configuration | development | no | `configuration` | yes |
| dev-charging | development | yes | `monetization_model` | yes |
| dev-value | development | yes | `value_communication` | yes |
| dev-discount | development | no | `price_integrity` | yes |
| dev-economics | development | no | `living_business_case` | yes |
| dev-background-price | development | no | `value_communication` | yes |
| dev-model-vs-level | development | no | `monetization_model` | yes |
| dev-recipe | development | yes | none | yes |
| dev-encryption | development | yes | none | yes |
| hold-segmentation | holdout | no | `segmentation` | yes |
| hold-unbundling | holdout | yes | `unbundling` | yes |
| hold-strategy | holdout | no | `pricing_strategy` | yes |
| hold-behavior | holdout | no | `behavioral_pricing` | yes |
| hold-implementation | holdout | no | `implementation` | yes |
| hold-failures | holdout | yes | `failure_patterns`, `configuration` | yes |
| hold-scope-vs-discount | holdout | no | `unbundling`, `price_integrity` | yes |
| hold-pilot-not-wtp | holdout | no | `willingness_to_pay` | yes |
| hold-calculus | holdout | yes | none | yes |
| hold-database | holdout | yes | none | yes |

## Verification and retained evidence

- The original 544 chunks and 2,014 excerpt windows have identical combined hashes across all three runs. The 12 footer-only exclusions remain; searchable chunks remain 532.
- `_read_page`, `_heading_pos`, `_own_span`, and `_structure_chunks` match the deployed NAS source by AST comparison.
- New regression tests failed before both selection fixes; assertions name framework and source identities, including a case where four relevant frameworks must survive.
- Final local suite: 45 passed, 5 existing checks deselected. Python compilation, deployment-shell syntax, and `git diff --check` passed. The five deselected checks were not rerun.
- Real local MCP HTTP calls replayed four frozen cases through authentication and `tools/call`, with the real LightRAG index. Framework/page identities matched the final evaluation, excerpt passages stayed within 60 words, and complete responses stayed below 7,000 tokens. No provider calls were made.
- Baseline recording made 20 embedding-provider batch calls. Both subsequent 20-case replays and the four HTTP checks made zero provider calls. There was no generation and no new book embedding.
- Private artifacts under `data/quality-20-20261122/`: `before.json`, `after.json`, `revised.json`, original source snapshots, synthetic embeddings, policy scans, the pre-holdout lock, and `final-verification.json`. These remain ignored by Git.
- Failure logs: `/private/tmp/product-advisor-selection-baseline.log` (3 failures) and `/private/tmp/product-advisor-context-cluster-baseline.log` (2 failures).

## Reproduction

With the existing book/vector data and provider credentials supplied through the environment, use a new ignored output directory. The recorder refuses to overwrite an existing report. Subsequent stages fail closed on an unrecorded embedding and exit nonzero if any case fails. Only synthetic fixtures may be recorded.

```sh
DATA_DIR="$PWD/data" .venv/bin/python eval/retrieval_quality.py before --output data/quality-new-run
# Apply the change to compare, preserving the fixture and book chunks.
DATA_DIR="$PWD/data" .venv/bin/python eval/retrieval_quality.py after --output data/quality-new-run
```

Re-recording with the final code establishes a new baseline; it does not reproduce the old implementation unless the retained baseline source snapshot is restored in an isolated checkout.

## Limits

- No untouched holdout remains after the two observed failures were used for repairs. New questions are needed to estimate generalization.
- The 0.5 floor, 60/40 weighting, and 0.05 separation rule apply to this model and corpus; they are not probabilities or guarantees.
- A labeled evidence-page hit is a coverage check. It does not prove that every additional passage is useful or that advice is correct.
- Broad questions can legitimately retain many frameworks. Similarity grouping is a heuristic and can still over-select or omit a useful framework.
- The new production `mix` selector adds one provider embedding batch for up to two query texts. Production query text/vectors remain transient; only this synthetic evaluation records vectors.
- No NAS deployment or external HTTPS acceptance was performed for this revision.

## Hashes

- Frozen fixture SHA-256: `f47b37904cb4499e2a7bca7f3210e53f027d0a37e9165065453ce4e9dc0a7abf`
- Original chunks plus excerpts SHA-256: `a5fea4bcfcef8b5eb180224439aba8f2017e221a24fa249f6ead846785ad2cdf`
