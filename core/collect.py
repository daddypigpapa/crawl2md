import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from core.adapter import Adapter
from core.errors import Blocked, FetchError, NotVerifiedError
from core.fetch import get_fetcher
from core.fetch.base import is_blocked
from core.models import PageRecord, TocEntry
from core.report import Report

log = logging.getLogger("crawl2md.collect")


def build_toc(root_html: str, adapter: Adapter) -> list[TocEntry]:
    soup = BeautifulSoup(root_html, "lxml")
    id_pattern = re.compile(adapter["toc.id_pattern"])
    exclude_patterns = [re.compile(p) for p in adapter["toc.exclude_patterns"]]
    drop_empty_title = adapter["toc.drop_empty_title"]
    dedupe_by = adapter["toc.dedupe_by"]
    order = adapter["toc.order"]
    page_url_tpl = adapter["entry.page_url"]
    root_url = adapter["entry.root_url"]

    seen: set[str] = set()
    entries: list[TocEntry] = []

    for a in soup.select(adapter["toc.selector"]):
        href = a.get("href")
        if not href:
            continue
        abs_url = urljoin(root_url, href)
        path = urlsplit(abs_url).path

        if any(p.search(path) for p in exclude_patterns):
            continue

        match = id_pattern.match(path)
        if not match:
            continue
        page_id = match.group(1)

        title = re.sub(r"\s+", " ", a.get_text()).strip()
        if drop_empty_title and title == "":
            continue

        dedupe_key = path if dedupe_by == "href" else page_id
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        entries.append(TocEntry(i=0, id=page_id, url=page_url_tpl.format(id=page_id), title=title))

    if order == "title":
        entries.sort(key=lambda e: e.title)

    for i, e in enumerate(entries):
        e.i = i

    return entries


def extract_content(page_html: str, adapter: Adapter) -> str:
    soup = BeautifulSoup(page_html, "lxml")
    node = soup.select_one(adapter["content.selector"])
    if node is None:
        return ""
    return node.decode_contents()


def _determine_book_title(root_html: str, adapter: Adapter) -> str | None:
    soup = BeautifulSoup(root_html, "lxml")

    if adapter["entry.book_title"]:
        return adapter["entry.book_title"]

    selector = adapter["entry.book_title_selector"]
    if selector:
        node = soup.select_one(selector)
        if node and node.get_text().strip():
            return node.get_text().strip()

    h1 = soup.find("h1")
    if h1 and h1.get_text().strip():
        return h1.get_text().strip()

    title_tag = soup.find("title")
    if title_tag and title_tag.get_text().strip():
        text = title_tag.get_text().strip()
        strip_pattern = adapter["entry.title_suffix_strip"]
        if strip_pattern:
            text = re.sub(strip_pattern, "", text)
        if text.strip():
            return text.strip()

    return None


def _load_raw(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_raw(path: Path, raw: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def run(adapter: Adapter, job: str, *, work_dir: Path, limit: int | None = None, resume: bool = True) -> Path:
    if not adapter["verified"]:
        print("어댑터가 verified: false 입니다. probe 결과를 사람이 검증한 뒤 실행하세요.", file=sys.stderr)
        sys.exit(2)

    site = adapter["site"]
    job_dir = work_dir / site / job
    raw_path = job_dir / "raw.json"

    raw = _load_raw(raw_path) if resume else None

    report = Report(job_dir / "report.json")
    with report.stage("collect") as stage:
        driver_name = adapter["fetch.driver"]
        root_url = adapter["entry.root_url"]
        origin = f"{urlsplit(root_url).scheme}://{urlsplit(root_url).netloc}"

        if driver_name == "auto":
            from core.fetch.requests_driver import RequestsFetcher

            probe_fetcher = RequestsFetcher(adapter)
            try:
                probe_fetcher.get(root_url)
                driver_name = "requests"
            except Blocked:
                driver_name = "playwright"
            finally:
                probe_fetcher.close()

        fetcher = get_fetcher(driver_name, adapter)
        fetcher.open(origin)
        stage["driver"] = driver_name

        try:
            root_resp = fetcher.get(root_url)
        except Blocked as exc:
            print(f"차단됨: {exc}", file=sys.stderr)
            sys.exit(3)

        toc = build_toc(root_resp.text, adapter)
        book_title = _determine_book_title(root_resp.text, adapter)

        if raw is not None and len(raw.get("toc", [])) != len(toc):
            stage.setdefault("warnings", []).append(
                {"kind": "toc_count_changed", "detail": f"{len(raw.get('toc', []))} -> {len(toc)}"}
            )

        if raw is None:
            raw = {
                "schema": "crawl2md/raw@1",
                "meta": {},
                "toc": [],
                "pages": {},
            }

        raw["meta"] = {
            "site": site,
            "job": job,
            "root_url": root_url,
            "book_title": book_title,
            "license": adapter["license"],
            "driver": driver_name,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "adapter_sha256": adapter.sha256,
        }
        raw["toc"] = [vars(e) for e in toc]

        pending = [e for e in toc if e.id not in raw["pages"] or raw["pages"][e.id].get("error")]
        # skipped_cached 는 "이미 수집돼 건너뛴 수"다. --limit 으로 잘라낸 몫은
        # 캐시가 아니라 이번 실행에서 손대지 않은 것이므로 여기 넣지 않는다.
        skipped_cached = len(toc) - len(pending)
        todo = pending[:limit] if limit is not None else pending

        total = len(toc)
        ok = 0
        failed = 0
        failures = []

        batch_size = adapter["fetch.batch_size"]
        concurrency = adapter["fetch.concurrency"]
        delay_ms = adapter["fetch.delay_ms"]

        entries_by_id = {e.id: e for e in toc}

        for batch_start in range(0, len(todo), batch_size):
            batch = todo[batch_start : batch_start + batch_size]
            urls = [e.url for e in batch]
            for url, result in fetcher.get_many(urls, concurrency=concurrency, delay_ms=delay_ms):
                entry = next(e for e in batch if e.url == url)
                if isinstance(result, Blocked):
                    _save_raw(raw_path, raw)
                    print(f"차단됨: {result}", file=sys.stderr)
                    sys.exit(3)
                if isinstance(result, FetchError):
                    failed += 1
                    failures.append({"id": entry.id, "url": url, "reason": str(result)})
                    raw["pages"][entry.id] = vars(
                        PageRecord(
                            i=entry.i, id=entry.id, url=url, title=entry.title, status=0, html="",
                            fetched_at=datetime.now(timezone.utc).isoformat(), error=str(result),
                        )
                    )
                    continue

                if result.status != 200:
                    failed += 1
                    reason = f"http {result.status}"
                    failures.append({"id": entry.id, "url": url, "reason": reason})
                    raw["pages"][entry.id] = vars(
                        PageRecord(
                            i=entry.i, id=entry.id, url=url, title=entry.title, status=result.status, html="",
                            fetched_at=datetime.now(timezone.utc).isoformat(), error=reason,
                        )
                    )
                    continue

                content = extract_content(result.text, adapter)
                if not content:
                    failed += 1
                    reason = "content selector not found"
                    failures.append({"id": entry.id, "url": url, "reason": reason})
                    raw["pages"][entry.id] = vars(
                        PageRecord(
                            i=entry.i, id=entry.id, url=url, title=entry.title, status=result.status, html="",
                            fetched_at=datetime.now(timezone.utc).isoformat(), error=reason,
                        )
                    )
                    continue

                ok += 1
                raw["pages"][entry.id] = vars(
                    PageRecord(
                        i=entry.i, id=entry.id, url=url, title=entry.title, status=result.status, html=content,
                        fetched_at=datetime.now(timezone.utc).isoformat(), error=None,
                    )
                )

            _save_raw(raw_path, raw)

        fetcher.close()

        stage["total"] = total
        stage["ok"] = ok
        stage["failed"] = failed
        stage["skipped_cached"] = skipped_cached
        stage["failures"] = failures

    _save_raw(raw_path, raw)
    return raw_path
