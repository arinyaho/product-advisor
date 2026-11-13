"""Compare embedding models on the situation set, in Korean and English, over the same retrieval pipeline:
200-word page windows (half overlap), best chunk per page, fused with page-level BM25 by reciprocal rank fusion.

    BOOK_PDF=... OPENROUTER_API_KEY=... python eval/embeddings.py openrouter:qwen/qwen3-embedding-4b
    python eval/embeddings.py st:Qwen/Qwen3-Embedding-0.6B          # sentence-transformers (needs torch; GPU if present)
    python eval/embeddings.py fastembed:intfloat/multilingual-e5-large
    python eval/embeddings.py st:Qwen/Qwen3-Embedding-4B --instruction "Given a business situation, ..."

Reports hit@3 per language, the same with BM25 fused in, and how much the Korean and English top 5 overlap.
"""

import argparse
import math
import re
from collections import Counter

import numpy as np

from common import ROOT, openrouter, pages
from sets import SITUATIONS

cli = argparse.ArgumentParser()
cli.add_argument("model", help="openrouter:<id> | st:<hf id> | fastembed:<id>")
cli.add_argument("--instruction", help="Qwen-style query instruction; it lowered scores when tried")
args = cli.parse_args()
kind, name = args.model.split(":", 1)

P = pages()
chunks = []
for p, text in P.items():
    w = text.split()
    chunks += [(p, " ".join(w[i:i + 200])) for i in range(0, max(len(w) - 100, 1), 100) if w]


def embedder():
    e5 = "e5" in name
    if kind == "openrouter":
        def run(texts):
            out = []
            for i in range(0, len(texts), 64):
                data = openrouter("embeddings", {"model": name, "input": texts[i:i + 64]})["data"]
                out += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
            return np.array(out)
        return run, run
    if kind == "st":
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(name, model_kwargs={"torch_dtype": "bfloat16"})
        query = (lambda qs: m.encode([f"Instruct: {args.instruction}\nQuery: {q}" for q in qs])) if args.instruction else m.encode
        return (lambda docs: m.encode(docs, batch_size=8)), query
    from fastembed import TextEmbedding
    m = TextEmbedding(name)
    pre = ("passage: ", "query: ") if e5 else ("", "")
    return (lambda docs: np.array(list(m.embed([pre[0] + d for d in docs])))), (lambda qs: np.array(list(m.embed([pre[1] + q for q in qs]))))


cache = ROOT / ".cache" / "embeddings" / (re.sub(r"\W", "_", args.model) + ".npy")
embed_docs, embed_queries = embedder()
if cache.exists():
    V = np.load(cache)
else:
    cache.parent.mkdir(parents=True, exist_ok=True)
    V = embed_docs([c for _, c in chunks])
    np.save(cache, V)
V = V / np.linalg.norm(V, axis=1, keepdims=True)

tok = lambda t: re.findall(r"[a-z0-9]+", t.lower())  # noqa: E731
docs = {p: Counter(tok(t)) for p, t in P.items()}
avg = sum(sum(c.values()) for c in docs.values()) / len(docs)
df = Counter(w for c in docs.values() for w in c)


def bm25(q):
    s = {p: sum(math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5)) * c[w] * 2.2 / (c[w] + 1.2 * (0.25 + 0.75 * sum(c.values()) / avg))
                for w in tok(q) if w in df) for p, c in docs.items()}
    return sorted((p for p in s if s[p] > 0), key=s.get, reverse=True)


def ranked(qv):
    best = {}
    for (p, _), x in zip(chunks, V @ (qv / np.linalg.norm(qv)), strict=True):
        best[p] = max(best.get(p, -1.0), float(x))
    return sorted(best, key=best.get, reverse=True)


def fuse(*lists):
    f = Counter()
    for r in lists:
        for i, p in enumerate(r):
            f[p] += 1 / (60 + i)
    return [p for p, _ in f.most_common()]


Q = embed_queries([s[0] for s in SITUATIONS] + [s[1] for s in SITUATIONS])
n = len(SITUATIONS)
score = Counter()
overlap = 0
for i, (ko, en, lo, hi) in enumerate(SITUATIONS):
    rk, re_ = ranked(np.asarray(Q[i])), ranked(np.asarray(Q[n + i]))
    hit = lambda r, lo=lo, hi=hi: any(lo <= p <= hi for p in r[:3])  # noqa: E731
    score["ko"] += hit(rk)
    score["en"] += hit(re_)
    score["ko+bm25"] += hit(fuse(bm25(ko), rk))
    score["en+bm25"] += hit(fuse(bm25(en), re_))
    overlap += len(set(rk[:5]) & set(re_[:5])) / 5
print(f"{args.model}: hit@3 Korean {score['ko']}/{n}, English {score['en']}/{n}; with BM25 Korean {score['ko+bm25']}/{n}, "
      f"English {score['en+bm25']}/{n}; Korean-English top-5 overlap {overlap / n:.0%}")
