"""识别层统一常量：后缀集合、分卷压缩包判定、通用目录词表。

历史上 scanner / attachments / album_infer 三处各自维护口径不一的词表
（识别机制.md 弱点 #10），现统一收口于此，其余模块一律从本模块引用。
"""

from __future__ import annotations

import re
from pathlib import Path

# 1. 音频扩展名（唯一事实来源；audio.py 转发导出以兼容既有引用）
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".wav", ".aiff", ".aif",
    ".ogg", ".oga", ".opus", ".wma", ".ape", ".wv",
}

# 无损格式（抓轨整夹判定的必要条件之一）
LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"}

# 2. 歌词扩展名：仅认 .lrc（.txt 一律按 info 附件处理，不做内容嗅探）
LYRICS_EXTS = {".lrc"}

# 3. 压缩包与分卷识别（统一 scanner 与 attachments 口径）
SINGLE_ARCHIVE_EXTS = {".zip", ".rar", ".7z"}

# 分卷命名模式：.part001.rar / .r00 / .z01 / .001
MULTIVOL_PATTERNS = [
    re.compile(r"^(?P<stem>.+)\.part0*(?P<vol>\d+)\.rar$", re.I),
    re.compile(r"^(?P<stem>.+)\.r(?P<vol>\d{2,3})$", re.I),
    re.compile(r"^(?P<stem>.+)\.z(?P<vol>\d{2,3})$", re.I),
    re.compile(r"^(?P<stem>.+)\.(?P<vol>\d{3})$", re.I),
]


def multivolume_stem(name: str) -> str | None:
    """分卷文件的同组主干名；非分卷命名返回 None。"""
    for pat in MULTIVOL_PATTERNS:
        m = pat.match(name)
        if m:
            return m.group("stem")
    return None


def is_archive_file(path: str | Path) -> bool:
    """该文件是否按压缩包处理（单包或分卷，口径唯一）。"""
    name = Path(path).name
    if Path(name).suffix.lower() in SINGLE_ARCHIVE_EXTS:
        return True
    return multivolume_stem(name) is not None


# 4. 重复隔离目录（非破坏性隔离工作流）：扫描/查重一律整棵剪枝，
#    防止已隔离进该目录的重复文件在下次扫描时回流（任务书 §4.3 防死循环）
ISOLATION_DIR_NAME = "a重复歌曲"


def is_isolation_dirname(name: str) -> bool:
    """目录名是否为隔离目录「a重复歌曲」（大小写/空白折叠对比）。"""
    return name.strip().casefold() == ISOLATION_DIR_NAME.casefold()


# 5. 通用无意义目录名（全小写折叠对比；不冒充专辑名）
GENERIC_DIR_WORDS = {
    "music", "audio", "songs", "tracks", "mp3", "flac", "wav", "lossless",
    "downloads", "download", "temp", "tmp", "new folder", "untitled",
    "音乐", "歌曲", "下载", "新建文件夹", "未分类", "散曲", "无损", "无损音乐",
    "精选", "合集", "杂锦", "伴奏", "备份", "backup",
}


def is_generic_dirname(name: str) -> bool:
    """目录名是否为通用无意义词（大小写/空白折叠后精确对比）。"""
    return name.strip().casefold() in GENERIC_DIR_WORDS
