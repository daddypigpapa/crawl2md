from core.adapter import Adapter
from core.fetch.base import Fetcher
from core.errors import ValidationError


def get_fetcher(name: str, adapter: Adapter) -> Fetcher:
    if name == "requests":
        from core.fetch.requests_driver import RequestsFetcher

        return RequestsFetcher(adapter)
    if name == "playwright":
        from core.fetch.playwright_driver import PlaywrightFetcher

        return PlaywrightFetcher(adapter)
    if name == "browser_mcp":
        from core.fetch.browser_mcp_driver import BrowserMcpFetcher

        return BrowserMcpFetcher(adapter)
    raise ValidationError(f"알 수 없는 fetch driver: {name!r}")
