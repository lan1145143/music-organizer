"""P0-7 回归：整夹转"按散曲整理"时的附件分配。

旧实现把整张专辑的全部已选附件复制给每一首曲目 → 移动模式第一首搬走后
其余曲目报"来源不存在"，复制模式生成 N 份重复封面。
新规则：只有明确对应的歌词挂到曲目；专辑级附件随专辑目录整理一次。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.models import Unit, UnitKind
from app.paths import DataPaths
from app.planning.plan import PlanBuilder, PlanOptions
from app.services import AppService


def _album(tmp_path: Path) -> Unit:
    album_dir = tmp_path / "src" / "[20200101] Ado - 专辑"
    album_dir.mkdir(parents=True)
    (album_dir / "cover.jpg").write_bytes(b"jpg")
    (album_dir / "唱.lrc").write_bytes(b"[00:01.00]ca")
    (album_dir / "extra.lrc").write_bytes(b"[00:01.00]other")
    (album_dir / "01 - Ado - 唱.wav").write_bytes(b"\x00" * 32)
    (album_dir / "02 - Ado - 踊.wav").write_bytes(b"\x00" * 32)
    unit = Unit(kind=UnitKind.ALBUM.value, path=str(album_dir), name=album_dir.name,
                boundary_path=str(album_dir), album="专辑", album_artist="Ado",
                audio_files=[str(album_dir / "01 - Ado - 唱.wav"),
                             str(album_dir / "02 - Ado - 踊.wav")])
    unit.attachments = [
        {"path": str(album_dir / "cover.jpg"), "role": "cover", "selected": True},
        {"path": str(album_dir / "唱.lrc"), "role": "lyrics", "selected": True,
         "owner_hint": "唱"},
        {"path": str(album_dir / "extra.lrc"), "role": "lyrics", "selected": True,
         "owner_hint": ""},
    ]
    return unit


# ---------------------------------------------------------------------------
# 服务层：ensure_track_units 的附件分配
# ---------------------------------------------------------------------------
def test_ensure_track_units_assigns_only_matching_lyrics(tmp_path):
    service = AppService(DataPaths(root=tmp_path / "data", portable=True))
    album = _album(tmp_path)
    album.mode_hint = "tracks"
    service.state.units.append(album)

    created = service.ensure_track_units(album)

    assert created == 2
    tracks = [u for u in service.state.units if u.is_track]
    by_stem = {Path(t.path).stem: t for t in tracks}
    singing = by_stem["01 - Ado - 唱"]
    dancing = by_stem["02 - Ado - 踊"]
    # 明确对应的歌词挂到对应曲目
    assert [Path(a["path"]).name for a in singing.attachments] == ["唱.lrc"]
    assert dancing.attachments == []
    # 专辑级附件（封面、未归属歌词）保留在专辑单元，不复制给每首歌
    album_names = sorted(Path(a["path"]).name for a in album.attachments)
    assert album_names == ["cover.jpg", "extra.lrc", "唱.lrc"]
    # 任何曲目都不携带封面
    for t in tracks:
        assert all(a.get("role") != "cover" for a in t.attachments)


# ---------------------------------------------------------------------------
# 计划层：专辑级附件随专辑目录整理一次（移动/复制两种模式）
# ---------------------------------------------------------------------------
def _tracks_mode_units(tmp_path: Path, mode_hint: str = "tracks") -> list[Unit]:
    album = _album(tmp_path)
    album.mode_hint = mode_hint
    album.checked = False
    album.user_checked = None
    album.status = "ready"
    track_dir = album.boundary_path or album.path
    units = [album]
    for name in ("01 - Ado - 唱.wav", "02 - Ado - 踊.wav"):
        t = Unit(kind=UnitKind.TRACK.value, path=str(Path(track_dir) / name),
                 name=Path(name).stem, title=name.split(" - ")[-1].rsplit(".", 1)[0],
                 artist="Ado", album="专辑", album_artist="Ado")
        t.checked = True
        t.status = "ready"
        units.append(t)
    return units


def _cover_copies(plan, tmp_path: Path) -> int:
    total = 0
    for item in plan.items:
        for att in (item.extra.get("attachments") or []):
            if Path(att.get("path", "")).name == "cover.jpg":
                total += 1
    return total


def test_plan_tracks_mode_cover_moved_once_move(tmp_path):
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "tgt"), mode="move")) \
        .build(_tracks_mode_units(tmp_path))
    assert _cover_copies(plan, tmp_path) == 1


def test_plan_tracks_mode_cover_copied_once_copy(tmp_path):
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "tgt"), mode="copy")) \
        .build(_tracks_mode_units(tmp_path))
    assert _cover_copies(plan, tmp_path) == 1


def test_plan_album_mode_keeps_attachments_inside_album(tmp_path):
    """整夹整体整理（mode_hint="album"）不受影响：附件不单独生成搬运项。"""
    plan = PlanBuilder(PlanOptions(target_root=str(tmp_path / "tgt"), mode="move")) \
        .build(_tracks_mode_units(tmp_path, mode_hint="album"))
    # 整夹模式下曲目是子项，不生成散曲项；封面随整夹原样保留
    assert _cover_copies(plan, tmp_path) == 0
