"""2026-09-08 wikidocs book/19414 실측에서 드러난 결함들의 회귀 테스트."""

import json
from unittest.mock import patch

from bs4 import BeautifulSoup

from core.convert import _count_table_blocks
from core.probe import detect_license, pick_content_selector, pick_representative_page
from core.retry import MAX_ATTEMPTS, call_with_retry


# --- ① Cloudflare 520 재시도 (§10.2) ---------------------------------------

def test_cloudflare_520_is_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            return 520, {}, None
        return 200, {}, "ok"

    with patch("time.sleep"):
        status, _headers, result = call_with_retry(fn)

    assert calls["n"] == 3
    assert status == 200 and result == "ok"


def test_520_gives_up_after_max_attempts():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return 520, {}, None

    with patch("time.sleep"):
        status, _h, _r = call_with_retry(fn)

    assert calls["n"] == MAX_ATTEMPTS and status == 520


# --- ② 표 계수가 코드펜스 안을 세지 않는다 (§8.6) ---------------------------

def test_table_blocks_ignore_code_fences():
    md = "본문\n\n```\n| 항목 | 값 |\n|------|-----|\n| 언어 | Python |\n```\n\n끝"
    assert _count_table_blocks(md) == 0


def test_real_markdown_table_still_counted():
    md = "본문\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n끝"
    assert _count_table_blocks(md) == 1


# --- ③ 본문 셀렉터: 사이드바 래퍼가 이기지 않는다 (§6.4) --------------------

def _page(page_content_words: int, sidebar_links: int) -> str:
    body = " ".join(f"본문문장{i}" for i in range(page_content_words))
    nav = "".join(f"<li><a href='/{i}'>목차항목 {i}</a></li>" for i in range(sidebar_links))
    return f"""
    <html><body>
      <div class="content">
        <ul class="sidebar">{nav}</ul>
        <div class="page-content"><p>{body}</p></div>
      </div>
    </body></html>
    """


def test_sidebar_wrapper_loses_to_real_content():
    # .content 는 사이드바 목차까지 품어 텍스트가 더 길지만 노드가 훨씬 많다.
    html = _page(page_content_words=300, sidebar_links=90)
    result = pick_content_selector(html)
    chosen = [c for c in result["candidates"] if c["chosen"]]
    assert chosen and chosen[0]["selector"] == ".page-content"


def test_empty_shell_does_not_win_on_density():
    # 텍스트가 거의 없는 껍데기(main)가 밀도만으로 이기면 안 된다.
    html = """
    <html><body>
      <main>짧음</main>
      <div class="page-content"><p>%s</p></div>
    </body></html>
    """ % (" ".join(f"문장{i}" for i in range(300)))
    result = pick_content_selector(html)
    chosen = [c for c in result["candidates"] if c["chosen"]]
    assert chosen and chosen[0]["selector"] == ".page-content"


# --- ④ 대표 페이지 선택: 장 표지를 쓰지 않는다 (§6.1 step 5) ----------------

def test_representative_page_skips_chapter_cover():
    cover = '<html><body><div class="page-content"><p>난이도를 고려하여 두 파트로 나눴습니다.</p></div></body></html>'
    real = '<html><body><div class="page-content"><p>%s</p></div></body></html>' % (
        " ".join(f"본문{i}" for i in range(400))
    )
    assert pick_representative_page([cover, real, cover]) == 1


# --- ⑤ 라이선스 오탐: 숫자는 라이선스가 아니다 (§6.8) -----------------------

def test_license_rejects_numeric_capture():
    html = "<html><body><p>저작권 : 120 명이 추천</p></body></html>"
    assert detect_license(html)["found"] == "unknown"


def test_license_still_detects_real_value():
    html = '<html><body><a href="https://creativecommons.org/licenses/by-nc-sa/4.0/">CC</a></body></html>'
    assert detect_license(html)["found"].startswith("CC")


# --- ⑥ skipped_cached 가 --limit 몫을 캐시로 세지 않는다 (§3.7) -------------

def test_skipped_cached_excludes_limit_remainder(tmp_path, monkeypatch):
    from core import collect as collect_mod
    from tests.test_collect import FakeFetcher, ROOT_HTML, PAGE_HTML, RunAdapter
    from core.models import Response

    adapter = RunAdapter()
    root_url = adapter["entry.root_url"]
    responses = {
        root_url: Response(root_url, 200, ROOT_HTML, 1),
        "https://example.com/100": Response("https://example.com/100", 200, PAGE_HTML, 1),
        "https://example.com/101": Response("https://example.com/101", 200, PAGE_HTML, 1),
    }
    monkeypatch.setattr(collect_mod, "get_fetcher", lambda name, a: FakeFetcher(responses))

    collect_mod.run(adapter, "joblimit", work_dir=tmp_path, limit=1, resume=False)

    report = json.loads((tmp_path / "fixturesite" / "joblimit" / "report.json").read_text(encoding="utf-8"))
    stage = report["stages"]["collect"]
    # toc 2개 중 1개만 limit 로 수집했고, 캐시로 건너뛴 것은 0건이어야 한다.
    assert stage["total"] == 2 and stage["ok"] == 1
    assert stage["skipped_cached"] == 0


# --- ⑦ 이스케이프된 \$ 는 수식 구분자가 아니다 (§8.6.1) ---------------------

def test_escaped_dollar_is_not_math_delimiter():
    from core.convert import stray_dollars

    md = r"회당 비용이 \$0.5~\$1.0에서 \$0으로 떨어졌습니다. 표: | \$0.0184 | stop |"
    assert stray_dollars(md) == 0


def test_escaped_dollar_fix_does_not_hide_real_breakage():
    from core.convert import stray_dollars

    # 진짜 파손(구분자 없는 생 $)은 여전히 남아야 한다.
    md = r"통화는 \$45.55이고 수식은 $x = 1 그리고 $y = 2 처럼 깨졌다"
    assert stray_dollars(md) >= 2
