# crawl2md 구현명세 v2

**대상 독자**: 이 문서만 읽고 전체 코드를 작성할 구현자(Claude Code).
**전제**: Python 3.11+. 이 문서에 정의되지 않은 동작은 만들지 않는다. 정의가 필요한데 빠져 있다면 그것은 명세의 결함이므로 임의로 채우지 말고 보고한다.
**근거**: 2026-09-08 wikidocs `book/2155`(208페이지, 이미지 454개, 실패 0건) 실수집·변환에서 검증된 값.

---

## 0. 읽는 순서

1. §1 원칙 — 판단이 갈릴 때 돌아올 기준
2. §3 데이터 계약 — 모든 모듈이 주고받는 것의 정의. **여기가 이 문서의 중심이다**
3. §4 어댑터 스키마 — YAML 전 필드
4. §5 모듈 API — 구현할 함수 목록
5. §6~§9 단계별 알고리즘
6. §12 테스트 — 완료 판정 기준

---

## 1. 목적·범위·원칙

### 1.1 목적

웹 문서 사이트를 `probe → collect → convert → build` 4단계로 처리해, 옵시디언에서 바로 쓸 수 있는 마크다운 묶음을 만든다. 1차 대상은 wikidocs 하나이며, 다른 사이트는 YAML 어댑터 추가로 확장한다.

### 1.2 명시적 범위 밖

만들지 않는다. 요구가 생기면 그때 별도로 논의한다.

- 로그인 세션, 유료 콘텐츠 우회, CAPTCHA 처리
- 증분 갱신(변경 감지 후 부분 재수집) — 전체 재수집만
- 다중 사이트 동시 처리, 작업 큐, DB, 웹 UI
- robots.txt 자동 준수 판정 — 사람이 판단하고 어댑터에 기록한다

### 1.3 설계 원칙

| ID | 원칙 | 구현상 의미 |
|---|---|---|
| P1 | 수집과 변환을 분리한다 | `convert`는 네트워크를 **절대** 호출하지 않는다. 유일한 입력은 `raw.json`. 위반 시 테스트 T-C5 실패 |
| P2 | 탐색 없이 수집하지 않는다 | `collect`는 어댑터의 `verified: true`를 확인하고, false면 즉시 종료(exit 2) |
| P3 | 원본을 보존한다 | 수식·코드 언어·표는 변환 손실 1순위. 이스케이프는 기본 전부 off |
| P4 | 실패는 조용히 넘어가지 않는다 | 모든 실패는 `report.json`에 항목으로 남는다. 로그만 찍고 끝내는 코드 금지 |
| P5 | 새 사이트는 코드 수정 없이 | 훅은 최후수단. YAML로 되는 일을 훅으로 옮기면 리뷰에서 반려 |

---

## 2. 디렉터리 구조

```
crawl2md/
├── pyproject.toml
├── cli.py
├── core/
│   ├── __init__.py
│   ├── models.py          # 모든 dataclass. §3
│   ├── adapter.py         # YAML 로드·검증·훅 바인딩. §4
│   ├── errors.py          # 예외 계층. §10
│   ├── retry.py           # 재시도 정책. §10.2
│   ├── probe.py           # Phase 0. §6
│   ├── collect.py         # Phase 1. §7
│   ├── convert.py         # Phase 2. §8
│   ├── assets.py          # 이미지. §8.3
│   ├── mdconv.py          # markdownify 서브클래스. §8.4
│   ├── emit.py            # Phase 3. §9
│   ├── report.py          # 리포트 누적·직렬화
│   └── fetch/
│       ├── __init__.py    # get_fetcher(name, cfg) 팩토리
│       ├── base.py        # Fetcher 프로토콜 + 기본 get_many
│       ├── requests_driver.py
│       ├── playwright_driver.py
│       └── browser_mcp_driver.py
├── sites/
│   ├── wikidocs.yaml
│   └── hooks/
│       └── __init__.py
├── tests/
│   ├── fixtures/          # §12.1
│   └── test_*.py
├── work/<site>/<job>/     # 중간 산출물. .gitignore
└── out/<job>/             # 최종 산출물. .gitignore
```

---

## 3. 데이터 계약

모든 구조체는 `core/models.py`에 dataclass로 정의한다. JSON 직렬화는 `dataclasses.asdict` + `json.dump(ensure_ascii=False, indent=2)`.

### 3.1 job 식별자

`job`은 하나의 수집 작업을 가리키는 문자열이며, 모든 경로 템플릿의 `{job}`에 치환된다.

- CLI `--job <문자열>`로 명시 가능
- 생략 시 규칙: `{site}-{slug}-{YYYYMMDD}`
  - `slug` = `entry.root_url` 경로의 마지막 세그먼트에서 `[^A-Za-z0-9_-]`를 `-`로 치환한 값
  - 예: `https://wikidocs.net/book/2155` → `wikidocs-2155-20260908`
- 허용 문자: `[A-Za-z0-9._-]`. 그 외 문자가 있으면 `ValidationError`

### 3.2 `Response` — Fetcher 반환값

```python
@dataclass(frozen=True)
class Response:
    url: str
    status: int          # HTTP 상태. 브라우저 fetch도 실제 상태를 넣는다
    text: str            # 응답 본문 (디코딩 완료)
    elapsed_ms: int
```

### 3.3 `TocEntry`

```python
@dataclass
class TocEntry:
    i: int               # 0부터. 목차 DOM 문서 순서. 이것이 책 순서의 유일한 근거
    id: str              # 사이트 내 페이지 식별자. toc.id_pattern의 캡처그룹 1
    url: str             # entry.page_url에 id를 치환한 절대 URL
    title: str           # 목차 링크의 텍스트 (strip 완료)
```

### 3.4 `PageRecord`

```python
@dataclass
class PageRecord:
    i: int
    id: str
    url: str
    title: str
    status: int
    html: str            # 본문 컨테이너의 innerHTML만. 페이지 전체 HTML 아님
    fetched_at: str      # ISO8601
    error: str | None    # 실패 시 사유. 성공이면 None이고 html은 빈 문자열이 아님
```

### 3.5 `raw.json` — collect 산출물

경로: `work/{site}/{job}/raw.json`

```json
{
  "schema": "crawl2md/raw@1",
  "meta": {
    "site": "wikidocs",
    "job": "wikidocs-2155-20260908",
    "root_url": "https://wikidocs.net/book/2155",
    "book_title": "딥 러닝을 이용한 자연어 처리 입문 - RAG, 에이전트, LLM 파인튜닝까지",
    "license": "CC BY-NC-SA",
    "driver": "playwright",
    "collected_at": "2026-09-08T07:05:00+09:00",
    "adapter_sha256": "3f9a..."
  },
  "toc": [ TocEntry, ... ],
  "pages": { "<id>": PageRecord, ... }
}
```

**`meta.book_title` 결정 순서** (앞의 것이 있으면 채택):

1. 어댑터 `entry.book_title` (문자열 직접 지정)
2. 어댑터 `entry.book_title_selector`로 루트 페이지에서 추출한 텍스트
3. 루트 페이지 `<h1>` 첫 번째의 텍스트
4. 루트 페이지 `<title>`에서 어댑터 `entry.title_suffix_strip` 정규식을 제거한 값
5. 위 전부 실패 시 `null`. 프론트매터에서 해당 키를 생략한다 (빈 문자열 금지)

**`meta.adapter_sha256`**: 수집에 쓰인 어댑터 YAML 원본 바이트의 SHA-256. `convert` 시점에 현재 어댑터와 다르면 경고를 리포트에 남긴다(중단하지는 않는다).

### 3.6 `probe.json` — probe 산출물

경로: `work/{site}/{job}/probe.json`

```json
{
  "schema": "crawl2md/probe@1",
  "probed_at": "2026-09-08T06:50:00+09:00",
  "root_url": "https://wikidocs.net/book/2155",
  "access": {
    "plain_requests": {"status": 403, "blocked": true, "marker": "Just a moment"},
    "spoofed_ua":     {"status": 403, "blocked": true, "marker": "Just a moment"},
    "verdict": "browser_required",
    "recommended_driver": "playwright"
  },
  "toc": {
    "patterns": [
      {"regex": "^/(\\d+)$", "count": 209, "sample": ["/21690", "/22660"]},
      {"regex": "^/book/(\\d+)$", "count": 1, "sample": ["/book/2155"]}
    ],
    "chosen": {"selector": "a[href^=\"/\"]", "id_pattern": "^/(\\d+)$",
               "count": 209, "empty_titles": 1}
  },
  "content": {
    "candidates": [
      {"selector": ".page-content", "text_len": 6946, "node_count": 214,
       "density": 32.46, "chosen": true},
      {"selector": "#load_content",  "text_len": 7161, "node_count": 239,
       "density": 29.96, "chosen": false}
    ],
    "reason": "텍스트 최대의 30% 이상 후보 중 텍스트/노드 밀도 최대"
  },
  "source_api": {
    "tried": [
      {"url": ".../api/page/22660", "status": 200, "len": 71184, "is_catchall": true}
    ],
    "verdict": "none"
  },
  "census": {
    "sample_pages": 20,
    "tags":    {"p": 4857, "code": 4280, "pre": 3268},
    "classes": {"language-python": 2719, "ad-wrapper markdown_tags": 161},
    "code_langs": {"python": 2719, "pyton": 1, "no-highlight": 1},
    "image_hosts": {"static.wikidocs.net": 473},
    "math": {"inline_dollar": 312, "block_dollar": 47, "mathml": 0, "katex": 0,
             "verdict": "latex_preserved"}
  },
  "assets": {
    "host_reachable_plain": {"static.wikidocs.net": 200}
  },
  "license": {"found": "CC BY-NC-SA", "evidence": "img[alt*=CC] in root page"},
  "paywall_candidates": [
    {"pattern": "유료 E-?book", "pages": 12, "confidence": "high"},
    {"pattern": "E-?book 구매 시에 볼 수 있는", "pages": 2, "confidence": "high"}
  ],
  "draft_adapter": "sites/wikidocs.draft.yaml"
}
```

### 3.7 `report.json` — 모든 단계 공통

경로: `work/{site}/{job}/report.json`. 단계별로 **누적 병합**한다(덮어쓰지 않는다).

```json
{
  "schema": "crawl2md/report@1",
  "job": "wikidocs-2155-20260908",
  "stages": {
    "collect": {
      "started_at": "...", "finished_at": "...",
      "driver": "playwright",
      "total": 208, "ok": 208, "failed": 0, "skipped_cached": 0,
      "failures": [{"id": "12345", "url": "...", "reason": "timeout after 3 retries"}]
    },
    "convert": {
      "started_at": "...", "finished_at": "...",
      "documents": 208,
      "images": {"unique": 454, "downloaded": 454, "cached": 0, "failed": 0,
                 "failures": []},
      "warnings": [
        {"kind": "paywall",        "id": "158088", "title": "18-04 ...",
         "detail": "matched /유료 E-?book/"},
        {"kind": "short_document", "id": "86768",  "title": "000. Part 1. 기본 과정",
         "detail": "markdown 154 chars < threshold 200"},
        {"kind": "unbalanced_fence",  "file": "...", "detail": "``` count = 7"},
        {"kind": "unbalanced_math",   "file": "...", "detail": "$ count = 13"},
        {"kind": "missing_asset",     "file": "...", "detail": "assets/x.png"},
        {"kind": "table_count_drift", "file": "...", "detail": "html 3 vs md 2"},
        {"kind": "adapter_changed",   "detail": "raw.json 수집 시점 어댑터와 불일치"}
      ]
    },
    "build": {
      "started_at": "...", "finished_at": "...",
      "target": "folder", "path": "out/wikidocs-2155-20260908",
      "files": 663, "bytes": 18874368, "index": "000_INDEX.md"
    }
  }
}
```

**경고 종류(`kind`)는 위 7개로 고정한다.** 새 종류를 추가하려면 이 문서를 먼저 고친다.

**`stages.collect.skipped_cached`의 정의**: 이미 수집돼 있어 이번 실행에서 건너뛴
페이지 수다. `--limit N`으로 잘라낸 나머지는 **여기 넣지 않는다** — 캐시가 아니라 아직
손대지 않은 것이므로, 넣으면 `--limit 3`으로 3개만 받고도 "77개가 캐시됨"으로 보인다.
따라서 `skipped_cached + len(todo) + (limit으로 미착수한 수) = total`이며, 세 번째 항은
리포트에 따로 적지 않는다.

---

## 4. 어댑터 스키마 (완전판)

`sites/<site>.yaml`. **아래에 없는 키가 YAML에 있으면 `ValidationError`로 거부한다**(오타 조기 발견 목적). 아래 있는데 YAML에 없으면 기본값을 쓴다.

| 경로 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `site` | str | ✔ | — | 어댑터 식별자. 파일명(`<site>.yaml`)과 일치해야 함 |
| `name` | str | ✔ | — | 사람이 읽는 이름 |
| `verified` | bool | ✔ | — | probe 결과를 사람이 확인했는가. `false`면 collect 거부 |
| `license` | str | ✔ | — | `"CC BY-NC-SA"` 등. `"unknown"`이면 build가 경고 |
| `notes` | str | | `""` | 자유 메모. 검증에서 무시 |
| **entry** ||||| |
| `entry.root_url` | url | ✔ | — | 목차 페이지 |
| `entry.page_url` | str | ✔ | — | 템플릿. `{id}` 치환 |
| `entry.book_title` | str\|null | | `null` | §3.5 1순위 |
| `entry.book_title_selector` | css\|null | | `null` | §3.5 2순위 |
| `entry.title_suffix_strip` | regex\|null | | `null` | §3.5 4순위. `<title>`에서 제거할 꼬리 |
| **toc** ||||| |
| `toc.strategy` | enum | ✔ | — | `link_pattern` \| `sitemap` \| `hook`. 1차는 `link_pattern`만 구현, 나머지는 `NotImplementedError` |
| `toc.selector` | css | ✔ | — | 링크를 찾을 셀렉터 |
| `toc.id_pattern` | regex | ✔ | — | `href`에서 id를 뽑는다. **캡처그룹 정확히 1개**. 아니면 ValidationError |
| `toc.drop_empty_title` | bool | | `true` | 제목이 빈 링크 제외 |
| `toc.dedupe_by` | enum | | `href` | `href` \| `id`. 중복 링크 제거 기준 |
| `toc.order` | enum | | `dom` | `dom` \| `title`. **`dom` 이외는 권장하지 않음** |
| `toc.exclude_patterns` | [regex] | | `[]` | href가 매칭되면 제외 |
| **content** ||||| |
| `content.selector` | css | ✔ | — | 본문 컨테이너 |
| `content.remove` | [css] | | `[]` | `decompose()` 대상 |
| `content.paywall_threshold` | int | | `0` | 변환 후 마크다운 길이 하한. 미만이면 `short_document` 경고. `0`이면 비활성 |
| `content.paywall_markers` | [regex] | | `[]` | 원본 HTML에 매칭되면 `paywall` 경고. **1순위 판정** |
| **assets** ||||| |
| `assets.download` | bool | | `true` | false면 원본 URL 유지 |
| `assets.dir` | str | | `assets` | 출력 하위 폴더명 |
| `assets.name_template` | str | | `{page_id}_{basename}` | §8.3.2 변수 |
| `assets.skip_data_uri` | bool | | `true` | `data:` 이미지는 `<img>` 제거 |
| `assets.strip_attrs` | [str] | | `[loading, decoding, width, height, style, class]` | `<img>`에서 제거할 속성 |
| `assets.concurrency` | int | | `8` | 다운로드 스레드 수 |
| `assets.driver` | enum | | `requests` | 이미지 전용 드라이버. 본문과 달라도 됨 |
| `assets.timeout_s` | int | | `30` | |
| `assets.max_bytes` | int | | `20971520` | 초과 시 스킵 + 경고(20MB) |
| **markdown** ||||| |
| `markdown.heading_style` | enum | | `ATX` | markdownify 옵션 그대로 |
| `markdown.bullets` | str | | `-` | |
| `markdown.escape_asterisks` | bool | | `false` | **true로 바꾸면 수식이 깨진다** |
| `markdown.escape_underscores` | bool | | `false` | 위와 동일 |
| `markdown.escape_misc` | bool | | `false` | 위와 동일 |
| `markdown.code_lang_map` | {str:str} | | `{}` | `language-<key>` → 펜스 언어. 값이 `""`면 언어 없는 펜스 |
| `markdown.code_lang_default` | str | | `""` | 클래스가 없는 `<pre>`의 언어 |
| `markdown.math` | enum | | `preserve` | `preserve` \| `strip`. `strip`은 1차에서 `NotImplementedError` |
| `markdown.collapse_blank_lines` | int | | `2` | 연속 빈 줄 상한 |
| **output** ||||| |
| `output.filename_template` | str | ✔ | — | §8.5.1 변수 |
| `output.filename_max` | int | | `90` | 제목 부분의 최대 길이(확장자 제외) |
| `output.filename_forbidden` | regex | | `[\\/:*?"<>\|]` | 이 문자를 `filename_replacement`로 치환 |
| `output.filename_replacement` | str | | `-` | |
| `output.frontmatter` | [str] | | `[title, order, source]` | 허용 키: `title, order, source, book, license, id`. 그 외는 ValidationError |
| `output.h1_from_title` | bool | | `true` | 본문 맨 앞에 `# {title}` 삽입 |
| `output.footer_template` | str\|null | | `null` | `{url}` `{license}` `{title}` `{id}` 치환 |
| `output.index_file` | str\|null | | `000_INDEX.md` | `null`이면 인덱스 생성 안 함 |
| `output.index_wikilink` | bool | | `true` | true면 `[[파일명(확장자 제외)\|제목]]`, false면 `[제목](파일명)` |
| `output.target` | enum | | `folder` | `folder` \| `zip` \| `obsidian` |
| `output.path` | str | ✔ | — | `{job}` `{site}` 치환. `target=zip`이면 `.zip` 경로 |
| **fetch** ||||| |
| `fetch.driver` | enum | | `auto` | `auto` \| `requests` \| `playwright` \| `browser_mcp` |
| `fetch.concurrency` | int | | `5` | |
| `fetch.delay_ms` | int | | `120` | 요청 간 간격(워커별) |
| `fetch.timeout_s` | int | | `30` | |
| `fetch.batch_size` | int | | `40` | `get_many` 1회 배치 크기. §11.3 |
| `fetch.block_markers` | [str] | | §11.1 기본값 | 차단 판정 문자열 |
| `fetch.user_agent` | str | | §11.2 기본값 | requests 드라이버용 |
| `fetch.playwright.channel` | str\|null | | `chrome` | `null`이면 번들 크로미움 |
| `fetch.playwright.headless` | bool | | `false` | **true는 챌린지 통과율이 낮다** |
| `fetch.playwright.persist_profile` | bool | | `true` | `user_data_dir` 사용 |
| `fetch.playwright.profile_dir` | str | | `work/.pwprofile/{site}` | |
| `fetch.playwright.mode` | enum | | `page_evaluate` | `page_evaluate`만 구현. `context_request`는 §11.4 이유로 금지 — 지정 시 ValidationError |
| **hooks** (선택) ||||| |
| `hooks.module` | str | | `null` | 임포트 경로. 예 `sites.hooks.wikidocs` |
| `hooks.clean_html` | str | | `null` | 함수명 |
| `hooks.page_title` | str | | `null` | |
| `hooks.filename` | str | | `null` | |
| `hooks.post_markdown` | str | | `null` | |

### 4.1 검증 규칙 (`adapter.load` 시점)

전부 `ValidationError`로 거부하고 메시지에 필드 경로를 포함한다.

1. 미지의 키 존재
2. 필수 키 누락
3. `site` != 파일명 stem
4. `toc.id_pattern`의 캡처그룹 수 != 1
5. 모든 regex 필드가 `re.compile` 실패
6. 모든 css 필드가 `soupsieve.compile` 실패
7. `output.frontmatter`에 허용 외 키
8. `output.filename_template`에 알 수 없는 변수
9. `fetch.playwright.mode == "context_request"`
10. `hooks.module`이 지정됐는데 임포트 실패, 또는 지정된 함수명이 모듈에 없음
11. `fetch.concurrency < 1` 또는 `> 16` (예의상 상한)

---

## 5. 모듈 API

구현자는 아래 시그니처를 **그대로** 만든다. 이름과 반환 타입을 바꾸지 않는다.

### core/adapter.py

```python
@dataclass(frozen=True)
class Adapter:
    raw: dict                  # 병합 후 전체 설정
    path: Path
    sha256: str
    hooks: "Hooks"
    def __getitem__(self, dotted: str): ...      # a["content.selector"]
    def get(self, dotted: str, default=None): ...

@dataclass(frozen=True)
class Hooks:
    clean_html:    Callable[[BeautifulSoup, "Ctx"], BeautifulSoup] | None
    page_title:    Callable[[BeautifulSoup, TocEntry, "Ctx"], str]  | None
    filename:      Callable[[TocEntry, "Ctx"], str]                 | None
    post_markdown: Callable[[str, "Ctx"], str]                      | None

@dataclass
class Ctx:
    adapter: Adapter
    page_id: str
    order: int
    title: str
    log: logging.Logger

def load(path: Path) -> Adapter: ...             # 검증 포함. §4.1
def default_config() -> dict: ...                # 기본값 트리
```

훅 규약: 예외를 던지지 않는다. 내부에서 잡아 `ctx.log.warning`을 남기고 입력을 그대로 반환한다. 호출부는 훅이 None이면 항등 함수로 취급한다.

### core/fetch/base.py

```python
class FetchError(Exception): ...
class Blocked(FetchError): ...                   # 차단 감지

@runtime_checkable
class Fetcher(Protocol):
    name: str
    supports_batch: bool
    def open(self, origin: str) -> None: ...
    def get(self, url: str) -> Response: ...
    def get_many(self, urls: list[str], *, concurrency: int,
                 delay_ms: int) -> Iterator[tuple[str, Response | FetchError]]: ...
    def close(self) -> None: ...

def default_get_many(fetcher, urls, *, concurrency, delay_ms): ...
    # ThreadPoolExecutor(concurrency)로 fetcher.get을 호출.
    # 각 워커는 호출 후 delay_ms 만큼 sleep.
    # supports_batch=False인 드라이버는 이것을 그대로 쓴다.

def is_blocked(resp: Response, markers: list[str]) -> str | None: ...
    # 차단 판정. 매칭된 마커 문자열 또는 None. §11.1

def get_fetcher(name: str, adapter: Adapter) -> Fetcher: ...   # core/fetch/__init__.py
```

### core/probe.py

```python
@dataclass
class ProbeResult:
    data: dict                 # §3.6 그대로
    draft_yaml: str

def run(root_url: str, site: str, *, sample: int = 20,
        work_dir: Path,
        save_fixtures: Path | None = None) -> ProbeResult: ...
    # work_dir 는 §3.6의 work/{site}/{job} 이다. probe 는 어댑터 없이 도는 유일한
    # 단계이므로 job 은 CLI 가 root_url 에서 §3.1 규칙으로 만들어 넘긴다.
    # save_fixtures 가 주어지면 루트 페이지와 대표 샘플의 전체 HTML을 그 폴더에
    # <site>_root.html / <site>_full_<id>.html 로 저장한다 (§12.1)

# 내부 단계. 각각 단독 테스트 가능해야 한다.
def check_access(root_url: str) -> dict: ...
def find_toc(html: str, base_url: str) -> dict: ...
def pick_content_selector(page_html: str) -> dict: ...
def pick_representative_page(pages_html: list[str]) -> int: ...   # §6.1 step 5
def probe_source_api(page_url: str, page_id: str, normal_len: int) -> dict: ...
def census(pages_html: list[str]) -> dict: ...
def detect_license(root_html: str) -> dict: ...
def suggest_paywall_markers(pages: list[tuple[str, str]]) -> list[dict]: ...
def render_draft_yaml(data: dict, site: str) -> str: ...
```

### core/collect.py

```python
def run(adapter: Adapter, job: str, *, work_dir: Path,
        limit: int | None = None, resume: bool = True) -> Path: ...
    # 반환: raw.json 경로

def build_toc(root_html: str, adapter: Adapter) -> list[TocEntry]: ...
def extract_content(page_html: str, adapter: Adapter) -> str: ...
    # content.selector로 뽑은 innerHTML. 못 찾으면 FetchError가 아니라 빈 문자열 +
    # PageRecord.error = "content selector not found"
```

### core/convert.py

```python
@dataclass
class ConvertResult:
    out_dir: Path
    documents: list["DocResult"]
    warnings: list[dict]

@dataclass
class DocResult:
    entry: TocEntry
    filename: str
    markdown_len: int
    paywalled: bool

def run(adapter: Adapter, job: str, *, work_dir: Path,
        out_dir: Path) -> ConvertResult: ...

def clean_html(html: str, adapter: Adapter, ctx: Ctx) -> BeautifulSoup: ...
def build_document(soup, entry, adapter, ctx, book_title, license_) -> str: ...
def quality_gate(md: str, soup, adapter, ctx) -> list[dict]: ...   # §8.6
```

### core/assets.py

```python
@dataclass
class AssetPlan:
    url_to_name: dict[str, str]       # 절대 URL → 저장 파일명

def plan(soups: dict[str, BeautifulSoup], adapter: Adapter,
         base_url: str) -> AssetPlan: ...
    # soup의 <img>를 순회하며 이름 확정 + src를 상대경로로 재작성(soup 변형)

def download(plan: AssetPlan, dest: Path, adapter: Adapter) -> dict: ...
    # 반환: {"downloaded": n, "cached": n, "failed": n, "failures": [...]}
```

### core/mdconv.py

```python
class Crawl2mdConverter(MarkdownConverter):
    def __init__(self, adapter: Adapter, **kw): ...
    def convert_pre(self, el, text, parent_tags=None) -> str: ...   # §8.4

def make_converter(adapter: Adapter) -> Crawl2mdConverter: ...
def assert_markdownify_compatible() -> None: ...                    # §8.4.2
```

### core/emit.py

```python
def write_index(docs: list[DocResult], out_dir: Path,
                adapter: Adapter, meta: dict) -> Path | None: ...
def finalize(out_dir: Path, adapter: Adapter, job: str) -> dict: ...
    # target에 따라 folder(그대로) / zip(압축) / obsidian(볼트 하위 복사)
```

### core/report.py

```python
class Report:
    def __init__(self, path: Path): ...          # 있으면 로드, 없으면 생성
    def stage(self, name: str) -> "StageWriter": ...   # 컨텍스트 매니저
    def save(self) -> None: ...
    def render_markdown(self) -> str: ...        # report.md
```

---

## 6. Phase 0 — probe

**목표**: 어댑터 초안을 사람이 검토 가능한 형태로 만든다. 추측을 확정으로 바꾸지 않는다.

**입력**: 목차 URL 1개
**출력**: `work/{site}/{job}/probe.json`, `sites/{site}.draft.yaml`

### 6.1 절차

```
1. check_access(root_url)
2. 루트 페이지 HTML 확보 (1의 결과에 따라 requests 또는 playwright)
3. find_toc(root_html)
4. 목차에서 무작위 sample개(기본 20) 페이지 수집
   - 목차 앞/중간/뒤에서 고르게 뽑는다(균등 간격). 앞 20개만 뽑지 말 것 —
     wikidocs는 뒤쪽에 유료 페이지가 몰려 있다
5. pick_content_selector(pick_representative_page로 고른 대표 페이지)
   - 샘플 0번을 그냥 쓰면 안 된다. 목차 맨 앞은 장 표지인 경우가 많고
     (19414의 "01. Hermes Agent 소개"는 본문이 161자다) 그 페이지로 판단하면
     셀렉터 선택이 통째로 틀어진다. 본문 후보의 최대 텍스트가 가장 긴 샘플을 쓴다
6. probe_source_api(샘플 1개)
7. census(샘플 전체)
8. detect_license(root_html)
9. suggest_paywall_markers(샘플 전체)
10. 이미지 호스트별 plain requests 도달성 확인
    - 호스트 루트에 HEAD를 던지지 말 것. 이미지 CDN이 405나 연결 거부를 돌려주어
      실제로는 받아지는 호스트가 "도달 불가"로 기록된다(실측 static.wikidocs.net).
      샘플에서 뽑은 **실제 이미지 URL을 GET**해서 확인한다
11. render_draft_yaml → verified: false 로 기록
```

### 6.2 `check_access`

```
a) requests.get(page_url, 기본 UA 없이)  → status, blocked?
b) requests.get(page_url, 실제 Chrome UA + Accept-Language) → status, blocked?
verdict:
  a 또는 b가 200이고 blocked=false      → "plain_ok",        recommended_driver="requests"
  a는 막히고 b는 통과                    → "ua_spoof_ok",     recommended_driver="requests"
  둘 다 막힘                             → "browser_required", recommended_driver="playwright"
```

**절대 하지 말 것**: TLS 지문 위조 라이브러리(`curl_cffi` 등)를 시도. 중계 프록시 환경에서 무의미하며(§13), 유지보수 부담만 는다.

### 6.3 `find_toc`

```
1. soup.select('a[href]')의 href를 전부 수집
2. 각 href를 정규화(상대→절대, 쿼리·프래그먼트 제거) 후, 경로만 남긴다
3. 경로를 일반화한 패턴으로 그룹핑:
   숫자 런 → \d+, 그 외 세그먼트 → 리터럴
   예: /22660 → ^/(\d+)$   /docs/intro → ^/docs/([\w-]+)$
4. 그룹별 개수를 세고, 개수 내림차순으로 정렬
5. 최다 그룹을 chosen으로 제안. 단, 개수가 3 미만이면 chosen=null로 두고 사람에게 넘긴다
6. chosen 그룹에서 링크 텍스트가 빈 것의 개수를 센다 → empty_titles
7. patterns 배열에 상위 5개를 모두 기록한다 (사람이 다른 것을 고를 수 있게)
```

`selector`는 `a[href^="/"]`처럼 chosen 패턴을 만족하는 가장 단순한 형태로 역산해 제안한다. 만들기 어려우면 `a[href]`를 쓰고 `id_pattern`으로 거른다.

### 6.4 `pick_content_selector`

후보 목록(이 순서로 시도, 존재하는 것만):

```
main, article, [role="main"],
.page-content, .post-content, .entry-content, .article-content, .doc-content,
.markdown-body, .markdown, .content, .container-content,
#content, #main, #load_content, #post, #article
```

선택 규칙:

```
1. 각 후보의 텍스트 길이(공백 정규화 후)와 하위 노드 수를 잰다
2. max_len = 최댓값
3. keep = {c : c.text_len >= 0.3 * max_len}     ← 껍데기 요소 배제용 하한
4. keep 중 텍스트 밀도(text_len / node_count)가 최대인 것을 선택
   (동률이면 후보 목록에서 먼저 나온 것)
5. keep이 비었거나 max_len < 200이면 chosen=null. 사람이 지정해야 한다
```

4번 규칙의 근거(2026-09-08 두 책 실측):

| 책 / 페이지 | 후보 | 텍스트 | 노드 | 밀도 | 정답 |
|---|---|---|---|---|---|
| 2155 / 22660 | `.page-content` | 6,946 | 214 | 32.5 | ✔ |
| | `#load_content` | 7,161 | 239 | 30.0 | (광고 래퍼) |
| 19414 / 336232 | `.page-content` | 4,419 | 306 | 14.4 | ✔ |
| | `.content` | 6,248 | 734 | 8.5 | (사이드바 목차) |
| | `#load_content` | 4,670 | 384 | 12.2 | |

**"텍스트가 가장 긴 것"을 기준으로 삼으면 안 된다.** 초판 규칙(`keep = 0.9 * max_len` 중 노드 최소)은 2155에서는 맞았지만 19414에서 `.content`를 골랐다 — 텍스트가 1,829자 더 많은 이유가 본문이 아니라 사이드바 목차 92개였기 때문이다. 밀도로 바꾸면 두 책 모두 정답이 나온다. 3번의 30% 하한은 텍스트가 거의 없는 껍데기(`<main>` 빈 컨테이너 등)가 밀도만으로 이기는 것을 막는다.

### 6.5 `probe_source_api`

**상태코드 200을 성공으로 보면 안 된다.** catch-all 라우트가 있는 사이트가 많다.

```
후보 URL:
  {origin}/api/v1/page/{id}
  {origin}/api/page/{id}
  {origin}/{id}.md
  {origin}/{id}/raw
  {origin}/page/{id}/source
  {origin}/api/v1/pages/{id}

각 응답에 대해 is_catchall 판정:
  - Content-Type이 text/html 이고
  - 본문에 "<!DOCTYPE" 또는 "<html" 이 있고
  - 길이가 정상 페이지 길이의 0.8~1.2배
  → catch-all 로 본다

verdict:
  is_catchall=false 이고 status=200 인 것이 있으면 "found" + 해당 URL
  전부 catch-all 또는 4xx/5xx 이면 "none"
```

`verdict="found"`이면 draft YAML에 주석으로 남기되, **1차 구현은 HTML 경로만 지원**하므로 사람이 판단하게 한다.

### 6.6 `census`

샘플 페이지들의 본문 컨테이너에 대해:

- 태그 빈도 (상위 30)
- `class` 속성 값 원문 빈도 (상위 20)
- `pre > code`의 `language-*` 클래스 빈도 → `code_langs`
- `<img src>`의 호스트별 빈도
- 수식 탐지:
  - `inline_dollar`: `\$[^$\n]{1,200}\$` 매칭 수
  - `block_dollar`: `\$\$[\s\S]{1,2000}?\$\$` 매칭 수
  - `mathml`: `<math` 수
  - `katex`: `class="katex` 수
  - verdict: `mathml`/`katex`가 0이고 dollar가 1 이상이면 `latex_preserved`(최선).
    `mathml`이나 `katex`가 있으면 `rendered_only` — 원본 LaTeX 복원 불가일 수 있으니 경고

### 6.7 `suggest_paywall_markers`

길이만으로는 못 잡는다(wikidocs `18-07`은 8,010자인데 유료). 반복 문구를 찾는다.

```
1. 각 샘플 페이지 본문을 문장 단위로 분할 (개행 + 마침표 기준)
2. 문장을 공백 정규화 후, 3개 이상 페이지에 등장하는 문장만 남긴다
3. 아래 키워드가 포함된 문장만 후보로 승격
   구매, 유료, 비공개, 결제, 멤버십, 구독, 로그인,
   purchase, subscribe, premium, paywall, members only
4. 후보 문장에서 공통 부분 문자열을 뽑아 정규식 초안을 만든다
   (숫자·공백은 \d+, \s+ 로 일반화)
5. 각 초안의 매칭 페이지 수를 세고, confidence 를 매긴다
   high: 샘플의 5~40% 매칭
   low:  40% 초과 (본문 공통 푸터일 가능성) 또는 5% 미만
6. 상위 10개를 probe.json에 넣고, high만 draft YAML에 넣는다
```

**주의**: 오탐 위험이 실재한다. wikidocs 저작권 안내문의 "구매하신 E-book 파일을"은 유료 페이지가 아니다. 그래서 `E-book` 단독 매칭은 잘못이고, `유료 E-?book`처럼 좁혀야 한다. draft에는 항상 `verified: false`가 붙으므로 사람이 최종 확정한다.

### 6.8 `detect_license`

이 순서로 찾고, 첫 히트를 채택. 못 찾으면 `"unknown"`.

```
1. <a href*="creativecommons.org"> 의 href에서 라이선스 코드 추출
2. <img alt*="CC"> 또는 src*="creativecommons" 의 alt
3. 루트 페이지 텍스트에서 정규식 /CC[ -]BY[A-Z-]*/ 또는 /저작권\s*:\s*(\S+)/
   - /저작권.../ 의 캡처값이 숫자([\d,.]+)뿐이면 버린다. 라이선스가 아니다
4. <meta name="license"> content
```

3번 단서의 근거: wikidocs는 저작권 아이콘 옆에 추천 수를 붙여 두어 페이지 텍스트가
`저작권 : 120 명이 추천`이 된다. 숫자 필터가 없으면 book/19414의 라이선스가 `"120"`으로
기록된다(실측). 이 책처럼 표기가 아예 없는 경우의 정답은 `"unknown"`이다.

---

## 7. Phase 1 — collect

**입력**: 어댑터 (`verified: true` 필수)
**출력**: `raw.json`, `report.json[stages.collect]`

### 7.1 절차

```
1. verified 확인. false면 stderr에 안내 후 exit 2
2. job 결정 (§3.1)
3. resume=True이고 raw.json이 있으면 로드. 없으면 새로 만든다
4. 드라이버 결정:
     fetch.driver == "auto":
        requests로 루트 페이지 1회 시도 → 차단이면 playwright로 승격
     그 외: 지정값 그대로
5. fetcher.open(origin)   # origin = root_url의 scheme://host
6. 루트 페이지 수집 → build_toc → toc 확정
   - 기존 raw.json에 toc가 있으면 비교. 개수가 다르면 경고를 리포트에 남기고
     새 toc를 채택한다(순서 i는 재계산)
7. book_title 결정 (§3.5)
8. 미수집 페이지 목록 = toc 중 pages에 없거나 error가 있는 것
   limit이 주어지면 앞에서 limit개만
9. fetch.batch_size 단위로 fetcher.get_many 호출
   - 배치 하나가 끝날 때마다 raw.json을 저장한다 (중단 내성)
10. 각 응답 처리:
     - is_blocked → PageRecord.error="blocked: <marker>", 전체 수집 중단(exit 3).
       차단된 상태로 계속 돌리면 IP가 더 오래 막힌다
     - status != 200 → error 기록, 계속 진행
     - extract_content 실패 → error="content selector not found", 계속 진행
     - 성공 → html 저장
11. 최종 raw.json 저장 + 리포트 기록
```

### 7.2 `build_toc`

```
1. soup.select(toc.selector)
2. 각 a에 대해:
     href 정규화(절대화 → 경로만)
     toc.exclude_patterns 매칭되면 스킵
     toc.id_pattern 매칭 안 되면 스킵
     id = 캡처그룹 1
     title = a.get_text().strip() (공백 정규화)
     drop_empty_title이고 title == "" 이면 스킵
3. toc.dedupe_by 기준으로 중복 제거 (첫 등장 유지)
4. toc.order == "dom" 이면 DOM 순서 유지. "title"이면 제목 정렬
5. i를 0부터 재부여
6. url = entry.page_url.format(id=id)
```

**`toc.order`의 기본값을 바꾸지 말 것.** wikidocs 목차에는 `99) ===텐서플로우 1버전 코드 (구버전)===`가 12장 중간에 있어 제목 정렬 시 순서가 깨진다.

### 7.3 동시성

- `fetch.concurrency` 5, `fetch.delay_ms` 120이 wikidocs 208페이지 실패 0건이 나온 값이다. 어댑터에서 올릴 수는 있지만 기본값을 바꾸지 않는다
- 배치가 끝날 때마다 저장하므로, 배치 크기(`fetch.batch_size` 40)는 "잃어도 되는 작업량"이다

---

## 8. Phase 2 — convert

**입력**: `raw.json` + 어댑터
**출력**: `out/{job}/*.md`, `out/{job}/assets/*`, `report.json[stages.convert]`

**P1 불변식**: 이 단계는 이미지 다운로드를 제외하면 네트워크를 쓰지 않는다. 이미지는 `assets.download: false`로 완전히 끌 수 있어야 한다.

### 8.1 순서

```
1. raw.json 로드. adapter_sha256 비교 → 다르면 adapter_changed 경고
2. 모든 페이지에 대해 clean_html → soup 딕셔너리
3. assets.plan(soups)  # 이름 확정 + src 재작성. 전체를 한 번에 봐야 충돌 처리가 된다
4. assets.download(plan)
5. 페이지별로 build_document → 파일 쓰기
6. quality_gate
7. 리포트 기록
```

3번을 페이지별로 하지 않는 이유: 파일명 충돌 해소(`_1`, `_2`)가 전역 상태를 필요로 한다.

### 8.2 `clean_html`

```
1. BeautifulSoup(html, "lxml")
2. content.remove의 각 셀렉터에 대해 soup.select(s) → decompose()
   - markdownify **전에** 해야 한다. 광고 스크립트 텍스트가 본문에 섞인다
3. hooks.clean_html이 있으면 호출
```

### 8.3 이미지

#### 8.3.1 `plan`

```
전역 used_names = set()
페이지 순서(i 오름차순)로:
  soup.find_all("img"):
    src = img.get("src", "").strip()
    if not src: img.decompose(); continue
    if src.startswith("data:"):
        if assets.skip_data_uri: img.decompose(); continue
        else: 그대로 두고 넘어간다
    abs_url = urljoin(page_url, src)
    if abs_url not in url_to_name:
        name = assets.name_template.format(...)   # §8.3.2
        name = sanitize(name)                     # output.filename_forbidden 규칙 재사용
        stem, n = name, 1
        while name in used_names:
            base, ext = splitext(stem); name = f"{base}_{n}{ext}"; n += 1
        used_names.add(name); url_to_name[abs_url] = name
    img["src"] = f"{assets.dir}/{url_to_name[abs_url]}"
    for a in assets.strip_attrs: img.attrs.pop(a, None)
```

`alt`는 **제거하지 않는다**(접근성·검색). 없으면 빈 문자열로 둔다.

#### 8.3.2 `name_template` 변수

| 변수 | 값 | 예 |
|---|---|---|
| `{page_id}` | 이미지가 처음 등장한 페이지의 id | `22660` |
| `{basename}` | URL 경로의 파일명 전체 | `word2vec.PNG` |
| `{stem}` | 확장자 제외 | `word2vec` |
| `{ext}` | 점 포함 확장자. 없으면 `""` | `.PNG` |
| `{index}` | 그 페이지 안에서의 0-기반 순번 | `3` |
| `{hash8}` | 절대 URL의 SHA-256 앞 8자 | `a3f91b2c` |

기본값 `{page_id}_{basename}`을 쓰는 이유: 페이지마다 `image1.png` 같은 흔한 이름이 겹친다.

#### 8.3.3 `download`

```
ThreadPoolExecutor(assets.concurrency), 드라이버는 assets.driver
각 항목:
  dest = out_dir / assets.dir / name
  if dest.exists() and dest.stat().st_size > 0: cached += 1; continue
  resp = get(url, timeout=assets.timeout_s)
  if len(content) > assets.max_bytes: failed, 사유 "too large"
  if status != 200 or not content: 재시도(§10.2) 후 failed
  else: 원자적 쓰기(임시파일 → rename)
```

### 8.4 markdownify

#### 8.4.1 변환기

````python
class Crawl2mdConverter(MarkdownConverter):
    def __init__(self, adapter, **kw):
        self.lang_map = adapter["markdown.code_lang_map"]
        self.lang_default = adapter["markdown.code_lang_default"]
        super().__init__(**kw)

    def convert_pre(self, el, text, parent_tags=None):
        if not text:
            return ''
        lang = self.lang_default
        code = el.find('code')
        if code and code.get('class'):
            for c in code.get('class'):
                if c.startswith('language-'):
                    lang = c[len('language-'):]
                    break
        lang = self.lang_map.get(lang, lang)
        return f'\n\n```{lang}\n{text.strip()}\n```\n\n'
````

(위 블록은 4-백틱 펜스다. 안에 마크다운 코드펜스 ` ``` `가 들어 있기 때문이다.)

생성:

```python
MarkdownConverter(
    heading_style = adapter["markdown.heading_style"],
    bullets       = adapter["markdown.bullets"],
    escape_asterisks   = adapter["markdown.escape_asterisks"],    # 기본 False
    escape_underscores = adapter["markdown.escape_underscores"],  # 기본 False
    escape_misc        = adapter["markdown.escape_misc"],         # 기본 False
)
```

**이스케이프 3종은 반드시 off가 기본이다.** 켜면 `$w_{t}$`의 `_`가 `\_`가 되어 수식이 통째로 깨진다. 부작용으로 본문의 `*`, `#`가 우연히 마크다운 문법이 될 수 있으나, 수식 보존이 우선한다.

#### 8.4.2 버전 가드

`convert_pre`의 시그니처는 markdownify 메이저 버전에 따라 다르다. 1.2.2는 `(self, el, text, parent_tags=None)`이다. 조용히 어긋나면 코드블록이 전부 4칸 들여쓰기로 나오고, 그 사실을 아무도 모른다.

```python
def assert_markdownify_compatible() -> None:
    import inspect, markdownify
    sig = inspect.signature(markdownify.MarkdownConverter.convert_pre)
    params = list(sig.parameters)
    if params[:3] != ["self", "el", "text"]:
        raise RuntimeError(
            f"markdownify convert_pre 시그니처 불일치: {params}. "
            "core/mdconv.py의 오버라이드를 이 버전에 맞게 수정할 것.")
```

`make_converter`가 최초 호출될 때 1회 실행한다.

### 8.5 문서 조립

```
1. md = converter.convert_soup(soup)
2. md = re.sub(r'\n{%d,}' % (collapse_blank_lines+1), '\n'*collapse_blank_lines, md).strip()
3. hooks.post_markdown이 있으면 호출
4. 프론트매터 생성 (output.frontmatter 순서대로, 값이 None인 키는 생략)
     title:   YAML 이스케이프. " → ' 로 치환 후 큰따옴표로 감싼다
     order:   entry.i (정수)
     source:  entry.url
     book:    meta.book_title
     license: adapter["license"]
     id:      entry.id
5. h1_from_title이면 "# {title}\n\n" 삽입
6. footer_template이 있으면 "\n\n---\n\n" + 치환 결과
7. 파일 쓰기 (UTF-8, 개행 LF, 마지막 개행 1개)
```

산출 예:

```markdown
---
title: "09-02 워드투벡터(Word2Vec)"
order: 70
source: https://wikidocs.net/22660
book: "딥 러닝을 이용한 자연어 처리 입문 - RAG, 에이전트, LLM 파인튜닝까지"
---

# 09-02 워드투벡터(Word2Vec)

(본문)

---

출처: <https://wikidocs.net/22660> (CC BY-NC-SA)
```

#### 8.5.1 `filename_template` 변수

| 변수 | 값 |
|---|---|
| `{order}` | `entry.i`. `{order:03d}` 처럼 포맷 스펙 허용 |
| `{title}` | 정제된 제목 |
| `{id}` | `entry.id` |
| `{slug}` | 제목을 소문자화하고 공백을 `-`로 바꾼 값 |

정제 순서(반드시 이 순서):

```
1. output.filename_forbidden 정규식 → filename_replacement 로 치환
2. 연속 공백을 하나로
3. 앞뒤 공백 제거
4. 끝의 '.' 제거 (윈도우에서 저장 실패)
5. output.filename_max 로 자르기
6. 결과가 빈 문자열이면 entry.id 를 쓴다
7. 확장자 .md 부가
8. 최종 파일명이 중복이면 "_2", "_3" 접미사 (stem 뒤, 확장자 앞)
```

`{order:03d}` 접두사를 쓰는 이유: 탐색기 정렬이 곧 책 순서가 된다.

### 8.6 품질 게이트

각 문서에 대해 검사하고, 걸리면 §3.7의 `warnings`에 항목을 추가한다. **중단하지 않는다.**

| kind | 조건 |
|---|---|
| `paywall` | 원본 HTML이 `content.paywall_markers` 중 하나에 매칭 |
| `short_document` | `paywall_threshold > 0` 이고 마크다운 길이가 그 미만 |
| `unbalanced_fence` | 마크다운의 ` ``` ` 개수가 홀수 |
| `unbalanced_math` | §8.6.1의 잔여 `$` 개수가 2 이상 |
| `missing_asset` | 마크다운이 참조하는 `assets/x`가 실제로 없음 |
| `table_count_drift` | soup의 `<table>` 수 != 마크다운에서 `^\|`로 시작하는 블록 수. **코드펜스 안은 세지 않는다** (§8.6.2) |

#### 8.6.1 `unbalanced_math` 판정

단순히 `$` 개수의 홀짝을 보면 안 된다. 208개 실문서로 검증한 결과 통화 표기(`$45.55`, `$0.02/1K 토큰`)가 오탐으로 잡힌다. 아래 순서로 **정상적인 수식을 먼저 소거하고 남은 것**을 센다.

````python
BLOCK  = re.compile(r'\$\$[\s\S]*?\$\$')
INLINE = re.compile(r'\$(?!\s)[^$]{1,500}?(?<!\s)\$')

def stray_dollars(md: str) -> int:
    md = re.sub(r'```.*?```', '', md, flags=re.S)   # 1. 코드 펜스 제거 (셸 변수 등)
    md = re.sub(r'`[^`\n]*`',  '', md)              # 2. 인라인 코드 제거
    md = BLOCK.sub('', md)                          # 3. 정상 블록 수식 소거
    md = INLINE.sub('', md)                         # 4. 정상 인라인 수식 소거
    return md.count('$')                            # 5. 남은 것이 이상한 것

# stray_dollars(md) >= 2 이면 unbalanced_math 경고
````

설계 근거:

- `INLINE`의 `(?!\s)` / `(?<!\s)`: 여는 `$` 뒤와 닫는 `$` 앞이 공백이 아니어야 한다. LaTeX의 관례이며, `$45.55와 같은 가격` 같은 산문을 배제한다
- `INLINE`이 개행을 허용하는 이유(`[^$]`, `[^$\n]`이 아님): 여러 줄에 걸친 `$P(\text{An}) × ... $` 수식이 실제로 존재한다. 개행을 막으면 그것이 오탐이 된다. 500자 상한으로 폭주를 막는다
- 임계값 **2**: 정상 문서에도 잔여 `$`가 1개 나올 수 있다(통화 표기 1회). 실제 파손은 항상 2개 이상을 남긴다 — 여는 `$$`를 하나 지우면 2, 닫는 인라인 `$`를 하나 지워도 2

208개 실문서에서 이 규칙의 오탐은 **0건**이다. 단순 홀짝 규칙이었다면 4건이 오탐으로 잡혔다.

**다른 책에서는 오탐이 난다(2026-09-08 실측).** book/19414의 한 문서는 통화 표기가
`\$0.5`, `\$0.0184`처럼 **원본 HTML에 이미 백슬래시로 이스케이프된 채** 28개 들어 있었고,
INLINE 소거를 거친 잔여가 4가 되어 `unbalanced_math` 경고가 붙었다. 수식은 멀쩡했다.
즉 임계값 2는 wikidocs book/2155에 맞춰진 값이지 보편값이 아니다. 통화 표기가 잦은
사이트를 추가한다면 어댑터에 임계값 옵션을 두는 것을 검토한다(현재는 상수 2).

#### 8.6.2 표 계수는 코드펜스를 먼저 지운다

`table_count_drift`를 셀 때 `^|`로 시작하는 줄을 그냥 세면, 문서가 **마크다운 표를
예시로 보여주는 코드블록**을 표로 오인한다. 실측(book/19414 `334968`): 원본 HTML의
`<table>`은 0개인데 `<pre><code>` 안에 리포트 예시 표가 두 개 들어 있어 `html 0 vs md 2`
경고가 났다. `stray_dollars`(§8.6.1 1단계)와 **같은 순서로 코드펜스를 먼저 제거**한 뒤
센다. 인라인 코드는 줄 첫 문자가 `|`가 될 수 없으므로 지우지 않아도 된다.

#### 8.6.3 게이트의 한계

**한계를 명확히 해둔다.** 이 게이트는 완전 탐지가 아니라 **저오탐 경보**다. 누락된 구분자가 바로 옆 수식과 결합해 하나의 `INLINE` 매칭으로 흡수되면 잔여가 1이 되어 잡히지 않는다(예: `$x 와 $y$` → `$x 와 $`가 아니라 `$x 와 $y$` 전체가 소거됨). 임계값을 1로 낮추면 이 경우를 잡을 수 있지만 통화 표기 오탐이 살아나므로, **오탐 0을 택했다.** 수식 정확성이 중요한 사이트를 추가할 때는 어댑터에 임계값 옵션을 추가하는 것을 검토한다.

---

## 9. Phase 3 — build

### 9.1 인덱스

`output.index_file`이 null이 아니면 생성. 문서를 `entry.i` 오름차순으로 나열한다.

```markdown
# {book_title} — 목차

원본: <{root_url}> · 라이선스: {license}

총 {n}개 문서 · 이미지 {m}개

- [[070_09-02 워드투벡터(Word2Vec)|09-02 워드투벡터(Word2Vec)]]
- [[142_18-04 ...|18-04 ...]] *(유료·비공개)*
```

- `index_wikilink: true` → `[[파일명(확장자 제외)|제목]]`
- `false` → `[제목](URL인코딩된 파일명)`
- `paywall` 경고가 붙은 문서에 ` *(유료·비공개)*` 표시

### 9.2 `finalize`

| target | 동작 |
|---|---|
| `folder` | `out/{job}` 그대로 둔다 |
| `zip` | `output.path`(`.zip`)로 압축. 아카이브 내부 최상위 폴더는 `{job}` |
| `obsidian` | `output.path`가 가리키는 볼트 하위 경로로 복사. 대상이 이미 있으면 `--force` 없이는 거부 |

라이선스가 `unknown`이거나 `NC`/`ND`를 포함하면 stdout에 재배포 주의 문구를 1줄 출력한다.

---

## 10. 오류 처리

### 10.1 예외 계층

```text
Crawl2mdError(Exception)
├── ValidationError      # 어댑터 스키마 위반. exit 2
├── NotVerifiedError     # verified: false. exit 2
├── FetchError           # 네트워크·HTTP. 페이지 단위로 기록하고 계속
│   └── Blocked          # 차단 감지. 전체 중단. exit 3
└── ConvertError         # 변환 불가. 문서 단위로 기록하고 계속
```

종료 코드: `0` 성공(경고 있어도 0), `1` 예상치 못한 예외, `2` 설정 오류, `3` 차단.

### 10.2 재시도 정책 (`core/retry.py`)

```python
MAX_ATTEMPTS = 3
BASE_DELAY_S = 1.0
FACTOR       = 2.0
JITTER       = 0.2          # ±20%
```

대기 시간: `BASE_DELAY_S * FACTOR**(attempt-1) * uniform(1-JITTER, 1+JITTER)`
→ 대략 1.0s, 2.0s (3회차 실패 시 포기)

**재시도하는 경우**: 연결 오류, 타임아웃, 상태코드 `429`, `500`, `502`, `503`, `504`,
그리고 Cloudflare 대역 `520`~`527`

`520`(오리진 불명 오류)을 빼면 안 된다. 2026-09-08 wikidocs book/19414을 동시 5·지연
120ms로 받자 80페이지 중 **30건이 520**으로 실패했는데, 같은 URL을 단독 재요청하면
전부 200이었다. 즉 일시적 오류이며, 재시도 없이 실패로 적으면 사용자가 collect를
한 번 더 돌려야 한다.
**재시도하지 않는 경우**: 그 외 4xx, 그리고 `Blocked` (즉시 중단)

`429`에 `Retry-After` 헤더가 있으면 그 값을 우선한다(최대 60초).

---

## 11. Fetcher 드라이버

### 11.1 차단 판정

`fetch.block_markers` 기본값:

```yaml
- "Just a moment"
- "cf-chl"
- "Checking your browser"
- "Enable JavaScript and cookies"
- "Attention Required! | Cloudflare"
```

판정: 상태코드가 `403`, `429`, `503` 중 하나 **또는** 본문에 위 마커 중 하나가 포함.
`is_blocked`는 매칭된 마커 문자열(상태코드만으로 걸렸으면 `"status:403"`)을 반환한다.

### 11.2 requests_driver

- `requests.Session` 1개 재사용
- 기본 헤더:
  ```
  User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36
  Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7
  Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
  ```
- `supports_batch = False` → `default_get_many` 사용
- 인코딩: `resp.encoding`이 None이거나 `ISO-8859-1`이면 `resp.apparent_encoding`으로 재해석. 한글 사이트에서 자주 문제가 된다

### 11.3 playwright_driver — 기본 권장

```python
def open(self, origin):
    self._pw = sync_playwright().start()
    kw = dict(headless=cfg["headless"])
    if cfg["channel"]: kw["channel"] = cfg["channel"]
    if cfg["persist_profile"]:
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=cfg["profile_dir"], **kw)
    else:
        self._browser = self._pw.chromium.launch(**kw)
        self._ctx = self._browser.new_context()
    self._page = self._ctx.new_page()
    self._page.goto(origin, wait_until="domcontentloaded",
                    timeout=cfg_timeout_ms)
```

`supports_batch = True`. `get_many`는 **페이지 컨텍스트 안에서 JS로** 동시성을 처리한다.

```python
def get_many(self, urls, *, concurrency, delay_ms):
    for chunk in chunks(urls, self.batch_size):
        res = self._page.evaluate("""
        async ([urls, conc, delay]) => {
          const out = []; let i = 0;
          const worker = async () => {
            while (i < urls.length) {
              const u = urls[i++];
              try {
                const t0 = performance.now();
                const r = await fetch(u, {credentials: 'include'});
                const text = await r.text();
                out.push({url: u, status: r.status, text,
                          elapsed_ms: Math.round(performance.now() - t0)});
              } catch (e) {
                out.push({url: u, error: String(e)});
              }
              await new Promise(s => setTimeout(s, delay));
            }
          };
          await Promise.all(Array.from({length: conc}, worker));
          return out;
        }""", [chunk, concurrency, delay_ms])
        for item in res:
            yield (item["url"],
                   FetchError(item["error"]) if "error" in item
                   else Response(item["url"], item["status"],
                                 item["text"], item["elapsed_ms"]))
```

제약과 근거:

- **같은 오리진만** 이 경로로 받는다. 크로스 오리진은 CORS에 걸린다. 이미지는 `assets.driver`(requests)가 따로 받는다
- `headless: false`가 기본인 이유: 헤드리스는 챌린지에 더 자주 걸린다
- `channel: chrome`이 기본인 이유: 번들 크로미움보다 통과율이 높다
- `persist_profile: true`면 한 번 통과한 clearance가 다음 실행에도 남는다
- 배치 크기 40~45. 208개를 한 번에 넣으면 `page.evaluate` 타임아웃

### 11.4 `context_request`를 금지하는 이유

`browser_context.request.get()`은 쿠키는 공유하지만 **Playwright 자체 HTTP 스택**을 쓴다. 브라우저 TLS 지문을 쓰지 않으므로 Cloudflare에 다시 막힌다. 어댑터에서 `mode: context_request`를 지정하면 `ValidationError`로 거부한다.

### 11.5 browser_mcp_driver — 폴백

Playwright를 설치할 수 없는 환경에서 사람이 붙어 진행하는 모드. `supports_batch = True`.

**구현 전 반드시 문서에 노출해야 하는 제약** (실제로 여기서 막혔다):

- Chrome은 한 오리진에서 **자동 다운로드를 1회만** 허용한다. 두 번째부터 조용히 차단되고 에러도 나지 않는다
- 따라서 이 드라이버는 **다운로드를 정확히 한 번만** 수행한다. 테스트 다운로드를 먼저 하면 본 파일이 막힌다
- 실행 시작 시 stdout에 안내: "Chrome 주소창 → 사이트 설정 → 자동 다운로드 → 허용을 먼저 켜주세요"
- 수집 데이터는 `window.__crawl2md`에 누적한다. **탭을 닫으면 전부 소실된다.** 배치마다 경고 출력
- 배치 크기는 40~45 고정

---

## 12. 테스트

### 12.1 픽스처

`tests/fixtures/`에 실제 응답을 저장해 둔다. 네트워크 없이 전 과정이 돌아야 한다.

**동봉된 픽스처** (`fixtures/`). HTML 파일은 **본문 컨테이너의 innerHTML**이며 전체 페이지가 아니다 — `raw.json`에 저장되는 것과 같은 형태다.

| 파일 | 크기 | 특성 | 쓰이는 테스트 |
|---|---|---|---|
| `raw_mini.json` | 6페이지 | 아래 6개로 만든 축소 `raw.json` | T-C*, T-M*, T-G*, T-N* |
| `wikidocs_math_images_22660.html` | 9.9KB | 이미지 10, 인라인·블록 수식 18 | T-M4, T-M5, T-N4 |
| `wikidocs_code_heavy_21698.html` | 17.2KB | `<pre>` 22개, **통화 표기 `$45.55` 1개** | T-M1~3, T-G4 |
| `wikidocs_table_21707.html` | 9.7KB | `<table>` 1, `<pre>` 15 | T-M6 |
| `wikidocs_paywall_long_151138.html` | 3.7KB | 본문이 긴 유료 페이지 (`18-07`) | T-G1 |
| `wikidocs_paywall_short_158088.html` | 179B | 짧은 유료 페이지 (`18-04`) | T-G1 |
| `wikidocs_chapter_cover_86768.html` | 154B | 장 표지 (`000. Part 1`) | T-G2 |
| `cloudflare_challenge.html` | 5.4KB | 실제 403 챌린지 응답 | T-P1 |

**구현자가 첫 `probe` 실행으로 직접 만들어야 하는 픽스처** (전체 페이지가 필요해 동봉 불가, 각 250KB 내외):

| 파일 | 만드는 법 | 쓰이는 테스트 |
|---|---|---|
| `wikidocs_root.html` | `book/2155` 목차 페이지 전체 HTML 저장 | T-P2, T-P3 |
| `wikidocs_full_22660.html` | `/22660` 페이지 전체 HTML 저장 | T-P4, T-P5, T-P6 |

`crawl2md probe`에 `--save-fixtures <dir>` 옵션을 두어 이 두 파일을 자동 저장하게 한다.

### 12.2 단위 테스트

| ID | 대상 | 기대 |
|---|---|---|
| T-A1 | `adapter.load` | 미지의 키 → ValidationError |
| T-A2 | `adapter.load` | `id_pattern` 캡처그룹 2개 → ValidationError |
| T-A3 | `adapter.load` | `mode: context_request` → ValidationError |
| T-A4 | `adapter.load` | `frontmatter`에 허용 외 키 → ValidationError |
| T-A5 | `adapter.load` | 기본값 병합. `assets.concurrency` 미지정 시 8 |
| T-P1 | `check_access` | `cloudflare_challenge.html` → `browser_required` |
| T-P2 | `find_toc` | `wikidocs_root.html` → 최다 패턴 `^/(\d+)$`, count 209 |
| T-P3 | `find_toc` | `/book/2155`가 chosen에 포함되지 않는다 |
| T-P4 | `pick_content_selector` | `wikidocs_22660.html` → `.page-content` (`#load_content` 아님) |
| T-P5 | `probe_source_api` | catch-all 200 응답 → `verdict: none` |
| T-P6 | `census` | `math.verdict == "latex_preserved"` |
| T-P7 | `suggest_paywall_markers` | `유료 E-?book` 계열이 high로 제안된다 |
| T-C1 | `build_toc` | 208개. 빈 제목 1개 제외 |
| T-C2 | `build_toc` | `order: dom`에서 `99) ===...===`가 `12-07` 다음 위치 |
| T-C3 | `collect.run` | `raw.json`에 `error`가 있는 페이지만 재수집(resume) |
| T-C4 | `collect.run` | `Blocked` 발생 시 exit 3, 부분 `raw.json` 보존 |
| T-C5 | `convert.run` | **네트워크 호출 0회** (`assets.download: false`, socket 몽키패치로 검증) |
| T-M1 | `convert_pre` | `language-python` → ` ```python ` 펜스 |
| T-M2 | `convert_pre` | `language-pyton` → 매핑되어 ` ```python ` |
| T-M3 | `convert_pre` | `language-no-highlight` → 언어 없는 펜스 |
| T-M4 | 이스케이프 | `$w_{t}$`가 `$w\_{t}$`가 되지 않는다 |
| T-M5 | 이스케이프 | `$$cost(\hat{y}, y) = -\sum_{j=1}^{V}...$$`가 원형 보존 |
| T-M6 | 표 | `wikidocs_table.html` → 마크다운 표 행 수 일치 |
| T-M7 | 광고 제거 | 출력에 `adsbygoogle` 문자열이 없다 |
| T-S1 | `assert_markdownify_compatible` | 시그니처가 다르면 RuntimeError |
| T-N1 | 파일명 | `18-04 TFBert...를 이용한 분류` → 금지문자 0 |
| T-N2 | 파일명 | 끝의 `.` 제거 |
| T-N3 | 파일명 | 중복 시 `_2` 접미사 |
| T-N4 | 이미지명 | 서로 다른 페이지의 동명 이미지가 충돌하지 않는다 |
| T-G1 | 품질 게이트 | `wikidocs_paywall_long.html` → `paywall` 경고 (길이로는 안 잡힘) |
| T-G2 | 품질 게이트 | `wikidocs_chapter_cover.html` → `short_document` 경고 |
| T-G3 | 품질 게이트 | 홀수 개 ` ``` ` → `unbalanced_fence` |
| T-G4 | `stray_dollars` | `"가격은 $45.55입니다"` → 1 (경고 없음). 통화 오탐 방지 |
| T-G5 | `stray_dollars` | 여러 줄에 걸친 `$P(x)\n× P(y)$` → 0 (경고 없음) |
| T-G6 | `stray_dollars` | `wikidocs_math_images_22660` 마크다운에서 `$$` 구분자 하나 제거 → **2** (경고 발생) |
| T-G7 | `stray_dollars` | 같은 문서에서 인라인 `$x$`를 `$x`로 → **1** (경고 없음). §8.6.1의 흡수 한계를 고정하는 회귀 테스트. 이 값이 2로 바뀌면 통화 오탐이 되살아났다는 뜻이다 |
| T-G9 | `quality_gate` | 208개 실문서 전체에서 `unbalanced_math` 경고 0건 |
| T-G8 | `stray_dollars` | 코드 펜스 안의 `echo $HOME` → 0 |
| T-R1 | 재시도 | 503 3회 → 3회 시도 후 FetchError |
| T-R2 | 재시도 | 404 → 재시도 없음 (1회) |
| T-R3 | 재시도 | `Retry-After: 5` → 5초 대기 (시간 모킹) |

### 12.3 통합 수용 기준 (wikidocs 전체 실행)

- [ ] `probe`가 §3.6의 모든 최상위 키를 채운 `probe.json`을 낸다
- [ ] `collect`가 208페이지를 실패 0건으로 수집. `raw.json` 1.7~2.7MB
- [ ] `convert`가 마크다운 208개 + 이미지 454개, 이미지 실패 0건
- [ ] 코드블록 언어 펜스 ` ```python ` 약 2,700개
- [ ] `$$cost(\hat{y}, y) = -\sum_{j=1}^{V}y_{j}\ log(\hat{y_{j}})$$` 원형 보존
- [ ] 마크다운 표 49개
- [ ] 출력 전체에 `adsbygoogle` 0회
- [ ] `paywall` 경고 15건 (유료 본문 14 + 안내 페이지 1)
- [ ] `short_document` 경고에 장 표지들이 잡힌다 (유료와 별도 집계)
- [ ] `unbalanced_math` 경고 **0건**, `unbalanced_fence` 경고 **0건** (208개 전체 실측 확인값)
- [ ] `convert`를 두 번 실행 → 두 번째는 이미지 `cached: 454`, `downloaded: 0`
- [ ] `collect`를 두 번 실행 → 두 번째는 `skipped_cached: 208`, 네트워크 요청은 루트 페이지 1회뿐

---

## 13. 실행 환경 주의

- **원격 컨테이너에서 대상 사이트를 직접 받지 말 것.** 데이터센터 IP인 데다, 아웃바운드가 TLS 중계 프록시를 거치면 지문 위조 도구(`curl_cffi` 등)도 무력화된다 — 프록시가 자기 지문으로 다시 핸드셰이크하기 때문이다. 실측 확인됨
- 수집은 사용자 PC에서, 변환은 어디서 해도 된다
- 원격↔로컬로 산출물을 옮겨야 하면 개별 파일이 아니라 zip 하나로. 663개 파일을 하나씩 옮기는 것은 현실적이지 않다

---

## 14. CLI

```
crawl2md probe   <root_url> --site <name> [--job <job>] [--sample 20]
                                          [--save-fixtures <dir>]
crawl2md collect --site <name> [--job <job>] [--limit N] [--no-resume]
crawl2md convert --site <name> [--job <job>] [--out <dir>]
crawl2md build   --site <name> [--job <job>] [--force]
crawl2md run     --site <name> [--job <job>] [--limit N]
```

공통 옵션: `--adapter-dir sites` `--work-dir work` `-v/--verbose` `-q/--quiet`

| 옵션 | 의미 |
|---|---|
| `--job` | 생략 시 §3.1 규칙으로 생성. `collect` 이후 단계에서 생략하면 `work/{site}/` 아래 **가장 최근** job을 쓴다 |
| `--limit N` | 목차 앞에서 N개만. 새 사이트 검증용 (`--limit 3` 권장) |
| `--no-resume` | 기존 `raw.json`을 무시하고 전체 재수집 |
| `--force` | `build`의 obsidian 대상이 이미 있어도 덮어쓴다 |
| `--sample` | probe가 census에 쓸 페이지 수 |
| `--save-fixtures` | 루트 페이지와 샘플 1개의 **전체 HTML**을 지정 폴더에 저장. 테스트 픽스처 생성용 (§12.1) |

`probe`만 URL을 직접 받는다. 나머지는 어댑터를 참조한다.

---

## 15. 새 사이트 추가 절차

1. `crawl2md probe <목차URL> --site <이름>`
2. `sites/<이름>.draft.yaml`을 열어 확인. 특히 `content.selector`, `license`,
   `paywall_markers`. probe의 제안은 근거와 함께 `probe.json`에 남아 있으니 값이
   미심쩍으면 후보 표(텍스트·노드·밀도)를 직접 보고 판단한다
3. `sites/<이름>.yaml`로 이름을 바꾸고, **검토를 마쳤으므로 `verified: true`로 바꾼다**
4. `crawl2md run --site <이름> --limit 3`으로 3개 문서에서 수식·코드·표를 확인하고,
   온전하면 전체 실행
5. 4에서 YAML로 해결되지 않는 문제가 있을 때만 훅을 추가한다 (P5)

**3번과 4번의 순서를 바꾸지 말 것.** `verified`의 뜻은 "probe 결과를 사람이 확인했는가"
(§4)이지 "산출물 3개를 봤는가"가 아니다. 초판은 `--limit 3` 실행을 `verified: true`보다
앞에 두었는데, P2에 따라 collect가 `verified: false`를 exit 2로 거부하므로 그 순서로는
절차가 진행되지 않는다.

---

## 16. 알려진 함정

실제로 밟은 것들이다.

| 함정 | 증상 | 대응 |
|---|---|---|
| Cloudflare 챌린지 | 403 + "Just a moment" | 브라우저 드라이버. UA 위조는 무효 |
| 중계 프록시 환경의 TLS 위조 | `curl_cffi`가 Connection reset | 원격에서 시도하지 말 것 (§13) |
| `context_request` 사용 | 세션은 열렸는데 다시 403 | `page.evaluate` 안에서 fetch (§11.4) |
| 가짜 API 엔드포인트 | 아무 경로나 200 | 응답 길이·Content-Type을 정상 페이지와 비교 (§6.5) |
| markdownify 이스케이프 | 수식의 `_`가 `\_` | 이스케이프 3종 모두 off |
| `convert_pre` 시그니처 | 오버라이드가 조용히 무시됨 | 버전 가드 (§8.4.2) |
| 광고 DOM | 본문에 스크립트 텍스트 유입 | markdownify **전에** `decompose()` |
| 이미지 파일명 충돌 | 뒤 페이지 이미지가 앞을 덮어씀 | `{page_id}_` 접두사 + 전역 이름 테이블 |
| 제목 정렬 | 책 순서가 뒤섞임 | `toc.order: dom` 고정 |
| 유료 판정을 길이로 | 본문 8,010자인 유료 페이지를 놓침 | 마커 정규식이 1순위 (§6.7) |
| 유료 마커를 너무 넓게 | 저작권 안내문 오탐 | `E-book` 단독 금지, `유료 E-?book`으로 좁힌다 |
| Chrome 자동 다운로드 2회차 | 조용히 실패, 에러 없음 | browser_mcp는 다운로드 1회만 (§11.5) |
| 브라우저 탭 종료 | 수집 데이터 전손 | 배치마다 경고 |
| 윈도우 파일명 | 저장 실패 | 금지문자 치환 + 끝 마침표 제거 (§8.5.1) |
| 한글 인코딩 | 본문이 깨짐 | `apparent_encoding` 재해석 (§11.2) |
| 차단된 채로 계속 수집 | IP 차단이 길어짐 | `Blocked`면 즉시 전체 중단 (exit 3) |
| Cloudflare 520 | 대량 수집 중 일부가 실패, 재요청하면 성공 | 520~527을 재시도 대상에 포함 (§10.2) |
| 본문 셀렉터를 텍스트 길이로 선택 | 사이드바·목차를 품은 래퍼가 뽑힘 | 텍스트/노드 밀도로 선택 (§6.4) |
| 셀렉터 판단을 목차 첫 페이지로 | 장 표지(본문 161자)라 판단이 틀어짐 | 본문이 가장 긴 샘플을 대표로 (§6.1) |
| 라이선스 정규식이 숫자를 집음 | `저작권 : 120 명이 추천` → `"120"` | 숫자뿐인 캡처는 버린다 (§6.8) |
| 표 계수가 코드펜스 안을 셈 | 예시 표를 실제 표로 오인 | 펜스 제거 후 센다 (§8.6.2) |

---

## 부록 A. wikidocs 실측값 (2026-09-08, book/2155)

```
목차 링크          209개 → 빈 제목 1개 제외 → 208개
수집               208/208 성공, 실패 0
raw.json           1,769,138자 (UTF-8 2.6MB)
고유 이미지        454개, 다운로드 454/454 성공
태그 빈도          p 4857 / code 4280 / pre 3268 / td 997 / h2 578
                   img 473 / li 441 / script 322 / tr 314 / ins 161 / table 49
클래스 빈도        language-python 2719 / ad-wrapper markdown_tags 161
                   adsbygoogle 161 / language-html 1 / language-no-highlight 1
                   language-pyton 1  ← 오타. 매핑 필요
본문 컨테이너 경합  .page-content (텍스트 6,946 / 노드 214)  ← 채택
                   #load_content  (텍스트 7,161 / 노드 239)  ← 광고 포함
수식               서버 HTML에 $...$, $$...$$ raw 보존 (MathJax 클라이언트 렌더)
이미지 호스트      static.wikidocs.net — Cloudflare 없음, plain requests 200
라이선스           CC BY-NC-SA
유료 비공개        본문 14 + 안내 페이지 1 = 15. 마커 정규식으로만 정확히 잡힘
                   길이 200자 하한으로는 절반 이상 놓침 (18-07은 8,010자인데 유료)
                   문구 4종: "유료 E-book에서만 공개합니다" /
                             "유료 E-book을 구매하시면" /
                             "E-book 구매 시에 볼 수 있는 페이지입니다" /
                             "위키독스 웹 사이트에서는 비공개 처리되어"
동시성/지연        5 / 120ms → 실패 0
최종 산출          마크다운 208 + 이미지 454 + 인덱스 1 = 663파일, 18MB (zip 14MB)
```

## 부록 B. 의존성

```toml
[project]
requires-python = ">=3.11"
dependencies = [
  "beautifulsoup4>=4.13",
  "lxml>=5.0",
  "markdownify>=1.2,<2",     # convert_pre 시그니처가 메이저 버전에 종속. §8.4.2
  "requests>=2.32",
  "PyYAML>=6.0",
  "soupsieve>=2.5",          # CSS 셀렉터 사전 검증용
]
[project.optional-dependencies]
browser = ["playwright>=1.47"]
dev = ["pytest>=8.0", "pytest-cov"]
```

`playwright`는 선택 의존성으로 둔다. 설치되지 않은 상태에서 `fetch.driver: playwright`가 지정되면, 설치 명령(`pip install 'crawl2md[browser]' && playwright install chrome`)을 안내하는 `ValidationError`를 낸다.
