"""MCP server that searches and reads "Monetizing Innovation" from a local PDF, over HTTP with per-person keys.

Commands:
    python server.py                                   serve on 0.0.0.0:8000/mcp
    python server.py issue-key NAME --days 30 [--scope full|excerpt]
    python server.py renew-key NAME --days 30
    python server.py revoke-key NAME
    python server.py list-keys
"""

import argparse
import bisect
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path

import numpy as np
import pymupdf
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BOOK_PDF = Path(os.environ.get("BOOK_PDF", DATA_DIR / "book.pdf"))
KEYS_FILE = DATA_DIR / "keys.json"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen/qwen3-embedding-4b")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "anthropic/claude-haiku-5.5")
MAX_SECTION_PAGES = 10  # one get_section call returns at most this many pages
MIN_SUMMARY_WORDS = 40  # below this a section's own text is little more than its heading
CHUNK_WORDS = 200  # ranking unit
EXCERPT_WORDS = 60  # what an excerpt-scope key gets per hit
SKIP = {"Cover", "Praise for Monetizing Innovation", "Title Page", "Copyright", "CONTENTS", "INDEX", "Back Cover"}

doc = pymupdf.open(BOOK_PDF)
toc = doc.get_toc()
CHAPTER_STARTS = sorted((p, int(re.search(r"\d+", t).group())) for lv, t, p in toc if lv == 2 and t.startswith("CHAPTER"))


def _chapter_of(page):
    return max((c for c in CHAPTER_STARTS if c[0] <= page), default=(0, 0))[1]


def _read_page(n):
    """Page text without superscript note markers, and the (chapter, note number) pairs those markers cite."""
    blocks, marks = [], []
    for block in doc[n - 1].get_text("dict")["blocks"]:
        lines = []
        for line in block.get("lines", []):
            text = ""
            for span in line["spans"]:
                if span["flags"] & 1 and re.fullmatch(r"\d+", span["text"].strip()):  # superscript digits
                    marks.append((_chapter_of(n), int(span["text"])))
                else:
                    text += span["text"]
            lines.append(text)
        if lines:
            blocks.append("\n".join(lines))
    # NFKC unfolds the ligature glyphs the PDF uses ("ﬁ" -> "fi"), so words and headings match their spelling
    return unicodedata.normalize("NFKC", "\n\n".join(blocks)), marks  # a blank line marks a paragraph break


_read = {n: _read_page(n) for n in range(1, doc.page_count + 1)}
PAGES = {n: text for n, (text, _) in _read.items()}
PAGE_NOTES = {n: marks for n, (_, marks) in _read.items() if marks}


def _end_page(i):
    level, _, start = toc[i]
    nxt = next((p for lv, _, p in toc[i + 1:] if lv <= level), doc.page_count + 1)
    return max(start, nxt - 1)


SECTIONS = [
    {"id": i, "level": lv, "title": t.strip(), "start": p, "end": _end_page(i)}
    for i, (lv, t, p) in enumerate(toc) if t.strip() not in SKIP
]
NOTES_START = next(p for _, t, p in toc if t == "NOTES")
CONTENT_PAGES = range(SECTIONS[0]["start"], NOTES_START)  # notes are linked to the body, not searched


def _parse_notes():
    """Endnotes keyed by (chapter, number). Numbers restart per chapter; a line starts a note only when it carries
    the next number in sequence, so a year or figure at the start of a wrapped line is not taken for one."""
    text = "\n".join(PAGES[p] for p in range(NOTES_START, next(p for _, t, p in toc if t == "INDEX")))
    notes, chapter, current = {}, None, None
    for line in text.splitlines():
        if m := re.fullmatch(r"\s*Chapter (\d+)\s*", line):
            chapter, current = int(m.group(1)), None
            continue
        expected = (current or 0) + 1
        if chapter and (m := re.match(rf"\s*{expected}\.\s+(.*)", line)):
            current = expected
            notes[(chapter, current)] = m.group(1)
        elif chapter and current:
            notes[(chapter, current)] += " " + line.strip()
    return notes


NOTES = _parse_notes()


def _sources(page):
    return [{"note": n, "text": NOTES.get((c, n), "")} for c, n in PAGE_NOTES.get(page, [])]


def _section_of(page):
    return max((s for s in SECTIONS if s["start"] <= page), key=lambda s: (s["start"], s["level"]))["title"]


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


DOCS = {p: Counter(_tokens(PAGES[p])) for p in CONTENT_PAGES}
AVG_LEN = sum(sum(c.values()) for c in DOCS.values()) / len(DOCS)
DF = Counter(w for c in DOCS.values() for w in c)


def _bm25_rank(query):
    terms = [w for w in _tokens(query) if w in DF]
    scores = {}
    for p, c in DOCS.items():
        n = sum(c.values())
        score = sum(
            math.log(1 + (len(DOCS) - DF[w] + 0.5) / (DF[w] + 0.5))
            * c[w] * 2.2 / (c[w] + 1.2 * (0.25 + 0.75 * n / AVG_LEN))
            for w in terms
        )
        if score > 0:
            scores[p] = score
    return sorted(scores, key=scores.get, reverse=True)


# --- OpenRouter: embeddings and section summaries --------------------------------------------

def _openrouter(path, payload):
    """POST to OpenRouter, retrying rate limits (honoring Retry-After) and server errors."""
    req = urllib.request.Request(
        f"https://openrouter.ai/api/v1/{path}", json.dumps(payload).encode(),
        {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"},
    )
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == 7:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))


def _embed(texts):
    vectors = []
    for i in range(0, len(texts), 64):
        data = _openrouter("embeddings", {"model": EMBED_MODEL, "input": texts[i:i + 64]})["data"]
        vectors += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    v = np.array(vectors, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


INDEX_READY = threading.Event()
SUMMARIES_READY = threading.Event()


@cache
def _index():
    """Embed the book once and keep the vectors in DATA_DIR. Raises if OpenRouter is unreachable; not cached then."""
    inputs = [c.text for c in CHUNKS], [c.text for c in EXCERPTS]
    digest = hashlib.sha256("\0".join(inputs[0] + inputs[1]).encode()).hexdigest()[:12]
    path = DATA_DIR / f"vectors-{EMBED_MODEL.replace('/', '_')}-{digest}.npz"  # new text or new cuts: new vectors
    if not path.exists():
        np.savez(path, chunks=_embed(inputs[0]), excerpts=_embed(inputs[1]))
    v = np.load(path)
    INDEX_READY.set()
    return v["chunks"], v["excerpts"]


def _children(id):
    """Ids of the direct subsections of section id, in book order."""
    i = next(i for i, s in enumerate(SECTIONS) if s["id"] == id)
    level, out = SECTIONS[i]["level"], []
    for s in SECTIONS[i + 1:]:
        if s["level"] <= level:
            break
        if s["level"] == level + 1:
            out.append(s["id"])
    return out


# Sections are cut where their heading appears in the text, not at page breaks, because a section usually
# starts mid-page and a page-based cut mixes the end of one section into the next.
BODY = "".join(PAGES[p] + "\n" for p in CONTENT_PAGES)
PAGE_OFFSET = {}
_at = 0
for _p in CONTENT_PAGES:
    PAGE_OFFSET[_p] = _at
    _at += len(PAGES[_p]) + 1
_NORM_POS = [i for i, c in enumerate(BODY) if c.isalnum()]
_NORM = "".join(BODY[i].lower() for i in _NORM_POS)


def _norm(text):
    return "".join(c for c in text.lower() if c.isalnum())


@cache
def _heading_pos(id):
    """Offset in BODY where section id's heading starts, searched on its first page and the next; None if not found."""
    s = next(x for x in SECTIONS if x["id"] == id)
    if s["start"] not in PAGE_OFFSET:
        return None
    lo = PAGE_OFFSET[s["start"]]
    hi = PAGE_OFFSET.get(s["start"] + 2, len(BODY))
    lo_n = next((k for k, i in enumerate(_NORM_POS) if i >= lo), len(_NORM))
    hi_n = next((k for k, i in enumerate(_NORM_POS) if i >= hi), len(_NORM))
    k = _NORM.find(_norm(s["title"])[:25], lo_n, hi_n)
    return _NORM_POS[k] if k >= 0 else None


def _heading_found(s):
    return _heading_pos(s["id"]) is not None


def _own_span(s):
    """BODY offsets from a section's heading to the next heading in the book: the whole section for a leaf, the
    intro for a parent. Empty for sections outside the searchable body (endnotes)."""
    if s["start"] not in PAGE_OFFSET:
        return 0, 0
    start = _heading_pos(s["id"]) or PAGE_OFFSET[s["start"]]
    later = [x for x in SECTIONS if x["id"] > s["id"] and x["start"] in PAGE_OFFSET]
    end = next((_heading_pos(x["id"]) or PAGE_OFFSET[x["start"]] for x in later), len(BODY))
    if end <= start:
        end = PAGE_OFFSET.get(s["end"] + 1, len(BODY))
    return start, end


def _own_text(s):
    a, b = _own_span(s)
    return BODY[a:b]


_PAGE_STARTS = [PAGE_OFFSET[p] for p in CONTENT_PAGES]


def _page_at(offset):
    return CONTENT_PAGES[bisect.bisect_right(_PAGE_STARTS, offset) - 1]


Chunk = namedtuple("Chunk", "page text section")


def _structure_chunks():
    """Chunks that follow the book's structure and its pages: each lies inside one section's own text and on one
    page, and groups whole paragraphs up to CHUNK_WORDS (a longer paragraph is split). Results are pages, so a chunk
    that ran onto the next page would credit only the page it started on. Excerpt windows are cut inside each chunk."""
    chunks, excerpts = [], []
    step = EXCERPT_WORDS // 2
    for s in SECTIONS:
        a, b = _own_span(s)
        words, breaks = [], set()
        for m in re.finditer(r"\S+|\n\s*\n", BODY[a:b]):
            if m.group().strip():
                words.append((a + m.start(), m.group()))
            else:
                breaks.add(len(words))
        pages = [_page_at(offset) for offset, _ in words]
        new_page = {i for i in range(1, len(words)) if pages[i] != pages[i - 1]}
        bounds = sorted({0, *breaks, *new_page} - {len(words)})
        groups = []
        for i, j in zip(bounds, bounds[1:] + [len(words)]):  # noqa: B905 - an empty section has no bounds, and zips to nothing
            if groups and i not in new_page and j - groups[-1][0] <= CHUNK_WORDS:
                groups[-1][1] = j
            else:
                groups.append([i, j])
        for i, j in groups:
            for k in range(i, j, CHUNK_WORDS):
                end = min(j, k + CHUNK_WORDS)
                chunks.append(Chunk(pages[k], " ".join(w for _, w in words[k:end]), s["id"]))
                for e in range(k, max(end - step, k + 1), step):
                    excerpts.append(Chunk(pages[k], " ".join(w for _, w in words[e:min(end, e + EXCERPT_WORDS)]), s["id"]))
    return chunks, excerpts


CHUNKS, EXCERPTS = _structure_chunks()


SUMMARY_PROMPT = (
    "Summarize this section of the book \"Monetizing Innovation\" titled \"{title}\" for a product team. "
    "Write in your own words, at most 150 words, as plain prose. Keep the names of every framework, list item, "
    "rule, and company case it presents, and the decision or principle each one supports. Use only the text below: "
    "do not refer to other chapters, do not spell out an abbreviation the text does not spell out, and if the text "
    "is short, summarize what is there without remarking on what is missing. Mention a chapter number only if the "
    "text names it. Do not quote more than five consecutive words.{intro}\n\n"
)
INTRO_NOTE = (" This text is only the introduction of the section; its subsections are summarized separately, so "
              "summarize the introduction and name what it says the subsections cover.")


def _summarize(title, text, intro):
    reply = _openrouter("chat/completions", {  # with reasoning on, the model can spend the whole budget thinking
        "model": SUMMARY_MODEL, "max_tokens": 2000, "reasoning": {"enabled": False}, "temperature": 0,
        "messages": [{"role": "user", "content": SUMMARY_PROMPT.format(title=title, intro=INTRO_NOTE if intro else "") + text}]})
    choice = reply["choices"][0]
    if not choice["message"].get("content"):
        raise RuntimeError(f"summary of {title!r} came back empty (finish_reason={choice.get('finish_reason')})")
    return choice["message"]["content"].strip()


def _summaries_path():
    inputs = SUMMARY_PROMPT + INTRO_NOTE + "\0".join(_own_text(s) for s in SECTIONS)  # new prompt or new cuts: new summaries
    return DATA_DIR / f"summaries-{SUMMARY_MODEL.replace('/', '_')}-{hashlib.sha256(inputs.encode()).hexdigest()[:12]}.json"


@cache
def _summaries():
    """Summaries of each section's own pages, written once by an LLM and kept in DATA_DIR. Excerpt-scope keys read these."""
    path = _summaries_path()
    if path.exists():
        out = {int(k): v for k, v in json.loads(path.read_text()).items()}
    else:
        todo = [(s, _own_text(s)) for s in SECTIONS]
        todo = [(s, t) for s, t in todo if len(t.split()) >= MIN_SUMMARY_WORDS]
        with ThreadPoolExecutor(8) as pool:
            done = pool.map(lambda st: _summarize(st[0]["title"], st[1], bool(_children(st[0]["id"]))), todo)
            out = {s["id"]: summary for (s, _), summary in zip(todo, done, strict=True)}
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    SUMMARIES_READY.set()
    return out


def _build_until_done():
    """Serve keyword search and refuse section summaries meanwhile; retry until OpenRouter answers."""
    for build, name in ((_index, "embedding index"), (_summaries, "section summaries")):
        for attempt in range(1000):
            try:
                build()
                print(f"{name} ready", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                print(f"{name} not built yet: {e}", file=sys.stderr)
                time.sleep(min(60 * 2 ** attempt, 3600))


def _best_per_page(items, scores):
    """For each page, the best-scoring chunk that starts on it, with its score."""
    best = {}
    for c, s in zip(items, scores, strict=True):
        if c.page not in best or s > best[c.page][0]:
            best[c.page] = (float(s), c)
    return best


def _keyword_excerpt(page, query):
    terms = set(_tokens(query))
    on_page = [c for c in EXCERPTS if c.page == page] or [Chunk(page, " ".join(PAGES[page].split()[:EXCERPT_WORDS]), None)]
    return max(on_page, key=lambda c: sum(w in terms for w in _tokens(c.text))).text


def search(query, k, scope):
    """Rank pages by BM25 and embeddings (reciprocal rank fusion); fall back to BM25 when OpenRouter fails."""
    ranked = [_bm25_rank(query)]
    excerpts, matched = {}, {}
    try:
        if not INDEX_READY.is_set():
            raise RuntimeError("embedding index is still being built")
        chunk_vecs, excerpt_vecs = _index()
        q = _embed([query])[0]
        by_page = _best_per_page(CHUNKS, chunk_vecs @ q)
        ranked.append(sorted(by_page, key=lambda p: by_page[p][0], reverse=True))
        matched = {p: c.section for p, (_, c) in by_page.items()}
        excerpts = {p: c.text for p, (_, c) in _best_per_page(EXCERPTS, excerpt_vecs @ q).items()}
        semantic = True
    except Exception as e:  # noqa: BLE001 - any OpenRouter failure degrades to keyword search
        print(f"semantic search unavailable: {e}", file=sys.stderr)
        semantic = False
    fused = Counter()
    for r in ranked:
        for i, p in enumerate(r):
            fused[p] += 1 / (60 + i)
    results = []
    for p, _ in fused.most_common(k):
        text = PAGES[p] if scope == "full" else excerpts.get(p) or _keyword_excerpt(p, query)
        section = next((s["title"] for s in SECTIONS if s["id"] == matched.get(p)), None) or _section_of(p)
        results.append({"page": p, "section": section, "text": text, "sources": _sources(p)})
    return {"semantic": semantic, "results": results}


def read_section(id, scope, start_page=None):
    """Full scope: the section's text, MAX_SECTION_PAGES pages at a time. Excerpt scope: LLM summaries of the section
    and every subsection under it, never the book's text."""
    s = next((s for s in SECTIONS if s["id"] == id), None)
    if s is None:
        raise ValueError(f"No section with id {id}; take ids from list_sections.")
    if scope == "full":
        first = max(start_page or s["start"], s["start"])
        last = min(s["end"], first + MAX_SECTION_PAGES - 1)
        out = f"# {s['title']} (pp.{s['start']}-{s['end']})\n\n" + "\n".join(f"[p.{p}]\n{PAGES[p]}" for p in range(first, last + 1))
        if last < s["end"]:
            out += f"\n\n[continued: call get_section with id={id}, start_page={last + 1}]"
        return out
    if not SUMMARIES_READY.is_set():
        raise PermissionError("Section summaries for excerpt-scope keys are being prepared; use search_book meanwhile.")
    summaries, parts = _summaries(), []

    def walk(sec, depth):
        parts.append(f"{'#' * depth} {sec['title']} (pp.{sec['start']}-{sec['end']})")
        if sec["id"] in summaries:
            parts.append(summaries[sec["id"]])
        for kid in _children(sec["id"]):
            walk(next(x for x in SECTIONS if x["id"] == kid), depth + 1)

    walk(s, 1)
    return "Summary written for this key's excerpt scope; cite the page ranges.\n\n" + "\n\n".join(parts)


# --- per-person keys -------------------------------------------------------------------------

def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _load_keys():
    return json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}


def _save_keys(keys):
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=2))
    tmp.chmod(0o600)
    tmp.replace(KEYS_FILE)


def _expiry(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def issue_key(name, days, scope="excerpt"):
    """Issue a new key for name, replacing any earlier one. Only the hash is stored; the key is returned once."""
    if scope not in ("full", "excerpt"):
        raise ValueError(scope)
    keys = {h: e for h, e in _load_keys().items() if e["name"] != name}
    key = secrets.token_urlsafe(32)
    keys[_hash(key)] = {"name": name, "scope": scope, "expires": _expiry(days)}
    _save_keys(keys)
    return key


def _update(name, change):
    keys = _load_keys()
    hits = [h for h, e in keys.items() if e["name"] == name]
    if not hits:
        raise SystemExit(f"no key named {name}")
    for h in hits:
        change(keys, h)
    _save_keys(keys)


def renew_key(name, days):
    _update(name, lambda keys, h: keys[h].update(expires=_expiry(days)))


def revoke_key(name):
    _update(name, lambda keys, h: keys.pop(h))


def key_scope(key):
    """Scope of a valid, unexpired key, else None. Reads the file on every call so changes apply without a restart."""
    entry = _load_keys().get(_hash(key))
    if entry and datetime.fromisoformat(entry["expires"]) > datetime.now(timezone.utc):
        return entry["scope"]
    return None


def _bearer(headers):
    value = headers.get("authorization", "")
    return key_scope(value[7:]) if value.lower().startswith("bearer ") else None


# --- MCP ---------------------------------------------------------------------------------------

mcp = MCPServer("product-advisor")


@mcp.tool()
def list_sections() -> list[dict]:
    """List the book's chapters and subsections (id, level, title, PDF page range)."""
    return SECTIONS


@mcp.tool()
def get_section(id: int, ctx: Context, start_page: int | None = None) -> str:
    """Read a chapter or subsection by id from list_sections. Full-scope keys get the text with PDF page markers,
    up to 10 pages per call; follow the [continued: ...] note with start_page to read on. Excerpt-scope keys get
    summaries of the section and all its subsections instead. Use this for questions that span a whole chapter."""
    try:
        return read_section(id, _bearer(ctx.headers), start_page)
    except (PermissionError, ValueError) as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def search_book(query: str, ctx: Context, k: int = 5) -> dict:
    """Search the book by meaning and keywords. Accepts Korean or English and either a described situation
    ("customers say our product is too expensive") or the book's terms ("willingness to pay",
    "leaders fillers killers", "good better best", "freemium", "price war").
    Returns the top k pages with page number and section title, the page text (full scope) or a short
    passage (excerpt scope), and "sources": the endnotes that page cites, for attributing the book's claims. "semantic": false means only keyword search ran; retry with the book's English terms."""
    return search(query, k, _bearer(ctx.headers))


def app():
    """The MCP HTTP app behind a bearer-key check. Host checks are off: keys authenticate, a reverse proxy fronts it."""
    inner = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            if _bearer(headers) is None:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"invalid or expired key"})
                return
        await inner(scope, receive, send)

    return guarded


def main():
    cli = argparse.ArgumentParser()
    sub = cli.add_subparsers(dest="cmd")
    p = sub.add_parser("issue-key")
    p.add_argument("name")
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--scope", choices=["full", "excerpt"], default="excerpt")
    p = sub.add_parser("renew-key")
    p.add_argument("name")
    p.add_argument("--days", type=int, required=True)
    sub.add_parser("revoke-key").add_argument("name")
    sub.add_parser("list-keys")
    args = cli.parse_args()
    if args.cmd == "issue-key":
        print(issue_key(args.name, args.days, args.scope))
    elif args.cmd == "renew-key":
        renew_key(args.name, args.days)
    elif args.cmd == "revoke-key":
        revoke_key(args.name)
    elif args.cmd == "list-keys":
        for e in sorted(_load_keys().values(), key=lambda e: e["name"]):
            print(f"{e['name']:20s} {e['scope']:8s} expires {e['expires']}")
    else:
        import uvicorn

        threading.Thread(target=_build_until_done, daemon=True).start()
        uvicorn.run(app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
