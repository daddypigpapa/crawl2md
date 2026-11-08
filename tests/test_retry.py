from unittest.mock import patch

from core.retry import call_with_retry, MAX_ATTEMPTS


def test_r1_503_three_times_then_give_up():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return 503, {}, "body"

    with patch("time.sleep"):
        status, headers, result = call_with_retry(fn)

    assert calls["n"] == MAX_ATTEMPTS
    assert status == 503


def test_r2_404_no_retry():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return 404, {}, "not found"

    with patch("time.sleep"):
        status, headers, result = call_with_retry(fn)

    assert calls["n"] == 1
    assert status == 404


def test_r3_retry_after_header():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"Retry-After": "5"}, "body"
        return 200, {}, "ok"

    sleeps = []
    with patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
        call_with_retry(fn)

    assert sleeps[0] == 5.0
