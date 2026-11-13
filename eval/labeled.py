"""Hit rates of one server version on the hand-labeled sets, and single-search coverage of the synthesis set.

    BOOK_PDF=... OPENROUTER_API_KEY=... python eval/labeled.py [--server mcp] [--data-dir eval/.cache/labeled]
"""

import argparse

from common import ROOT, load_server
from sets import BOUNDARY, SITUATIONS, SYNTHESIS

cli = argparse.ArgumentParser()
cli.add_argument("--server", default=ROOT.parent / "mcp")
cli.add_argument("--data-dir", default=ROOT / ".cache" / "labeled")
args = cli.parse_args()
server = load_server(args.server, args.data_dir)
by_title = {s["title"]: s for s in server.SECTIONS}


def hits(rows, ok):
    """Hit@1, @3, @5 over rows of (query, expected)."""
    res = [server.search(q, 5, "full")["results"] for q, _ in rows]
    return [sum(ok(r[:k], e) for r, (_, e) in zip(res, rows, strict=True)) for k in (1, 3, 5)]


in_pages = lambda res, e: any(e[0] <= r["page"] <= e[1] for r in res)  # noqa: E731
for lang, i in (("Korean", 0), ("English", 1)):
    print(f"situations {lang:7s} page hit@1/3/5", hits([(s[i], s[2:]) for s in SITUATIONS], in_pages), f"of {len(SITUATIONS)}")
rows = [(q, by_title[t]) for t, q in BOUNDARY]
print("boundary section hit@1/3/5", hits(rows, lambda res, s: any(r["section"] == s["title"] for r in res)), f"of {len(rows)}")
print("boundary page    hit@1/3/5", hits(rows, lambda res, s: any(s["start"] <= r["page"] <= s["end"] for r in res)), f"of {len(rows)}")

total = {5: 0, 10: 0}
for q, prefixes in SYNTHESIS:
    expected = [s for s in server.SECTIONS if s["title"].startswith(tuple(prefixes))]
    for k in total:
        pages = [r["page"] for r in server.search(q, k, "full")["results"]]
        total[k] += sum(any(s["start"] <= p <= s["end"] for p in pages) for s in expected)
n = sum(len([s for s in server.SECTIONS if s["title"].startswith(tuple(p))]) for _, p in SYNTHESIS)
print(f"synthesis, one search: expected sections reached in top 5 {total[5]}/{n}, top 10 {total[10]}/{n}")
