from core.errors import Blocked, FetchError, ValidationError
from core.fetch.base import is_blocked
from core.models import Response


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class PlaywrightFetcher:
    name = "playwright"
    supports_batch = True

    def __init__(self, adapter):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            raise ValidationError(
                "fetch.driver: playwright 가 설치되지 않았습니다. "
                "pip install 'crawl2md[browser]' && playwright install chrome"
            )
        self.adapter = adapter
        self.timeout_s = adapter["fetch.timeout_s"]
        self.batch_size = adapter["fetch.batch_size"]
        self.block_markers = adapter["fetch.block_markers"]
        self._pw = None
        self._ctx = None
        self._browser = None
        self._page = None

    def open(self, origin: str) -> None:
        from playwright.sync_api import sync_playwright

        cfg = self.adapter.raw["fetch"]["playwright"]
        self._pw = sync_playwright().start()
        kw = {"headless": cfg["headless"]}
        if cfg["channel"]:
            kw["channel"] = cfg["channel"]
        if cfg["persist_profile"]:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=cfg["profile_dir"], **kw
            )
        else:
            self._browser = self._pw.chromium.launch(**kw)
            self._ctx = self._browser.new_context()
        self._page = self._ctx.new_page()
        self._page.goto(origin, wait_until="domcontentloaded", timeout=self.timeout_s * 1000)

    def get(self, url: str) -> Response:
        for _url, result in self.get_many([url], concurrency=1, delay_ms=0):
            if isinstance(result, FetchError):
                raise result
            return result
        raise FetchError(f"{url}: 응답 없음")

    def get_many(self, urls, *, concurrency, delay_ms):
        for chunk in _chunks(urls, self.batch_size):
            res = self._page.evaluate(
                """
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
                }
                """,
                [chunk, concurrency, delay_ms],
            )
            for item in res:
                if "error" in item:
                    yield (item["url"], FetchError(item["error"]))
                    continue
                response = Response(item["url"], item["status"], item["text"], item["elapsed_ms"])
                marker = is_blocked(response, self.block_markers)
                if marker:
                    yield (item["url"], Blocked(marker))
                else:
                    yield (item["url"], response)

    def close(self) -> None:
        if self._ctx:
            self._ctx.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
