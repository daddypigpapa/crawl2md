from dataclasses import dataclass


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    text: str
    elapsed_ms: int


@dataclass
class TocEntry:
    i: int
    id: str
    url: str
    title: str


@dataclass
class PageRecord:
    i: int
    id: str
    url: str
    title: str
    status: int
    html: str
    fetched_at: str
    error: str | None
