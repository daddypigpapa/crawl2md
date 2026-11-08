import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Protocol, runtime_checkable

from core.errors import Blocked, FetchError  # 재노출. §10.1 계층과 통일
from core.models import Response

__all__ = ["FetchError", "Blocked", "Fetcher", "default_get_many", "is_blocked"]


@runtime_checkable
class Fetcher(Protocol):
    name: str
    supports_batch: bool

    def open(self, origin: str) -> None: ...
    def get(self, url: str) -> Response: ...
    def get_many(
        self, urls: list[str], *, concurrency: int, delay_ms: int
    ) -> Iterator[tuple[str, "Response | FetchError"]]: ...
    def close(self) -> None: ...


def default_get_many(
    fetcher, urls: list[str], *, concurrency: int, delay_ms: int
) -> Iterator[tuple[str, "Response | FetchError"]]:
    def worker(url: str):
        try:
            resp = fetcher.get(url)
        except FetchError as exc:
            result = (url, exc)
        else:
            result = (url, resp)
        time.sleep(delay_ms / 1000)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        yield from pool.map(worker, urls)


def is_blocked(resp: Response, markers: list[str]) -> str | None:
    if resp.status in (403, 429, 503):
        return f"status:{resp.status}"
    for marker in markers:
        if marker in resp.text:
            return marker
    return None
