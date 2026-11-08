import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup

from core import assets
from core.adapter import Adapter, Ctx
from core.mdconv import make_converter
from core.models import TocEntry
from core.report import Report

import logging

log = logging.getLogger("crawl2md.convert")


@dataclass
class DocResult:
    entry: TocEntry
    filename: str
    markdown_len: int
    paywalled: bool


@dataclass
class ConvertResult:
    out_dir: Path
    documents: list[DocResult] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


def clean_html(html: str, adapter: Adapter, ctx: Ctx) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for selector in adapter["content.remove"]:
        for node in soup.select(selector):
            node.decompose()
    if adapter.hooks.clean_html:
        try:
            soup = adapter.hooks.clean_html(soup, ctx) or soup
        except Exception:  # noqa: BLE001 - 훅은 예외를 던지지 않는다 (규약)
            ctx.log.warning("hooks.clean_html 실패, 원본 사용")
    return soup


def _sanitize_filename_part(text: str, adapter: Adapter) -> str:
    forbidden = re.compile(adapter["output.filename_forbidden"])
    text = forbidden.sub(adapter["output.filename_replacement"], text)
    text = re.sub(r"\s+", " ", text).strip()
    while text.endswith("."):
        text = text[:-1]
    return text


def _build_filename(entry: TocEntry, title: str, adapter: Adapter, ctx: Ctx, used: set[str]) -> str:
    if adapter.hooks.filename:
        try:
            name = adapter.hooks.filename(entry, ctx)
            if name:
                return _dedupe_filename(name, used)
        except Exception:  # noqa: BLE001
            ctx.log.warning("hooks.filename 실패, 기본 규칙 사용")

    max_len = adapter["output.filename_max"]
    clean_title = _sanitize_filename_part(title, adapter)
    clean_title = clean_title[:max_len]
    if not clean_title:
        clean_title = entry.id
    slug = clean_title.lower().replace(" ", "-")

    name = adapter["output.filename_template"].format(
        order=entry.i, title=clean_title, id=entry.id, slug=slug
    )
    if not name.endswith(".md"):
        name += ".md"
    return _dedupe_filename(name, used)


def _dedupe_filename(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, ext = name.rsplit(".", 1) if "." in name else (name, "")
    n = 2
    while True:
        candidate = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _yaml_escape(value: str) -> str:
    return '"' + value.replace('"', "'") + '"'


def build_document(soup, entry: TocEntry, adapter: Adapter, ctx: Ctx, book_title, license_) -> str:
    converter = make_converter(adapter)
    md = converter.convert_soup(soup)
    n = adapter["markdown.collapse_blank_lines"]
    md = re.sub(r"\n{%d,}" % (n + 1), "\n" * n, md).strip()

    if adapter.hooks.post_markdown:
        try:
            md = adapter.hooks.post_markdown(md, ctx) or md
        except Exception:  # noqa: BLE001
            ctx.log.warning("hooks.post_markdown 실패, 원본 사용")

    ctx.body_md = md  # quality_gate 가 프론트매터 없는 본문 길이를 볼 수 있도록 보관

    title = ctx.title
    values = {
        "title": _yaml_escape(title),
        "order": entry.i,
        "source": entry.url,
        "book": _yaml_escape(book_title) if book_title else None,
        "license": _yaml_escape(license_) if license_ else None,
        "id": _yaml_escape(entry.id),
    }
    lines = ["---"]
    for key in adapter["output.frontmatter"]:
        value = values.get(key)
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    frontmatter = "\n".join(lines)

    body = md
    if adapter["output.h1_from_title"]:
        body = f"# {title}\n\n{body}"

    footer_template = adapter["output.footer_template"]
    footer = ""
    if footer_template:
        footer = "\n\n---\n\n" + footer_template.format(
            url=entry.url, license=license_ or "", title=title, id=entry.id
        )

    return f"{frontmatter}\n\n{body}{footer}\n"


_FENCE_RE = re.compile(r"```")
_BLOCK_MATH_RE = re.compile(r"\$\$[\s\S]*?\$\$")
_INLINE_MATH_RE = re.compile(r"\$(?!\s)[^$]{1,500}?(?<!\s)\$")
_CODE_FENCE_STRIP_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_STRIP_RE = re.compile(r"`[^`\n]*`")

_ESCAPED_DOLLAR_RE = re.compile(r"\\\$")


def stray_dollars(md: str) -> int:
    md = _CODE_FENCE_STRIP_RE.sub("", md)
    md = _INLINE_CODE_STRIP_RE.sub("", md)
    # 이스케이프된 \$ 는 리터럴 달러 표시이지 수식 구분자가 아니다. 원본이 통화를
    # \$0.5 로 적어 두면(실측 wikidocs 354403: 28개) 잔여가 쌓여 오탐이 된다.
    md = _ESCAPED_DOLLAR_RE.sub("", md)
    md = _BLOCK_MATH_RE.sub("", md)
    md = _INLINE_MATH_RE.sub("", md)
    return md.count("$")


def quality_gate(md: str, soup, adapter: Adapter, ctx: Ctx, *, raw_html: str = "", out_dir: Path | None = None) -> list[dict]:
    warnings = []

    for marker in adapter["content.paywall_markers"]:
        if re.search(marker, raw_html):
            warnings.append(
                {"kind": "paywall", "id": ctx.page_id, "title": ctx.title, "detail": f"matched /{marker}/"}
            )
            break

    threshold = adapter["content.paywall_threshold"]
    if threshold > 0 and len(md) < threshold:
        warnings.append(
            {
                "kind": "short_document",
                "id": ctx.page_id,
                "title": ctx.title,
                "detail": f"markdown {len(md)} chars < threshold {threshold}",
            }
        )

    fence_count = len(_FENCE_RE.findall(md))
    if fence_count % 2 != 0:
        warnings.append({"kind": "unbalanced_fence", "file": ctx.page_id, "detail": f"``` count = {fence_count}"})

    stray = stray_dollars(md)
    if stray >= 2:
        warnings.append({"kind": "unbalanced_math", "file": ctx.page_id, "detail": f"$ count = {stray}"})

    if out_dir is not None:
        for m in re.finditer(r"\]\((assets/[^)\s]+)\)", md):
            asset_path = out_dir / unquote(m.group(1))
            if not asset_path.exists():
                warnings.append({"kind": "missing_asset", "file": ctx.page_id, "detail": m.group(1)})

    html_table_count = len(soup.find_all("table"))
    # 표 하나당 헤더+구분자+행 여러 줄이 |로 시작하므로, 연속된 블록 수를 센다.
    md_table_blocks = _count_table_blocks(md)
    if html_table_count != md_table_blocks:
        warnings.append(
            {
                "kind": "table_count_drift",
                "file": ctx.page_id,
                "detail": f"html {html_table_count} vs md {md_table_blocks}",
            }
        )

    return warnings


def _count_table_blocks(md: str) -> int:
    # 코드펜스 안의 파이프 줄은 표가 아니다. 문서가 마크다운 표를 예시로 보여주는
    # 코드블록(실측: wikidocs 334968)을 세면 table_count_drift 오탐이 난다.
    # stray_dollars 와 같은 순서로 펜스를 먼저 지운다.
    md = _CODE_FENCE_STRIP_RE.sub("", md)
    blocks = 0
    in_block = False
    for line in md.splitlines():
        if line.startswith("|"):
            if not in_block:
                blocks += 1
                in_block = True
        else:
            in_block = False
    return blocks


def run(adapter: Adapter, job: str, *, work_dir: Path, out_dir: Path) -> ConvertResult:
    raw_path = work_dir / adapter["site"] / job / "raw.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    warnings: list[dict] = []
    if raw["meta"].get("adapter_sha256") != adapter.sha256:
        warnings.append({"kind": "adapter_changed", "detail": "raw.json 수집 시점 어댑터와 불일치"})

    book_title = raw["meta"].get("book_title")
    license_ = adapter["license"]

    toc = [TocEntry(**e) for e in raw["toc"]]
    toc.sort(key=lambda e: e.i)

    out_dir.mkdir(parents=True, exist_ok=True)
    log_ = logging.getLogger("crawl2md")

    soups: dict[str, BeautifulSoup] = {}
    raw_html_by_id: dict[str, str] = {}
    ctx_by_id: dict[str, Ctx] = {}
    entry_by_id: dict[str, TocEntry] = {}

    for entry in toc:
        page = raw["pages"].get(entry.id)
        if not page or page.get("error"):
            continue
        ctx = Ctx(adapter=adapter, page_id=entry.id, order=entry.i, title=entry.title, log=log_)
        if adapter.hooks.page_title:
            try:
                title = adapter.hooks.page_title(BeautifulSoup(page["html"], "lxml"), entry, ctx)
                if title:
                    ctx.title = title
            except Exception:  # noqa: BLE001
                log_.warning("hooks.page_title 실패, 원본 제목 사용")

        soup = clean_html(page["html"], adapter, ctx)
        soup._c2m_url = entry.url  # type: ignore[attr-defined]
        soups[entry.id] = soup
        raw_html_by_id[entry.id] = page["html"]
        ctx_by_id[entry.id] = ctx
        entry_by_id[entry.id] = entry

    plan = assets.plan(soups, adapter, base_url=raw["meta"]["root_url"])
    asset_stats = assets.download(plan, out_dir / adapter["assets.dir"], adapter)

    documents: list[DocResult] = []
    used_filenames: set[str] = set()

    for page_id, soup in soups.items():
        entry = entry_by_id[page_id]
        ctx = ctx_by_id[page_id]
        md_document = build_document(soup, entry, adapter, ctx, book_title, license_)

        filename = _build_filename(entry, ctx.title, adapter, ctx, used_filenames)
        (out_dir / filename).write_text(md_document, encoding="utf-8", newline="\n")

        doc_warnings = quality_gate(
            ctx.body_md, soup, adapter, ctx, raw_html=raw_html_by_id[page_id], out_dir=out_dir
        )
        warnings.extend(doc_warnings)
        paywalled = any(w["kind"] == "paywall" for w in doc_warnings)

        documents.append(
            DocResult(entry=entry, filename=filename, markdown_len=len(md_document), paywalled=paywalled)
        )

    report = Report(work_dir / adapter["site"] / job / "report.json")
    with report.stage("convert") as stage:
        stage["driver"] = raw["meta"].get("driver")
        stage["documents"] = len(documents)
        stage["images"] = {
            "unique": len(plan.url_to_name),
            "downloaded": asset_stats["downloaded"],
            "cached": asset_stats["cached"],
            "failed": asset_stats["failed"],
            "failures": asset_stats["failures"],
        }
        stage["warnings"] = warnings
        stage["doc_index"] = [
            {"id": d.entry.id, "filename": d.filename, "paywalled": d.paywalled} for d in documents
        ]

    return ConvertResult(out_dir=out_dir, documents=documents, warnings=warnings)
