from core.models import Response

# 명세 §11.5: browser_mcp 드라이버는 사람이 브라우저에 붙어 진행하는 폴백이며,
# 명세에 playwright_driver(§11.3)처럼 구체적 구현 코드가 주어지지 않았다.
# 제약(다운로드 1회, window.__crawl2md 누적, 배치 40~45)만 문서화되어 있고
# 실제 MCP 호출 방식은 정의되어 있지 않다 — 이는 명세의 결함이므로 임의로 채우지
# 않고 안내만 남긴다. 필요해지면 명세에 구체 절차를 먼저 추가할 것.


class BrowserMcpFetcher:
    name = "browser_mcp"
    supports_batch = True

    def __init__(self, adapter):
        self.adapter = adapter

    def open(self, origin: str) -> None:
        raise NotImplementedError(
            "browser_mcp 드라이버는 명세 §11.5에 구체 구현이 정의되어 있지 않습니다. "
            "Chrome 자동 다운로드 설정을 켠 뒤 사람이 개입하는 절차가 필요합니다."
        )

    def get(self, url: str) -> Response:
        raise NotImplementedError("browser_mcp: §11.5 참고, 미구현")

    def get_many(self, urls, *, concurrency, delay_ms):
        raise NotImplementedError("browser_mcp: §11.5 참고, 미구현")

    def close(self) -> None:
        pass
