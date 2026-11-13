# Evaluation

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
