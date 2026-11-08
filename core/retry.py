import random
import time

MAX_ATTEMPTS = 3
BASE_DELAY_S = 1.0
FACTOR = 2.0
JITTER = 0.2

# 520~527 은 Cloudflare 가 오리진 문제를 알릴 때 쓰는 5xx 대역이다.
# 2026-09-08 wikidocs book/19414 수집에서 520 이 30건 발생했고 같은 URL 단독
# 재요청은 전부 200 이었다 — 일시적이므로 재시도한다.
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}


def compute_delay(attempt: int) -> float:
    base = BASE_DELAY_S * (FACTOR ** (attempt - 1))
    return base * random.uniform(1 - JITTER, 1 + JITTER)


def should_retry(*, status: int | None, exc: Exception | None) -> bool:
    if exc is not None:
        return True
    if status in RETRYABLE_STATUS:
        return True
    return False


def retry_after_delay(headers: dict) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None


def call_with_retry(fn, *, on_attempt=None):
    """fn() -> (status, headers, result) or raises. Retries per policy.

    fn must raise on connection error/timeout, or return (status, headers, result)
    for HTTP-level responses so status-based retry can apply.
    """
    last_exc = None
    last_status = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status, headers, result = fn()
        except Exception as exc:  # connection/timeout
            last_exc = exc
            if attempt >= MAX_ATTEMPTS or not should_retry(status=None, exc=exc):
                raise
            delay = compute_delay(attempt)
            if on_attempt:
                on_attempt(attempt, delay, exc)
            time.sleep(delay)
            continue

        last_status = status
        if not should_retry(status=status, exc=None):
            return status, headers, result
        if attempt >= MAX_ATTEMPTS:
            return status, headers, result
        delay = retry_after_delay(headers) or compute_delay(attempt)
        if on_attempt:
            on_attempt(attempt, delay, None)
        time.sleep(delay)
    if last_exc:
        raise last_exc
    return last_status, {}, None
