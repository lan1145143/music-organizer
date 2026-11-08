"""命名与路径规则：目录模板、散曲文件名、Windows 非法字符、长度与冲突。

规则全部只作用于"新生成的目标路径"，不修改原文件与标签。
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

MAX_PATH = 259  # Windows 默认 MAX_PATH 260，含结尾空字符

# 非法字符 → 对应全角字符
INVALID_CHAR_MAP = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
}

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

TEMPLATE_LABELS = {
    "A": "目录艺术家/专辑/",
    "B": "目录艺术家/年份 - 专辑/",
}

UNKNOWN_ALBUM = "未分类专辑"
UNKNOWN_ARTIST = "未知艺术家"
UNKNOWN_TITLE = "未命名歌曲"


class NamingError(ValueError):
    """无法生成合法目标名称（缺必需信息或名称不可用）。"""


def sanitize_component(name: str, fallback: str = "未命名") -> str:
    """把任意字符串变成合法的单个 Windows 文件名/目录名片段。"""
    if name is None:
        name = ""
    text = unicodedata.normalize("NFC", str(name))
    # 控制字符替换为空格
    text = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)
    for bad, good in INVALID_CHAR_MAP.items():
        text = text.replace(bad, good)
    text = text.strip()
    # 去掉结尾空格与句点（Windows 不允许）
    text = re.sub(r"[ .]+$", "", text)
    if not text or text in (".", ".."):
        text = fallback
    stem = text.split(".")[0].upper()
    if stem in RESERVED_NAMES or text.upper() in RESERVED_NAMES:
        text = "_" + text
    # 单段过长保护（不静默截断用户可见的名称，保留前 120 字符并标记）
    if len(text) > 120:
        text = text[:120].rstrip(" .")
        if not text:
            text = fallback
    return text


def sanitize_artist(name: str) -> str:
    return sanitize_component(name, UNKNOWN_ARTIST)


def sanitize_album(name: str) -> str:
    return sanitize_component(name, UNKNOWN_ALBUM)


def display_artist(unit) -> str:
    """目录艺术家：优先专辑艺术家，缺失时用歌曲艺术家。"""
    return (unit.album_artist or unit.artist or "").strip()


def album_dir_parts(unit, template: str = "A", artist_dir: str | None = None) -> list[str]:
    """散曲/专辑的目标目录片段（相对目标根）。

    整夹整理不使用该函数（整夹保留原目录名，只按艺术家归档）。
    artist_dir：解析到的真实艺术家目录名（已有目录复用其原名；
    未提供时按清洗规则生成）。
    """
    artist = sanitize_artist(display_artist(unit))
    album = sanitize_album((unit.album or "").strip() or UNKNOWN_ALBUM)
    year = (unit.year or "").strip()
    if template == "B":
        album_dir = f"{year} - {album}" if year else album
    else:
        album_dir = album
    parts = [artist_dir or artist, sanitize_component(album_dir, UNKNOWN_ALBUM)]
    total_discs = int(unit.total_discs or 1)
    if total_discs > 1:
        disc = int(unit.disc or 1)
        parts.append(f"CD{disc}")
    return parts


def album_target_dir(target_root: str | Path, unit, template: str = "A",
                     artist_dir: str | None = None) -> Path:
    """整夹整理目标：目标根/目录艺术家/元数据专辑名。

    专辑目录名**强制取自元数据**（CUE TITLE / 标签专辑，随扫描写入 unit.album），
    不再沿用来源目录名（基于元数据动态重命名专辑目录方案）；元数据缺失时
    落"未分类专辑"兜底目录，计划内同名自动改名 (2)(3)。
    """
    artist = artist_dir or sanitize_artist(display_artist(unit))
    folder = (unit.album or "").strip() or UNKNOWN_ALBUM
    return Path(target_root) / artist / sanitize_album(folder)


def track_filename(unit, digits: int = 2) -> str:
    """散曲文件名：``曲号 - 歌曲艺术家 - 歌曲名.原扩展名``。

    全自动模式：缺歌名/艺术家时用兜底值（未命名歌曲/未知艺术家），
    不再抛出 NamingError 阻止流转。
    """
    title = sanitize_component((unit.title or "").strip(), UNKNOWN_TITLE)
    artist = sanitize_component((unit.artist or "").strip(), UNKNOWN_ARTIST)
    parts: list[str] = []
    if unit.track:
        width = max(int(digits or 2), 1)
        num = str(int(unit.track)).zfill(width)
        parts.append(num)
    parts.append(artist)
    parts.append(title)
    ext = os.path.splitext(unit.path)[1]
    if not ext:
        ext = "." + (unit.fmt or "audio").lower()
    return " - ".join(parts) + ext


def track_target_path(target_root: str | Path, unit, template: str = "A", digits: int = 2,
                      artist_dir: str | None = None) -> Path:
    base = Path(target_root).joinpath(*album_dir_parts(unit, template, artist_dir))
    return base / track_filename(unit, digits)


def normalize_key(path: str | Path) -> str:
    """Windows 大小写不敏感比较键（含斜杠归一）。"""
    text = os.path.normpath(str(path))
    return text.replace("/", "\\").casefold()


def path_length_ok(path: str | Path, limit: int = MAX_PATH) -> tuple[bool, int]:
    length = len(str(path))
    return length <= limit, length


def normalize_path_key(path: str | Path) -> str:
    """Windows 路径语义的比较键：normpath + normcase，去掉末尾分隔符。"""
    text = os.path.normpath(str(path))
    text = text.rstrip("\\/")
    return text.replace("/", "\\").casefold()


def real_path(path: str | Path) -> str:
    """解析符号链接/目录联接后的真实路径；失败时退回原路径。"""
    try:
        return os.path.realpath(str(path))
    except OSError:
        return str(path)


def prune_junctions(dirpath: str | Path, dirnames: list[str]) -> None:
    """os.walk 回调：把目录联接（junction）从待访问子目录列表剪掉。

    followlinks=False 只拦符号链接；Windows 目录联接会被当真走进去，
    遇到指回祖先的联接会成环——同一物理文件按多层路径重复枚举，
    扫描重复计数、执行时"来源不存在"。凡 walk 用户库目录，先调用本函数。
    旧解释器无 os.path.isjunction 时静默跳过（保持原行为）。
    """
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is None:  # pragma: no cover - Python < 3.12
        return
    base = os.fspath(dirpath)
    dirnames[:] = [name for name in dirnames
                   if not isjunction(os.path.join(base, name))]


def walk_pruned(root: str | Path):
    """os.walk 并剪枝目录联接（语义见 prune_junctions）。

    凡 walk 用户库目录统一走这里，杜绝联接环导致的重复枚举/挂死。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        prune_junctions(dirpath, dirnames)
        yield dirpath, dirnames, filenames


def paths_related(a: str | Path, b: str | Path) -> bool:
    """两个路径是否相同或互为祖先/后代（含大小写、尾分隔符、`..`、
    现有目录的符号链接/目录联接指向同一位置的情况）。

    用于目标/来源包含关系校验与计划期的自我复制防护，判定必须一致。
    """
    if not a or not b:
        return False
    ka = normalize_path_key(real_path(a))
    kb = normalize_path_key(real_path(b))
    if ka == kb:
        return True
    return ka.startswith(kb + "\\") or kb.startswith(ka + "\\")


def unique_name(path: Path, taken: set[str] | None = None) -> Path:
    """在磁盘与计划内不冲突的名称（追加 (2)、(3)…），不覆盖任何现有文件。"""
    taken = taken if taken is not None else set()
    if not path.exists() and normalize_key(path) not in taken:
        return path
    stem, suffix = path.stem, path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists() and normalize_key(candidate) not in taken:
            return candidate
        index += 1


def fit_target_path(path: str | Path, limit: int = MAX_PATH) -> Path:
    """把目标路径截断到 Windows MAX_PATH 之内（自动消解，不再阻止计划）。

    只截断**文件名主干**：严格保留扩展名与已有防重名后缀（如 " (2)"）；
    目录段过长时逐段收紧。返回仍超长时（目录部分本身超限）原样返回，
    由调用方按不可消解处理。不改变名称语义之外的任何内容。
    """
    p = Path(path)
    total = len(str(p))
    if total <= limit:
        return p
    # 先分离 " (N)" 防重名后缀，截断后原样接回
    m = re.match(r"^(.*?)( \(\d+\))?$", p.stem)
    base_stem, dup_suffix = m.group(1), (m.group(2) or "")
    real_suffix = p.suffix
    overflow = total - limit
    keep = len(base_stem) - overflow
    if keep >= 1:
        return p.with_name(base_stem[:keep].rstrip(" .") + dup_suffix + real_suffix)
    # 文件名截到 1 仍超长：收紧最长的目录段
    parts = list(p.parts)
    for i in range(len(parts) - 2, 0, -1):
        segment = parts[i]
        spare = overflow - max(0, len(base_stem) - 1)
        if len(segment) > 8 and len(segment) - 4 > spare:
            parts[i] = segment[:max(4, len(segment) - spare)]
            trimmed = len(segment) - len(parts[i])
            overflow -= trimmed
        else:
            trimmed = len(segment) - 4
            if trimmed <= 0:
                continue
            parts[i] = segment[:4]
            overflow -= trimmed
        keep = len(base_stem) - overflow
        if keep >= 1:
            return Path(*parts[:-1]) / (
                base_stem[:keep].rstrip(" .") + dup_suffix + real_suffix)
    return p
