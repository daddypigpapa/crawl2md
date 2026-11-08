import shutil
import socket

import pytest

from core import convert
from core.adapter import load as load_adapter
from tests.conftest import FIXTURES, ROOT


@pytest.fixture
def prepared_job(tmp_path):
    adapter = load_adapter(ROOT / "sites" / "wikidocs.yaml")
    adapter.raw["assets"]["download"] = False  # 네트워크 금지 (T-C5)

    job = "fixture-mini"
    job_dir = tmp_path / "work" / "wikidocs" / job
    job_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "raw_mini.json", job_dir / "raw.json")

    return adapter, job, tmp_path / "work"


def test_c5_no_network_calls(prepared_job, monkeypatch):
    adapter, job, work_dir = prepared_job
    out_dir = work_dir.parent / "out" / job

    def fail_connect(*a, **kw):
        raise AssertionError("네트워크 호출이 발생했습니다 (P1 위반)")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)

    result = convert.run(adapter, job, work_dir=work_dir, out_dir=out_dir)
    assert len(result.documents) == 6


def test_convert_produces_expected_warnings(prepared_job):
    adapter, job, work_dir = prepared_job
    out_dir = work_dir.parent / "out" / job

    result = convert.run(adapter, job, work_dir=work_dir, out_dir=out_dir)

    kinds = {w["kind"] for w in result.warnings}
    assert "paywall" in kinds
    assert "short_document" in kinds
    assert "unbalanced_math" not in kinds
    assert "unbalanced_fence" not in kinds
    # missing_asset 은 이 테스트가 assets.download=false(T-C5용)로 돌기 때문에
    # 예상되는 부작용이다 — 실제 다운로드 시나리오에서는 별도로 검증한다.

    md_files = list(out_dir.glob("*.md"))
    assert len(md_files) == 6

    combined = "\n".join(f.read_text(encoding="utf-8") for f in md_files)
    # 원본 raw fixture 는 y 에 유니코드 이탤릭 문자(U+1D466)를 쓴다(ASCII y 아님).
    # P3(원본 보존) 검증이므로 원본 그대로의 문자로 대조한다.
    assert "cost(\\hat{\U0001d466}, y) = -\\sum_{j=1}^{V}y_{j}\\ log(\\hat{\U0001d466_{j}})" in combined
    assert "```python" in combined
