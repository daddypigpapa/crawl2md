import time

import requests

from core.errors import Blocked, FetchError
from core.fetch.base import default_get_many, is_blocked
from core.models import Response
from core.retry import call_with_retry


class RequestsFetcher:
    name = "requests"
    supports_batch = False

    def __init__(self, adapter):
        self.adapter = adapter
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": adapter["fetch.user_agent"],
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        self.timeout_s = adapter["fetch.timeout_s"]
        self.block_markers = adapter["fetch.block_markers"]

    def open(self, origin: str) -> None:
        pass

    def get(self, url: str) -> Response:
        def attempt():
            t0 = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.timeout_s)
            except requests.RequestException as exc:
                raise FetchError(str(exc))
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            encoding = resp.encoding
            if encoding is None or encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return resp.status_code, dict(resp.headers), (resp.text, elapsed_ms)

        status, _headers, result = call_with_retry(attempt)
        if result is None:
            raise FetchError(f"{url}: 재시도 후에도 실패")
        text, elapsed_ms = result
        response = Response(url=url, status=status, text=text, elapsed_ms=elapsed_ms)
        marker = is_blocked(response, self.block_markers)
        if marker:
            raise Blocked(marker)
        return response

    def get_many(self, urls, *, concurrency, delay_ms):
        yield from default_get_many(self, urls, concurrency=concurrency, delay_ms=delay_ms)

    def close(self) -> None:
        self.session.close()
