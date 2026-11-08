"""散装附件识别与关联建议。

依据充分才推荐；不因位于同一大目录就全部关联。未关联附件单独展示，可手动关联。

外置歌词只认 .lrc（同目录确定性关联交给 lyrics_recognizer）；
`.txt` 一律按 info 附件处理，不做内容嗅探。
"""

from __future__ import annotations

import os
from pathlib import Path

from .audio import is_audio
from .constants import LYRICS_EXTS, is_archive_file
from ..domain.models import Attachment
from ..domain.naming import prune_junctions

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
COVER_NAMES = {"cover", "folder", "front", "package", "封面", "front_cover", "album",
               "albumart", "folder_thumb"}
SCAN_HINTS = {"scan", "scans", "booklet", "inner", "back", "obi", "disc", "cd", "page",
              "扫描", "内页", "封底", "碟面", "手册", "bk"}
LOG_EXTS = {".log", ".accurip"}
INFO_EXTS = {".txt", ".m3u", ".m3u8", ".ffp", ".md5", ".sfv", ".url", ".nfo", ".ini",
             ".xml", ".json", ".pdf", ".cue", ".lrc"}


def classify(path: str | Path) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    stem = p.stem.casefold()
    parent = p.parent.name.casefold()
    if ext == ".cue":
        return "cue"
    if ext in LYRICS_EXTS:
        return "lyrics"
    if ext in LOG_EXTS:
        return "log"
    if ext in IMAGE_EXTS:
        if stem in COVER_NAMES or parent in COVER_NAMES:
            return "cover"
        if parent in {"scans", "scan", "bk", "booklet", "artwork"}:
            return "scans"
        if any(hint in stem for hint in SCAN_HINTS):
            return "scans"
        return "cover" if stem.startswith(("cover", "front")) else "scans"
    if ext in INFO_EXTS:
        return "info"
    return "misc"


def is_attachment_candidate(path: str | Path) -> bool:
    p = Path(path)
    if is_audio(p):
        return False
    if is_archive_file(p):
        return False  # 压缩包单独处理，不当作附件（口径统一：弱点 #8）
    return True


def iter_files(directory: str | Path, recursive: bool = True) -> list[str]:
    """列出目录下文件。recursive=False 时只看当前层（旧版形参未生效、
    恒递归子树，把嵌套子目录文件当附件关联——识别机制.md 弱点 #7）。"""
    root = Path(directory)
    out: list[str] = []
    if not root.is_dir():
        return out
    if not recursive:
        try:
            for entry in os.scandir(root):
                if entry.is_file():
                    out.append(entry.path)
        except OSError:
            return []
        return sorted(out)
    for dirpath, dirnames, filenames in os.walk(root):
        prune_junctions(dirpath, dirnames)
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            out.append(str(Path(dirpath) / name))
    return out


def album_attachments(boundary: str | Path) -> list[Attachment]:
    """整夹模式下，专辑边界内所有非音频文件都随夹保留。

    歌词（含歌词文本、各语言版本）在这里不拆分、不判断归属，
    随原文件夹原样保留，只在详情里标注为“外置歌词”。
    """
    result: list[Attachment] = []
    for path in iter_files(boundary):
        if not is_attachment_candidate(path):
            continue
        role = classify(path)
        if role == "lyrics":
            note = "位于专辑目录内，随整夹原样保留（不改写、不转码）"
        else:
            note = "位于专辑目录内，随整夹原样保留"
        result.append(Attachment(path=path, role=role, selected=True,
                                 confidence="high", note=note, owner_hint="专辑目录",
                                 is_lyrics=role == "lyrics"))
    return result


def suggest_for_tracks(track_paths: list[str], directory: str | Path) -> tuple[list[Attachment], list[Attachment]]:
    """为散曲建议附件；返回 (可确定的附件, 未关联附件)。

    规则：
    - 与某首歌同名的图片/日志 → 高置信度关联该曲。
    - 同目录只有一个专辑分组时，目录级封面可作该分组封面（中置信度）。
    - 歌词文件不在此处处理（交给 lyrics_recognizer 判定归属，避免仅凭文件名误关）。
    - 其余进入未关联列表。
    """
    directory = Path(directory)
    stems = {Path(p).stem.casefold(): p for p in track_paths}
    confirmed: list[Attachment] = []
    unassociated: list[Attachment] = []
    for path in iter_files(directory, recursive=False):
        if not is_attachment_candidate(path):
            continue
        p = Path(path)
        role = classify(p)
        if role == "lyrics":
            continue
        stem = p.stem.casefold()
        if stem in stems and role != "cover":
            confirmed.append(Attachment(path=path, role=role, selected=True, confidence="high",
                                        note=f"与《{Path(stems[stem]).name}》同名",
                                        owner_hint=Path(stems[stem]).name))
            continue
        if role == "cover":
            confirmed.append(Attachment(path=path, role="cover", selected=True, confidence="medium",
                                        note="同目录封面，可作为该专辑封面", owner_hint="目录封面"))
            continue
        unassociated.append(Attachment(path=path, role=role, selected=False, confidence="none",
                                       note="未能确定归属，需手动关联", owner_hint=""))
    return confirmed, unassociated
