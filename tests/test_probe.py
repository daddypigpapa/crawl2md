from core.probe import _blocked_marker
from tests.conftest import read_fixture


def test_p1_cloudflare_challenge_detected():
    html = read_fixture("cloudflare_challenge.html")
    marker = _blocked_marker(403, html)
    assert marker is not None
