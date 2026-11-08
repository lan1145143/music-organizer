"""测试与演示用素材构造（全部为合成数据，不涉及真实音乐）。"""

from __future__ import annotations

import math
import os
import shutil
import struct
import wave
import zipfile
from pathlib import Path


def make_wav(path: str | Path, seconds: float = 0.4, tags: dict | None = None,
             freq: float = 440.0, sample_rate: int = 8000) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample_rate * seconds)
    with wave.open(str(p), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        data = bytearray()
        for i in range(frames):
            value = int(12000 * math.sin(2 * math.pi * freq * i / sample_rate))
            data += struct.pack("<h", value)
        fh.writeframes(bytes(data))
    if tags:
        write_wav_tags(p, tags)
    return p


def write_wav_tags(path: str | Path, tags: dict) -> None:
    try:
        from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
        from mutagen.wave import WAVE
    except ImportError:  # pragma: no cover
        return
    audio = WAVE(str(path))
    if audio.tags is None:
        audio.add_tags()
    mapping = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2,
               "year": TDRC, "genre": TCON}
    for key, frame in mapping.items():
        if tags.get(key):
            audio.tags.delall(frame.__name__)
            audio.tags.add(frame(encoding=3, text=str(tags[key])))
    if tags.get("track"):
        audio.tags.delall("TRCK")
        audio.tags.add(TRCK(encoding=3, text=str(tags["track"])))
    if tags.get("disc"):
        audio.tags.delall("TPOS")
        audio.tags.add(TPOS(encoding=3, text=str(tags["disc"])))
    audio.save()


def make_cue(path: str | Path, audio_name: str, album: str, performer: str,
             tracks: list[tuple[int, str, str]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'PERFORMER "{performer}"', f'TITLE "{album}"',
             f'FILE "{audio_name}" WAVE']
    for number, title, track_performer in tracks:
        lines.append(f"  TRACK {number:02d} AUDIO")
        lines.append(f'    TITLE "{title}"')
        if track_performer:
            lines.append(f'    PERFORMER "{track_performer}"')
        mm, ss = divmod((number - 1) * 15, 60)
        lines.append(f"    INDEX 01 {mm:02d}:{ss:02d}:00")
    p.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return p


def make_zip(path: str | Path, members: dict[str, bytes]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return p


def make_7z(path: str | Path, members: dict[str, bytes], password: str | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        import py7zr
    except ImportError:
        # 本机预存环境限制（py7zr 依赖 pybcj 需 MSVC 编译器装不上）。
        # 全自动架构不解压压缩包：占位字节即可满足扫描/归档断言。
        p.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"placeholder" * 8)
        return p
    with py7zr.SevenZipFile(p, "w", password=password) as zf:
        for name, data in members.items():
            tmp = p.parent / ("__tmp_" + Path(name).name)
            tmp.write_bytes(data)
            try:
                zf.write(tmp, arcname=name)
            finally:
                tmp.unlink(missing_ok=True)
    return p


def build_full_rip_album(root: str | Path) -> Path:
    """截图型完整抓轨专辑（文档 5.1 的示例结构）。"""
    album = Path(root) / "[250827]安田レイ - プリズム"
    album.mkdir(parents=True, exist_ok=True)
    (album / "BD_ROM").mkdir(exist_ok=True)
    (album / "BD_ROM" / "menu.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (album / "Scans").mkdir(exist_ok=True)
    (album / "Scans" / "IMG001.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
    (album / "Scans" / "IMG002.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 64)
    (album / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"c" * 128)
    (album / "info.txt").write_text("VVCL-2788 / 2025-08-27 / 初回限定盤\r\n", encoding="utf-8")
    make_wav(album / "VVCL-2788.wav", seconds=0.6, freq=440)
    make_cue(album / "VVCL-2788.cue", "VVCL-2788.wav", "プリズム", "安田レイ",
             [(1, "プリズム", "安田レイ"), (2, "君の声", "安田レイ"), (3, "夜明け", "安田レイ")])
    (album / "VVCL-2788.log").write_text(
        "Exact Audio Copy V1.6\r\n\r\nTrack |  Start  |  Length  | Start sector | End sector\r\n"
        "  1   | 00:00.00 | 03:12.00 | 0 | 14399\r\n", encoding="utf-8")
    return album


def build_multidisc_album(root: str | Path) -> Path:
    base = Path(root) / "[240915]米津玄師 - LOST CORNER"
    for disc, titles in ((1, ["RED OUT", "さよーならまたいつか！"]),
                         (2, ["がらくた", "毎日"])):
        d = base / f"Disc{disc}"
        d.mkdir(parents=True, exist_ok=True)
        wav = d / f"disc{disc}.wav"
        make_wav(wav, seconds=0.5, freq=440 + disc * 40)
        make_cue(d / f"disc{disc}.cue", wav.name, "LOST CORNER", "米津玄師",
                 [(i + 1, title, "米津玄師") for i, title in enumerate(titles)])
        (d / f"disc{disc}.log").write_text("Exact Audio Copy V1.6\r\n", encoding="utf-8")
        (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + bytes([disc]) * 96)
    return base


def build_loose_tracks(root: str | Path) -> Path:
    base = Path(root) / "散曲收集"
    base.mkdir(parents=True, exist_ok=True)
    items = [
        ("01 - Ado - 唱.wav", {"title": "唱", "artist": "Ado", "album": "狂言", "track": 1,
                               "year": "2022", "genre": "J-Pop"}),
        ("02 - Ado - 踊.wav", {"title": "踊", "artist": "Ado", "album": "狂言", "track": 2,
                               "year": "2022", "genre": "J-Pop"}),
        ("01 - YOASOBI - 夜に駆ける.wav", {"title": "夜に駆ける", "artist": "YOASOBI",
                                          "album": "THE BOOK", "track": 1, "year": "2021"}),
        ("未知曲目.wav", {}),
    ]
    for name, tags in items:
        make_wav(base / name, seconds=0.35, tags=tags, freq=440)
    (base / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"L" * 128)
    (base / "booklet.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"B" * 128)
    (base / "readme.txt").write_text("散曲收集说明\r\n", encoding="utf-8")
    return base


def build_archives(root: str | Path) -> Path:
    base = Path(root) / "压缩包"
    base.mkdir(parents=True, exist_ok=True)
    tmp_track = Path(root) / "_tmp" / "track.wav"
    make_wav(tmp_track, seconds=0.3, tags={"title": "归档曲", "artist": "测试", "album": "包内专辑"})
    make_zip(base / "包内散曲.zip", {"包内散曲/01 - 测试 - 归档曲.wav": tmp_track.read_bytes()})
    make_7z(base / "加密包.7z", {"secret/track.wav": tmp_track.read_bytes()}, password="pw")
    (base / "损坏包.zip").write_bytes(b"PK\x03\x04this is not a real zip")
    # 真分卷组（≥2 个同主干分卷才算分卷；单个 .part1.rar 按普通单包处理）
    (base / "分卷.part1.rar").write_bytes(b"Rar!\x1a\x07\x00" + b"0" * 64)
    (base / "分卷.part2.rar").write_bytes(b"Rar!\x1a\x07\x00" + b"1" * 64)
    (base / "单卷.part1.rar").write_bytes(b"Rar!\x1a\x07\x00" + b"2" * 64)
    make_zip(base / "嵌套外.zip", {"inner.zip": (base / "包内散曲.zip").read_bytes(),
                                  "readme.txt": b"nested"})
    shutil.rmtree(Path(root) / "_tmp", ignore_errors=True)
    return base


def build_duplicates(root: str | Path) -> Path:
    """同名不同盘的两份字节一致音频（确定重复）。"""
    base = Path(root) / "重复素材"
    a = base / "版本A"
    b = base / "版本B"
    for target in (a, b):
        target.mkdir(parents=True, exist_ok=True)
        make_wav(target / "01 - テスト - 同じ曲.wav", seconds=0.4,
                 tags={"title": "同じ曲", "artist": "テスト", "album": "重複"})
        (target / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 64)
    same = (a / "01 - テスト - 同じ曲.wav").read_bytes()
    (b / "01 - テスト - 同じ曲.wav").write_bytes(same)
    return base


def build_dispersed(root: str | Path) -> Path:
    """资料分散：音频/CUE 在子目录，BK/DVD/压缩包在上级。"""
    base = Path(root) / "[231201]分散サンプル - アルバム"
    inner = base / "CD"
    inner.mkdir(parents=True, exist_ok=True)
    make_wav(inner / "disc.wav", seconds=0.3)
    make_cue(inner / "disc.cue", "disc.wav", "アルバム", "分散サンプル", [(1, "A", "分散サンプル")])
    (base / "BK").mkdir(exist_ok=True)
    (base / "BK" / "scan01.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"k" * 64)
    (base / "DVD").mkdir(exist_ok=True)
    (base / "DVD" / "menu.vob").write_bytes(b"\x00" * 128)
    make_zip(base / "特典.zip", {"readme.txt": b"bonus"})
    return base


def build_demo_library(root: str | Path) -> Path:
    """完整演示库，含整夹、多碟、散曲、重复、分散、压缩包。"""
    root = Path(root)
    source_a = root / "来源A-完整抓轨"
    source_b = root / "来源B-散曲"
    source_c = root / "来源C-压缩包与疑难"
    source_a.mkdir(parents=True, exist_ok=True)
    source_b.mkdir(parents=True, exist_ok=True)
    source_c.mkdir(parents=True, exist_ok=True)
    build_full_rip_album(source_a)
    build_multidisc_album(source_a)
    build_loose_tracks(source_b)
    build_archives(source_c)
    build_dispersed(source_c)
    (root / "目标音乐库").mkdir(parents=True, exist_ok=True)
    return root
