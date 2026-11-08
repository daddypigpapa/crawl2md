import json

import pytest

from core.collect import build_toc, extract_content
from core.errors import Blocked
from core.models import Response
from tests.conftest import read_fixture


class A(dict):
    def __getitem__(self, key):
        defaults = {
            "toc.selector": "a[href]",
            "toc.id_pattern": r"^/(\d+)$",
            "toc.exclude_patterns": [],
            "toc.drop_empty_title": True,
            "toc.dedupe_by": "href",
            "toc.order": "dom",
            "entry.page_url": "https://example.com/{id}",
            "entry.root_url": "https://example.com/book/1",
            "content.selector": ".page-content",
        }
        return defaults[key]


TOC_HTML = """
<html><body>
<a href="/book/1">book cover</a>
<a href="/100">chapter one</a>
<a href="/101"></a>
<a href="/102">99) ===old code (deprecated)===</a>
<a href="/103">chapter three</a>
</body></html>
"""


def test_c1_build_toc_drops_empty_title_and_book_link():
    entries = build_toc(TOC_HTML, A())
    ids = [e.id for e in entries]
    assert "1" not in ids  # /book/1 은 id_pattern에 안 걸림
    assert "101" not in ids  # 빈 제목 제외
    assert ids == ["100", "102", "103"]


def test_c2_dom_order_preserved_for_odd_title():
    entries = build_toc(TOC_HTML, A())
    titles = [e.title for e in entries]
    assert titles[1].startswith("99)")
    assert [e.i for e in entries] == [0, 1, 2]


def test_extract_content_missing_selector_returns_empty():
    html = "<div class='other'>x</div>"
    assert extract_content(html, A()) == ""


def test_extract_content_found():
    html = '<div class="page-content"><p>hello</p></div>'
    assert "<p>hello</p>" in extract_content(html, A())


class FakeFetcher:
    name = "fake"
    supports_batch = True

    def __init__(self, responses, blocked_on=None):
        self.responses = responses
        self.blocked_on = blocked_on or set()

    def open(self, origin):
        pass

    def get(self, url):
        return self.responses[url]

    def get_many(self, urls, *, concurrency, delay_ms):
        for url in urls:
            if url in self.blocked_on:
                yield (url, Blocked("test-marker"))
            else:
                yield (url, self.responses[url])

    def close(self):
        pass


ROOT_HTML = """
<html><body>
<a href="/100">first</a>
<a href="/101">second</a>
</body></html>
"""

PAGE_HTML = '<div class="page-content"><p>content</p></div>'


class RunAdapter(dict):
    def __getitem__(self, key):
        defaults = {
            "verified": True,
            "site": "fixturesite",
            "fetch.driver": "fake",
            "entry.root_url": "https://example.com/book/1",
            "entry.page_url": "https://example.com/{id}",
            "entry.book_title": None,
            "entry.book_title_selector": None,
            "entry.title_suffix_strip": None,
            "toc.id_pattern": r"^/(\d+)$",
            "toc.exclude_patterns": [],
            "toc.drop_empty_title": True,
            "toc.dedupe_by": "href",
            "toc.order": "dom",
            "content.selector": ".page-content",
            "toc.selector": 'a[href^="/"]',
            "fetch.batch_size": 40,
            "fetch.concurrency": 5,
            "fetch.delay_ms": 0,
            "license": "CC BY-NC-SA",
        }
        return defaults[key]

    sha256 = "testsha"


def test_c3_resume_only_refetches_errors(tmp_path, monkeypatch):
    from core import collect as collect_mod

    adapter = RunAdapter()
    root_url = adapter["entry.root_url"]
    responses = {
        root_url: Response(root_url, 200, ROOT_HTML, 1),
        "https://example.com/100": Response("https://example.com/100", 200, PAGE_HTML, 1),
        "https://example.com/101": Response("https://example.com/101", 200, PAGE_HTML, 1),
    }
    fetcher = FakeFetcher(responses)
    monkeypatch.setattr(collect_mod, "get_fetcher", lambda name, a: fetcher)

    job_dir = tmp_path / "fixturesite" / "job1"
    job_dir.mkdir(parents=True)
    (job_dir / "raw.json").write_text(
        json.dumps(
            {
                "schema": "crawl2md/raw@1",
                "meta": {},
                "toc": [],
                "pages": {"100": {"id": "100", "error": None, "html": "cached", "i": 0, "url": "u", "title": "t", "status": 200, "fetched_at": "x"}},
            }
        ),
        encoding="utf-8",
    )

    raw_path = collect_mod.run(adapter, "job1", work_dir=tmp_path, resume=True)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["pages"]["100"]["html"] == "cached"  # 재수집 안 됨
    assert raw["pages"]["101"]["html"]  # 새로 수집됨


def test_c4_blocked_exits_3(tmp_path, monkeypatch):
    from core import collect as collect_mod

    adapter = RunAdapter()
    root_url = adapter["entry.root_url"]
    responses = {
        root_url: Response(root_url, 200, ROOT_HTML, 1),
        "https://example.com/100": Response("https://example.com/100", 200, PAGE_HTML, 1),
        "https://example.com/101": Response("https://example.com/101", 200, PAGE_HTML, 1),
    }
    fetcher = FakeFetcher(responses, blocked_on={"https://example.com/101"})
    monkeypatch.setattr(collect_mod, "get_fetcher", lambda name, a: fetcher)

    with pytest.raises(SystemExit) as exc_info:
        collect_mod.run(adapter, "job2", work_dir=tmp_path, resume=False)

    assert exc_info.value.code == 3
    assert (tmp_path / "fixturesite" / "job2" / "raw.json").exists()
