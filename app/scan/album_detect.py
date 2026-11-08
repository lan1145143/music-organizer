"""专辑边界候选识别：CUE 实际引用 + 目录结构 + 标签信息，最终由用户确认。

原则：
- 最小合理专辑边界为默认候选；只有多个碟片目录专辑信息一致时才推荐上层。
- 上层出现多张专辑或关系不明的资料组时停止自动扩大边界。
- 目录名、封面、LOG 单独存在不足以确认边界。
- 缺 CUE 引用文件 → 标"可能不完整"，不隐瞒。
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..scan.audio import AudioMeta, read_audio
from ..scan.constants import LOSSLESS_EXTS
from ..scan.cue import CueSheet, parse_cue

ACCESSORY_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
    ".log", ".txt", ".cue", ".m3u", ".m3u8", ".accurip", ".ffp", ".md5",
    ".sfv", ".url", ".pdf", ".ini", ".nfo", ".dsf", ".db", ".xml", ".json",
    ".zip", ".rar", ".7z", ".iso", ".mp4", ".mkv", ".avi", ".vob", ".bin",
}


@dataclass
class AlbumCandidate:
    path: str = ""
    album_title: str = ""
    album_origin: str = ""   # 专辑名来源：cue | tag | ""（空=未识别到，走兜底目录）
    album_artist: str = ""
    audio_files: list[str] = field(default_factory=list)
    cue_paths: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    disc: int = 1
    total_discs: int = 1
    year: str = ""
    genre: str = ""
    incomplete: bool = False
    dispersed: bool = False
    dispersed_note: str = ""
    manual: bool = False
    parent_option: str = ""
    child_options: list[str] = field(default_factory=list)
    source_titles: dict[str, str] = field(default_factory=dict)
    single_image_cues: list[dict] = field(default_factory=list)  # 整轨 CUE 的虚拟曲目信息（避免二次 parse_cue）

    @property
    def size(self) -> int:
        total = 0
        for path in self.audio_files:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total


def _dir_of(paths: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for p in paths:
        grouped.setdefault(str(Path(p).parent), []).append(p)
    return grouped


def _consistent_album(files: list[str], metas: dict[str, AudioMeta]) -> str:
    values = {metas[f].album.strip() for f in files if f in metas and metas[f].album.strip()}
    if len(values) == 1:
        return values.pop()
    return ""


def _audio_under(directory: Path, audio_files: list[str],
                 by_prefix: dict[str, list[str]] | None = None) -> list[str]:
    """目录及其子树内的全部音频。

    by_prefix：预先按"祖先目录前缀"分桶的索引（normcase 键）——
    每个 CUE 目录全量线性扫所有音频在千专辑库是 O(CUE×N)，分桶一次 O(N)。
    未提供索引时按旧逻辑逐个 relative_to（保持单独调用可用）。
    """
    if by_prefix is not None:
        return list(by_prefix.get(os.path.normcase(str(directory)), []))
    out = []
    for f in audio_files:
        try:
            Path(f).relative_to(directory)
            out.append(f)
        except ValueError:
            continue
    return out


def _audio_prefix_index(audio_files: list[str]) -> dict[str, list[str]]:
    """把音频按所有祖先目录前缀分桶（normcase 键），供 _audio_under O(1) 查询。"""
    index: dict[str, list[str]] = {}
    for f in audio_files:
        parts = Path(f).parts
        for i in range(1, len(parts)):
            index.setdefault(os.path.normcase(str(Path(*parts[:i]))), []).append(f)
    return index


def detect_candidates(root: str | Path,
                      audio_files: list[str],
                      cue_paths: list[str],
                      metas: dict[str, AudioMeta] | None = None,
                      base_evidence: list[str] | None = None) -> list[AlbumCandidate]:
    """对一次扫描结果给出专辑候选列表（未确认）。"""
    metas = metas or {}
    audio_files = sorted(audio_files)
    sheets: dict[str, CueSheet] = {p: parse_cue(p) for p in sorted(cue_paths)}
    by_dir = _dir_of(cue_paths)
    candidates: list[AlbumCandidate] = []
    prefix_index = _audio_prefix_index(audio_files)   # CUE 目录音频归属 O(1) 查询

    # 1) CUE 所在目录 → 候选
    for directory, cues in by_dir.items():
        cand = AlbumCandidate(path=directory, cue_paths=sorted(cues))
        cand.audio_files = _audio_under(Path(directory), audio_files, prefix_index)
        referenced: list[str] = []
        for cue_path in cand.cue_paths:
            sheet = sheets[cue_path]
            name = Path(cue_path).name
            if sheet.error:
                cand.issues.append(f"{name}：{sheet.error}")
            resolved = sheet.resolve_files()
            missing = sheet.missing_files()
            referenced.extend(resolved)
            if resolved:
                cand.evidence.append(f"{name} 引用 {len(resolved)} 个音频文件")
            if missing:
                cand.incomplete = True
                preview = "、".join(missing[:3])
                more = " 等" if len(missing) > 3 else ""
                cand.issues.append(f"{name} 缺少被引用文件：{preview}{more}")
            if sheet.album_title and not sheet.encoding_uncertain:
                if not cand.album_title:
                    cand.album_title = sheet.album_title
                    cand.album_origin = "cue"
                cand.evidence.append(f"{name} 专辑信息：{sheet.album_title}")
            elif sheet.album_title and sheet.encoding_uncertain:
                cand.issues.append(
                    f"{name}：编码无法确认，专辑名未采用（避免乱码进入目录名）")
            if sheet.album_performer and not sheet.encoding_uncertain:
                cand.album_artist = cand.album_artist or sheet.album_performer
                cand.evidence.append(f"{name} 专辑艺术家：{sheet.album_performer}")
            if sheet.is_single_image:
                cand.evidence.append(f"{name} 为整轨 CUE（{sheet.track_count} 个虚拟曲目）")
                cand.source_titles["cue_tracks"] = str(sheet.track_count)
                cand.single_image_cues.append({
                    "cue": cue_path,
                    "tracks": [{"number": t.number, "title": t.title,
                                "performer": t.performer, "index": t.index}
                               for t in sheet.tracks],
                })
        if not cand.audio_files:
            cand.audio_files = sorted(set(referenced))
        # 同目录未引用音频
        referenced_keys = {os.path.normcase(p) for p in referenced}
        extra = [f for f in cand.audio_files if os.path.normcase(f) not in referenced_keys]
        if referenced and extra:
            names = "、".join(Path(f).name for f in extra[:3])
            cand.evidence.append(f"同目录另有未引用音频：{names}")
        if not cand.album_title:
            tag_album = _consistent_album(cand.audio_files, metas)
            if tag_album:
                cand.album_title = tag_album
                cand.album_origin = "tag"
                cand.evidence.append(f"音频标签专辑一致：{tag_album}")
        # 不再用目录名冒充专辑名（基于元数据动态重命名专辑目录方案）：
        # 无 CUE TITLE 且标签缺失 → album_title 留空，目标落“未分类专辑”兜底
        candidates.append(cand)

    # 2) 无 CUE 的抓轨整夹候选（条件收紧，防普通散曲目录误判）：
    #    全部满足才立候选——①音频 ≥3 首；②全部无损 或 同目录含抓轨日志
    #    （.log/.accurip）；③专辑标签非空且 100% 唯一一致；④曲号 1..N
    #    连续完整无重复无断号。不满足的目录一律交散曲直读流程逐首处理。
    audio_by_dir = _dir_of(audio_files)
    cue_dirs_norm = {os.path.normcase(d) for d in by_dir}
    for directory, files in audio_by_dir.items():
        if os.path.normcase(directory) in cue_dirs_norm:
            continue
        if len(files) < 3:
            continue
        album = _consistent_album(files, metas)
        if not album:
            continue
        if not _looks_like_rip_dir(directory, files, metas):
            continue
        if not _track_numbers_complete(files, metas):
            continue
        if any(_norm_inside(directory, c.path)
               for c in candidates):
            continue
        cand = AlbumCandidate(path=directory, audio_files=sorted(files))
        cand.album_title = album
        cand.album_origin = "tag"
        cand.evidence.append(f"目录内 {len(files)} 个音频标签专辑一致：{album}")
        candidates.append(cand)

    candidates = _resolve_overlaps(candidates)
    candidates = _merge_multidisc(candidates, audio_files, metas)
    candidates = _flag_dispersed(candidates, audio_files)
    for cand in candidates:
        _fill_year_genre(cand, metas)
        if base_evidence:
            cand.evidence.extend(base_evidence)
    return candidates


def _norm_inside(path: str, directory: str) -> bool:
    """纯字符串判断 path 是否在 directory 内（含相等的调用方自行排除）。

    两边都先 normcase；目录侧补尾部分隔符后做前缀比较，避免逐文件
    resolve() 造成的扫描瓶颈（Win 上 resolve 走 _getfinalpathname，极慢）。
    """
    p = os.path.normcase(os.path.normpath(path))
    d = os.path.normcase(os.path.normpath(directory))
    if p == d:
        return False
    return p.startswith(d + os.sep)


def _looks_like_rip_dir(directory: str, files: list[str],
                        metas: dict[str, AudioMeta]) -> bool:
    """无 CUE 目录是否满足抓轨整夹的格式条件（全部无损 或 含抓轨日志）。"""
    if any(metas.get(f) and metas[f].fmt and
           f".{metas[f].fmt.lower()}" not in LOSSLESS_EXTS for f in files):
        # 存在明确的非无损格式（如 mp3/m4a）→ 不按整夹处理
        return False
    try:
        for entry in os.scandir(directory):
            if entry.is_file() and entry.name.lower().endswith((".log", ".accurip")):
                return True
    except OSError:
        pass
    return all(Path(f).suffix.lower() in LOSSLESS_EXTS for f in files)


def _track_numbers_complete(files: list[str], metas: dict[str, AudioMeta]) -> bool:
    """曲号必须 1..N 连续完整、无重复、无断号（N=音频文件数）。"""
    numbers = []
    for f in files:
        meta = metas.get(f)
        if meta is None or meta.track is None:
            return False
        numbers.append(int(meta.track))
    return sorted(numbers) == list(range(1, len(files) + 1))


def _resolve_overlaps(candidates: list[AlbumCandidate]) -> list[AlbumCandidate]:
    """父/子候选同时存在时保留更小的边界（最小合理边界）。

    按路径深度从深到浅（子目录优先）处理：若某候选的全部音频已被
    已保留的子候选 100% 覆盖，则剔除该父候选。旧版过滤条件方向写反
    （父目录永远不会"位于"子目录内），嵌套候选全保留（弱点 #2）。
    """
    kept: list[AlbumCandidate] = []
    kept_audio: set[str] = set()
    for cand in sorted(candidates, key=lambda c: len(Path(c.path).parts), reverse=True):
        if cand.audio_files and all(os.path.normcase(f) in kept_audio
                                    for f in cand.audio_files):
            continue
        kept.append(cand)
        kept_audio.update(os.path.normcase(f) for f in cand.audio_files)
    # 记录可改选的上级/子级
    for cand in kept:
        parents = [k for k in kept if _norm_inside(cand.path, k.path)]
        children = [k for k in kept if _norm_inside(k.path, cand.path)]
        cand.child_options = [k.path for k in children]
        if parents:
            cand.parent_option = sorted(parents, key=lambda k: len(Path(k.path).parts))[0].path
    return kept


_NAT_SPLIT_RE = re.compile(r"(\d+)")


def _natural_key(path: str) -> list:
    """自然排序键：数字段按数值比较（CD2 < CD10，弱点 #3）。"""
    return [int(part) if part.isdigit() else part.casefold()
            for part in _NAT_SPLIT_RE.split(Path(path).name)]


def _merge_multidisc(candidates: list[AlbumCandidate], audio_files: list[str],
                     metas: dict[str, AudioMeta]) -> list[AlbumCandidate]:
    """多个碟片目录专辑信息一致时推荐上层作为多碟整体。"""
    by_parent: dict[str, list[AlbumCandidate]] = {}
    for cand in candidates:
        parent = str(Path(cand.path).parent)
        if os.path.normcase(parent) == os.path.normcase(cand.path):
            continue
        by_parent.setdefault(parent, []).append(cand)
    consumed: set[int] = set()
    merged: list[AlbumCandidate] = []
    for parent, group in by_parent.items():
        if len(group) < 2:
            continue
        titles = {_norm_title(c.album_title) for c in group if c.album_title}
        if len(titles) != 1:
            # 上层出现多张不同专辑 → 不扩大边界
            for c in group:
                c.issues.append("上级目录存在多张专辑，已按最小边界保留，未自动合并")
            continue
        # 纯标签多碟（无 CUE）同样合并：tagged FLAC 双碟是最常见的无 CUE 形态，
        # 不合并会落成"专辑"与"专辑 (2)"两个目标。专辑名一致性检查已在上面兜底。
        total = len(group)
        if total < 2:
            continue
        group.sort(key=lambda c: _natural_key(c.path))
        for index, cand in enumerate(group, start=1):
            cand.disc = index
            cand.total_discs = total
        parent_cand = AlbumCandidate(
            path=parent,
            album_title=group[0].album_title,
            album_origin=next((c.album_origin for c in group if c.album_origin), ""),
            album_artist=next((c.album_artist for c in group if c.album_artist), ""),
            audio_files=sorted(a for c in group for a in c.audio_files),
            cue_paths=sorted(p for c in group for p in c.cue_paths),
            total_discs=total,
            disc=1,
        )
        # 合并时累加子候选的整轨 CUE 虚拟曲目与问题清单（旧版丢失，弱点 #3）
        for sc in group:
            parent_cand.single_image_cues.extend(sc.single_image_cues)
            parent_cand.issues.extend(sc.issues)
        parent_cand.evidence.append(f"上级包含 {total} 个碟片目录，专辑名一致：{group[0].album_title}")
        parent_cand.evidence.append("碟片顺序：" + " / ".join(Path(c.path).name for c in group))
        parent_cand.child_options = [c.path for c in group]
        for c in group:
            consumed.add(id(c))
        merged.append(parent_cand)
    result = [c for c in candidates if id(c) not in consumed]
    # 被合并的子候选不再单独输出
    result.extend(merged)
    return result


def _norm_title(text: str) -> str:
    return "".join(text.split()).casefold()


def _flag_dispersed(candidates: list[AlbumCandidate], audio_files: list[str]) -> list[AlbumCandidate]:
    """候选同级或上级散落附件 → 标记"资料分散，待手动处理"。

    同级若还有其它专辑（含音频的目录）不算"分散"，只视为多个资料组，不扩大边界。
    """
    audio_parents = {os.path.normcase(str(Path(f).parent)) for f in audio_files}

    def _dir_has_audio(entry_norm: str) -> bool:
        prefix = entry_norm + os.sep
        return any(p == entry_norm or p.startswith(prefix) for p in audio_parents)

    for cand in candidates:
        parent = Path(cand.path).parent
        if os.path.normcase(str(parent)) == os.path.normcase(cand.path):
            continue
        extras: list[str] = []
        try:
            for entry in parent.iterdir():
                if os.path.normcase(str(entry)) == os.path.normcase(cand.path):
                    continue
                if entry.is_dir():
                    if _dir_has_audio(os.path.normcase(str(entry))):
                        continue  # 另一个资料组/专辑，不视为分散附件
                    if any(entry.iterdir()):
                        extras.append(entry.name + "/")
                elif entry.suffix.lower() not in {".cue", ".log", ".txt"}:
                    extras.append(entry.name)
        except OSError:
            continue
        if extras:
            cand.dispersed = True
            cand.dispersed_note = ("上级目录还有未归入本专辑的资料：" + "、".join(extras[:6])
                                   + (" 等" if len(extras) > 6 else ""))
            cand.issues.append(cand.dispersed_note + "（不自动合并，可手动指定整体边界）")
    return candidates


def _fill_year_genre(cand: AlbumCandidate, metas: dict[str, AudioMeta]) -> None:
    years = {metas[f].year for f in cand.audio_files if f in metas and metas[f].year}
    if len(years) == 1:
        cand.year = years.pop()
    genres = {metas[f].genre for f in cand.audio_files if f in metas and metas[f].genre}
    if len(genres) == 1:
        cand.genre = genres.pop()


def resolve_album_artist(files: list[str], metas: dict[str, AudioMeta],
                         cue_performer: str = "") -> tuple[str, str, str]:
    """确定整夹的归属艺术家，返回 (artist, origin, evidence)。

    优先级（重构方案 §3.2，绝不产生“群星”）：
    1. 曲目 meta.artist 唯一一致 → tag；
    2. 曲目 meta.album_artist 唯一一致 → tag；
    3. CUE 顶层 PERFORMER（编码可靠时，由 detect 阶段过滤）→ cue；
    4. 曲目 meta.artist 出现频次最高者（并列时 Counter 保序即首轨优先）→ tag；
    5. 无任何线索 → 返回空串，由调用方落“未知艺术家”兜底。
    """
    track_artists = [metas[f].artist.strip() for f in files
                     if f in metas and metas[f].artist.strip()]
    unique_ta = list(dict.fromkeys(track_artists))
    if len(unique_ta) == 1:
        return unique_ta[0], "tag", f"曲目艺术家标签唯一一致：{unique_ta[0]}"

    album_artists = [metas[f].album_artist.strip() for f in files
                     if f in metas and metas[f].album_artist.strip()]
    unique_aa = list(dict.fromkeys(album_artists))
    if len(unique_aa) == 1:
        return unique_aa[0], "tag", f"专辑艺术家标签唯一一致：{unique_aa[0]}"

    if cue_performer and cue_performer.strip():
        return cue_performer.strip(), "cue", f"采用 CUE 专辑级 PERFORMER：{cue_performer.strip()}"

    if track_artists:
        top_artist, count = Counter(track_artists).most_common(1)[0]
        return top_artist, "tag", (
            f"含多位歌手，按主创/首轨歌手归属：{top_artist}（{count}/{len(track_artists)} 首）")

    return "", "none", "未检测到有效艺术家标签"


def read_metas(files: list[str], progress=None) -> dict[str, AudioMeta]:
    metas: dict[str, AudioMeta] = {}
    total = len(files)
    for index, path in enumerate(files, start=1):
        metas[path] = read_audio(path)
        if progress is not None:
            progress(index, total, path)
    return metas
