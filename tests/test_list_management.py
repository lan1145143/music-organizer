"""AppService 列表管理：完成后自动移除条目、清空列表、状态备份滚动。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.models import SourceRoot, Unit, UnitKind, UnitStatus
from app.paths import DataPaths
from app.services import AppService
from tests.fixtures import build_full_rip_album


def make_service(tmp_path: Path) -> AppService:
    paths = DataPaths(root=tmp_path / "data", portable=True)
    return AppService(paths)


def add_album_source(service: AppService, tmp_path: Path) -> Unit:
    """添加一个真实扫描出来的整夹来源（不含音频标签，保持扫描快）。"""
    source_root = tmp_path / "src"
    album_dir = build_full_rip_album(source_root)
    service.state.sources.append(SourceRoot(path=str(source_root)))
    from app.scan.scanner import Scanner
    outcome = Scanner().scan_source(service.state.sources[0])
    unit = [u for u in outcome.units if u.is_album][0]
    unit.status = UnitStatus.READY.value
    unit.boundary_confirmed = True
    service.state.units.append(unit)
    return unit


def test_drop_units_removes_only_listed(tmp_path):
    service = make_service(tmp_path)
    album = add_album_source(service, tmp_path)
    track = Unit(kind=UnitKind.TRACK.value, source_id=album.source_id,
                 path=str(tmp_path / "elsewhere" / "song.wav"), name="song")
    service.state.units.append(track)
    service.save_state()

    removed = service.drop_units([album.id])

    assert removed == 1
    assert service.state.unit_by_id(album.id) is None
    assert service.state.unit_by_id(track.id) is not None
    # 状态文件确实持久化了移除结果
    data = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert all(u["id"] != album.id for u in data["units"])


def test_drop_units_also_drops_child_tracks(tmp_path):
    service = make_service(tmp_path)
    album = add_album_source(service, tmp_path)
    child = Unit(kind=UnitKind.TRACK.value, source_id=album.source_id,
                 path=str(Path(album.path) / "inner.wav"), name="inner")
    child.status = UnitStatus.CHILD.value
    service.state.units.append(child)
    service.save_state()

    removed = service.drop_units([album.id])

    assert removed == 2
    assert service.state.unit_by_id(child.id) is None


def test_drop_units_prunes_duplicate_groups(tmp_path):
    service = make_service(tmp_path)
    album = add_album_source(service, tmp_path)
    other = Unit(kind=UnitKind.TRACK.value, source_id=album.source_id,
                 path=str(tmp_path / "b.wav"), name="b")
    service.state.units.append(other)
    from app.domain.models import DuplicateGroup
    group = DuplicateGroup(kind="exact", unit_ids=[album.id, other.id])
    service.state.duplicate_groups.append(group)
    service.save_state()

    service.drop_units([album.id])

    # 组内只剩 1 个单元，分组整体清掉
    assert service.state.duplicate_groups == []


def test_clear_all_keeps_disk_files(tmp_path):
    service = make_service(tmp_path)
    album = add_album_source(service, tmp_path)
    assert Path(album.path).is_dir()

    removed = service.clear_all()

    assert removed == 1 + 1  # 1 来源 + 1 单元
    assert service.state.sources == []
    assert service.state.units == []
    # 磁盘上的音乐文件原封不动
    assert Path(album.path).is_dir()
    assert any(Path(album.path).glob("*.wav"))
    data = json.loads((tmp_path / "data" / "state.json").read_text(encoding="utf-8"))
    assert data["sources"] == [] and data["units"] == []


def test_state_save_rolls_backup(tmp_path):
    service = make_service(tmp_path)
    add_album_source(service, tmp_path)
    service.save_state()

    # 第二次保存：主文件与备份应同时存在，且备份是上一版内容
    service.state.notes = ["changed"]
    service.save_state()

    backup = tmp_path / "data" / "state.bak.json"
    main = tmp_path / "data" / "state.json"
    assert backup.is_file() and main.is_file()
    old = json.loads(backup.read_text(encoding="utf-8"))
    new = json.loads(main.read_text(encoding="utf-8"))
    assert old["notes"] == [] and new["notes"] == ["changed"]


def test_startup_check_reports_missing_source(tmp_path):
    service = make_service(tmp_path)
    unit = add_album_source(service, tmp_path)
    service.save_state()
    assert service.startup_check() == []  # 来源还在

    # 删除来源目录后再检查：应报告失联，且不抛异常
    for path in sorted(Path(service.state.sources[0].path).rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    Path(service.state.sources[0].path).rmdir()
    notes = service.startup_check()
    assert any("不存在" in n for n in notes)
    assert unit is not None
