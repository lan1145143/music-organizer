"""P0-4 回归：重扫合并后用户显式勾选/取消必须保留（merge_units + 默认勾选）。

覆盖：用户取消 → 重扫不勾回；用户勾选 → 重扫不丢；
来源文件变化/失联/重新出现 → user_checked 全程保留。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.models import UnitStatus
from app.paths import DataPaths
from app.services import AppService


def make_service(tmp_path: Path) -> AppService:
    return AppService(DataPaths(root=tmp_path / "data", portable=True))


def make_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "01 - A - one.wav").write_bytes(b"\x00" * 64)
    (src / "02 - A - two.wav").write_bytes(b"\x00" * 64)
    return src


def scan(service: AppService, src: Path) -> None:
    service.register_sources([str(src)])
    service.rescan()


def test_rescan_keeps_user_uncheck(tmp_path):
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    scan(service, src)
    first = next(u for u in service.state.units if u.is_track)
    service.set_checked([first], False, explicit=True)
    assert first.checked is False

    scan(service, src)

    again = service.state.unit_by_id(first.id)
    assert again is not None
    assert again.user_checked is False
    assert again.checked is False


def test_rescan_keeps_user_check(tmp_path):
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    scan(service, src)
    first = next(u for u in service.state.units if u.is_track)
    service.set_checked([first], True, explicit=True)

    scan(service, src)

    again = service.state.unit_by_id(first.id)
    assert again.user_checked is True
    assert again.checked is True


def test_rescan_source_change_keeps_user_uncheck(tmp_path):
    """来源文件变化（大小/mtime 变化）后仍不勾回。"""
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    scan(service, src)
    first = next(u for u in service.state.units if u.is_track)
    service.set_checked([first], False, explicit=True)

    (Path(first.path)).write_bytes(b"\x00" * 128)  # 大小变化 → sig 变化

    scan(service, src)
    again = service.state.unit_by_id(first.id)
    assert again.user_checked is False
    assert again.checked is False


def test_missing_then_reappear_keeps_user_uncheck(tmp_path):
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    scan(service, src)
    first = next(u for u in service.state.units if u.is_track)
    service.set_checked([first], False, explicit=True)

    Path(first.path).unlink()
    scan(service, src)
    missing = service.state.unit_by_id(first.id)
    assert missing is not None and missing.status == UnitStatus.MISSING.value
    assert missing.user_checked is False

    (Path(first.path)).write_bytes(b"\x00" * 64)  # 重新出现
    scan(service, src)
    again = service.state.unit_by_id(first.id)
    assert again is not None
    assert again.status == UnitStatus.READY.value
    assert again.user_checked is False
    assert again.checked is False


def test_new_units_still_default_checked(tmp_path):
    """新出现的项目仍按全自动规则默认勾选（未被用户操作过）。"""
    service = make_service(tmp_path)
    src = make_source(tmp_path)
    scan(service, src)
    before = {u.id for u in service.state.units if u.is_track}

    (src / "03 - A - three.wav").write_bytes(b"\x00" * 64)
    scan(service, src)

    fresh = [u for u in service.state.units if u.is_track and u.id not in before]
    assert len(fresh) == 1
    assert fresh[0].user_checked is None
    assert fresh[0].checked is True
