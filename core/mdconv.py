import inspect

import markdownify
from markdownify import MarkdownConverter

_compat_checked = False


def assert_markdownify_compatible() -> None:
    sig = inspect.signature(markdownify.MarkdownConverter.convert_pre)
    params = list(sig.parameters)
    if params[:3] != ["self", "el", "text"]:
        raise RuntimeError(
            f"markdownify convert_pre 시그니처 불일치: {params}. "
            "core/mdconv.py의 오버라이드를 이 버전에 맞게 수정할 것."
        )


class Crawl2mdConverter(MarkdownConverter):
    def __init__(self, adapter, **kw):
        self.lang_map = adapter["markdown.code_lang_map"]
        self.lang_default = adapter["markdown.code_lang_default"]
        super().__init__(**kw)

    def convert_pre(self, el, text, parent_tags=None):
        if not text:
            return ""
        lang = self.lang_default
        code = el.find("code")
        if code and code.get("class"):
            for c in code.get("class"):
                if c.startswith("language-"):
                    lang = c[len("language-"):]
                    break
        lang = self.lang_map.get(lang, lang)
        return f"\n\n```{lang}\n{text.strip()}\n```\n\n"


def make_converter(adapter) -> Crawl2mdConverter:
    global _compat_checked
    if not _compat_checked:
        assert_markdownify_compatible()
        _compat_checked = True
    return Crawl2mdConverter(
        adapter,
        heading_style=adapter["markdown.heading_style"],
        bullets=adapter["markdown.bullets"],
        escape_asterisks=adapter["markdown.escape_asterisks"],
        escape_underscores=adapter["markdown.escape_underscores"],
        escape_misc=adapter["markdown.escape_misc"],
    )
