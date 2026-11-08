import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from core.adapter import Adapter
from core.retry import call_with_retry


@dataclass
class AssetPlan:
    url_to_name: dict[str, str] = field(default_factory=dict)


def _sanitize(name: str, forbidden: re.Pattern, replacement: str) -> str:
    return forbidden.sub(replacement, name)


def plan(soups: dict[str, "BeautifulSoup"], adapter: Adapter, base_url: str) -> AssetPlan:
    forbidden = re.compile(adapter["output.filename_forbidden"])
    replacement = adapter["output.filename_replacement"]
    skip_data_uri = adapter["assets.skip_data_uri"]
    strip_attrs = adapter["assets.strip_attrs"]
    name_template = adapter["assets.name_template"]

    url_to_name: dict[str, str] = {}
    used_names: set[str] = set()

    # 호출자가 soups 를 페이지 순서(i 오름차순)로 넘긴다고 가정한다(§8.3.1).
    # 파이썬 dict는 삽입 순서를 보존하므로 그대로 순회하면 된다.
    for page_id, soup in soups.items():
        page_url = getattr(soup, "_c2m_url", base_url)
        index = 0
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                img.decompose()
                continue
            if src.startswith("data:"):
                if skip_data_uri:
                    img.decompose()
                continue

            abs_url = urljoin(page_url, src)
            if abs_url not in url_to_name:
                basename = os.path.basename(urlsplit(abs_url).path) or "image"
                stem, ext = os.path.splitext(basename)
                hash8 = hashlib.sha256(abs_url.encode("utf-8")).hexdigest()[:8]
                name = name_template.format(
                    page_id=page_id,
                    basename=basename,
                    stem=stem,
                    ext=ext,
                    index=index,
                    hash8=hash8,
                )
                name = _sanitize(name, forbidden, replacement)
                candidate = name
                n = 1
                while candidate in used_names:
                    stem2, ext2 = os.path.splitext(name)
                    candidate = f"{stem2}_{n}{ext2}"
                    n += 1
                used_names.add(candidate)
                url_to_name[abs_url] = candidate

            img["src"] = f"{adapter['assets.dir']}/{url_to_name[abs_url]}"
            for attr in strip_attrs:
                img.attrs.pop(attr, None)
            index += 1

    return AssetPlan(url_to_name=url_to_name)


def download(plan: AssetPlan, dest: Path, adapter: Adapter) -> dict:
    if not adapter["assets.download"]:
        return {"downloaded": 0, "cached": 0, "failed": 0, "failures": []}

    dest.mkdir(parents=True, exist_ok=True)
    timeout_s = adapter["assets.timeout_s"]
    max_bytes = adapter["assets.max_bytes"]
    concurrency = adapter["assets.concurrency"]

    import requests

    result = {"downloaded": 0, "cached": 0, "failed": 0, "failures": []}
    lock_names = list(plan.url_to_name.items())

    def fetch_one(item):
        url, name = item
        out_path = dest / name
        if out_path.exists() and out_path.stat().st_size > 0:
            return ("cached", None)

        def attempt():
            resp = requests.get(url, timeout=timeout_s)
            return resp.status_code, dict(resp.headers), resp.content

        try:
            status, _headers, content = call_with_retry(attempt)
        except Exception as exc:  # noqa: BLE001
            return ("failed", {"url": url, "name": name, "reason": str(exc)})

        if content is None or status != 200 or not content:
            return ("failed", {"url": url, "name": name, "reason": f"status {status}"})
        if len(content) > max_bytes:
            return ("failed", {"url": url, "name": name, "reason": "too large"})

        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp_path.write_bytes(content)
        tmp_path.replace(out_path)
        return ("downloaded", None)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for status, failure in pool.map(fetch_one, lock_names):
            result[status] += 1
            if failure:
                result["failures"].append(failure)

    return result
