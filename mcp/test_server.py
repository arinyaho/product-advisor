"""Checks against the real PDF and OpenRouter.

Run with BOOK_PDF and OPENROUTER_API_KEY set:
    uv run --python 3.13 --with-requirements requirements.txt python test_server.py
"""

import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())  # point at a data dir to reuse its cached index

from starlette.testclient import TestClient  # noqa: E402

import server  # noqa: E402

server._index()  # semantic tests need the embedding index; the server builds it in the background


def test_sections():
    titles = [s["title"] for s in server.list_sections()]
    assert any(t.startswith("CHAPTER 4") for t in titles)
    assert "Cover" not in titles and "INDEX" not in titles


def test_full_scope_reads_a_whole_chapter():
    ch4 = next(s for s in server.list_sections() if s["title"].startswith("CHAPTER 4"))
    text = server.read_section(ch4["id"], "full")
    assert text.startswith(f"# {ch4['title']}") and "willingness" in text.lower()


def test_full_scope_pages_through_a_long_chapter():
    ch13 = next(s for s in server.list_sections() if s["title"].startswith("CHAPTER 13"))
    first = server.read_section(ch13["id"], "full")
    shown = [int(p) for p in re.findall(r"^\[p\.(\d+)\]$", first, re.M)]
    assert len(shown) == server.MAX_SECTION_PAGES and shown[0] == ch13["start"]
    assert f"start_page={shown[-1] + 1}" in first
    rest = server.read_section(ch13["id"], "full", start_page=shown[-1] + 1)
    assert int(re.search(r"^\[p\.(\d+)\]$", rest, re.M).group(1)) == shown[-1] + 1


def _norm(text):
    return "".join(c for c in text.lower() if c.isalnum())


def test_sections_are_cut_at_their_headings_not_at_page_breaks():
    content = [s for s in server.SECTIONS if s["start"] < server.NOTES_START]
    found = [s for s in content if server._heading_found(s)]
    assert len(found) >= 0.9 * len(content), [s["title"] for s in content if s not in found]
    for s in found:
        own = _norm(server._own_text(s))
        assert own.startswith(_norm(s["title"])[:20]), s["title"]
        nxt = next((x for x in content if x["id"] > s["id"] and server._heading_found(x)), None)
        if nxt:
            assert _norm(nxt["title"])[:30] not in own, (s["title"], nxt["title"])


def test_ligatures_are_unfolded_so_words_and_headings_match():
    # the PDF sets "fi" and "fl" as single ligature glyphs; left as is, "difficult" tokenizes as "dif" + "cult"
    assert not any(re.search("[\ufb00-\ufb06]", text) for text in server.PAGES.values())
    assert "difficult" in server.DF
    content = [s for s in server.SECTIONS if s["start"] < server.NOTES_START]
    assert all(server._heading_found(s) for s in content), [s["title"] for s in content if not server._heading_found(s)]


def test_heading_only_sections_get_no_summary():
    calls = []
    real = server._summarize
    intros = {}
    server._summarize = lambda title, text, intro: calls.append(len(text.split())) or intros.update({title: intro}) or "s"
    real_dir = server.DATA_DIR
    server.DATA_DIR = Path(tempfile.mkdtemp())
    server._summaries.cache_clear()
    try:
        server._summaries()
    finally:
        server._summarize, server.DATA_DIR = real, real_dir
        server._summaries.cache_clear()
    assert calls and min(calls) >= server.MIN_SUMMARY_WORDS
    # a parent's own text only introduces its subsections, which are summarized separately
    parents = {s["title"] for s in server.SECTIONS if server._children(s["id"])}
    assert all(intro == (title in parents) for title, intro in intros.items())


def test_chunks_stay_inside_one_section_and_know_their_page():
    sections = {s["id"]: s for s in server.SECTIONS}
    for c in server.CHUNKS + server.EXCERPTS:
        body = _norm(c.text)
        assert body[:40] in _norm(server._own_text(sections[c.section])), (c.section, c.page)
        # results are pages, so a chunk that ran onto the next page would credit only the page it started on
        assert body in _norm(server.PAGES[c.page]), (c.section, c.page)
    assert max(len(c.text.split()) for c in server.CHUNKS) <= server.CHUNK_WORDS * 1.3


def test_results_name_the_section_that_matched_not_the_first_on_the_page():
    cases = [("과금 모델을 정할 때 경쟁사를 어떻게 봐야 하나", "4. What Are Your Competitors Doing?"),
             ("회사의 성장 단계에 따라 과금 모델 선택이 달라지나", "3. What Stage Is Your Company In")]
    for query, title in cases:
        sections = [r["section"] for r in server.search(query, 3, "full")["results"]]
        assert any(s.startswith(title) for s in sections), (query, sections)


def _with_fake_summaries(fn):
    real, real_dir, ready = server._summarize, server.DATA_DIR, server.SUMMARIES_READY.is_set()
    server._summarize = lambda title, text, intro: f"summary of {title} ({len(text.split())} words)"
    server.DATA_DIR = Path(tempfile.mkdtemp())  # never touch real summaries
    server._summaries.cache_clear()
    try:
        server._summaries()
        fn()
    finally:
        server._summarize, server.DATA_DIR = real, real_dir
        server._summaries.cache_clear()
        if not ready:
            server.SUMMARIES_READY.clear()


def test_excerpt_scope_reads_summaries_instead_of_text():
    def check():
        ch6 = next(s for s in server.list_sections() if s["title"].startswith("CHAPTER 6"))
        subtree = [s for s in server.SECTIONS if ch6["start"] <= s["start"] <= ch6["end"] and s["level"] > ch6["level"]]
        text = server.read_section(ch6["id"], "excerpt")
        # every section in the chapter appears; those with pages of their own (not only subsections) carry a summary
        assert all(s["title"] in text for s in subtree)
        with_text = [s for s in subtree if len(server._own_text(s).split()) >= server.MIN_SUMMARY_WORDS]
        assert len(with_text) >= 5 and all(f"summary of {s['title']}" in text for s in with_text)
        assert "[p." not in text and "summary of CHAPTER 6" in text  # its own intro page before the first child
        leaf = next(s for s in subtree if not server._children(s["id"]))
        assert server.read_section(leaf["id"], "excerpt").count("summary of") == 1
    _with_fake_summaries(check)


def test_summary_survives_a_reasoning_model_and_fails_loudly_when_cut_off():
    real, sent = server._openrouter, []

    def fake(path, payload):
        sent.append(payload)
        content = None if len(sent) == 2 else "a summary"
        return {"choices": [{"finish_reason": "length" if content is None else "stop", "message": {"content": content}}]}

    server._openrouter = fake
    try:
        assert server._summarize("t", "text", False) == "a summary"
        assert sent[0]["reasoning"] == {"enabled": False}  # reasoning can use up the whole token budget
        assert sent[0]["max_tokens"] >= 2000
        try:
            server._summarize("t", "text", False)
            raise AssertionError("an empty reply was accepted")
        except RuntimeError as e:
            assert "length" in str(e)
    finally:
        server._openrouter = real


def test_excerpt_scope_waits_for_summaries():
    try:
        server.read_section(-1, "full")
        raise AssertionError("unknown id did not raise")
    except ValueError as e:
        assert "list_sections" in str(e)
    server.SUMMARIES_READY.clear()
    try:
        server.read_section(server.SECTIONS[0]["id"], "excerpt")
    except PermissionError as e:
        assert "being prepared" in str(e)
        return
    raise AssertionError("excerpt scope got section text before summaries existed")


def test_korean_and_english_situations_find_their_chapters():
    cases = [
        ("고객과 가격 얘기는 개발 초기에 언제 꺼내야 하나", 55, 68),
        ("경쟁사가 가격을 내리면 어떻게 대응해야 하나", 165, 176),
        ("어떤 기능을 기본 요금제에 넣고 어떤 기능을 상위 요금제에 넣을까", 79, 94),
        ("We sell our product too cheaply and leave money on the table", 36, 39),
    ]
    for query, lo, hi in cases:
        found = server.search(query, 3, "full")
        assert found["semantic"], found
        pages = [r["page"] for r in found["results"]]
        assert any(lo <= p <= hi for p in pages), (query, pages)


def test_notes_pages_are_not_searched():
    notes_start = next(s["start"] for s in server.SECTIONS if s["title"] == "NOTES")
    assert max(server.CONTENT_PAGES) < notes_start
    assert not any(c.page >= notes_start for c in server.CHUNKS + server.EXCERPTS)
    for query in ["Harvard Business Review", "price war", "구독으로 팔지 사용량 기반으로 과금할지 고민이다"]:
        pages = [r["page"] for r in server.search(query, 5, "full")["results"]]
        assert all(p < notes_start for p in pages), (query, pages)


def test_body_note_markers_link_to_notes():
    linked = [n for marks in server.PAGE_NOTES.values() for n in marks]
    assert len(server.NOTES) >= 80 and len(linked) >= 78, (len(server.NOTES), len(linked))
    assert all(n in server.NOTES for n in linked), [n for n in linked if n not in server.NOTES]
    assert all(n < 100 for _, n in server.NOTES), "a year or long number was parsed as a note number"


def test_search_results_carry_the_notes_their_page_cites():
    page = next(p for p, marks in server.PAGE_NOTES.items() if len(marks) >= 2)
    sources = server._sources(page)
    assert [s["note"] for s in sources] == [n for _, n in server.PAGE_NOTES[page]]
    assert all(s["text"] for s in sources)


def test_page_text_has_no_note_markers_glued_to_words():
    page = next(p for p, marks in server.PAGE_NOTES.items() if marks)
    assert not re.search(r"[a-z.,”\"]\d{1,2}\b", server.PAGES[page]), page


def test_excerpt_scope_returns_a_short_passage():
    found = server.search("leaders fillers killers", 3, "excerpt")
    for r in found["results"]:
        assert len(r["text"].split()) <= server.EXCERPT_WORDS, len(r["text"].split())
    assert any("filler" in r["text"].lower() for r in found["results"])


def test_search_falls_back_to_keywords_when_openrouter_fails():
    real = server._embed

    def down(texts):
        raise OSError("openrouter unreachable")

    server._embed = down
    try:
        found = server.search("price war", 3, "full")
    finally:
        server._embed = real
    assert not found["semantic"] and found["results"]
    assert 165 <= found["results"][0]["page"] <= 176


def test_embed_retries_after_rate_limit():
    import io
    import urllib.error
    import urllib.request

    real, calls = urllib.request.urlopen, []

    def flaky(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "0"}, io.BytesIO())
        return io.BytesIO(b'{"data": [{"index": 0, "embedding": [1.0, 0.0]}]}')

    urllib.request.urlopen = flaky
    try:
        v = server._embed(["a"])
    finally:
        urllib.request.urlopen = real
    assert len(calls) == 2 and v.shape == (1, 2)


def test_keys_issue_expire_renew_revoke():
    key = server.issue_key("alice", days=30, scope="excerpt")
    assert server.key_scope(key) == "excerpt"
    assert server.key_scope("wrong") is None

    keys = server._load_keys()
    keys[server._hash(key)]["expires"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    server._save_keys(keys)
    assert server.key_scope(key) is None

    server.renew_key("alice", days=7)
    assert server.key_scope(key) == "excerpt"

    server.revoke_key("alice")
    assert server.key_scope(key) is None


def _rpc(client, key, method, params, session=None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if session:
        headers["mcp-session-id"] = session
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def _call_tool(client, key, name, arguments):
    init = _rpc(client, key, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
    assert init.status_code == 200, init.text
    return _rpc(client, key, "tools/call", {"name": name, "arguments": arguments}, init.headers.get("mcp-session-id"))


def test_http_requires_a_valid_key_and_applies_its_scope():
    with TestClient(server.app()) as client:
        assert _rpc(client, None, "tools/list", {}).status_code == 401
        assert _rpc(client, "not-a-key", "tools/list", {}).status_code == 401

        excerpt_key = server.issue_key("bob", days=1, scope="excerpt")
        denied = _call_tool(client, excerpt_key, "get_section", {"id": 20})
        assert "excerpt" in denied.text and "isError\":true" in denied.text.replace(" ", ""), denied.text

        full_key = server.issue_key("me", days=1, scope="full")
        allowed = _call_tool(client, full_key, "get_section", {"id": 20})
        assert "isError\":false" in allowed.text.replace(" ", "") or "isError" not in allowed.text, allowed.text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
