import hashlib
import json
import re
import textwrap
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

BLOCK_MARKERS = [
    "Just a moment",
    "cf-chl",
    "Checking your browser",
    "Enable JavaScript and cookies",
    "Attention Required! | Cloudflare",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

CONTENT_SELECTOR_CANDIDATES = [
    "main", "article", '[role="main"]',
    ".page-content", ".post-content", ".entry-content", ".article-content", ".doc-content",
    ".markdown-body", ".markdown", ".content", ".container-content",
    "#content", "#main", "#load_content", "#post", "#article",
]

PAYWALL_KEYWORDS = [
    "구매", "유료", "비공개", "결제", "멤버십", "구독", "로그인",
    "purchase", "subscribe", "premium", "paywall", "members only",
]

SOURCE_API_TEMPLATES = [
    "{origin}/api/v1/page/{id}",
    "{origin}/api/page/{id}",
    "{origin}/{id}.md",
    "{origin}/{id}/raw",
    "{origin}/page/{id}/source",
    "{origin}/api/v1/pages/{id}",
]


def _blocked_marker(status: int, text: str) -> str | None:
    if status in (403, 429, 503):
        return f"status:{status}"
    for marker in BLOCK_MARKERS:
        if marker in text:
            return marker
    return None


def check_access(root_url: str) -> dict:
    def try_get(headers):
        try:
            resp = requests.get(root_url, headers=headers, timeout=30)
            return resp.status_code, resp.text
        except requests.RequestException:
            return 0, ""

    status_a, text_a = try_get({})
    marker_a = _blocked_marker(status_a, text_a)
    a = {"status": status_a, "blocked": marker_a is not None, "marker": marker_a}

    status_b, text_b = try_get(
        {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"}
    )
    marker_b = _blocked_marker(status_b, text_b)
    b = {"status": status_b, "blocked": marker_b is not None, "marker": marker_b}

    if not a["blocked"] or not b["blocked"]:
        if not a["blocked"]:
            verdict, driver = "plain_ok", "requests"
        else:
            verdict, driver = "ua_spoof_ok", "requests"
    else:
        verdict, driver = "browser_required", "playwright"

    return {"plain_requests": a, "spoofed_ua": b, "verdict": verdict, "recommended_driver": driver}


def _generalize_path(path: str) -> str:
    parts = path.strip("/").split("/")
    out = []
    for p in parts:
        out.append(r"\d+" if re.fullmatch(r"\d+", p) else re.escape(p) if not p else r"[\w-]+")
    return "^/" + "/".join(out) + "$" if out != [""] else "^/$"


def find_toc(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    groups: dict[str, list[tuple[str, str]]] = {}

    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        split = urlsplit(abs_url)
        path = split.path
        if not path or path == "/":
            continue
        pattern = _generalize_path(path)
        groups.setdefault(pattern, []).append((path, a.get_text().strip()))

    patterns = []
    for pattern, items in groups.items():
        paths = [p for p, _ in items]
        patterns.append({"regex": pattern, "count": len(items), "sample": paths[:5]})
    patterns.sort(key=lambda p: p["count"], reverse=True)

    chosen = None
    if patterns and patterns[0]["count"] >= 3:
        top = patterns[0]
        items = groups[top["regex"]]
        empty_titles = sum(1 for _, t in items if t == "")
        id_pattern = top["regex"].replace(r"\d+", r"(\d+)")
        selector = 'a[href^="/"]' if base_url else "a[href]"
        chosen = {
            "selector": selector,
            "id_pattern": id_pattern,
            "count": top["count"],
            "empty_titles": empty_titles,
        }

    return {"patterns": patterns[:5], "chosen": chosen}


def pick_content_selector(page_html: str) -> dict:
    soup = BeautifulSoup(page_html, "lxml")
    candidates = []
    for selector in CONTENT_SELECTOR_CANDIDATES:
        node = soup.select_one(selector)
        if node is None:
            continue
        text = re.sub(r"\s+", " ", node.get_text()).strip()
        node_count = len(list(node.find_all(True))) + 1
        candidates.append({"selector": selector, "text_len": len(text), "node_count": node_count, "chosen": False})

    if not candidates:
        return {"candidates": [], "reason": "본문 후보 없음"}

    max_len = max(c["text_len"] for c in candidates)

    # 텍스트 길이 최댓값을 기준으로 삼으면 사이드바·목차를 품은 상위 래퍼가 이긴다
    # (실측 wikidocs book/19414: .content 6,248자/노드 734 vs 정답 .page-content
    # 4,419자/노드 306, 차이 1,829자가 전부 사이드바였다). 대신 텍스트 밀도
    # (텍스트/노드)로 고른다 — 껍데기 요소가 이기지 않도록 최대 텍스트의 30%
    # 미만인 후보는 먼저 버린다.
    keep = [c for c in candidates if c["text_len"] >= 0.3 * max_len]

    if not keep or max_len < 200:
        return {"candidates": candidates, "reason": "적합한 후보 없음. 사람이 지정해야 한다"}

    for c in candidates:
        c["density"] = round(c["text_len"] / c["node_count"], 2)

    keep.sort(key=lambda c: (-c["text_len"] / c["node_count"], CONTENT_SELECTOR_CANDIDATES.index(c["selector"])))
    chosen_selector = keep[0]["selector"]
    for c in candidates:
        if c["selector"] == chosen_selector:
            c["chosen"] = True
            break

    return {"candidates": candidates, "reason": "텍스트 최대의 30% 이상 후보 중 텍스트/노드 밀도 최대"}


def pick_representative_page(pages_html: list[str]) -> int:
    """본문 셀렉터 판단에 쓸 '정상 페이지'의 인덱스를 고른다 (§6.1 step 5).

    샘플 0번을 그냥 쓰면 목차 맨 앞의 장 표지(본문 161자)가 걸려 판단이 통째로
    틀어진다(실측 wikidocs book/19414). 본문 후보의 최대 텍스트가 가장 긴 페이지를
    대표로 삼는다.
    """
    best_i, best_len = 0, -1
    for i, html in enumerate(pages_html):
        soup = BeautifulSoup(html, "lxml")
        longest = 0
        for selector in CONTENT_SELECTOR_CANDIDATES:
            node = soup.select_one(selector)
            if node is None:
                continue
            longest = max(longest, len(re.sub(r"\s+", " ", node.get_text()).strip()))
        if longest > best_len:
            best_i, best_len = i, longest
    return best_i


def probe_source_api(page_url: str, page_id: str, normal_len: int) -> dict:
    split = urlsplit(page_url)
    origin = f"{split.scheme}://{split.netloc}"

    tried = []
    found_url = None
    for template in SOURCE_API_TEMPLATES:
        url = template.format(origin=origin, id=page_id)
        try:
            resp = requests.get(url, timeout=30)
            status, text, content_type = resp.status_code, resp.text, resp.headers.get("Content-Type", "")
        except requests.RequestException:
            status, text, content_type = 0, "", ""

        is_catchall = False
        if "text/html" in content_type and ("<!DOCTYPE" in text or "<!doctype" in text or "<html" in text):
            if normal_len and 0.8 * normal_len <= len(text) <= 1.2 * normal_len:
                is_catchall = True

        tried.append({"url": url, "status": status, "len": len(text), "is_catchall": is_catchall})
        if status == 200 and not is_catchall and found_url is None:
            found_url = url

    verdict = "found" if found_url else "none"
    result = {"tried": tried, "verdict": verdict}
    if found_url:
        result["found_url"] = found_url
    return result


_INLINE_DOLLAR_RE = re.compile(r"\$[^$\n]{1,200}\$")
_BLOCK_DOLLAR_RE = re.compile(r"\$\$[\s\S]{1,2000}?\$\$")


def census(pages_html: list[str]) -> dict:
    tag_counter = Counter()
    class_counter = Counter()
    code_lang_counter = Counter()
    image_host_counter = Counter()
    inline_dollar = block_dollar = mathml = katex = 0

    for html in pages_html:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(True):
            tag_counter[tag.name] += 1
            classes = tag.get("class")
            if classes:
                class_counter[" ".join(classes)] += 1
        for code in soup.select("pre > code"):
            for cls in code.get("class", []) or []:
                if cls.startswith("language-"):
                    code_lang_counter[cls[len("language-"):]] += 1

        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src:
                host = urlsplit(src).netloc
                if host:
                    image_host_counter[host] += 1

        inline_dollar += len(_INLINE_DOLLAR_RE.findall(html))
        block_dollar += len(_BLOCK_DOLLAR_RE.findall(html))
        mathml += html.count("<math")
        katex += html.count('class="katex')

    if mathml == 0 and katex == 0 and (inline_dollar > 0 or block_dollar > 0):
        math_verdict = "latex_preserved"
    elif mathml > 0 or katex > 0:
        math_verdict = "rendered_only"
    else:
        math_verdict = "none"

    return {
        "tags": dict(tag_counter.most_common(30)),
        "classes": dict(class_counter.most_common(20)),
        "code_langs": dict(code_lang_counter),
        "image_hosts": dict(image_host_counter),
        "math": {
            "inline_dollar": inline_dollar,
            "block_dollar": block_dollar,
            "mathml": mathml,
            "katex": katex,
            "verdict": math_verdict,
        },
    }


def detect_license(root_html: str) -> dict:
    soup = BeautifulSoup(root_html, "lxml")

    cc_link = soup.select_one('a[href*="creativecommons.org"]')
    if cc_link:
        m = re.search(r"/licenses/([a-z-]+)/", cc_link.get("href", ""))
        if m:
            return {"found": f"CC {m.group(1).upper().replace('-', ' ')}", "evidence": "a[href*=creativecommons.org]"}

    cc_img = soup.select_one('img[alt*="CC"], img[src*="creativecommons"]')
    if cc_img:
        return {"found": cc_img.get("alt", "CC"), "evidence": "img[alt*=CC] in root page"}

    text = soup.get_text()
    m = re.search(r"CC[ -]BY[A-Z-]*", text)
    if m:
        return {"found": m.group(0), "evidence": "text regex /CC[ -]BY[A-Z-]*/"}
    m = re.search(r"저작권\s*:\s*(\S+)", text)
    # 캡처값이 숫자뿐이면 라이선스가 아니다. wikidocs 는 "저작권 : 120 명이 추천"
    # 처럼 저작권 아이콘 옆에 추천 수를 붙여 두어 이 정규식이 "120"을 집는다(실측).
    if m and not re.fullmatch(r"[\d,.]+", m.group(1)):
        return {"found": m.group(1), "evidence": "text regex /저작권\\s*:\\s*(\\S+)/"}

    meta = soup.select_one('meta[name="license"]')
    if meta and meta.get("content"):
        return {"found": meta["content"], "evidence": 'meta[name="license"]'}

    return {"found": "unknown", "evidence": None}


def suggest_paywall_markers(pages: list[tuple[str, str]]) -> list[dict]:
    sentence_pages: dict[str, set[str]] = {}
    for page_id, html in pages:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text()
        sentences = re.split(r"[\n.]+", text)
        for s in sentences:
            norm = re.sub(r"\s+", " ", s).strip()
            if not norm:
                continue
            sentence_pages.setdefault(norm, set()).add(page_id)

    common = {s: pids for s, pids in sentence_pages.items() if len(pids) >= 3}

    candidates = []
    for s, pids in common.items():
        if any(kw in s for kw in PAYWALL_KEYWORDS):
            candidates.append((s, pids))

    drafts: dict[str, set[str]] = {}
    for s, pids in candidates:
        pattern = re.escape(s)
        pattern = re.sub(r"\\d+", r"\\d+", pattern)
        pattern = re.sub(r"(\\ )+", r"\\s+", pattern)
        drafts.setdefault(pattern, set()).update(pids)

    n_pages = len({pid for pid, _ in pages}) or 1
    results = []
    for pattern, pids in drafts.items():
        ratio = len(pids) / n_pages
        if 0.05 <= ratio <= 0.40:
            confidence = "high"
        else:
            confidence = "low"
        results.append({"pattern": pattern, "pages": len(pids), "confidence": confidence})

    results.sort(key=lambda r: r["pages"], reverse=True)
    return results[:10]


def render_draft_yaml(data: dict, site: str) -> str:
    access = data.get("access", {})
    toc = data.get("toc", {})
    content = data.get("content", {})
    license_info = data.get("license", {})
    paywall_candidates = [c for c in data.get("paywall_candidates", []) if c.get("confidence") == "high"]

    chosen_toc = toc.get("chosen") or {}
    chosen_content_selector = None
    for c in content.get("candidates", []):
        if c.get("chosen"):
            chosen_content_selector = c["selector"]
            break

    markers_yaml = "\n".join(f"    - '{c['pattern']}'" for c in paywall_candidates) or "    []"

    return textwrap.dedent(f"""\
        # crawl2md 어댑터 초안 — probe 자동 생성. 사람이 검토 후 verified: true 로 바꿀 것.
        site: {site}
        name: {site}
        verified: false
        license: "{license_info.get('found', 'unknown')}"
        notes: "probe 자동 생성 초안"

        entry:
          root_url: "{data.get('root_url', '')}"
          page_url: "TODO: {{id}} 템플릿"

        toc:
          strategy: link_pattern
          selector: '{chosen_toc.get("selector", "a[href]")}'
          id_pattern: '{chosen_toc.get("id_pattern", "^/(\\\\d+)$")}'

        content:
          selector: '{chosen_content_selector or "TODO"}'
          paywall_markers:
        {markers_yaml}

        output:
          filename_template: "{{order:03d}}_{{title}}.md"
          path: "./out/{{job}}"
        """)


@dataclass
class ProbeResult:
    data: dict
    draft_yaml: str


def _evenly_sample(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _sample_image_urls(pages_html: list[str]) -> dict[str, str]:
    """호스트별 대표 이미지 절대 URL 1개를 뽑는다 (도달성 확인용)."""
    out: dict[str, str] = {}
    for html in pages_html:
        for img in BeautifulSoup(html, "lxml").find_all("img"):
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            host = urlsplit(src).netloc
            if host and host not in out:
                out[host] = src
    return out


def run(root_url: str, site: str, *, sample: int = 20, work_dir: Path,
        save_fixtures: Path | None = None) -> ProbeResult:
    access = check_access(root_url)
    driver = access["recommended_driver"]

    if driver == "playwright":
        raise NotImplementedError(
            "browser_required 판정입니다. playwright로 루트 페이지를 직접 받아 "
            "probe.run에 넘기는 절차가 명세에 CLI 레벨로만 기술되어 있어, "
            "이 함수는 requests 로만 동작합니다. cli.py에서 처리하세요."
        )

    root_resp = requests.get(root_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    root_html = root_resp.text

    toc_info = find_toc(root_html, root_url)
    chosen = toc_info.get("chosen")
    if not chosen:
        raise NotImplementedError("toc.chosen 이 없습니다. 사람이 selector/id_pattern 을 지정해야 합니다.")

    id_pattern = re.compile(chosen["id_pattern"])
    all_ids = []
    for a in BeautifulSoup(root_html, "lxml").select("a[href]"):
        href = a.get("href", "")
        abs_url = urljoin(root_url, href)
        m = id_pattern.match(urlsplit(abs_url).path)
        if m:
            all_ids.append(m.group(1))

    sample_ids = _evenly_sample(all_ids, sample)

    origin = f"{urlsplit(root_url).scheme}://{urlsplit(root_url).netloc}"
    page_url_guess = origin + "/{id}"

    pages_html = []
    pages_for_paywall = []
    for pid in sample_ids:
        try:
            resp = requests.get(page_url_guess.format(id=pid), headers={"User-Agent": USER_AGENT}, timeout=30)
            pages_html.append(resp.text)
            pages_for_paywall.append((pid, resp.text))
        except requests.RequestException:
            continue

    if pages_html:
        rep = pick_representative_page(pages_html)  # 장 표지 회피 (§6.1 step 5)
        content_info = pick_content_selector(pages_html[rep])
        normal_len = len(pages_html[rep])
        rep_id = sample_ids[rep]
    else:
        rep = 0
        content_info = {"candidates": [], "reason": "샘플 없음"}
        normal_len = 0
        rep_id = None

    source_api_info = (
        probe_source_api(page_url_guess.format(id=rep_id), rep_id, normal_len)
        if rep_id else {"tried": [], "verdict": "none"}
    )
    census_info = census(pages_html)
    license_info = detect_license(root_html)
    paywall_info = suggest_paywall_markers(pages_for_paywall)

    # 호스트 루트에 HEAD 를 던지면 이미지 CDN 이 0/405 를 돌려주어 "도달 불가"로
    # 잘못 보인다(실측 static.wikidocs.net). 실제 이미지 URL 을 GET 해서 확인한다.
    asset_reach = {}
    for host, img_url in _sample_image_urls(pages_html).items():
        try:
            r = requests.get(img_url, headers={"User-Agent": USER_AGENT}, timeout=10)
            asset_reach[host] = r.status_code
        except requests.RequestException:
            asset_reach[host] = 0

    work_dir.mkdir(parents=True, exist_ok=True)
    if save_fixtures is not None:
        save_fixtures.mkdir(parents=True, exist_ok=True)
        (save_fixtures / f"{site}_root.html").write_text(root_html, encoding="utf-8")
        if pages_html:
            (save_fixtures / f"{site}_full_{rep_id}.html").write_text(pages_html[rep], encoding="utf-8")
    draft_path = Path("sites") / f"{site}.draft.yaml"

    data = {
        "schema": "crawl2md/probe@1",
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "root_url": root_url,
        "access": access,
        "toc": toc_info,
        "content": content_info,
        "source_api": source_api_info,
        "census": census_info,
        "assets": {"host_reachable_plain": asset_reach},
        "license": license_info,
        "paywall_candidates": paywall_info,
        "draft_adapter": str(draft_path),
    }

    draft_yaml = render_draft_yaml(data, site)

    probe_path = work_dir / "probe.json"
    probe_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft_yaml, encoding="utf-8")

    return ProbeResult(data=data, draft_yaml=draft_yaml)
