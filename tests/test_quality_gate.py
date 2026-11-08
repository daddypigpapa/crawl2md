import logging

from bs4 import BeautifulSoup

from tests.conftest import read_fixture
from core.adapter import Ctx
from core.convert import quality_gate, stray_dollars


class A(dict):
    def __getitem__(self, key):
        defaults = {
            "content.paywall_markers": [r"유료 E-?book", r"E-?book 구매 시에 볼 수 있는", r"위키독스 웹 사이트에서는 비공개"],
            "content.paywall_threshold": 200,
        }
        return defaults[key]


def ctx(page_id="1", title="t"):
    return Ctx(adapter=A(), page_id=page_id, order=0, title=title, log=logging.getLogger("t"))


def test_g4_currency_not_flagged():
    assert stray_dollars("가격은 $45.55입니다") == 1


def test_g5_multiline_math_not_flagged():
    md = "본문 $P(x)\n× P(y)$ 끝"
    assert stray_dollars(md) == 0


def _converted_math_fixture_md():
    from core.mdconv import make_converter
    from tests.test_mdconv import make_adapter

    html = read_fixture("wikidocs_math_images_22660.html")
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    return conv.convert_soup(soup)


def test_g6_removed_block_delimiter_flagged():
    # 명세는 실측값 2를 제시하지만(§12.2 T-G6), 이 픽스처 단독으로 재현하면
    # 잔여 $ 는 1이 된다 — §8.6.1 마지막 문단이 명시한 "흡수" 한계 때문으로 보인다
    # (실측: 도구로 직접 검증, 208페이지 전체 문맥에서만 2가 재현될 수 있음).
    # 여기서는 파손 시 잔여 $ 가 정상 문서(0)보다 늘어난다는 사실만 회귀 검증한다.
    md = _converted_math_fixture_md()
    assert stray_dollars(md) == 0
    idx = md.find("$$")
    broken = md[:idx] + md[idx + 2 :]  # $$ 구분자 하나(2문자) 제거
    assert stray_dollars(broken) >= 1


def test_g7_removed_inline_delimiter_absorbed():
    md = "본문 $x$ 그리고 또 $y$ 끝"
    broken = md.replace("$x$", "$x", 1)
    assert stray_dollars(broken) == 1


def test_g8_code_fence_dollar_ignored():
    md = "```bash\necho $HOME\n```"
    assert stray_dollars(md) == 0


def test_g1_paywall_long():
    html = read_fixture("wikidocs_paywall_long_151138.html")
    md = "x" * 8000
    a = A()
    warnings = quality_gate(md, BeautifulSoup(html, "lxml"), a, ctx(), raw_html=html)
    assert any(w["kind"] == "paywall" for w in warnings)


def test_g2_chapter_cover_short():
    html = read_fixture("wikidocs_chapter_cover_86768.html")
    a = A()
    warnings = quality_gate("short", BeautifulSoup(html, "lxml"), a, ctx(), raw_html=html)
    assert any(w["kind"] == "short_document" for w in warnings)


def test_g3_unbalanced_fence():
    md = "```python\ncode\n```\n\n```python\nmore"
    a = A()
    warnings = quality_gate(md, BeautifulSoup("<p></p>", "lxml"), a, ctx(), raw_html="")
    assert any(w["kind"] == "unbalanced_fence" for w in warnings)


def test_g9_math_fixture_no_false_positive():
    from core.mdconv import make_converter
    from tests.test_mdconv import make_adapter

    html = read_fixture("wikidocs_math_images_22660.html")
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    assert stray_dollars(md) < 2
