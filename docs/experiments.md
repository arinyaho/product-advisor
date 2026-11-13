# Experiments

## Summary

| Question | Outcome | Section |
|---|---|---|
| Vanilla RAG or a knowledge graph (LightRAG, GraphRAG) for one book? | Hybrid search plus table-of-contents navigation and section reads answered multi-section questions completely; no graph | [Retrieval architecture](#retrieval-architecture), [Synthesis questions](#synthesis-questions) |
| Which embedding model? | `qwen/qwen3-embedding-4b` through OpenRouter, without a query instruction: 11 of 11 situations in Korean and in English at hit@3 | [Embedding models](#embedding-models) |
| Do query instructions help Qwen3-Embedding? | No; the instruction tried lowered every Qwen size | [Embedding models](#embedding-models) |
| Can teammates without the book answer chapter-wide questions? | Yes, from section summaries: all 34 expected items with an excerpt key | [Synthesis questions](#synthesis-questions), [Section summaries](#section-summaries) |
| Search endnotes, or link them? | Link: 80 of 81 notes attach to the body pages that cite them | [Endnotes](#endnotes) |
| Chunk by page windows or by document structure? | Structure chunks that also break at pages: nDCG@5 0.811 against 0.792 for page windows (difference within noise); chunks that cross pages lose up to 0.134 | [Chunking](#chunking) |
| Is chunk size the reason page windows did well? | No; page windows of 400 words matched 200, and larger structure chunks scored lower. Crossing page breaks was the cause | [Chunking](#chunking) |
| Do hand-labeled sets decide such comparisons? | No; single-label sets mark better answers as misses and title-written questions favor title-aware methods. A blind pooled evaluation decided | [Evaluation methods](#evaluation-methods) |
| Why were five headings never found? | The PDF sets "fi" and "fl" as ligature glyphs; NFKC normalization fixed headings and keyword search | [Ligatures](#ligatures) |

## Setup

**Book**: *Monetizing Innovation* (2022 edition PDF, 259 pages, about 77,000 words of extractable text, with a bookmark outline). Front matter, the index, and the endnotes are outside the searchable body.

**Machine**: Intel Xeon Gold 6248 (80 logical CPUs), 6 × NVIDIA A40 46 GB shared with other jobs, Ubuntu 24.04.4, Python 3.13.16, mcp 2.3.0, PyMuPDF 2.1, NumPy 2.5.4, fastembed 0.9.1, sentence-transformers 6.2.0, PyTorch 2.15.0.

**Remote models**: called through OpenRouter on 2026-11-09 and 2026-11-10, under an account data policy that admits only providers that do not retain data.

**Measures**: hit@k counts questions whose expected answer appears among the top k results; nDCG@5 uses graded judgments with the pool's best five grades as the ideal. Times are wall clock.

## Evaluation methods

Three kinds of evaluation were used, and their limits shaped the decisions.

| Method | Questions | Ground truth | Limit |
|---|---|---|---|
| Situation set | 11 situations written in Korean and English, each with one expected page range | Chosen by hand | One expected answer: a better page elsewhere counts as a miss |
| Boundary set | 19 Korean questions aimed at short sections and sections that start mid-page | One expected section, from its title | Written from section titles, so methods that embed titles are favored |
| Synthesis set | 6 Korean questions whose complete answer spans 4 to 9 sections (34 items in total) | Items from the table of contents | Measures coverage, not correctness of each claim |
| Pooled evaluation | 30 Korean questions written by `openai/gpt-5.4-mini` from 150-word windows sampled evenly across the book, half describing a situation without the book's terms | Every page any compared method returned in its top 5, judged 0 to 2 by `google/gemini-3.5-flash`, blind to the method, in shuffled order | LLM judges are lenient; 20 judgments are set aside for a human check in `eval/results/pooled/spot_check.md` |

The pooled evaluation follows the literature on test collections: synthetic queries with LLM judgments rank systems close to human judgments (Rahmani et al., SIGIR 2024; UMBRELA, 2024), unjudged documents treated as non-relevant penalize systems that retrieve differently (incremental pooling, arXiv 2609.02745), and LLM judges grade higher than people and favor outputs like their own (arXiv 2506.10301). Hence the pool of all methods' results, blind shuffled judging, and a judge from a different model family than the question writer, the summarizer, and the embedder.

## Retrieval architecture

LightRAG and GraphRAG extract entities and relations with an LLM and query a graph. That pays off when answers depend on relations across many documents. For one book, the authors' table of contents already expresses the structure, the extraction would cost LLM calls and add errors that are hard to see, and the cross-document case would require indexing team data from Slack and Notion outside their permissions. The design therefore uses hybrid search with tools that let Claude navigate the book, and the [synthesis questions](#synthesis-questions) test whether that suffices.

## Embedding models

Pipeline for every model: 200-word windows over each page with 100-word overlap, the best window per page, and BM25 over page tokens fused by reciprocal rank fusion. Questions: the situation set. Script: `eval/embeddings.py`.

| Model | Where | Korean hit@3 | English hit@3 | With BM25, Korean / English | Korean–English top-5 overlap |
|---|---|---|---|---|---|
| `qwen/qwen3-embedding-4b` | OpenRouter | **11** | **11** | 11 / 11 | 60% |
| `Qwen/Qwen3-Embedding-4B`, no instruction | Local A40, bf16 | **11** | **11** | 11 / 11 | 62% |
| `Qwen/Qwen3-Embedding-8B`, no instruction | Local A40, bf16 | 9 | 10 | 9 / 10 | 71% |
| `qwen/qwen3-embedding-8b` | OpenRouter | 9 | 10 | 9 / 10 | 67% |
| `Qwen/Qwen3-Embedding-0.6B`, no instruction | Local A40, bf16 | 9 | 9 | 9 / 10 | 62% |
| `openai/text-embedding-3-large` | OpenRouter | 8 | 9 | 8 / 10 | 69% |
| `intfloat/multilingual-e5-large` | Local CPU, fastembed | 7 | 6 | 7 / 7 | 49% |

A rerun of `eval/embeddings.py` for `qwen/qwen3-embedding-4b` reproduced 11 of 11 in both languages, with 58% overlap. With the query instruction "Given a business situation described by a product team, retrieve book passages with relevant pricing and monetization advice", Qwen3-Embedding scored 4 (0.6B), 5 (4B), and 6 (8B) of 11 in Korean with BM25, all below the same models without it. `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (80-word windows, CPU) found the expected chapter for one of two early Korean test questions and was not carried further. `google/gemini-embedding-2`, `mongodb/voyage-4-large`, `cohere/embed-5-pro`, and `nvidia/nemotron-3.5-embed-8b:free` were refused by the account's data policy and are untested.

| Model | Embedding the book once | One query |
|---|---|---|
| `qwen/qwen3-embedding-4b`, OpenRouter, 8 parallel requests | 7 s | network round trip |
| Qwen3-Embedding 0.6B / 4B / 8B, one A40, bf16, batch 8 | 7 s / 22 s / 34 s | |
| Qwen3-Embedding-0.6B, CPU, 8 threads (a laptop's scale) | about 33 min (1.31 s per 200-word chunk) | 0.44 s |
| Qwen3-Embedding-4B, CPU, 8 threads | 64 chunks did not finish in 10 min | |
| multilingual-e5-large, CPU, all 80 threads | about 4 min | |

Reading: the misses of weaker models were concept-level, such as a Korean description of selling too cheaply against the book's term for it; larger models did not close that gap reliably, the 4B model did on this set, and the skill's concept map gives Claude the book's terms for the cases any model misses. Running the 4B model locally would need a GPU on every machine, so the server calls it through OpenRouter.

## Synthesis questions

Six questions whose complete answer spans several sections, graded by which of the 34 expected items the answer covers. Agents answered through the server's tools (a command-line wrapper with the same functions), using only tool output, with the same model as the main session.

| Setup | Items covered | Tool calls | Time |
|---|---|---|---|
| One search, top 5 pages (page windows) | 14 / 34 | 1 per question | |
| One search, top 10 pages (page windows) | 18 / 34 | 1 per question | |
| One search, top 5 / top 10 pages (structure chunks with page breaks, ligatures fixed) | 12 / 34, 19 / 34 | 1 per question | |
| Agent with the tools, no skill, full key | **34 / 34** | 45 | 7.2 min |
| Agent with the tools and the skill, full key | **34 / 34** | 19 | 4.1 min |
| Agent with the tools and the skill, excerpt key | **34 / 34** | 26 | 5.3 min |

A single search, which is what vanilla RAG does, cannot cover a question such as "every company case and the principle it applied"; its seven cases spread over 40 pages. Navigation through the table of contents and section reads covers it, and the skill's concept map more than halves the calls by telling Claude which chapter to read. With an excerpt key the agent worked from summaries and passages and still covered every item; it also reported defects in the first version of the summaries, described next.

## Section summaries

Excerpt keys receive one LLM summary per section from `get_section`. Model: `anthropic/claude-haiku-5.5`, temperature 0, at most 150 words requested. The full build summarizes 129 sections in about 46 seconds for about two cents.

| Problem | Cause | Fix | Effect |
|---|---|---|---|
| 10 of 134 replies empty | With reasoning on, the model spent the whole 400-token budget thinking (`finish_reason: length`); 2,000 tokens still left one empty | Reasoning disabled, 2,000-token budget, an empty reply raises an error naming the section | No empty replies |
| One company case mixed into another's summary | Sections were cut at page breaks, and most sections start mid-page | Sections cut at their heading's position in the text | No cross-case mentions among the case summaries |
| Summaries saying the text was missing (5) | A parent section's own text only announces its subsections | The prompt tells the model the text is an introduction whose subsections are summarized separately | 3 remain |
| Chapter numbers the text does not name (4) | The model linked topics to chapters on its own | The prompt allows a chapter number only if the text names it | 2 remain |
| Sections with only a heading | Nothing to summarize | Sections with fewer than 40 words of their own text get no summary | |

The remaining remarks and references are minor; excerpt responses are labeled as summaries that should be cited by page range.

## Endnotes

The endnotes are bibliographic citations without the claims they support, and indexed as ordinary pages they surfaced in results. The body marks each citation with a superscript number in its PDF font flags. The server reads these markers, parses the endnotes per chapter (numbers restart in each chapter, and a note starts only at the next number in sequence, so a year at the start of a wrapped line is not taken for one), removes the endnote pages from the index, and returns the notes a page cites as `sources`.

| Measure | Value |
|---|---|
| Endnotes parsed | 81 across 11 chapters |
| Superscript markers in the body | 80 on 47 pages |
| Markers resolved to a note | 80 |
| Note never linked | Chapter 11, note 2, whose marker is not set in superscript |
| Situation set after the change | 11 / 11 Korean and English at hit@3, no endnote page among the results |

## Chunking

Search results and citations are pages. The question was whether chunks should follow pages or the document's structure, and why.

On the hand-labeled sets, structure chunks (paragraphs grouped up to 200 words within a section, with a "Chapter > Section" title prefix) looked better on the boundary set and worse on the situation set:

| Set, measure | Page windows @1 / @3 / @5 | Structure chunks with title prefix @1 / @3 / @5 |
|---|---|---|
| Situations, Korean, page hit | 7 / 11 / 11 | 7 / 9 / 10 |
| Situations, English, page hit | 8 / 11 / 11 | 6 / 10 / 11 |
| Boundary, section hit | 10 / 13 / 15 | 14 / 17 / 17 |
| Boundary, page hit | 12 / 15 / 17 | 14 / 17 / 17 |

The boundary questions were written from section titles, which favors a title prefix, and both sets accept one answer only, so the pooled evaluation decided. Each variant changes one factor; 315 pages were judged across all of them (58 graded 0, 160 graded 1, 97 graded 2).

| Variant | Boundaries | Size | Overlap | Title prefix | nDCG@5 | Against page windows (95% CI) | First result graded 2 |
|---|---|---|---|---|---|---|---|
| **Paragraphs within section and page, ligatures fixed (shipped)** | heading, page | ≤ 200 | none | no | **0.811** | +0.019 [−0.022, +0.066] | **77%** |
| Paragraphs within section and page | heading, page | ≤ 200 | none | no | 0.808 | +0.016 [−0.020, +0.058] | 77% |
| Paragraphs within section and page | heading, page | ≤ 200 | none | yes | 0.805 | +0.013 [−0.036, +0.055] | 73% |
| Page windows | page | 400 | 200 | no | 0.794 | +0.002 [−0.043, +0.046] | 73% |
| Page windows | page | 200 | 100 | no | 0.792 | baseline | 67% |
| Paragraphs within section | heading | ≤ 200 | none | no | 0.755 | −0.036 [−0.079, +0.005] | 63% |
| Paragraphs within section | heading | ≤ 200 | none | yes | 0.718 | −0.074 [−0.127, −0.022] | 53% |
| Paragraphs within section | heading | ≤ 400 | none | no | 0.671 | −0.120 [−0.207, −0.035] | 43% |
| Paragraphs within section | heading | ≤ 400 | none | yes | 0.657 | −0.134 [−0.224, −0.048] | 50% |

Intervals are 95% paired bootstrap intervals (10,000 resamples over the 30 questions).

- Size does not explain the page windows' result: 400-word page windows matched 200-word ones, and larger structure chunks scored lower.
- Crossing page breaks does. A result is a page, and a chunk that runs onto the next page credits only the page it starts on; the larger the chunk, the more often it crosses. Cutting structure chunks at page breaks restored them to the page windows' level.
- The title prefix never helped and hurt chunks that crossed pages.
- The shipped variant ranks as well as page windows (the interval includes zero) and adds two properties: each result names the section of the chunk that matched, so a page holding several short sections reports the right one, and excerpt passages start at paragraph boundaries.

The conclusion is tied to a page as the unit of results and citations. A reflowable ebook has no stable pages; there, sections would be the unit of results and citations, and chunks should not cross sections.

The shipped version on the hand-labeled sets (`eval/labeled.py`): situations 7 / 10 / 10 Korean and 5 / 9 / 11 English at hit@1 / @3 / @5, boundary 15 / 17 / 17 by section and 11 / 18 / 18 by page.

## Ligatures

Five section headings were never located in the text, so their sections absorbed their neighbors. The PDF sets "fi" and "fl" as single ligature glyphs (U+FB01, U+FB02), so the extracted text read "ﬁ" where the table of contents read "fi". The same glyphs split words for keyword search: "difficult" tokenized as "dif" and "cult". Page text is NFKC-normalized at extraction, which locates every heading in the body; the shipped variant's evaluation above includes the fix.
