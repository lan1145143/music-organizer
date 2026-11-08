import os
import shutil
from pathlib import Path

from app.domain.models import SourceRoot, UnitStatus
from app.scan.scanner import Scanner, dedupe_sources, is_archive_name
from tests.fixtures import (build_archives, build_dispersed, build_full_rip_album,
                            build_loose_tracks, build_multidisc_album, make_wav)


def scan(path: Path):
    return Scanner().scan_source(SourceRoot(path=str(path)))


def test_full_rip_album(tmp_path):
    album_dir = build_full_rip_album(tmp_path)
    outcome = scan(tmp_path)
    assert outcome.stats["albums"] == 1
    unit = outcome.units[0]
    assert unit.is_album
    assert unit.name == album_dir.name
    assert unit.boundary_path == str(album_dir)
    assert unit.album == "プリズム"
    assert unit.album_artist == "安田レイ"
    # 依据充分且无歧义（独立目录 + CUE 引用齐全 + 无分散资料）→ 自动采用推荐边界
    assert unit.status == UnitStatus.READY.value
    assert unit.boundary_confirmed
    assert unit.auto_boundary
    assert any("自动采用" in m for m in unit.messages)
    assert len(unit.virtual_tracks) == 3
    roles = {a["role"] for a in unit.attachments}
    assert {"cue", "log", "cover", "scans", "info"} <= roles
    assert unit.file_count >= 7
    assert unit.size > 0


def test_multidisc_merges_to_parent(tmp_path):
    build_multidisc_album(tmp_path)
    outcome = scan(tmp_path)
    assert outcome.stats["albums"] == 1
    unit = outcome.units[0]
    assert unit.total_discs == 2
    assert len(unit.audio_files) == 2
    assert unit.album == "LOST CORNER"
    assert any("碟片" in e for e in unit.evidence)


def test_loose_tracks_and_status(tmp_path):
    build_loose_tracks(tmp_path)
    outcome = scan(tmp_path)
    assert outcome.stats["tracks"] == 4
    titles = {u.title for u in outcome.units}
    assert "唱" in titles and "夜に駆ける" in titles
    # 全自动：缺信息不再 NEEDS_INFO，全部 READY（缺名用兜底值 + 提醒）
    assert all(u.status == UnitStatus.READY.value for u in outcome.units if u.is_track)
    unknown = [u for u in outcome.units if u.title == "未知曲目"][0]
    assert unknown.artist == "未知艺术家"
    assert any("缺少艺术家" in m and "兜底" in m for m in unknown.messages)
    assert outcome.stats["loose_files"] >= 3


def test_archives_detected(tmp_path):
    build_archives(tmp_path)
    outcome = scan(tmp_path)
    names = {u.name for u in outcome.archives}
    assert "包内散曲.zip" in names
    assert "分卷.part1.rar" in names
    volume = [u for u in outcome.archives if u.name == "分卷.part1.rar"][0]
    assert volume.status == UnitStatus.READY.value
    assert "分卷" in volume.messages[0] and "自动归档" in volume.messages[0]


def test_archive_inside_album_not_separate(tmp_path):
    album = build_full_rip_album(tmp_path)
    (album / "bonus.zip").write_bytes(b"PK\x03\x04data")
    outcome = scan(tmp_path)
    assert all(u.name != "bonus.zip" for u in outcome.archives)
    unit = [u for u in outcome.units if u.is_album][0]
    assert any(Path(a["path"]).name == "bonus.zip" for a in unit.attachments) is False


def test_dispersed_marked_manual(tmp_path):
    build_dispersed(tmp_path)
    outcome = scan(tmp_path)
    unit = [u for u in outcome.units if u.is_album][0]
    assert unit.manual_note
    assert "BK" in unit.manual_note
    assert any("手动" in m for m in unit.messages)


def test_dedupe_sources(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    sources = [SourceRoot(path=str(tmp_path / "a")), SourceRoot(path=str(tmp_path / "a")),
               SourceRoot(path=str(tmp_path / "a" / "b"))]
    kept, notes = dedupe_sources(sources)
    assert len(kept) == 1
    assert len(notes) == 2


def test_is_archive_name():
    assert is_archive_name("x.zip")
    assert is_archive_name("x.part1.rar")
    assert not is_archive_name("x.flac")


def test_unreadable_audio_reports_reason(tmp_path):
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"not really an mp3")
    outcome = scan(tmp_path)
    track = [u for u in outcome.units if u.is_track][0]
    assert track.messages
    assert "读取失败" in track.messages[0] or "无法识别" in track.messages[0]
    # 全自动：读取失败≠拦截，走兜底值继续流转
    assert track.status == UnitStatus.READY.value


def test_empty_source_reports_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    outcome = scan(empty)
    assert outcome.errors
    assert "空目录" in outcome.errors[0]


def test_rescan_preserves_user_edits(tmp_path):
    from app.scan.scanner import merge_units
    build_loose_tracks(tmp_path)
    first = scan(tmp_path)
    target = first.units[0]
    target.album_artist = "用戶指定"
    target.origins["album_artist"] = "user"
    target.checked = True
    second = scan(tmp_path)
    merged, notes = merge_units(first.units, second.units)
    match = [u for u in merged if u.path == target.path][0]
    assert match.album_artist == "用戶指定"
    assert match.checked is True
    assert notes == []


def test_tagged_multidisc_without_cue_merges(tmp_path):
    """纯标签多碟（无 CUE）双碟专辑：合并为一个单元、边界为父目录。

    回归：多碟合并曾强制要求 CUE，tagged FLAC 双碟（最常见无 CUE 形态）
    被落成"专辑"与"专辑 (2)"两个目标。"""
    base = tmp_path / "音乐" / "测试专辑"
    for disc in (1, 2):
        d = base / f"Disc{disc}"
        d.mkdir(parents=True)
        for i in (1, 2, 3):
            make_wav(d / f"disc{disc}-0{i}.wav", seconds=0.3,
                     tags={"title": f"曲目{disc}-{i}", "artist": "歌手",
                           "album": "测试专辑", "album_artist": "歌手",
                           "track": i, "disc": disc})
    outcome = Scanner().scan_source(SourceRoot(path=str(tmp_path / "音乐")))
    albums = [u for u in outcome.units if u.is_album]
    assert len(albums) == 1
    assert albums[0].total_discs == 2
    assert os.path.normcase(albums[0].boundary_path) == os.path.normcase(str(base))


def test_tagged_same_parent_different_albums_not_merged(tmp_path):
    """同父目录但专辑标签不同的两个目录：不合并（防误合安全网）。"""
    base = tmp_path / "音乐"
    for dirname, album in (("甲专辑", "甲"), ("乙专辑", "乙")):
        d = base / dirname
        d.mkdir(parents=True)
        for i in (1, 2, 3):
            make_wav(d / f"0{i}.wav", seconds=0.3,
                     tags={"title": f"曲{i}", "artist": "歌手", "album": album,
                           "album_artist": "歌手", "track": i})
    outcome = Scanner().scan_source(SourceRoot(path=str(tmp_path / "音乐")))
    albums = [u for u in outcome.units if u.is_album]
    assert len(albums) == 2


def _make_junction(link: Path, target: Path) -> bool:
    """创建 Windows 目录联接；失败（非 NTFS/权限不足）返回 False。"""
    import subprocess
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    return result.returncode == 0 and link.exists()


def test_junction_loop_pruned_from_scan(tmp_path):
    """目录联接（junction）不跟随：指回祖先的联接环不会重复枚举/挂死。

    os.walk(followlinks=False) 只拦 symlink 不拦 junction（Python 3.12 实测），
    库内联接环会让同一物理文件按多层路径重复枚举成多个单元。"""
    source = tmp_path / "lib"
    artist = source / "歌手"
    artist.mkdir(parents=True)
    for i in (1, 2):   # 2 首（<3）不触发无 CUE 专辑候选，保持散曲形态便于断言
        make_wav(artist / f"0{i} - 歌手 - 曲{i}.wav", seconds=0.3,
                 tags={"title": f"曲{i}", "artist": "歌手", "album": "专辑", "track": i})
    if not _make_junction(artist / "loop", source):
        pytest.skip("无法创建目录联接（需要 NTFS）")
    outcome = Scanner().scan_source(SourceRoot(path=str(source)))
    tracks = [u for u in outcome.units if u.is_track]
    assert len(tracks) == 2
    # 联接目录不得出现在任何单元路径里（按路径段匹配，避免误伤 tmp 目录名）
    offenders = [u.path for u in outcome.units if "loop" in Path(u.path).parts]
    assert not offenders, str(offenders)
