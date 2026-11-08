from bs4 import BeautifulSoup

from core.mdconv import assert_markdownify_compatible, make_converter


class FakeAdapter(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def make_adapter(**overrides):
    base = {
        "markdown.code_lang_map": {"pyton": "python", "no-highlight": ""},
        "markdown.code_lang_default": "",
        "markdown.heading_style": "ATX",
        "markdown.bullets": "-",
        "markdown.escape_asterisks": False,
        "markdown.escape_underscores": False,
        "markdown.escape_misc": False,
    }
    base.update(overrides)
    return FakeAdapter(base)


def test_s1_compat_guard_ok():
    assert_markdownify_compatible()


def test_m1_language_python_fence():
    html = '<pre><code class="language-python">print(1)</code></pre>'
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    assert "```python" in md
    assert "print(1)" in md


def test_m2_language_typo_mapped():
    html = '<pre><code class="language-pyton">print(1)</code></pre>'
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    assert "```python" in md


def test_m3_no_highlight_no_lang_fence():
    html = '<pre><code class="language-no-highlight">plain text</code></pre>'
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    assert "```\n" in md
    assert "```no-highlight" not in md


def test_m4_underscore_not_escaped():
    html = "<p>$w_{t}$</p>"
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    assert "$w_{t}$" in md
    assert r"\_" not in md


def test_m5_block_math_preserved():
    html = r"<p>$$cost(\hat{y}, y) = -\sum_{j=1}^{V}y_{j}\ log(\hat{y_{j}})$$</p>"
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    assert r"$$cost(\hat{y}, y) = -\sum_{j=1}^{V}y_{j}\ log(\hat{y_{j}})$$" in md


def test_m6_table_row_count():
    html = "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
    soup = BeautifulSoup(html, "lxml")
    conv = make_converter(make_adapter())
    md = conv.convert_soup(soup)
    rows = [l for l in md.splitlines() if l.startswith("|")]
    assert len(rows) >= 2


def test_m7_ads_removed_before_convert():
    from core.convert import clean_html
    from core.adapter import Ctx
    import logging

    html = '<div><script>adsbygoogle=1</script><p>hello</p></div>'

    class A(dict):
        def __getitem__(self, key):
            data = {"content.remove": ["script"]}
            return data[key]

        hooks = type("H", (), {"clean_html": None})()

    a = A()
    ctx = Ctx(adapter=a, page_id="1", order=0, title="t", log=logging.getLogger("t"))
    soup = clean_html(html, a, ctx)
    assert "adsbygoogle" not in str(soup)
