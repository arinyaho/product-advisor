"""Judge every (question, page) pair that any run returned, blind to which run returned it, and score all runs on the
shared pool. Earlier judgments are reused, so adding a run only judges its new pages.

    BOOK_PDF=... OPENROUTER_API_KEY=... python eval/pooled/judge.py [--out eval/results/pooled] [--baseline page]

Metrics per run: nDCG@5 with the pool's best five grades as the ideal, the share of questions whose first result is
graded 2, and the paired difference in nDCG@5 against the baseline with a 95% bootstrap interval.
"""

import argparse
import json
import math
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RESULTS, chat, pages  # noqa: E402

JUDGE = "google/gemini-3.5-flash"
PROMPT = ("You judge search results. Given a question from a product team and one page from a business book on pricing "
          "and monetizing new products, rate how useful the page is for answering the question.\n"
          "2 = the page directly answers the question or holds its key advice\n"
          "1 = the page is related and partly useful\n"
          "0 = the page does not help\n"
          'Reply with JSON only: {{"score": <0|1|2>, "reason": "<one sentence>"}}\n\nQuestion: {q}\n\nPage:\n{page}')

cli = argparse.ArgumentParser()
cli.add_argument("--out", type=Path, default=RESULTS / "pooled")
cli.add_argument("--baseline", default="page")
args = cli.parse_args()
Q = {it["id"]: it for it in json.loads((args.out / "questions.json").read_text())}
runs = {f.stem[4:]: {int(k): v for k, v in json.loads(f.read_text()).items()} for f in sorted(args.out.glob("run_*.json"))}
jpath = args.out / "judgments.json"
J = json.loads(jpath.read_text()) if jpath.exists() else []
new = sorted({(q, r["page"]) for run in runs.values() for q, rs in run.items() for r in rs} - {(j["q"], j["page"]) for j in J})
random.Random(2).shuffle(new)  # no order that hints at the source run
if new:
    P = pages()

    def grade(pair):
        q, p = pair
        text = chat(JUDGE, PROMPT.format(q=Q[q]["question"], page=P.get(p, "")))
        j = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return {"q": q, "page": p, "score": int(j["score"]), "reason": j["reason"]}

    with ThreadPoolExecutor(8) as ex:
        J += list(ex.map(grade, new))
    jpath.write_text(json.dumps(J, ensure_ascii=False, indent=1))

g = {(j["q"], j["page"]): j["score"] for j in J}
dcg = lambda s: sum((2 ** x - 1) / math.log2(i + 2) for i, x in enumerate(s))  # noqa: E731
ideal = {q: dcg(sorted((v for (qq, _), v in g.items() if qq == q), reverse=True)[:5]) or 1 for q in Q}
ndcg = lambda run, q: dcg([g[(q, r["page"])] for r in run[q]]) / ideal[q]  # noqa: E731
print(f"judged {len(new)} new pairs; pool {len(J)}; grades " + ", ".join(f"{s}: {sum(j['score'] == s for j in J)}" for s in (0, 1, 2)))
base = runs[args.baseline]
for name, run in sorted(runs.items(), key=lambda kv: -sum(ndcg(kv[1], q) for q in Q)):
    d = [ndcg(run, q) - ndcg(base, q) for q in Q]
    rng = random.Random(0)
    boot = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(10000))
    top = sum(g[(q, run[q][0]["page"])] == 2 for q in Q) / len(Q)
    print(f"{name:30s} nDCG@5 {sum(ndcg(run, q) for q in Q) / len(Q):.3f}  vs {args.baseline} {sum(d) / len(d):+.3f} "
          f"[{boot[250]:+.3f}, {boot[9750]:+.3f}]  first result graded 2: {top:.0%}")
