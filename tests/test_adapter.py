import textwrap
from pathlib import Path

import pytest

from core import adapter
from core.errors import ValidationError

MINIMAL = """
site: {site}
name: test site
verified: true
license: "CC BY-NC-SA"
entry:
  root_url: "https://example.com/book/1"
  page_url: "https://example.com/{{id}}"
toc:
  strategy: link_pattern
  selector: 'a[href^="/"]'
  id_pattern: '^/(\\d+)$'
content:
  selector: ".content"
output:
  filename_template: "{{order:03d}}_{{title}}.md"
  path: "./out/{{job}}"
"""


def write_adapter(tmp_path: Path, site: str, extra: str = "") -> Path:
    path = tmp_path / f"{site}.yaml"
    path.write_text(MINIMAL.format(site=site) + extra, encoding="utf-8")
    return path


def test_a1_unknown_key(tmp_path):
    path = write_adapter(tmp_path, "site_a1", extra="\nbogus_key: 1\n")
    with pytest.raises(ValidationError):
        adapter.load(path)


def test_a2_id_pattern_two_groups(tmp_path):
    text = MINIMAL.format(site="site_a2").replace(
        "id_pattern: '^/(\\d+)$'", "id_pattern: '^/(\\d+)/(\\d+)$'"
    )
    path = tmp_path / "site_a2.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        adapter.load(path)


def test_a3_context_request_forbidden(tmp_path):
    extra = "\nfetch:\n  playwright:\n    mode: context_request\n"
    path = write_adapter(tmp_path, "site_a3", extra=extra)
    with pytest.raises(ValidationError):
        adapter.load(path)


def test_a4_frontmatter_unknown_key(tmp_path):
    extra = "\noutput:\n  frontmatter: [title, bogus]\n"
    path = write_adapter(tmp_path, "site_a4", extra=extra)
    with pytest.raises(ValidationError):
        adapter.load(path)


def test_a5_default_merge(tmp_path):
    path = write_adapter(tmp_path, "site_a5")
    a = adapter.load(path)
    assert a["assets.concurrency"] == 8
