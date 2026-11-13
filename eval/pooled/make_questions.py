"""Write the pooled evaluation's questions: 30 windows of 150 words sampled evenly across the book, one question each
from a model of a third family. Even ids describe a situation without the book's terms; odd ids may use them.

    BOOK_PDF=... OPENROUTER_API_KEY=... python eval/pooled/make_questions.py [--out eval/results/pooled]
"""

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RESULTS, chat, pages  # noqa: E402

GENERATOR = "openai/gpt-5.4-mini"
INTRO = "Below is a passage from a business book about pricing and monetizing new products. Write one question in Korean that "
SITUATION = INTRO + ("a member of a product or sales team might ask about their own work, which this passage would help answer. "
                     "Describe their situation in everyday words. Do not use the passage's distinctive terms, framework names, "
                     "company names, or headings. Output only the question.\n\n")
TERMS = INTRO + ("a reader of the book might ask, which this passage would help answer. You may use the book's own terms "
                 "(in English or Korean). Do not mention page numbers or headings. Output only the question.\n\n")

cli = argparse.ArgumentParser()
cli.add_argument("--out", type=Path, default=RESULTS / "pooled")
args = cli.parse_args()
P = pages()
rng = random.Random(7)
nums = sorted(p for p in P if len(P[p].split()) > 200)
items = []
for i in range(30):
    p = nums[int(i * len(nums) / 30 + rng.random() * len(nums) / 30)]
    words = P[p].split()
    start = rng.randrange(0, len(words) - 150)
    items.append({"id": i, "page": p, "style": "situation" if i % 2 == 0 else "terms", "window": " ".join(words[start:start + 150])})
with ThreadPoolExecutor(8) as ex:
    questions = ex.map(lambda it: chat(GENERATOR, (SITUATION if it["style"] == "situation" else TERMS) + it["window"], 400), items)
    for it, q in zip(items, questions, strict=True):
        it["question"] = q.splitlines()[0].strip()
        del it["window"]  # keep no book text in the results
args.out.mkdir(parents=True, exist_ok=True)
(args.out / "questions.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
print(f"wrote {len(items)} questions to {args.out / 'questions.json'}")
