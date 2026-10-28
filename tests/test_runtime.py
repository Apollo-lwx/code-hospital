"""Lock 和 Driver 可以换掉。默认路径仍是进程内锁和同步 12 步。"""

import subprocess
from pathlib import Path

import pytest

from code_hospital.core.errors import HospitalError, conflict
from code_hospital.departments import reception, treat


def _repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return str(tmp_path)


@pytest.mark.asyncio
async def test_register_claims_through_the_lock(tmp_path, monkeypatch):
    class Other:
        async def claim(self, db, project_root, *, force):
            raise conflict("换成了另一把锁", target="case_id")

    monkeypatch.setattr(reception, "case_lock", Other())
    with pytest.raises(HospitalError) as exc:
        await reception.register(
            title="换锁", scope_type="staged", scope={}, project_root=_repo(tmp_path),
        )
    assert exc.value.code == "Conflict"
    assert exc.value.message == "换成了另一把锁"


@pytest.mark.asyncio
async def test_treat_runs_through_the_driver(monkeypatch):
    seen = {}

    class Other:
        async def run(self, **kwargs):
            seen.update(kwargs)
            return {"status": "driver-replaced"}

    monkeypatch.setattr(treat, "driver", Other())
    out = await treat.treat(title="换编排", scope_type="staged", timeout_ms=1000)
    assert out["status"] == "driver-replaced"
    assert seen["title"] == "换编排"
    assert seen["timeout_ms"] == 1000


@pytest.mark.asyncio
async def test_negative_timeout_stops_before_the_driver(monkeypatch):
    class Other:
        async def run(self, **kwargs):
            raise AssertionError("负超时不应进入 Driver")

    monkeypatch.setattr(treat, "driver", Other())
    with pytest.raises(HospitalError) as exc:
        await treat.treat(title="停", scope_type="staged", timeout_ms=-1)
    assert exc.value.code == "BadArgument"
    assert exc.value.target == "timeout_ms"
