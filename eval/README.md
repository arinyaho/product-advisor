# Evaluation

TL;DR: Run the frozen 15-question v2 source regression and the separate three-question body-evidence regression in both mix and naive. Every result must preserve its source identity, content, location, book scope, actual mode and 4,000-token response limit. Index/contents and heading-only hits fail a case even if another returned source is correct. These are development regressions, not held-out precision benchmarks.

## Three-book evaluations

| Fixture | Purpose |
|---|---|
| `three_book_cases.json` | Original v1 questions and annotations; unchanged historical failures remain failures. |
| `three_book_cases_v2.json` | Same 15 questions; explicit source-reviewed additions correct the pilot-question annotations. See the [annotation audit](../docs/source-annotation-audit.md). |
| `three_book_evidence_cases.json` | Three generalized review questions with source-checked body passages; tests lifecycle, pilot qualification and an all-book reoffer question. Contains no customer records or book text. |

Use a sealed private library with the matching query profile. Supply `OPENROUTER_API_KEY` through your secret mechanism, not command arguments:

```sh
export DATA_DIR="$PWD/data" PYTHONPATH=mcp
python eval/three_books.py data/libraries/<version> --cases eval/three_book_cases_v2.json --output data/comparison-v2.json
python eval/three_books.py data/libraries/<version> --cases eval/three_book_evidence_cases.json --output data/comparison-body.json
```

- Expected body passages for the three new questions were read before changing the filter. Both modes use k=5, the same original question and a 4,000-token final evidence limit. Positive cases require an expected source; negative cases require no hits. These criteria do not measure the relevance of every substantive result.
- The runner pins the corpus hash and records fixture, retrieval and evaluation versions. Use a new output file for a changed question set, query configuration or evaluation criterion; do not overwrite or resume an older passing result under stricter rules.
- Completed receipts are reused when resuming a matching interrupted run. Runtime failures and their uncertain charges remain separate; ready semantic failures are not retried until they pass. Each invocation has a $0.20 / 90-call limit. No index is built by the evaluator.
- See [body-evidence validation](../docs/evidence-filter.md) for the current local result and [deployed validation](../docs/three-book-validation.md) for production acceptance. Local results are not production acceptance.

The filter's local regression tests need the legacy reference PDF because the current server imports it. They make no provider calls:

```sh
export DATA_DIR="$PWD/data" PYTHONPATH=mcp
python -m pytest mcp/test_evidence_filter.py mcp/test_query_profile_reuse.py eval/test_three_book_annotations.py -q
```

Install the pinned `mcp/requirements.txt` and pytest in your local environment. The existing suite also has provider-backed legacy tests; do not describe an unexecuted paid test as passed.

## Historical single-book evaluations

Scripts that reproduce the measurements in [docs/experiments.md](../docs/experiments.md), and the recorded results of the pooled evaluation. Nothing here contains book text: questions are written by a model, results hold page numbers and section titles, and judgments hold a one-sentence reason.

All scripts need `BOOK_PDF` (the book's PDF) and `OPENROUTER_API_KEY`, and run with this directory's requirements:

```sh
uv run --python 3.13 --with-requirements eval/requirements.txt python eval/<script>.py
```

Caches go to `eval/.cache/` (git-ignored). A first run of a script that builds a server index takes about three minutes.

## Scripts

| Script | Measures | Cost |
|---|---|---|
| `labeled.py [--server DIR]` | Hit@1/3/5 of one server version on the situation and boundary sets, and how much of the synthesis set a single search reaches | One index build |
| `embeddings.py MODEL [--instruction TEXT]` | One embedding model on the situation set in Korean and English over a fixed pipeline; `MODEL` is `openrouter:<id>`, `st:<hf id>` (sentence-transformers, GPU if present), or `fastembed:<id>` | One book embedding per model |
| `pooled/make_questions.py` | Writes 30 questions from passages sampled across the book (`gpt-5.4-mini`) | Under a cent |
| `pooled/run_system.py NAME [--server DIR]` | Saves one server version's top 5 pages per question as `run_NAME.json` | One index build |
| `pooled/judge.py [--baseline NAME]` | Judges every new (question, page) pair blind (`gemini-3.5-flash`), then reports nDCG@5, the first result's grade, and the paired difference against the baseline with a bootstrap interval | Cents per new run |

`sets.py` holds the hand-labeled question sets; `common.py` holds page extraction, OpenRouter calls, and loading any version of `mcp/server.py`.

## Compare a change

1. Save the current server's results: `python eval/pooled/run_system.py before`.
2. Make the change in `mcp/server.py`, or point `--server` at a directory with the other version.
3. Save the changed server's results: `python eval/pooled/run_system.py after`.
4. Judge and compare: `python eval/pooled/judge.py --baseline before`.

The judge reuses earlier judgments, so only pages the new run brings in are judged. Keep `questions.json` fixed across a comparison; writing new questions resets what the scores mean.

## Recorded results

`results/pooled/` holds the 30 questions, the runs of the nine chunking variants in docs/experiments.md, 315 judgments, and `spot_check.md`: 20 judgments sampled for a person to check against the book, since LLM judges grade more leniently than people.
