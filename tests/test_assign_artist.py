"""批量指定整理艺术家：目录归属、映射持久化、计划可达性、散曲不受影响。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.models import PlanStatus, SourceRoot, Unit, UnitKind, UnitStatus
from app.paths import DataPaths
from app.planning.plan import PlanBuilder, PlanOptions
from app.services import AppService
from app.scan.scanner import Scanner
from tests.fixtures import build_full_rip_album


def make_service(tmp_path: Path) -> AppService:
    return AppService(DataPaths(root=tmp_path / "data", portable=True))


def add_album_unit(service: AppService, source_root: Path) -> Unit:
    service.state.sources.append(SourceRoot(path=str(source_root)))
    outcome = Scanner().scan_source(service.state.sources[0])
    unit = [u for u in outcome.units if u.is_album][0]
    service.state.units.append(unit)
    return unit


def test_assign_artist_does_not_confirm_boundary(tmp_path):
    """全自动：边界始终自动生效；指定整理艺术家只改归属，不改边界。"""
    service = make_service(tmp_path)
    source_root = tmp_path / "src"
    album_dir = build_full_rip_album(source_root)
    unit = add_album_unit(service, source_root)
    assert unit.boundary_confirmed, "全自动模式边界自动采用"

    result = service.assign_organize_artist([unit], "安田レイ")

    assert result["albums"] == 1
    assert unit.album_artist == "安田レイ"
    assert unit.origins["album_artist"] == "user"
    assert unit.status == UnitStatus.READY.value
    service.state.units[0].checked = True
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"),
                                   mode="move")).build(service.state.units)
    item = plan.items[0]
    assert item.status == PlanStatus.READY.value
    assert Path(item.target) == tmp_path / "target" / "安田レイ" / "プリズム"


def test_assign_artist_keeps_track_missing_title(tmp_path):
    """散曲缺少歌曲名时，指定整理艺术家不能把缺歌名当作已解决。"""
    service = make_service(tmp_path)
    track = Unit(kind=UnitKind.TRACK.value, path=str(tmp_path / "x.flac"),
                 name="x", title="", artist="某歌手", album="专辑")
    track.status = UnitStatus.NEEDS_INFO.value
    track.add_message("缺少歌曲名，需补全后才能执行")
    service.state.units.append(track)

    result = service.assign_organize_artist([track], "某歌手")

    assert result["tracks"] == 1
    assert track.album_artist == "某歌手"
    # 全自动：缺歌名不再拦截（兜底值流转），但原始提醒保留供详情可见
    assert track.status == UnitStatus.READY.value
    assert any("缺少歌曲名" in m for m in track.messages)
    assert any("歌曲名" in s for s in result["still_missing"])


def test_assign_artist_saves_mapping_for_next_scan(tmp_path):
    """映射写入 artist_map 并持久化，下次扫描 apply_artist_map 使用。"""
    service = make_service(tmp_path)
    source_root = tmp_path / "src"
    build_full_rip_album(source_root)
    unit = add_album_unit(service, source_root)
    raw_artist = (unit.album_artist or unit.artist or unit.name).strip()

    service.assign_organize_artist([unit], "安田レイ")

    # 落盘的 artist_map.json（fixture 的 CUE 已含专辑艺术家，键为原始值）
    import json
    mapping = json.loads((tmp_path / "data" / "artist_map.json").read_text(encoding="utf-8"))
    assert mapping.get(raw_artist) == "安田レイ"
    assert service.suggest_artist(raw_artist) == "安田レイ"
    # 状态里的映射同样可用
    assert service.state.artist_map.get(raw_artist) == "安田レイ"


def test_existing_artist_dirs_filters_by_keyword(tmp_path):
    service = make_service(tmp_path)
    target = tmp_path / "target"
    (target / "安田レイ").mkdir(parents=True)
    (target / "YUI").mkdir()
    (target / "cover.jpg").write_bytes(b"x")  # 文件不算
    service.settings.target_dir = str(target)
    service.save_settings()

    names = [info["name"] for info in service.existing_artist_dirs()]
    assert names == ["YUI", "安田レイ"]  # 大小写折叠排序
    assert [i["name"] for i in service.existing_artist_dirs("レイ")] == ["安田レイ"]
    assert service.existing_artist_dirs("不存在") == []


def test_preview_artist_target_without_touching_disk(tmp_path):
    service = make_service(tmp_path)
    source_root = tmp_path / "src"
    album_dir = build_full_rip_album(source_root)
    unit = add_album_unit(service, source_root)
    service.settings.target_dir = str(tmp_path / "target")
    service.save_settings()

    preview = service.preview_artist_target("安田レイ", unit)

    assert preview == str(tmp_path / "target" / "安田レイ" / "プリズム")
    assert not (tmp_path / "target" / "安田レイ").exists()  # 只预览，不创建


def test_batch_assign_multiple_units(tmp_path):
    """一次指定多张专辑：全部就绪，映射只记一次。"""
    service = make_service(tmp_path)
    units: list[Unit] = []
    for name in ("专辑A", "专辑B", "专辑C"):
        source_root = tmp_path / name
        build_full_rip_album(source_root)
        units.append(add_album_unit(service, source_root))

    result = service.assign_organize_artist(units, "群星")

    assert result["albums"] == 3
    assert all(u.status == UnitStatus.READY.value for u in units)
    assert all(u.album_artist == "群星" for u in units)


def test_dispersed_album_auto_ready(tmp_path):
    """全自动：分散资料不再拦截，直接 READY 并连带打包。"""
    from tests.fixtures import build_dispersed
    source_root = tmp_path / "src"
    build_dispersed(source_root)
    service = make_service(tmp_path)
    unit = add_album_unit(service, source_root)
    assert unit.boundary_confirmed and unit.status == UnitStatus.READY.value
    unit.checked = True
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "target"),
                                   mode="move")).build(service.state.units)
    assert plan.items[0].status == PlanStatus.READY.value
