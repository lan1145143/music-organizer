"""重复隔离（非破坏性）：组内最佳版本优选与「a重复歌曲」目录规则。

方案四（docs/code_artifardtgct.md）：不删除任何文件——重复组中自动选出
1 个最佳版本保留原位，其余副本移入来源根目录下的「a重复歌曲」
（字母 a 开头保证资源管理器按名称排序置顶），供用户人工核查。

优选权重（从高到低）：
1. 无损优先于有损（扩展名口径：FLAC/WAV/AIFF/APE/WV > 有损）；
2. 高码率 / 高采样率 / 高位深优先；
3. 元数据完整度优先（内嵌封面 > 内嵌歌词 > 标签字段完整）；
4. 兜底：文件体积较大、修改时间较早者优先。

整夹（专辑目录）重复整组比较：无损占比、总字节、文件数，规则同上。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ..domain.models import Unit

# 无损判定扩展名（任务书 §2.1：FLAC / WAV / ALAC / APE > MP3 / AAC / OGG）。
# ALAC 装在 .m4a 容器内，扩展名层无法区分，按容器归入有损侧（宁可少隔离）。
LOSSLESS_SUFFIXES = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"}

_LOG_NAME = "isolation_log.txt"


# ----------------------------------------------------------------------
# 元数据完整度探测（仅对重复组成员调用，数量少；失败按 0 分兜底）
# ----------------------------------------------------------------------
def metadata_probe(path: str | Path) -> tuple[int, int, int]:
    """返回 (有内嵌封面, 有内嵌歌词, 完整标签字段数)。

    mutagen 按扩展名解析；读不出/不支持一律 (0, 0, 0)，不抛错——
    优选只影响保留项选择，绝不能因探测失败中断隔离流程。
    """
    try:
        from mutagen import File as mutagen_file
        audio = mutagen_file(str(path))
    except Exception:  # noqa: BLE001 - 探测失败不阻断
        return (0, 0, 0)
    if audio is None:
        return (0, 0, 0)
    has_cover = has_lyrics = 0
    complete = 0
    try:
        if getattr(audio, "pictures", None):        # FLAC / APEv2 内嵌图
            has_cover = 1
        tags = audio.tags
        if tags is not None:
            if not has_cover and _tag_all(tags, "APIC", "METADATA_BLOCK_PICTURE",
                                          "COVR", "COVERART"):
                has_cover = 1
            if _tag_all(tags, "USLT", "SYLT", "©LYR", "LYRICS", "LYR"):
                has_lyrics = 1
            for keys in (("TIT2", "©NAM", "TITLE"),
                         ("TPE1", "©ART", "ARTIST"),
                         ("TALB", "©ALB", "ALBUM"),
                         ("TPE2", "©AART", "ALBUMARTIST"),
                         ("TDRC", "TYER", "©DAY", "DATE")):
                if _tag_all(tags, *keys):
                    complete += 1
    except Exception:  # noqa: BLE001 - 个别标签损坏时按已得分数兜底
        pass
    return (has_cover, has_lyrics, complete)


def _tag_all(tags, *keys: str) -> bool:
    lower = {str(k).lower() for k in tags.keys()}
    for key in keys:
        try:
            if hasattr(tags, "getall") and tags.getall(key):
                return True
        except Exception:  # noqa: BLE001
            pass
        if key.lower() in lower:
            return True
    return False


# ----------------------------------------------------------------------
# 优选排序键（越大越好；最终保留 max）
# ----------------------------------------------------------------------
def track_rank(unit: Unit, probe=None) -> tuple:
    """散曲优选排序键。probe 为 path → (cover, lyrics, complete) 缓存查询函数。"""
    cover, lyrics, complete = (0, 0, 0)
    if probe is not None:
        cover, lyrics, complete = probe(unit.path)
    return (
        1 if Path(unit.path).suffix.lower() in LOSSLESS_SUFFIXES else 0,   # 1. 无损优先
        unit.bitrate or 0,                                                # 2. 码率
        unit.sample_rate or 0,                                            #    采样率
        unit.bits or 0,                                                   #    位深
        cover,                                                            # 3. 内嵌封面
        lyrics,                                                           #    内嵌歌词
        complete,                                                         #    标签完整度
        unit.size or 0,                                                   # 4. 体积较大
        -_unit_mtime(unit),                                               #    时间较早
        # 稳定兜底：路径字典序，保证完全一致时结果可复现
        _neg_str(unit.path),
    )


def album_rank(unit: Unit, probe=None) -> tuple:
    """整夹优选排序键：无损占比 → 总字节 → 文件数 → 修改时间较早。"""
    lossless = 0
    audio_total = list(unit.audio_files or [])
    if audio_total:
        lossless = sum(1 for p in audio_total
                       if Path(p).suffix.lower() in LOSSLESS_SUFFIXES) / len(audio_total)
    cover, lyrics, complete = (0, 0, 0)
    if probe is not None and audio_total:
        # 整夹看任一音频的最大元数据得分（同内容整夹各看一份代表文件）
        for path in sorted(audio_total)[:3]:
            got = probe(path)
            cover = max(cover, got[0])
            lyrics = max(lyrics, got[1])
            complete = max(complete, got[2])
    return (
        round(lossless, 4),
        unit.size or 0,
        unit.file_count or 0,
        cover,
        lyrics,
        complete,
        -_unit_mtime(unit),
        _neg_str(unit.path),
    )


def _unit_mtime(unit: Unit) -> int:
    """文件修改时间：优先 sig（扫描时记录），缺失时现读；都拿不到按 0。"""
    for record in (unit.sig or {}).values():
        if isinstance(record, (list, tuple)) and len(record) >= 2:
            try:
                return int(record[1])
            except (TypeError, ValueError):
                continue
    try:
        return int(os.stat(unit.path).st_mtime)
    except OSError:
        return 0


def _neg_str(text: str) -> tuple:
    # 字符串没有自然"取反"：用 (False, text) 让路径字典序靠前者胜出
    return (False, text)


def select_keep(members: list[Unit], preferred: str = "",
                probe=None) -> tuple[Unit, list[Unit]]:
    """在重复组内选出保留版本与其余待隔离副本。

    preferred：用户已指定的保留单元 id（历史选择），命中时优先。
    返回 (keep, isolates)；members 少于 2 个时返回 (首个, [])。
    """
    if len(members) < 2:
        return (members[0], []) if members else (None, [])
    rank = album_rank if members[0].is_album else track_rank
    # 用户指定保留项优先，其余按优选排序（保证隔离名单确定性）
    if preferred and any(u.id == preferred for u in members):
        keep = next(u for u in members if u.id == preferred)
        ordered = sorted((u for u in members if u.id != preferred),
                         key=lambda u: rank(u, probe), reverse=True)
    else:
        ordered = sorted(members, key=lambda u: rank(u, probe), reverse=True)
        keep = ordered[0]
        ordered = ordered[1:]
    return keep, list(ordered)


# ----------------------------------------------------------------------
# 隔离目录与重命名
# ----------------------------------------------------------------------
def isolation_dir(source_root: str | Path) -> Path:
    """来源根目录下的「a重复歌曲」隔离目录（普通可见，不隐藏）。"""
    return Path(source_root) / "a重复歌曲"


def unique_isolation_name(dest_dir: Path, name: str, used: set[str]) -> str:
    """隔离目录内重名冲突：追加 _dup1 / _dup2 …（绝不覆盖已有文件）。

    used 收集本次批次已分配的名字（normcase），保证同一批次内也不撞车。
    """
    candidate = name
    if not (dest_dir / candidate).exists() and \
            os.path.normcase(candidate) not in used:
        used.add(os.path.normcase(candidate))
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 1
    while True:
        candidate = f"{stem}_dup{index}{suffix}"
        if not (dest_dir / candidate).exists() and \
                os.path.normcase(candidate) not in used:
            used.add(os.path.normcase(candidate))
            return candidate
        index += 1


def append_isolation_log(dest_dir: Path, moved_name: str, source_path: str,
                         keep_path: str, when: datetime | None = None) -> bool:
    """追加一条隔离记录到 isolation_log.txt（UTF-8，无则创建）。

    格式：[YYYY-MM-DD HH:MM:SS] 移入文件: "x" | 原路径: "y" | 保留版本: "z"
    写失败返回 False（只影响日志，不影响已完成的安全移动）。
    """
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    line = (f'[{stamp}] 移入文件: "{moved_name}" | 原路径: "{source_path}"'
            f' | 保留版本: "{keep_path}"')
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(dest_dir / _LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError:
        return False
