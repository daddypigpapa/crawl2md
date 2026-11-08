import logging

from core.adapter import Ctx
from core.convert import _build_filename
from core.models import TocEntry


class A(dict):
    def __getitem__(self, key):
        defaults = {
            "output.filename_template": "{order:03d}_{title}.md",
            "output.filename_max": 20,
            "output.filename_forbidden": r'[\\/:*?"<>|]',
            "output.filename_replacement": "-",
        }
        return defaults[key]

    hooks = type("H", (), {"filename": None})()


def ctx():
    return Ctx(adapter=A(), page_id="1", order=0, title="t", log=logging.getLogger("t"))


def test_n1_forbidden_chars_removed():
    entry = TocEntry(i=0, id="1", url="u", title='18-04 TFBert<>:"|를 이용한 분류')
    name = _build_filename(entry, entry.title, A(), ctx(), set())
    for ch in '\\/:*?"<>|':
        assert ch not in name


def test_n2_trailing_dot_removed():
    entry = TocEntry(i=0, id="1", url="u", title="abc...")
    name = _build_filename(entry, entry.title, A(), ctx(), set())
    stem = name[:-3]
    assert not stem.endswith(".")


def test_n3_duplicate_suffix():
    a = A()
    used = set()
    entry1 = TocEntry(i=0, id="1", url="u", title="same")
    entry2 = TocEntry(i=0, id="2", url="u", title="same")
    n1 = _build_filename(entry1, entry1.title, a, ctx(), used)
    n2 = _build_filename(entry2, entry2.title, a, ctx(), used)
    assert n1 != n2
    assert n2.endswith("_2.md")


def test_n4_image_name_no_collision():
    from bs4 import BeautifulSoup
    from core import assets

    class AA(dict):
        def __getitem__(self, key):
            defaults = {
                "output.filename_forbidden": r'[\\/:*?"<>|]',
                "output.filename_replacement": "-",
                "assets.skip_data_uri": True,
                "assets.strip_attrs": [],
                "assets.name_template": "{page_id}_{basename}",
                "assets.dir": "assets",
            }
            return defaults[key]

    # 서로 다른 절대 URL이 같은 basename(image1.png)을 가지는 경우 (실제 wikidocs 함정)
    soup1 = BeautifulSoup('<img src="https://cdn.example.com/a/image1.png">', "lxml")
    soup2 = BeautifulSoup('<img src="https://cdn.example.com/b/image1.png">', "lxml")
    soup1._c2m_url = "https://example.com/p1"
    soup2._c2m_url = "https://example.com/p2"
    plan = assets.plan({"p1": soup1, "p2": soup2}, AA(), base_url="https://example.com")
    assert len(plan.url_to_name) == 2
    assert len(set(plan.url_to_name.values())) == 2
