import hashlib
import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import soupsieve
import yaml

from core.errors import ValidationError

# --- 스키마 정의 (§4) ---------------------------------------------------
# 각 leaf: (required: bool, default, kind)
# kind: "str" "bool" "int" "regex" "css" "list[regex]" "list[css]" "list[str]"
#       "dict[str,str]" "enum:<a>|<b>..." "any"

SCHEMA: dict[str, dict] = {
    "site": {"required": True, "default": None, "kind": "str"},
    "name": {"required": True, "default": None, "kind": "str"},
    "verified": {"required": True, "default": None, "kind": "bool"},
    "license": {"required": True, "default": None, "kind": "str"},
    "notes": {"required": False, "default": "", "kind": "str"},

    "entry.root_url": {"required": True, "default": None, "kind": "str"},
    "entry.page_url": {"required": True, "default": None, "kind": "str"},
    "entry.book_title": {"required": False, "default": None, "kind": "str_or_null"},
    "entry.book_title_selector": {"required": False, "default": None, "kind": "css_or_null"},
    "entry.title_suffix_strip": {"required": False, "default": None, "kind": "regex_or_null"},

    "toc.strategy": {"required": True, "default": None, "kind": "enum:link_pattern|sitemap|hook"},
    "toc.selector": {"required": True, "default": None, "kind": "css"},
    "toc.id_pattern": {"required": True, "default": None, "kind": "regex1group"},
    "toc.drop_empty_title": {"required": False, "default": True, "kind": "bool"},
    "toc.dedupe_by": {"required": False, "default": "href", "kind": "enum:href|id"},
    "toc.order": {"required": False, "default": "dom", "kind": "enum:dom|title"},
    "toc.exclude_patterns": {"required": False, "default": [], "kind": "list[regex]"},

    "content.selector": {"required": True, "default": None, "kind": "css"},
    "content.remove": {"required": False, "default": [], "kind": "list[css]"},
    "content.paywall_threshold": {"required": False, "default": 0, "kind": "int"},
    "content.paywall_markers": {"required": False, "default": [], "kind": "list[regex]"},

    "assets.download": {"required": False, "default": True, "kind": "bool"},
    "assets.dir": {"required": False, "default": "assets", "kind": "str"},
    "assets.name_template": {"required": False, "default": "{page_id}_{basename}", "kind": "str"},
    "assets.skip_data_uri": {"required": False, "default": True, "kind": "bool"},
    "assets.strip_attrs": {
        "required": False,
        "default": ["loading", "decoding", "width", "height", "style", "class"],
        "kind": "list[str]",
    },
    "assets.concurrency": {"required": False, "default": 8, "kind": "int"},
    "assets.driver": {"required": False, "default": "requests", "kind": "enum:requests|playwright|browser_mcp"},
    "assets.timeout_s": {"required": False, "default": 30, "kind": "int"},
    "assets.max_bytes": {"required": False, "default": 20971520, "kind": "int"},

    "markdown.heading_style": {"required": False, "default": "ATX", "kind": "str"},
    "markdown.bullets": {"required": False, "default": "-", "kind": "str"},
    "markdown.escape_asterisks": {"required": False, "default": False, "kind": "bool"},
    "markdown.escape_underscores": {"required": False, "default": False, "kind": "bool"},
    "markdown.escape_misc": {"required": False, "default": False, "kind": "bool"},
    "markdown.code_lang_map": {"required": False, "default": {}, "kind": "dict[str,str]"},
    "markdown.code_lang_default": {"required": False, "default": "", "kind": "str"},
    "markdown.math": {"required": False, "default": "preserve", "kind": "enum:preserve|strip"},
    "markdown.collapse_blank_lines": {"required": False, "default": 2, "kind": "int"},

    "output.filename_template": {"required": True, "default": None, "kind": "str"},
    "output.filename_max": {"required": False, "default": 90, "kind": "int"},
    "output.filename_forbidden": {"required": False, "default": r'[\\/:*?"<>|]', "kind": "regex"},
    "output.filename_replacement": {"required": False, "default": "-", "kind": "str"},
    "output.frontmatter": {"required": False, "default": ["title", "order", "source"], "kind": "list[str]"},
    "output.h1_from_title": {"required": False, "default": True, "kind": "bool"},
    "output.footer_template": {"required": False, "default": None, "kind": "str_or_null"},
    "output.index_file": {"required": False, "default": "000_INDEX.md", "kind": "str_or_null"},
    "output.index_wikilink": {"required": False, "default": True, "kind": "bool"},
    "output.target": {"required": False, "default": "folder", "kind": "enum:folder|zip|obsidian"},
    "output.path": {"required": True, "default": None, "kind": "str"},

    "fetch.driver": {"required": False, "default": "auto", "kind": "enum:auto|requests|playwright|browser_mcp"},
    "fetch.concurrency": {"required": False, "default": 5, "kind": "int"},
    "fetch.delay_ms": {"required": False, "default": 120, "kind": "int"},
    "fetch.timeout_s": {"required": False, "default": 30, "kind": "int"},
    "fetch.batch_size": {"required": False, "default": 40, "kind": "int"},
    "fetch.block_markers": {
        "required": False,
        "default": [
            "Just a moment",
            "cf-chl",
            "Checking your browser",
            "Enable JavaScript and cookies",
            "Attention Required! | Cloudflare",
        ],
        "kind": "list[str]",
    },
    "fetch.user_agent": {
        "required": False,
        "default": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "kind": "str",
    },
    "fetch.playwright.channel": {"required": False, "default": "chrome", "kind": "str_or_null"},
    "fetch.playwright.headless": {"required": False, "default": False, "kind": "bool"},
    "fetch.playwright.persist_profile": {"required": False, "default": True, "kind": "bool"},
    "fetch.playwright.profile_dir": {"required": False, "default": "work/.pwprofile/{site}", "kind": "str"},
    "fetch.playwright.mode": {"required": False, "default": "page_evaluate", "kind": "enum:page_evaluate|context_request"},

    "hooks.module": {"required": False, "default": None, "kind": "str_or_null"},
    "hooks.clean_html": {"required": False, "default": None, "kind": "str_or_null"},
    "hooks.page_title": {"required": False, "default": None, "kind": "str_or_null"},
    "hooks.filename": {"required": False, "default": None, "kind": "str_or_null"},
    "hooks.post_markdown": {"required": False, "default": None, "kind": "str_or_null"},
}

ALLOWED_FRONTMATTER_KEYS = {"title", "order", "source", "book", "license", "id"}

FILENAME_TEMPLATE_VARS = {"order", "title", "id", "slug"}


def default_config() -> dict:
    """Nested dict of defaults (required leaves get their default, usually None)."""
    out: dict = {}
    for dotted, spec in SCHEMA.items():
        _set_dotted(out, dotted, spec["default"])
    return out


def _set_dotted(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _get_dotted(d: dict, dotted: str, default=None):
    parts = dotted.split(".")
    cur = d
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _flatten_keys(d: dict, prefix: str = "", schema_leaves: set[str] | None = None) -> list[str]:
    """모든 leaf dotted path를 반환한다. SCHEMA에 이미 leaf로 정의된 경로(예:
    markdown.code_lang_map 같은 dict[str,str] 값)는 그 안으로 더 내려가지 않는다."""
    if schema_leaves is None:
        schema_leaves = set(SCHEMA.keys())
    keys = []
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and dotted not in schema_leaves:
            keys.extend(_flatten_keys(v, dotted, schema_leaves))
        else:
            keys.append(dotted)
    return keys


@dataclass
class Ctx:
    adapter: "Adapter"
    page_id: str
    order: int
    title: str
    log: Any


@dataclass(frozen=True)
class Hooks:
    clean_html: Callable | None = None
    page_title: Callable | None = None
    filename: Callable | None = None
    post_markdown: Callable | None = None


@dataclass(frozen=True)
class Adapter:
    raw: dict
    path: Path
    sha256: str
    hooks: Hooks

    def __getitem__(self, dotted: str):
        return _get_dotted(self.raw, dotted)

    def get(self, dotted: str, default=None):
        v = _get_dotted(self.raw, dotted)
        return default if v is None else v


def _validate_regex(value: str, field: str) -> None:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValidationError(f"{field}: 잘못된 정규식 {value!r} ({exc})")


def _validate_css(value: str, field: str) -> None:
    try:
        soupsieve.compile(value)
    except Exception as exc:  # noqa: BLE001 - soupsieve raises various error types
        raise ValidationError(f"{field}: 잘못된 CSS 셀렉터 {value!r} ({exc})")


def _validate_value(dotted: str, spec: dict, value) -> None:
    kind = spec["kind"]
    if value is None:
        if kind.endswith("_or_null") or not spec["required"]:
            return
        raise ValidationError(f"{dotted}: 값이 없습니다")

    if kind == "str" or kind == "str_or_null":
        if not isinstance(value, str):
            raise ValidationError(f"{dotted}: 문자열이어야 합니다")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ValidationError(f"{dotted}: bool 이어야 합니다")
    elif kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"{dotted}: 정수여야 합니다")
    elif kind == "regex" or kind == "regex_or_null":
        _validate_regex(value, dotted)
    elif kind == "regex1group":
        _validate_regex(value, dotted)
        ngroups = re.compile(value).groups
        if ngroups != 1:
            raise ValidationError(f"{dotted}: 캡처그룹이 정확히 1개여야 합니다 (현재 {ngroups}개)")
    elif kind == "css" or kind == "css_or_null":
        _validate_css(value, dotted)
    elif kind == "list[regex]":
        if not isinstance(value, list):
            raise ValidationError(f"{dotted}: 리스트여야 합니다")
        for item in value:
            _validate_regex(item, dotted)
    elif kind == "list[css]":
        if not isinstance(value, list):
            raise ValidationError(f"{dotted}: 리스트여야 합니다")
        for item in value:
            _validate_css(item, dotted)
    elif kind == "list[str]":
        if not isinstance(value, list):
            raise ValidationError(f"{dotted}: 리스트여야 합니다")
    elif kind == "dict[str,str]":
        if not isinstance(value, dict):
            raise ValidationError(f"{dotted}: dict 여야 합니다")
    elif kind.startswith("enum:"):
        allowed = kind[len("enum:"):].split("|")
        if value not in allowed:
            raise ValidationError(f"{dotted}: {allowed} 중 하나여야 합니다 (현재 {value!r})")
    else:
        raise ValidationError(f"{dotted}: 알 수 없는 검증 종류 {kind}")


def _validate_unknown_keys(raw: dict) -> None:
    known = set(SCHEMA.keys())
    known_prefixes = set()
    for k in known:
        parts = k.split(".")
        for i in range(1, len(parts)):
            known_prefixes.add(".".join(parts[:i]))

    for dotted in _flatten_keys(raw):
        if dotted in known:
            continue
        if dotted in known_prefixes:
            continue
        raise ValidationError(f"알 수 없는 키: {dotted}")


def _validate_filename_template(template: str) -> None:
    for match in re.finditer(r"\{(\w+)(:[^}]*)?\}", template):
        name = match.group(1)
        if name not in FILENAME_TEMPLATE_VARS:
            raise ValidationError(f"output.filename_template: 알 수 없는 변수 {{{name}}}")


def load(path: Path) -> "Adapter":
    path = Path(path)
    raw_bytes = path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    user_yaml = yaml.safe_load(raw_bytes.decode("utf-8")) or {}

    if not isinstance(user_yaml, dict):
        raise ValidationError("어댑터 YAML 최상위는 dict 여야 합니다")

    _validate_unknown_keys(user_yaml)

    for dotted, spec in SCHEMA.items():
        value = _get_dotted(user_yaml, dotted, spec["default"])
        if spec["required"] and value is None:
            raise ValidationError(f"{dotted}: 필수 키가 없습니다")
        _validate_value(dotted, spec, value)

    site = _get_dotted(user_yaml, "site")
    if site != path.stem:
        raise ValidationError(f"site({site!r}) != 파일명({path.stem!r})")

    frontmatter = _get_dotted(user_yaml, "output.frontmatter", SCHEMA["output.frontmatter"]["default"])
    for key in frontmatter:
        if key not in ALLOWED_FRONTMATTER_KEYS:
            raise ValidationError(f"output.frontmatter: 허용되지 않은 키 {key!r}")

    _validate_filename_template(_get_dotted(user_yaml, "output.filename_template"))

    if _get_dotted(user_yaml, "fetch.playwright.mode", "page_evaluate") == "context_request":
        raise ValidationError("fetch.playwright.mode: context_request 는 금지됩니다 (§11.4)")

    concurrency = _get_dotted(user_yaml, "fetch.concurrency", SCHEMA["fetch.concurrency"]["default"])
    if not (1 <= concurrency <= 16):
        raise ValidationError(f"fetch.concurrency: 1~16 이어야 합니다 (현재 {concurrency})")

    merged = default_config()
    _deep_merge(merged, user_yaml)

    profile_dir = _get_dotted(merged, "fetch.playwright.profile_dir")
    if profile_dir and "{site}" in profile_dir:
        _set_dotted(merged, "fetch.playwright.profile_dir", profile_dir.format(site=site))

    hooks = _load_hooks(merged)

    return Adapter(raw=merged, path=path, sha256=sha256, hooks=hooks)


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _load_hooks(merged: dict) -> Hooks:
    module_name = _get_dotted(merged, "hooks.module")
    if not module_name:
        return Hooks()
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValidationError(f"hooks.module: 임포트 실패 {module_name!r} ({exc})")

    fns = {}
    for hook_name in ("clean_html", "page_title", "filename", "post_markdown"):
        fn_name = _get_dotted(merged, f"hooks.{hook_name}")
        if not fn_name:
            continue
        fn = getattr(module, fn_name, None)
        if fn is None:
            raise ValidationError(f"hooks.{hook_name}: {module_name}에 함수 {fn_name!r}가 없습니다")
        fns[hook_name] = fn

    return Hooks(**fns)
