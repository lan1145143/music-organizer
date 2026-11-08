"""P0-3 回归：散曲确定重复检测（by_size 键类型）。

覆盖：相同内容相同大小 → 确定重复；相同大小不同内容 → 不判确定重复；
不同大小 → 不做无意义的哈希比较。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.models import Unit, UnitKind
from app.scan.duplicates import detect_duplicates


def _track(tmp_path: Path, name: str, data: bytes) -> Unit:
    path = tmp_path / name
    path.write_bytes(data)
    unit = Unit(kind=UnitKind.TRACK.value, path=str(path), name=name,
                title=name, artist="歌手")
    unit.size = len(data)
    unit.duration = 100.0
    return unit


def test_identical_tracks_form_exact_group(tmp_path):
    a = _track(tmp_path, "a.flac", b"same-bytes-123456")
    b = _track(tmp_path, "b.flac", b"same-bytes-123456")
    report = detect_duplicates([a, b])
    exact = [g for g in report.groups if g.kind == "exact"]
    assert len(exact) == 1
    assert set(exact[0].unit_ids) == {a.id, b.id}


def test_same_size_different_content_not_exact(tmp_path):
    a = _track(tmp_path, "a.flac", b"content-one-123456")
    b = _track(tmp_path, "b.flac", b"content-two-123456")
    report = detect_duplicates([a, b])
    assert report.exact_count == 0


def test_different_size_tracks_skip_hashing(tmp_path):
    calls: list[str] = []

    def counting_hasher(path):
        calls.append(str(path))
        return "hash-" + Path(path).name

    a = _track(tmp_path, "a.flac", b"one-size")
    b = _track(tmp_path, "b.flac", b"a-strictly-different-size")
    assert a.size != b.size
    report = detect_duplicates([a, b], hasher=counting_hasher)
    assert report.exact_count == 0
    # 大小不同 → 散曲不做无意义的哈希比较
    assert calls == []


def test_same_size_still_hashed_even_without_peer_content_match(tmp_path):
    calls: list[str] = []

    def counting_hasher(path):
        calls.append(str(path))
        return "h-" + Path(path).name

    a = _track(tmp_path, "a.flac", b"xxxx")
    b = _track(tmp_path, "b.flac", b"yyyy")
    assert a.size == b.size
    detect_duplicates([a, b], hasher=counting_hasher)
    assert sorted(Path(p).name for p in calls) == ["a.flac", "b.flac"]


def _album_unit(tmp_path: Path, name: str, files: list[tuple[str, bytes]]) -> Unit:
    """构造整夹单元：真实落盘（detect 预筛后逐文件哈希），audio_files 文件名可不同。"""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    audio: list[str] = []
    total = 0
    for file_name, data in files:
        (root / file_name).write_bytes(data)
        audio.append(str(root / file_name))
        total += len(data)
    unit = Unit(kind=UnitKind.ALBUM.value, path=str(root), name=name,
                album="专辑", audio_files=audio)
    unit.size = total
    unit.file_count = len(files)
    return unit


def test_album_renamed_copies_form_exact_group(tmp_path):
    """整夹签名按哈希集合排序：同内容、不同文件名的两份专辑必须判为一组
    （跨来源抓轨文件名不同是常态；按路径排序会让拼接顺序不同而漏判）。"""
    a = _album_unit(tmp_path, "副本A", [("Track01.flac", b"one-0001"),
                                        ("Track02.flac", b"two-0002")])
    b = _album_unit(tmp_path, "副本B", [("01 - 歌手 - 第一首.flac", b"one-0001"),
                                        ("02 - 歌手 - 第二首.flac", b"two-0002")])
    report = detect_duplicates([a, b])
    exact = [g for g in report.groups if g.kind == "exact"]
    assert len(exact) == 1
    assert set(exact[0].unit_ids) == {a.id, b.id}


def test_album_different_content_not_grouped(tmp_path):
    a = _album_unit(tmp_path, "专辑甲", [("Track01.flac", b"one-0001")])
    b = _album_unit(tmp_path, "专辑乙", [("Track01.flac", b"different-999")])
    report = detect_duplicates([a, b])
    assert not [g for g in report.groups if g.kind == "exact"]
