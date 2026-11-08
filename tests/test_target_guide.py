"""添加来源后的目标引导：登记/扫描分离、统一目标验证、目标变更重算。"""

import os
from pathlib import Path

import pytest

from app.domain.models import PlanStatus, SourceRoot, Unit, UnitKind
from app.paths import DataPaths
from app.services import AppService
from tests.fixtures import build_full_rip_album, make_wav


def make_service(tmp_path: Path) -> AppService:
    return AppService(DataPaths(root=tmp_path / "data", portable=True))


def track(path: str, title="B", artist="A") -> Unit:
    return Unit(kind=UnitKind.TRACK.value, path=path, name="01",
                title=title, artist=artist, album="专辑")


# ---------------------------------------------------------------------------
# 登记 / 扫描分离
# ---------------------------------------------------------------------------
def test_register_sources_defers_scan(tmp_path):
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.wav").write_bytes(b"x")

    result = service.register_sources([str(src)])
    assert result["added"] == 1 and not service.state.units
    assert service.has_pending_scan()
    assert not service.state.sources[0].scanned

    service.rescan()
    assert not service.has_pending_scan()
    assert service.state.sources[0].scanned is True


def test_register_dedupes_and_rejects_files(tmp_path):
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    f = tmp_path / "file.txt"
    f.write_text("x")

    first = service.register_sources([str(src)])
    second = service.register_sources([str(src)])
    files = service.register_sources([str(f)])
    assert first["added"] == 1
    assert second["added"] == 0 and any("重复" in s for s in second["skipped"])
    assert files["added"] == 0 and any("不是文件夹" in s for s in files["skipped"])
    assert len(service.state.sources) == 1


def test_add_sources_compat_registers_and_scans(tmp_path):
    """兼容入口保持旧行为：登记 + 立即扫描（端到端脚本依赖）。"""
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.wav").write_bytes(b"x")
    result = service.add_sources([str(src)])
    assert result["added"] == 1
    assert len(service.state.units) >= 1
    assert not service.has_pending_scan()


# ---------------------------------------------------------------------------
# 统一目标验证
# ---------------------------------------------------------------------------
def test_validate_target_basic_cases(tmp_path):
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    service.register_sources([str(src)])

    empty = service.validate_target("")
    assert not empty["ok"]

    file_target = tmp_path / "f.txt"
    file_target.write_text("x")
    assert not service.validate_target(str(file_target))["ok"]

    missing = service.validate_target(str(tmp_path / "新建目标"))
    assert missing["ok"] and not missing["exists"]  # 允许，确认后创建

    assert not service.validate_target(str(src))["ok"], "目标不能与来源相同"

    parent = service.validate_target(str(tmp_path))
    assert not parent["ok"], "来源的祖先目录不能作为目标"

    child = service.validate_target(str(src / "子目录"))
    assert not child["ok"], "来源内部不能作为目标"


def test_validate_target_case_trailing_dotdot(tmp_path):
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    service.register_sources([str(src)])

    variants = [
        str(src) + "\\",                       # 末尾分隔符
        str(src).upper(),                      # 大小写
        str(tmp_path / "src" ),                # 等价路径
        str(tmp_path / "other" / ".." / "src"),  # .. 归一后相同
    ]
    for variant in variants:
        result = service.validate_target(variant)
        assert not result["ok"], f"应被拒绝：{variant}（{result.get('reason')}）"


def test_validate_target_junction_same_location(tmp_path):
    """目录联接指向来源时不得绕过验证（Windows junction）。"""
    service = make_service(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    service.register_sources([str(src)])

    link = tmp_path / "link_to_src"
    try:
        import _winapi
        _winapi.CreateJunction(str(src), str(link))
    except (ImportError, OSError):
        pytest.skip("无法创建目录联接")
    result = service.validate_target(str(link))
    assert not result["ok"], "联接指向同一位置应被拒绝"


def test_validate_target_writable_probe_leaves_no_file(tmp_path):
    service = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    result = service.validate_target(str(target))
    assert result["ok"]
    assert not (target / ".target_write_test").exists(), "探测不得留下测试文件"


# ---------------------------------------------------------------------------
# 目标保存与变更重算
# ---------------------------------------------------------------------------
def test_set_target_saves_and_invalidates_plan(tmp_path):
    service = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    assert service.set_target(str(target))["ok"]
    assert service.settings.target_dir == str(target)
    assert service.last_plan is None

    # 相同路径再次设置：不触发重算
    result = service.set_target(str(target))
    assert result["changed"] is False


def test_invalid_input_keeps_last_valid_target(tmp_path):
    service = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    service.set_target(str(target))
    src = tmp_path / "src"
    src.mkdir()
    service.register_sources([str(src)])

    bad = service.set_target(str(src))          # 与来源相同
    assert not bad["ok"]
    assert service.settings.target_dir == str(target), "无效输入不得覆盖最后有效配置"


def test_apply_target_change_invalidates_plan(tmp_path):
    """换目标后旧计划必须失效（计划永远重建）；用户编辑不受影响。

    识别机制重构后已无依赖目标目录的艺术家推断建议，无需重估。
    """
    service = make_service(tmp_path)
    unit = Unit(kind=UnitKind.ALBUM.value, path="X:/a", name="a",
                boundary_path="X:/a", album="专辑", audio_files=["X:/a/01.wav"],
                boundary_confirmed=True)
    service.state.units = [unit]
    new_target = tmp_path / "新目标"
    new_target.mkdir()
    service.settings.target_dir = str(new_target)
    service.last_plan = object()  # 旧计划占位

    service.apply_target_change()
    assert service.last_plan is None, "旧计划必须失效"


def test_target_change_keeps_user_edits_and_checks(tmp_path):
    service = make_service(tmp_path)
    unit = track(str(tmp_path / "a.wav"))
    unit.checked = True
    unit.user_checked = True
    unit.origins["title"] = "user"
    service.state.units = [unit]
    target = tmp_path / "target"
    target.mkdir()
    service.set_target(str(target))
    assert unit.checked and unit.user_checked is True
    assert unit.origins["title"] == "user"


# ---------------------------------------------------------------------------
# 规则影响反事实
# ---------------------------------------------------------------------------
